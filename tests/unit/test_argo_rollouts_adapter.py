import asyncio
import json
from datetime import timedelta

import pytest

from testbeds.adapters.command_runner import AllowlistedCommandRunner, CommandResult
from testbeds.adapters.argo_rollouts import ArgoRolloutsDemoAdapter
from testbeds.environments.argo_rollouts import ARGO_ROLLOUTS_ENVIRONMENT
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
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return result(argv)


def result(argv=(), stdout="", stderr="", returncode=0):
    return CommandResult(tuple(argv), returncode, stdout, stderr, 0.01)


def rollout(
    *,
    image=None,
    phase="Healthy",
    paused=False,
    stable="stablehash",
    canary="canaryhash",
):
    return {
        "metadata": {"name": "canary-demo", "generation": 3},
        "spec": {
            "replicas": 5,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "canary-demo",
                            "image": image or ARGO_ROLLOUTS_ENVIRONMENT.stable_image,
                        }
                    ]
                }
            },
            "strategy": {"canary": {"canaryService": "canary-demo-preview"}},
        },
        "status": {
            "phase": phase,
            "paused": paused,
            "observedGeneration": 3,
            "readyReplicas": 5,
            "updatedReplicas": 5,
            "availableReplicas": 5,
            "unavailableReplicas": 0,
            "stableRS": stable,
            "currentPodHash": canary,
            "conditions": [{"type": "Healthy", "status": "True"}],
        },
    }


def replica_set(name, image, *, ready=5, labels=None):
    return {
        "metadata": {
            "name": name,
            "labels": {"rollouts-pod-template-hash": labels or name},
        },
        "spec": {
            "replicas": 5,
            "template": {"spec": {"containers": [{"image": image}]}},
        },
        "status": {"readyReplicas": ready, "availableReplicas": ready},
    }


def cluster_payload():
    return tuple(
        json.dumps({"items": value})
        for value in (
            [rollout()],
            [
                replica_set(
                    "canary-demo-stable",
                    ARGO_ROLLOUTS_ENVIRONMENT.stable_image,
                    labels="stablehash",
                ),
                replica_set(
                    "canary-demo-canary",
                    ARGO_ROLLOUTS_ENVIRONMENT.canary_image,
                    labels="canaryhash",
                ),
            ],
            [
                {
                    "metadata": {"name": "canary-pod"},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                }
            ],
            [{"metadata": {"name": "canary-demo-preview"}}],
            [
                {
                    "metadata": {"name": "canary-demo-preview"},
                    "subsets": [{"addresses": [{"ip": "10.0.0.1"}]}],
                }
            ],
        )
    )


def test_pinned_release_and_supported_fixture_capabilities_are_explicit():
    release = ARGO_ROLLOUTS_ENVIRONMENT.release()
    assert release.repository == "https://github.com/argoproj/rollouts-demo.git"
    assert len(release.commit_sha) == 40
    int(release.commit_sha, 16)
    assert ARGO_ROLLOUTS_ENVIRONMENT.approval_gating_supported is False
    assert all(
        "@sha256:" in image for image in ARGO_ROLLOUTS_ENVIRONMENT.images.values()
    )


def test_command_runner_permits_kubectl_kustomize_for_the_pinned_fixture(tmp_path):
    runner = AllowlistedCommandRunner()
    runner._validate(("kubectl", "kustomize", str(tmp_path / "examples" / "canary")))


def test_command_runner_permits_kubectl_argo_rollouts_plugin_commands():
    runner = AllowlistedCommandRunner()
    runner._validate(("kubectl", "argo", "rollouts", "set", "image", "canary-demo"))


def test_namespace_is_dedicated_sanitized_and_validated(tmp_path):
    adapter = ArgoRolloutsDemoAdapter(workspace=tmp_path, run_id="PR_42/Attempt.1")
    assert adapter.namespace == "guardian-argo-rollouts-pr-42-attempt-1"
    with pytest.raises(ValueError, match="namespace"):
        ArgoRolloutsDemoAdapter(workspace=tmp_path, namespace="default;delete")


def test_install_creates_the_dedicated_namespace_before_applying_the_pinned_fixture(
    tmp_path,
):
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    runner = FakeRunner(
        [
            result(),
            result(stdout=ARGO_ROLLOUTS_ENVIRONMENT.commit_sha + "\n"),
            result(),
            result(
                stdout="kind: Rollout\nspec:\n  template:\n    spec:\n      containers:\n      - name: canary-demo\n        image: argoproj/rollouts-demo:blue\n"
            ),
            *[result(stdout=value) for value in cluster_payload()],
        ]
    )
    adapter = ArgoRolloutsDemoAdapter(runner=runner, workspace=tmp_path, run_id="run-1")
    asyncio.run(adapter.install(ARGO_ROLLOUTS_ENVIRONMENT.release()))
    namespace_call = next(
        call
        for call in runner.calls
        if call[0][:3] == ("kubectl", "apply", "-f")
        and '"kind": "Namespace"' in (call[3] or "")
    )
    fixture_call = next(
        call
        for call in runner.calls
        if call[0][:3] == ("kubectl", "apply", "-f")
        and "kind: Rollout" in (call[3] or "")
    )
    assert adapter.namespace in namespace_call[3]
    assert adapter.namespace in fixture_call[0]


def test_pin_images_rejects_unexpected_mutable_fixture_image():
    manifest = """
apiVersion: argoproj.io/v1alpha1
kind: Rollout
spec:
  template:
    spec:
      containers:
      - name: canary-demo
        image: argoproj/rollouts-demo:blue
      - name: helper
        image: busybox:latest
"""

    with pytest.raises(RuntimeError, match="unpinned image"):
        ArgoRolloutsDemoAdapter._pin_images(manifest)


