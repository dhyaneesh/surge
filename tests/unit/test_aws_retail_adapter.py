import asyncio
import json
from datetime import timedelta

import pytest

from testbeds.adapters.aws_retail import AwsRetailAdapter
from testbeds.adapters.command_runner import CommandResult
from testbeds.environments.aws_retail import AWS_RETAIL_ENVIRONMENT
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
                "metadata": {"labels": {"app.kubernetes.io/name": name}},
                "spec": {"containers": [{"name": name, "image": image}]},
            },
        },
        "status": {"availableReplicas": ready, "observedGeneration": generation},
    }


def cluster_payload(*, ready=1):
    deployments = [
        deployment(name, f"public.ecr.aws/example/{name}:1.6.1", ready=ready)
        for name in ("ui", "catalog", "carts", "orders", "checkout")
    ]
    services = [
        {"metadata": {"name": name}}
        for name in ("ui", "catalog", "carts", "orders", "checkout")
    ]
    pods = [
        {
            "metadata": {"name": f"{name}-pod"},
            "status": {
                "conditions": [
                    {"type": "Ready", "status": "True" if ready else "False"}
                ]
            },
        }
        for name in ("ui", "catalog", "carts", "orders", "checkout")
    ]
    endpoints = [
        {
            "metadata": {"name": name},
            "subsets": [{"addresses": [{"ip": "10.0.0.1"}]}],
        }
        for name in ("ui", "catalog", "carts", "orders", "checkout")
    ]
    return (
        json.dumps({"items": deployments}),
        json.dumps({"items": services}),
        json.dumps({"items": pods}),
        json.dumps({"items": endpoints}),
    )


def test_environment_pin_is_real_full_sha_and_immutable_package_digest():
    assert (
        AWS_RETAIL_ENVIRONMENT.repository
        == "https://github.com/aws-containers/retail-store-sample-app.git"
    )


