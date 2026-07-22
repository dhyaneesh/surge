import asyncio
import json
from datetime import timedelta

import pytest

from testbeds.adapters.command_runner import CommandResult
from testbeds.adapters.online_boutique import OnlineBoutiqueAdapter
from testbeds.environments.online_boutique import ONLINE_BOUTIQUE_ENVIRONMENT
from testbeds.models import (
    DeploymentSpecification,
    FaultSpecification,
    FaultType,
    LoadProfile,
    WorkloadSelector,
)


class FakeRunner:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    async def run(self, argv, *, timeout, cwd=None, input_text=None):
        self.calls.append((tuple(argv), timeout, cwd, input_text))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return result(argv)


def result(argv=(), stdout="", stderr="", returncode=0):
    return CommandResult(tuple(argv), returncode, stdout, stderr, 0.01)


def deployment(name, image, *, replicas=1, ready=1, generation=2):
    return {
        "metadata": {"name": name, "generation": generation},
        "spec": {
            "replicas": replicas,
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"containers": [{"name": "server", "image": image}]},
            },
        },
        "status": {"availableReplicas": ready, "observedGeneration": generation},
    }


def cluster_payload(*, ready=1):
    names = ONLINE_BOUTIQUE_ENVIRONMENT.workload_names
    deployments = []
    for name in names:
        if name == "redis-cart":
            image = (
                "redis:7.2.4-alpine@"
                + ONLINE_BOUTIQUE_ENVIRONMENT.image_digests[name]
            )
        else:
            image = (
                f"gcr.io/google-samples/microservices-demo/{name}:v0.9.0@"
                f"{ONLINE_BOUTIQUE_ENVIRONMENT.image_digests[name]}"
            )
        deployments.append(deployment(name, image, ready=ready))
    services = [{"metadata": {"name": name}} for name in names if name != "loadgenerator"]
    pods = [
        {
            "metadata": {"name": f"{name}-pod"},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
        }
        for name in names
    ]
    endpoints = [
        {"metadata": {"name": name}, "subsets": [{"addresses": [{"ip": "10.0.0.1"}]}]}
        for name in names
        if name != "loadgenerator"
    ]
    return tuple(
        json.dumps({"items": items})
        for items in (deployments, services, pods, endpoints)
    )


def test_release_and_every_image_are_immutable():
    release = ONLINE_BOUTIQUE_ENVIRONMENT.release()
    assert release.repository == "https://github.com/GoogleCloudPlatform/microservices-demo.git"
    assert len(release.commit_sha) == 40
    int(release.commit_sha, 16)
    assert release.commit_sha != "0" * 40
    assert all(
        digest.startswith("sha256:") and len(digest) == 71
        for digest in ONLINE_BOUTIQUE_ENVIRONMENT.image_digests.values()
    )


def test_traceability_records_every_requested_adapter_obligation():
    from pathlib import Path

    import yaml

    path = Path(__file__).parents[2] / "testbeds" / "environments" / "online_boutique_traceability.yaml"
    traceability = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert traceability["source_sections"] == [
        "20.3 Online Boutique",
        "21 Test environment adapter contract",
    ]
    assert set(traceability["obligations"]) == {
        "immutable_release",
        "namespace_isolation",
        "healthy_baseline",
        "deterministic_reset",
        "specified_faults",
        "normalized_observation",
        "failure_diagnostics",
        "complete_cleanup",
        "production_separation",
        "contract_and_smoke_tests",
    }


def test_namespace_is_dedicated_sanitized_and_validated(tmp_path):
    adapter = OnlineBoutiqueAdapter(workspace=tmp_path, run_id="PR_42/Attempt.1")
    assert adapter.namespace == "guardian-online-boutique-pr-42-attempt-1"
    with pytest.raises(ValueError, match="namespace"):
        OnlineBoutiqueAdapter(workspace=tmp_path, namespace="default;delete")


def test_install_verifies_commit_and_applies_only_digest_pinned_images(tmp_path):
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    (source / "release").mkdir()
    (source / "release" / "kubernetes-manifests.yaml").write_text(
        """apiVersion: apps/v1
kind: Deployment
metadata:
  name: frontend
spec:
  template:
    spec:
      containers:
      - name: server
        image: gcr.io/google-samples/microservices-demo/frontend:v0.9.0
""",
        encoding="utf-8",
    )
    runner = FakeRunner(
        [result(), result(stdout=ONLINE_BOUTIQUE_ENVIRONMENT.commit_sha + "\n"), result(), result(), *[result(stdout=value) for value in cluster_payload()]]
    )
    adapter = OnlineBoutiqueAdapter(runner=runner, workspace=tmp_path, run_id="run-1")
    asyncio.run(adapter.install(ONLINE_BOUTIQUE_ENVIRONMENT.release()))
    apply_call = next(
        call
        for call in runner.calls
        if call[0][:3] == ("kubectl", "apply", "-f") and "frontend" in (call[3] or "")
    )
    assert adapter.namespace in apply_call[0]
    assert ":latest" not in apply_call[3]
    assert f"frontend:v0.9.0@{ONLINE_BOUTIQUE_ENVIRONMENT.image_digests['frontend']}" in apply_call[3]