def test_observe_state_normalizes_rollout_replica_set_pod_service_versions_and_health(
    tmp_path,
):
    runner = FakeRunner(result(stdout=value) for value in cluster_payload())
    state = asyncio.run(
        ArgoRolloutsDemoAdapter(runner=runner, workspace=tmp_path).observe_state()
    )
    assert state.healthy
    assert state.rollouts[0].phase == "Healthy"
    assert state.rollouts[0].stable_hash == "stablehash"
    assert state.rollouts[0].canary_hash == "canaryhash"
    assert state.rollouts[0].recovery_healthy
    assert {workload.role for workload in state.workloads} == {"stable", "canary"}
    assert {service.version for service in state.services} == {"blue", "yellow"}


def test_baseline_requires_rollout_health_stable_replicaset_endpoint_and_expected_version(
    tmp_path,
):
    runner = FakeRunner(
        result(stdout=value) for _ in range(2) for value in cluster_payload()
    )
    adapter = ArgoRolloutsDemoAdapter(
        runner=runner, workspace=tmp_path, baseline_poll_seconds=0
    )
    baseline = asyncio.run(adapter.wait_for_healthy_baseline(timedelta(seconds=1)))
    assert baseline.healthy
    assert {item.name for item in baseline.checks} >= {
        "rollout_healthy",
        "stable_replicaset_ready",
        "service_available",
        "expected_version",
        "stable_observation",
    }


@pytest.mark.parametrize(
    ("fault_type", "role", "expected"),
    [(FaultType.SERVICE_FAILURE, "canary", "bad-orange")],
)
def test_faults_map_to_supported_fixture_images_and_reset_is_deterministic(
    tmp_path, fault_type, role, expected
):
    runner = FakeRunner(
        [
            result(stdout=json.dumps(rollout())),
            result(),
            result(),
            result(),
            result(),
            result(),
        ]
    )
    adapter = ArgoRolloutsDemoAdapter(runner=runner, workspace=tmp_path)
    execution = asyncio.run(
        adapter.inject_fault(FaultSpecification(fault_type, WorkloadSelector(role), 1))
    )
    assert execution.active
    assert any(expected in token for call in runner.calls for token in call[0])
    asyncio.run(adapter.reset())
    assert any(
        ARGO_ROLLOUTS_ENVIRONMENT.stable_image in token
        for call in runner.calls
        for token in call[0]
    )


def test_deploy_canary_abort_and_repeated_reset_restore_stable_version_and_delete_created_resources(
    tmp_path,
):
    runner = FakeRunner(
        [
            *[result(stdout=value) for value in cluster_payload()],
            result(),
            result(),
            result(),
            result(),
            result(),
        ]
    )
    adapter = ArgoRolloutsDemoAdapter(runner=runner, workspace=tmp_path)
    event = asyncio.run(
        adapter.deploy_version(
            DeploymentSpecification(
                WorkloadSelector("canary"),
                ARGO_ROLLOUTS_ENVIRONMENT.canary_image,
                ARGO_ROLLOUTS_ENVIRONMENT.image_digests["yellow"],
            )
        )
    )
    assert event.to_version == ARGO_ROLLOUTS_ENVIRONMENT.canary_image
    asyncio.run(adapter.reset())
    asyncio.run(adapter.reset())
    assert any(
        call[0][:4] == ("kubectl", "argo", "rollouts", "abort") for call in runner.calls
    )
    assert any(
        call[0][:4] == ("kubectl", "argo", "rollouts", "promote")
        for call in runner.calls
    )
    assert any("analysisruns" in call[0] for call in runner.calls)
    assert any("experiments" in call[0] for call in runner.calls)


def test_load_uses_test_created_generator_and_reset_removes_it(tmp_path):
    runner = FakeRunner([result(), result()])
    adapter = ArgoRolloutsDemoAdapter(runner=runner, workspace=tmp_path)
    assert asyncio.run(adapter.apply_load(LoadProfile(concurrent_users=10))).active
    asyncio.run(adapter.reset())
    assert any(call[0][:3] == ("kubectl", "apply", "-f") for call in runner.calls)
    assert any(
        call[0][:3] == ("kubectl", "delete", "deployment") for call in runner.calls
    )


def test_failure_collects_rollout_diagnostics_and_cleanup_is_idempotent(tmp_path):
    adapter = ArgoRolloutsDemoAdapter(
        runner=FakeRunner([RuntimeError("token=secret")]), workspace=tmp_path
    )
    with pytest.raises(RuntimeError):
        asyncio.run(
            adapter.inject_fault(
                FaultSpecification(
                    FaultType.SERVICE_FAILURE, WorkloadSelector("canary"), 1
                )
            )
        )
    categories = {item.category for item in adapter._diagnostics}
    assert {
        "resources",
        "rollout",
        "replicasets",
        "events",
        "logs",
        "analysis",
        "adapter-state",
    } <= categories
    asyncio.run(adapter.cleanup())
    asyncio.run(adapter.cleanup())


def test_production_sources_do_not_import_argo_rollouts_adapter():
    from pathlib import Path

    root = Path(__file__).parents[2]
    assert not [
        path
        for area in ("apps", "services", "packages")
        for path in (root / area).rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".go", ".ts", ".tsx"}
        and "testbeds.adapters.argo_rollouts" in path.read_text(encoding="utf-8")
    ]
