import asyncio
import inspect
import os
from datetime import timedelta

import pytest

from testbeds.adapters.argo_rollouts import ArgoRolloutsDemoAdapter
from testbeds.adapters.base import EnvironmentAdapter
from testbeds.environments.argo_rollouts import ARGO_ROLLOUTS_ENVIRONMENT


def test_argo_rollouts_adapter_satisfies_environment_adapter_protocol(tmp_path):
    adapter = ArgoRolloutsDemoAdapter(workspace=tmp_path)
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


@pytest.mark.smoke
@pytest.mark.skipif(
    os.getenv("GUARDIAN_ARGO_ROLLOUTS_SMOKE") != "1",
    reason="set GUARDIAN_ARGO_ROLLOUTS_SMOKE=1 for a disposable cluster with Argo Rollouts installed",
)
def test_real_cluster_install_baseline_canary_fault_reset_and_cleanup(tmp_path):
    async def scenario():
        adapter = ArgoRolloutsDemoAdapter(workspace=tmp_path)
        try:
            await adapter.install(ARGO_ROLLOUTS_ENVIRONMENT.release())
            assert (
                await adapter.wait_for_healthy_baseline(timedelta(minutes=15))
            ).healthy
            assert (
                await adapter.apply_load(ARGO_ROLLOUTS_ENVIRONMENT.smoke_load)
            ).active
            assert (
                await adapter.inject_fault(ARGO_ROLLOUTS_ENVIRONMENT.smoke_fault)
            ).active
            await adapter.reset()
            assert (
                await adapter.wait_for_healthy_baseline(timedelta(minutes=15))
            ).healthy
        finally:
            await adapter.cleanup()

    asyncio.run(scenario())