def test_aws_retail_traceability_records_normative_adapter_obligations():
    from pathlib import Path

    import yaml

    path = (
        Path(__file__).parents[2]
        / "testbeds"
        / "environments"
        / "aws_retail_traceability.yaml"
    )
    traceability = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert traceability["environment"] == "aws-retail"
    assert traceability["source_sections"] == [
        "20.2 AWS Containers Retail Sample",
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
    assert len(AWS_RETAIL_ENVIRONMENT.commit_sha) == 40
    int(AWS_RETAIL_ENVIRONMENT.commit_sha, 16)
    assert AWS_RETAIL_ENVIRONMENT.package_digest.startswith("sha256:")
    assert len(AWS_RETAIL_ENVIRONMENT.package_digest) == 71
    assert AWS_RETAIL_ENVIRONMENT.commit_sha != "0" * 40
    assert AWS_RETAIL_ENVIRONMENT.image_digests
    assert all(
        digest.startswith("sha256:") and len(digest) == 71
        for digest in AWS_RETAIL_ENVIRONMENT.image_digests.values()
    )


def test_namespace_is_sanitized_and_invalid_override_is_rejected(tmp_path):
    adapter = AwsRetailAdapter(workspace=tmp_path, run_id="PR_42/Attempt.1")
    assert adapter.namespace == "guardian-aws-retail-pr-42-attempt-1"
    with pytest.raises(ValueError, match="namespace"):
        AwsRetailAdapter(workspace=tmp_path, namespace="default;delete")


def test_install_verifies_head_before_first_kubernetes_mutation(tmp_path):
    (tmp_path / "source" / ".git").mkdir(parents=True)
    runner = FakeRunner(
        [result(), result(stdout=AWS_RETAIL_ENVIRONMENT.commit_sha + "\n")]
    )
    adapter = AwsRetailAdapter(runner=runner, workspace=tmp_path, run_id="run-1")
    asyncio.run(adapter.install(AWS_RETAIL_ENVIRONMENT.release()))
    assert runner.calls[1][0] == ("git", "rev-parse", "HEAD")
    first_mutation = next(
        call for call in runner.calls if call[0][0] in {"helm", "kubectl"}
    )
    assert runner.calls.index(first_mutation) > 1
    assert "guardian-aws-retail-run-1" in first_mutation[0]
    helm_calls = [
        call[0] for call in runner.calls if call[0][:2] == ("helm", "upgrade")
    ]
    assert helm_calls
    assert all(any("@sha256:" in token for token in call) for call in helm_calls)


def test_install_rejects_wrong_head_without_cluster_mutation(tmp_path):
    (tmp_path / "source" / ".git").mkdir(parents=True)
    runner = FakeRunner([result(), result(stdout="0" * 40 + "\n")])
    adapter = AwsRetailAdapter(runner=runner, workspace=tmp_path)
    with pytest.raises(RuntimeError, match="pinned commit"):
        asyncio.run(adapter.install(AWS_RETAIL_ENVIRONMENT.release()))
    assert not any(call[0][0] in {"helm", "kubectl"} for call in runner.calls)


def test_partially_failed_install_removes_namespace(tmp_path):
    (tmp_path / "source" / ".git").mkdir(parents=True)
    runner = FakeRunner(
        [
            result(),
            result(stdout=AWS_RETAIL_ENVIRONMENT.commit_sha + "\n"),
            RuntimeError("helm connection failed after submission"),
        ]
    )
    adapter = AwsRetailAdapter(runner=runner, workspace=tmp_path)
    with pytest.raises(RuntimeError, match="helm connection"):
        asyncio.run(adapter.install(AWS_RETAIL_ENVIRONMENT.release()))
    assert any(
        call[0][:3] == ("kubectl", "delete", "namespace") for call in runner.calls
    )


def test_observe_state_normalizes_versions_workloads_and_health(tmp_path):
    deployments, services, pods, endpoints = cluster_payload()
    runner = FakeRunner(
        [
            result(stdout=deployments),
            result(stdout=services),
            result(stdout=pods),
            result(stdout=endpoints),
        ]
    )
    state = asyncio.run(
        AwsRetailAdapter(runner=runner, workspace=tmp_path).observe_state()
    )
    assert state.environment == "aws-retail"
    assert {item.role for item in state.workloads} == {
        "frontend",
        "catalog",
        "cart",
        "orders",
        "checkout",
    }
    assert {item.version for item in state.services} == {"1.6.1"}
    assert state.healthy


def test_observe_state_is_unhealthy_when_required_endpoints_have_no_addresses(
    tmp_path,
):
    deployments, services, pods, _ = cluster_payload()
    runner = FakeRunner(
        [
            result(stdout=deployments),
            result(stdout=services),
            result(stdout=pods),
            result(stdout=json.dumps({"items": []})),
        ]
    )
    state = asyncio.run(
        AwsRetailAdapter(runner=runner, workspace=tmp_path).observe_state()
    )
    assert not state.healthy


def test_baseline_check_reports_missing_endpoints_separately_from_ready_workloads(
    tmp_path,
):
    deployments, services, pods, _ = cluster_payload()
    adapter = AwsRetailAdapter(
        runner=FakeRunner(
            [
                result(stdout=deployments),
                result(stdout=services),
                result(stdout=pods),
                result(stdout=json.dumps({"items": []})),
            ]
        ),
        workspace=tmp_path,
    )
    state = asyncio.run(adapter.observe_state())

    checks = {check.name: check for check in adapter._baseline_checks(state, stable=0)}

    assert checks["workloads_ready"].passed
    assert not checks["required_endpoints"].passed


@pytest.mark.parametrize(
    ("fault_type", "role"),
    [
        (FaultType.HIGH_CPU, "database"),
        (FaultType.ARTIFICIAL_LATENCY, "checkout"),
    ],
)
def test_normalized_health_is_degraded_while_chaos_fault_is_active(
    tmp_path, fault_type, role
):
    payload = cluster_payload()
    runner = FakeRunner([result(), *(result(stdout=value) for value in payload)])
    adapter = AwsRetailAdapter(runner=runner, workspace=tmp_path)
    fault = FaultSpecification(fault_type, WorkloadSelector(role), 0.5)
    asyncio.run(adapter.inject_fault(fault))
    state = asyncio.run(adapter.observe_state())
    assert not state.healthy


def test_deploy_version_requires_digest(tmp_path):
    deployments, services, pods, endpoints = cluster_payload()
    runner = FakeRunner(
        [
            result(stdout=deployments),
            result(stdout=services),
            result(stdout=pods),
            result(stdout=endpoints),
        ]
    )
    adapter = AwsRetailAdapter(runner=runner, workspace=tmp_path)
    from testbeds.models import DeploymentSpecification

    deployment_spec = DeploymentSpecification(
        target=WorkloadSelector("checkout"), version="registry.example/checkout:bad"
    )
    with pytest.raises(ValueError, match="digest"):
        asyncio.run(adapter.deploy_version(deployment_spec))


@pytest.mark.parametrize(
    ("version", "digest"),
    [
        ("registry.example/checkout:latest", "sha256:" + "a" * 64),
        ("registry.example/checkout:v2", "invalid"),
    ],
)
def test_deploy_version_validates_immutable_image_before_cluster_reads(
    tmp_path, version, digest
):
    runner = FakeRunner()
    adapter = AwsRetailAdapter(runner=runner, workspace=tmp_path)

    with pytest.raises(ValueError, match="immutable"):
        asyncio.run(
            adapter.deploy_version(
                DeploymentSpecification(WorkloadSelector("checkout"), version, digest)
            )
        )

    assert runner.calls == []


def test_reset_restores_original_deployment_image(tmp_path):
    deployments, services, pods, endpoints = cluster_payload()
    runner = FakeRunner(
        [
            result(stdout=deployments),
            result(stdout=services),
            result(stdout=pods),
            result(stdout=endpoints),
        ]
    )
    adapter = AwsRetailAdapter(runner=runner, workspace=tmp_path)
    from testbeds.models import DeploymentSpecification

    deployment_spec = DeploymentSpecification(
        target=WorkloadSelector("checkout"),
        version="registry.example/checkout:bad",
        image_digest="sha256:" + "a" * 64,
    )
    asyncio.run(adapter.deploy_version(deployment_spec))
    asyncio.run(adapter.reset())
    assert any(
        "*=public.ecr.aws/example/checkout:1.6.1" in call[0] for call in runner.calls
    )


def test_failed_deployment_rollout_is_reported_and_original_image_can_reset(
    tmp_path,
):
    payload = cluster_payload()
    runner = FakeRunner(
        [
            *(result(stdout=value) for value in payload),
            result(),
            RuntimeError("rollout did not converge"),
        ]
    )
    adapter = AwsRetailAdapter(runner=runner, workspace=tmp_path)
    from testbeds.models import DeploymentSpecification

    specification = DeploymentSpecification(
        target=WorkloadSelector("checkout"),
        version="registry.example/checkout:broken",
        image_digest="sha256:" + "b" * 64,
    )
    with pytest.raises(RuntimeError, match="rollout did not converge"):
        asyncio.run(adapter.deploy_version(specification))
    asyncio.run(adapter.reset())
    assert any(
        "*=public.ecr.aws/example/checkout:1.6.1" in call[0] for call in runner.calls
    )


def test_baseline_requires_two_stable_ready_observations(tmp_path):
    payload = cluster_payload()
    runner = FakeRunner([result(stdout=value) for _ in range(2) for value in payload])
    adapter = AwsRetailAdapter(
        runner=runner, workspace=tmp_path, baseline_poll_seconds=0
    )
    baseline = asyncio.run(adapter.wait_for_healthy_baseline(timedelta(seconds=1)))
    assert baseline.healthy
    assert {check.name for check in baseline.checks} >= {
        "workloads_ready",
        "required_endpoints",
        "pods_ready",
        "stable_readiness",
    }


def test_baseline_timeout_collects_bounded_diagnostics(tmp_path):
    payload = cluster_payload(ready=0)
    runner = FakeRunner([result(stdout=value) for _ in range(4) for value in payload])
    adapter = AwsRetailAdapter(
        runner=runner, workspace=tmp_path, baseline_poll_seconds=0
    )
    with pytest.raises(TimeoutError):
        asyncio.run(adapter.wait_for_healthy_baseline(timedelta(microseconds=1)))
    assert list((tmp_path / "diagnostics").rglob("adapter-state.json"))


def test_load_resource_is_digest_pinned_and_removed_by_reset(tmp_path):
    runner = FakeRunner()
    adapter = AwsRetailAdapter(runner=runner, workspace=tmp_path)
    execution = asyncio.run(adapter.apply_load(LoadProfile(concurrent_users=10)))
    assert execution.active
    manifest = json.loads(runner.calls[0][3])
    assert "@sha256:" in manifest["spec"]["template"]["spec"]["containers"][0]["image"]
    asyncio.run(adapter.reset())
    assert any(
        call[0][:4] == ("kubectl", "delete", "deployment", "guardian-load-generator")
        for call in runner.calls
    )


@pytest.mark.parametrize(
    ("fault_type", "role", "resource_kind"),
    [
        (FaultType.HIGH_CPU, "database", "StressChaos"),
        (FaultType.ARTIFICIAL_LATENCY, "checkout", "NetworkChaos"),
        (FaultType.DEPENDENCY_UNAVAILABLE, "cache", "Deployment"),
    ],
)
def test_supported_fault_apply_and_reset_restores_original_state(
    tmp_path, fault_type, role, resource_kind
):
    original = json.dumps(deployment("checkout-redis", "redis:6.0-alpine", replicas=2))
    runner = FakeRunner([result(stdout=original)])
    adapter = AwsRetailAdapter(runner=runner, workspace=tmp_path)
    fault = FaultSpecification(
        fault_type=fault_type, target=WorkloadSelector(role=role), magnitude=0.5
    )
    execution = asyncio.run(adapter.inject_fault(fault))
    assert execution.active
    assert execution.changed_resources[0].kind == resource_kind
    asyncio.run(adapter.reset())
    if fault_type == FaultType.DEPENDENCY_UNAVAILABLE:
        assert any("--replicas=2" in call[0] for call in runner.calls)
    else:
        assert any(call[0][:2] == ("kubectl", "delete") for call in runner.calls)


def test_database_saturation_selector_matches_only_pinned_chart_mysql_labels(
    tmp_path,
):
    runner = FakeRunner()
    adapter = AwsRetailAdapter(runner=runner, workspace=tmp_path)
    fault = FaultSpecification(FaultType.HIGH_CPU, WorkloadSelector("database"), 0.5)
    asyncio.run(adapter.inject_fault(fault))
    manifest = json.loads(runner.calls[0][3])
    assert manifest["spec"]["selector"]["labelSelectors"] == {
        "app.kubernetes.io/name": "catalog",
        "app.kubernetes.io/instance": "catalog",
        "app.kubernetes.io/component": "mysql",
    }


def test_service_latency_selector_excludes_checkout_redis(tmp_path):
    runner = FakeRunner()
    adapter = AwsRetailAdapter(runner=runner, workspace=tmp_path)
    fault = FaultSpecification(
        FaultType.ARTIFICIAL_LATENCY, WorkloadSelector("checkout"), 0.1
    )
    asyncio.run(adapter.inject_fault(fault))
    manifest = json.loads(runner.calls[0][3])
    assert manifest["spec"]["selector"]["labelSelectors"] == {
        "app.kubernetes.io/name": "checkout",
        "app.kubernetes.io/instance": "checkout",
        "app.kubernetes.io/component": "service",
    }


def test_reset_and_cleanup_are_idempotent_after_partial_fault_failure(tmp_path):
    runner = FakeRunner([RuntimeError("password=hunter2 token=abc")])
    adapter = AwsRetailAdapter(runner=runner, workspace=tmp_path)
    fault = FaultSpecification(FaultType.HIGH_CPU, WorkloadSelector("database"), 0.5)
    with pytest.raises(RuntimeError):
        asyncio.run(adapter.inject_fault(fault))
    asyncio.run(adapter.reset())
    assert any(
        call[0][:4]
        == ("kubectl", "delete", "stresschaos", "guardian-database-saturation")
        for call in runner.calls
    )
    asyncio.run(adapter.reset())
    asyncio.run(adapter.cleanup())
    asyncio.run(adapter.cleanup())
    assert (
        sum(call[0][:3] == ("kubectl", "delete", "namespace") for call in runner.calls)
        == 1
    )
    diagnostic_text = "\n".join(
        path.read_text() for path in (tmp_path / "diagnostics").rglob("*.txt")
    )
    assert "hunter2" not in diagnostic_text
    assert "token=abc" not in diagnostic_text


def test_production_sources_do_not_import_aws_retail_adapter():
    from pathlib import Path

    root = Path(__file__).parents[2]
    offenders = []
    for area in ("apps", "services", "packages"):
        for path in (root / area).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".go", ".ts", ".tsx"}:
                continue
            if "testbeds.adapters.aws_retail" in path.read_text(encoding="utf-8"):
                offenders.append(path)
    assert offenders == []
