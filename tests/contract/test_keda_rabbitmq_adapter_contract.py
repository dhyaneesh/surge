import inspect
import os
from datetime import timedelta

import pytest

from testbeds.adapters.base import EnvironmentAdapter
from testbeds.adapters.keda_rabbitmq import KedaRabbitMqAdapter
from testbeds.environments.keda_rabbitmq import KEDA_RABBITMQ_ENVIRONMENT


def test_keda_rabbitmq_adapter_satisfies_environment_adapter_protocol(tmp_path):
    adapter = KedaRabbitMqAdapter(workspace=tmp_path)
    assert isinstance(adapter, EnvironmentAdapter)
    assert all(
        inspect.iscoroutinefunction(getattr(adapter, operation))
        for operation in (
            "install",
            "reset",
            "wait_for_healthy_baseline",
            "apply_load",
            "inject_fault",
            "deploy_version",
            "observe_state",
            "cleanup",
        )
    )


@pytest.mark.smoke
@pytest.mark.skipif(
    os.getenv("GUARDIAN_KEDA_RABBITMQ_SMOKE") != "1",
    reason="set GUARDIAN_KEDA_RABBITMQ_SMOKE=1 for a disposable cluster with KEDA",
)
def test_real_cluster_install_baseline_load_fault_reset_and_cleanup(tmp_path):
    async def scenario():
        adapter = KedaRabbitMqAdapter(workspace=tmp_path)
        try:
            await adapter.install(KEDA_RABBITMQ_ENVIRONMENT.release())
            baseline = await adapter.wait_for_healthy_baseline(timedelta(minutes=15))
            assert baseline.healthy
            state = baseline.environment
            assert state is not None
            rabbitmq = next(item for item in state.workloads if item.role == "rabbitmq")
            assert rabbitmq.name == "rabbitmq"
            assert rabbitmq.ready_replicas == rabbitmq.desired_replicas == 1
            assert state.scaling is not None and state.scaling.scaled_object_ready
            assert (
                await adapter.apply_load(KEDA_RABBITMQ_ENVIRONMENT.smoke_load)
            ).active
            await adapter.reset()
        finally:
            await adapter.cleanup()

    import asyncio

    asyncio.run(scenario())
