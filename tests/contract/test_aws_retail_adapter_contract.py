import inspect
import json
import os
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from testbeds.adapters.aws_retail import AwsRetailAdapter
from testbeds.adapters.base import EnvironmentAdapter
from testbeds.adapters.command_runner import AllowlistedCommandRunner
from testbeds.environments.aws_retail import AWS_RETAIL_ENVIRONMENT
from testbeds.models import (
    DeploymentSpecification,
    FaultSpecification,
    FaultType,
    WorkloadSelector,
)


def test_aws_retail_adapter_satisfies_environment_adapter_protocol(tmp_path):
    adapter = AwsRetailAdapter(workspace=tmp_path)
    assert isinstance(adapter, EnvironmentAdapter)
    for operation in (
        "install",
        "reset",
        "wait_for_healthy_baseline",
        "apply_load",
        "inject_fault",
        "deploy_version",
        "observe_state",
        "cleanup",
    ):
        assert inspect.iscoroutinefunction(getattr(adapter, operation))


def test_capabilities_cover_only_normative_aws_retail_fault_controls(tmp_path):
    adapter = AwsRetailAdapter(workspace=tmp_path)
    assert adapter.capabilities.fault_types == frozenset(
        {
            FaultType.HIGH_CPU,
            FaultType.DEPENDENCY_UNAVAILABLE,
            FaultType.ARTIFICIAL_LATENCY,
        }
    )
    assert adapter.capabilities.adjustable_load
    assert adapter.capabilities.version_deployment


