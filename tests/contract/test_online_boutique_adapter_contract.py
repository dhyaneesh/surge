import inspect
import os
from datetime import timedelta

import pytest

from testbeds.adapters.base import EnvironmentAdapter
from testbeds.adapters.online_boutique import OnlineBoutiqueAdapter
from testbeds.environments.online_boutique import ONLINE_BOUTIQUE_ENVIRONMENT
from testbeds.models import FaultSpecification, FaultType, WorkloadSelector


def test_online_boutique_adapter_satisfies_protocol(tmp_path):
    adapter = OnlineBoutiqueAdapter(workspace=tmp_path)
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
    os.getenv("GUARDIAN_ONLINE_BOUTIQUE_SMOKE") != "1",
    reason="set GUARDIAN_ONLINE_BOUTIQUE_SMOKE=1 for a disposable cluster with Chaos Mesh",
)
def test_real_cluster_install_baseline_fault_reset_and_cleanup(tmp_path):
    async def scenario():
        adapter = OnlineBoutiqueAdapter(workspace=tmp_path)
        try:
            await adapter.install(ONLINE_BOUTIQUE_ENVIRONMENT.release())
            assert (
                await adapter.wait_for_healthy_baseline(timedelta(minutes=15))
            ).healthy
            load = await adapter.apply_load(ONLINE_BOUTIQUE_ENVIRONMENT.smoke_load)
            assert load.active
            await adapter.reset()
            assert (
                await adapter.wait_for_healthy_baseline(timedelta(minutes=10))
            ).healthy
            for specification in (
                FaultSpecification(FaultType.HIGH_CPU, WorkloadSelector("cart"), 0.25),
                FaultSpecification(
                    FaultType.ARTIFICIAL_LATENCY,
                    WorkloadSelector("checkout"),
                    0.1,
                ),
                ONLINE_BOUTIQUE_ENVIRONMENT.smoke_fault,
            ):
                fault = await adapter.inject_fault(specification)
                assert fault.active
                await adapter.reset()
                assert (
                    await adapter.wait_for_healthy_baseline(timedelta(minutes=10))
                ).healthy
        finally:
            await adapter.cleanup()

    import asyncio

    asyncio.run(scenario())