def test_observe_state_normalizes_roles_versions_digests_and_health(tmp_path):
    runner = FakeRunner(result(stdout=value) for value in cluster_payload())
    state = asyncio.run(OnlineBoutiqueAdapter(runner=runner, workspace=tmp_path).observe_state())
    assert state.environment == "online-boutique"
    assert {item.role for item in state.workloads} >= {"frontend", "checkout", "cart", "cache", "load-generator"}
    assert {item.version for item in state.services} == {"v0.9.0", "7.2.4-alpine"}
    assert all(item.image_digest for item in state.services)
    assert state.healthy


def test_baseline_requires_two_consecutive_healthy_observations(tmp_path):
    payload = cluster_payload()
    runner = FakeRunner(result(stdout=value) for _ in range(2) for value in payload)
    adapter = OnlineBoutiqueAdapter(runner=runner, workspace=tmp_path, baseline_poll_seconds=0)
    baseline = asyncio.run(adapter.wait_for_healthy_baseline(timedelta(seconds=1)))
    assert baseline.healthy
    assert {check.name for check in baseline.checks} >= {
        "workloads_ready", "required_endpoints", "pods_ready", "stable_readiness", "faults_clear"
    }


@pytest.mark.parametrize(
    ("fault_type", "role", "kind"),
    [
        (FaultType.HIGH_CPU, "cart", "StressChaos"),
        (FaultType.ARTIFICIAL_LATENCY, "checkout", "NetworkChaos"),
        (FaultType.DEPENDENCY_UNAVAILABLE, "cache", "Deployment"),
    ],
)
def test_faults_are_targeted_and_reset_restores_state(tmp_path, fault_type, role, kind):
    runner = FakeRunner([result(stdout=json.dumps(deployment("redis-cart", "redis:7.2.4-alpine", replicas=2)))])
    adapter = OnlineBoutiqueAdapter(runner=runner, workspace=tmp_path)
    execution = asyncio.run(adapter.inject_fault(FaultSpecification(fault_type, WorkloadSelector(role), 0.5)))
    assert execution.active
    assert execution.changed_resources[0].kind == kind
    asyncio.run(adapter.reset())
    if fault_type is FaultType.DEPENDENCY_UNAVAILABLE:
        assert any("--replicas=2" in call[0] for call in runner.calls)
    else:
        assert any(call[0][:2] == ("kubectl", "delete") for call in runner.calls)


def test_apply_load_scales_pinned_upstream_generator_and_reset_restores_it(tmp_path):
    runner = FakeRunner([result(stdout=json.dumps(deployment("loadgenerator", "example/load:v0.9.0", replicas=1)))])
    adapter = OnlineBoutiqueAdapter(runner=runner, workspace=tmp_path)
    execution = asyncio.run(adapter.apply_load(LoadProfile(concurrent_users=25)))
    assert execution.active
    assert any(call[0][:3] == ("kubectl", "scale", "deployment/loadgenerator") and "--replicas=5" in call[0] for call in runner.calls)
    asyncio.run(adapter.reset())
    assert any("--replicas=1" in call[0] for call in runner.calls)


def test_deploy_version_requires_digest_and_reset_restores_original_image(tmp_path):
    payload = cluster_payload()
    runner = FakeRunner(result(stdout=value) for value in payload)
    adapter = OnlineBoutiqueAdapter(runner=runner, workspace=tmp_path)
    with pytest.raises(ValueError, match="digest"):
        asyncio.run(adapter.deploy_version(DeploymentSpecification(WorkloadSelector("checkout"), "example/checkout:bad")))
    spec = DeploymentSpecification(WorkloadSelector("checkout"), "example/checkout:bad", "sha256:" + "a" * 64)
    asyncio.run(adapter.deploy_version(spec))
    asyncio.run(adapter.reset())
    assert any("checkoutservice:v0.9.0@" in token for call in runner.calls for token in call[0])


def test_failure_collects_redacted_diagnostics_and_cleanup_is_idempotent(tmp_path):
    runner = FakeRunner([RuntimeError("password=hunter2 token=abc")])
    adapter = OnlineBoutiqueAdapter(runner=runner, workspace=tmp_path)
    with pytest.raises(RuntimeError):
        asyncio.run(adapter.inject_fault(FaultSpecification(FaultType.HIGH_CPU, WorkloadSelector("cart"), 0.5)))
    assert list((tmp_path / "diagnostics").rglob("adapter-state.json"))
    text = "\n".join(path.read_text() for path in (tmp_path / "diagnostics").rglob("*.*"))
    assert "hunter2" not in text and "token=abc" not in text
    asyncio.run(adapter.reset())
    asyncio.run(adapter.cleanup())
    asyncio.run(adapter.cleanup())
    assert sum(call[0][:3] == ("kubectl", "delete", "namespace") for call in runner.calls) == 1


def test_production_sources_do_not_import_online_boutique_adapter():
    from pathlib import Path

    root = Path(__file__).parents[2]
    offenders = []
    for area in ("apps", "services", "packages"):
        for path in (root / area).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".go", ".ts", ".tsx"}:
                if "testbeds.adapters.online_boutique" in path.read_text(encoding="utf-8"):
                    offenders.append(path)
    assert offenders == []