@pytest.mark.skipif(
    os.getenv("GUARDIAN_AWS_RETAIL_SMOKE") != "1",
    reason="set GUARDIAN_AWS_RETAIL_SMOKE=1 for a cluster with Chaos Mesh",
)
def test_real_cluster_install_baseline_fault_reset_and_cleanup(tmp_path):
    import asyncio

    adapter = AwsRetailAdapter(workspace=tmp_path)

    def kubectl(*args, check=True):
        return subprocess.run(
            ["kubectl", *args],
            shell=False,
            check=check,
            capture_output=True,
            text=True,
        )

    async def scenario():
        try:
            await adapter.install(AWS_RETAIL_ENVIRONMENT.release())
            baseline = await adapter.wait_for_healthy_baseline(timedelta(minutes=15))
            assert baseline.healthy
            original = {
                item.name: (item.desired_replicas, item.image)
                for item in baseline.environment.workloads
            }

            load = await adapter.apply_load(AWS_RETAIL_ENVIRONMENT.smoke_load)
            assert load.active
            kubectl(
                "get", "deployment", "guardian-load-generator", "-n", adapter.namespace
            )
            await adapter.reset()
            assert (
                kubectl(
                    "get",
                    "deployment",
                    "guardian-load-generator",
                    "-n",
                    adapter.namespace,
                    check=False,
                ).returncode
                != 0
            )
            assert (
                await adapter.wait_for_healthy_baseline(timedelta(minutes=10))
            ).healthy

            faults = (
                FaultSpecification(
                    FaultType.HIGH_CPU, WorkloadSelector("database"), 0.25
                ),
                FaultSpecification(
                    FaultType.DEPENDENCY_UNAVAILABLE, WorkloadSelector("cache"), 1.0
                ),
                FaultSpecification(
                    FaultType.ARTIFICIAL_LATENCY, WorkloadSelector("checkout"), 0.1
                ),
            )
            resources = {
                FaultType.HIGH_CPU: ("stresschaos", "guardian-database-saturation"),
                FaultType.ARTIFICIAL_LATENCY: (
                    "networkchaos",
                    "guardian-service-latency",
                ),
            }
            for specification in faults:
                fault = await adapter.inject_fault(specification)
                assert fault.active
                if specification.fault_type in resources:
                    kind, name = resources[specification.fault_type]
                    resource = json.loads(
                        kubectl(
                            "get", kind, name, "-n", adapter.namespace, "-o", "json"
                        ).stdout
                    )
                    assert resource["metadata"]["namespace"] == adapter.namespace
                    kubectl(
                        "wait",
                        f"{kind}/{name}",
                        "--for=condition=AllInjected",
                        "-n",
                        adapter.namespace,
                        "--timeout=2m",
                    )
                    selector = ",".join(
                        f"{key}={value}"
                        for key, value in resource["spec"]["selector"][
                            "labelSelectors"
                        ].items()
                    )
                    selected = json.loads(
                        kubectl(
                            "get",
                            "pods",
                            "-n",
                            adapter.namespace,
                            "-l",
                            selector,
                            "-o",
                            "json",
                        ).stdout
                    )["items"]
                    assert len(selected) == 1
                assert not (await adapter.observe_state()).healthy
                await adapter.reset()
                restored = await adapter.wait_for_healthy_baseline(
                    timedelta(minutes=10)
                )
                assert restored.healthy
                assert {
                    item.name: (item.desired_replicas, item.image)
                    for item in restored.environment.workloads
                } == original
                if specification.fault_type in resources:
                    kind, name = resources[specification.fault_type]
                    assert (
                        kubectl(
                            "get",
                            kind,
                            name,
                            "-n",
                            adapter.namespace,
                            check=False,
                        ).returncode
                        != 0
                    )

            deployment = DeploymentSpecification(
                target=WorkloadSelector("checkout"),
                version="public.ecr.aws/aws-containers/retail-store-sample-checkout:1.6.0",
                image_digest="sha256:886448c6ee6c774d7429a0733fad76fd7aa832e06f0592a4f060f8dcc9c23eee",
            )
            event = await adapter.deploy_version(deployment)
            assert event.to_version.endswith(":1.6.0")
            assert any(
                item.version == "1.6.0"
                for item in (await adapter.observe_state()).services
            )
            await adapter.reset()
            restored = await adapter.wait_for_healthy_baseline(timedelta(minutes=10))
            assert restored.healthy
            assert {
                item.name: (item.desired_replicas, item.image)
                for item in restored.environment.workloads
            } == original
        finally:
            await adapter.cleanup()

        assert (
            kubectl("get", "namespace", adapter.namespace, check=False).returncode != 0
        )

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.getenv("GUARDIAN_AWS_RETAIL_SMOKE") != "1",
    reason="set GUARDIAN_AWS_RETAIL_SMOKE=1 for a cluster with Chaos Mesh",
)
def test_real_cluster_partial_failure_reset_and_cleanup(tmp_path):
    import asyncio

    artifact_root = Path(os.getenv("GUARDIAN_AWS_RETAIL_ARTIFACT_DIR", str(tmp_path)))
    artifact_root.mkdir(parents=True, exist_ok=True)

    class FailingRunner:
        def __init__(self, predicate, *, after=False):
            self.base = AllowlistedCommandRunner()
            self.predicate = predicate
            self.after = after
            self.armed = True

        async def run(self, argv, *, timeout, cwd=None, input_text=None):
            matches = self.armed and self.predicate(tuple(argv))
            if matches and not self.after:
                self.armed = False
                raise RuntimeError("injected real-cluster transport failure")
            result = await self.base.run(
                argv, timeout=timeout, cwd=cwd, input_text=input_text
            )
            if matches:
                self.armed = False
                raise RuntimeError("injected real-cluster post-mutation failure")
            return result

    def namespace_absent(namespace):
        return (
            subprocess.run(
                ["kubectl", "get", "namespace", namespace],
                shell=False,
                capture_output=True,
                text=True,
            ).returncode
            != 0
        )

    async def scenario():
        checkout_runner = FailingRunner(lambda argv: argv[:2] == ("helm", "upgrade"))
        checkout_adapter = AwsRetailAdapter(
            runner=checkout_runner,
            workspace=artifact_root / "checkout-before-install",
            run_id="partial-checkout",
        )
        with pytest.raises(RuntimeError, match="injected real-cluster"):
            await checkout_adapter.install(AWS_RETAIL_ENVIRONMENT.release())
        assert namespace_absent(checkout_adapter.namespace)

        helm_mutations = 0

        def fail_second_helm(argv):
            nonlocal helm_mutations
            if argv[:2] == ("helm", "upgrade"):
                helm_mutations += 1
                return helm_mutations == 2
            return False

        partial_runner = FailingRunner(fail_second_helm)
        partial_adapter = AwsRetailAdapter(
            runner=partial_runner,
            workspace=artifact_root / "partial-manifests",
            run_id="partial-manifests",
        )
        with pytest.raises(RuntimeError, match="injected real-cluster"):
            await partial_adapter.install(AWS_RETAIL_ENVIRONMENT.release())
        assert namespace_absent(partial_adapter.namespace)

        runner = FailingRunner(lambda argv: False)
        adapter = AwsRetailAdapter(
            runner=runner,
            workspace=artifact_root / "post-install-failures",
            run_id="post-install-failures",
            baseline_poll_seconds=0,
        )
        try:
            await adapter.install(AWS_RETAIL_ENVIRONMENT.release())
            with pytest.raises(TimeoutError):
                await adapter.wait_for_healthy_baseline(timedelta(microseconds=1))

            await adapter.inject_fault(
                FaultSpecification(
                    FaultType.HIGH_CPU, WorkloadSelector("database"), 0.25
                )
            )
            runner.predicate = lambda argv: argv[:3] == (
                "kubectl",
                "get",
                "deployments",
            )
            runner.armed = True
            with pytest.raises(RuntimeError, match="injected real-cluster"):
                await adapter.observe_state()
            subprocess.run(
                [
                    "kubectl",
                    "get",
                    "stresschaos",
                    "guardian-database-saturation",
                    "-n",
                    adapter.namespace,
                ],
                shell=False,
                check=True,
                capture_output=True,
                text=True,
            )
            await adapter.reset()
            assert (
                await adapter.wait_for_healthy_baseline(timedelta(minutes=10))
            ).healthy

            runner.predicate = lambda argv: argv[:3] == (
                "kubectl",
                "rollout",
                "status",
            )
            runner.armed = True
            with pytest.raises(RuntimeError, match="injected real-cluster"):
                await adapter.deploy_version(
                    DeploymentSpecification(
                        target=WorkloadSelector("checkout"),
                        version="public.ecr.aws/aws-containers/retail-store-sample-checkout:1.6.0",
                        image_digest="sha256:886448c6ee6c774d7429a0733fad76fd7aa832e06f0592a4f060f8dcc9c23eee",
                    )
                )
            await adapter.reset()
            assert (
                await adapter.wait_for_healthy_baseline(timedelta(minutes=10))
            ).healthy
        finally:
            await adapter.cleanup()
            await adapter.cleanup()
        assert namespace_absent(adapter.namespace)

    asyncio.run(scenario())
