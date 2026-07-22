import inspect
import os
from datetime import timedelta

import pytest

from testbeds.adapters.base import EnvironmentAdapter
from testbeds.adapters.otel_demo import OpenTelemetryDemoAdapter
from testbeds.environments.otel_demo import OTEL_DEMO_ENVIRONMENT
from testbeds.models import FaultType


def test_otel_demo_adapter_satisfies_environment_adapter_protocol(tmp_path):
    adapter = OpenTelemetryDemoAdapter(workspace=tmp_path)
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


def test_capabilities_cover_specified_otel_demo_fault_classes(tmp_path):
    adapter = OpenTelemetryDemoAdapter(workspace=tmp_path)
    assert adapter.capabilities.fault_types == frozenset(
        {
            FaultType.SERVICE_FAILURE,
            FaultType.PARTIAL_FAILURE,
            FaultType.HIGH_CPU,
            FaultType.MANUAL_GC,
            FaultType.MEMORY_LEAK,
            FaultType.READINESS_FAILURE,
            FaultType.ARTIFICIAL_LATENCY,
            FaultType.QUEUE_LAG,
            FaultType.DEPENDENCY_UNAVAILABLE,
        }
    )
    assert adapter.capabilities.adjustable_load


@pytest.mark.smoke
@pytest.mark.skipif(
    os.getenv("GUARDIAN_OTEL_DEMO_SMOKE") != "1",
    reason="set GUARDIAN_OTEL_DEMO_SMOKE=1 for a real cluster smoke test",
)
def test_real_cluster_install_baseline_load_fault_reset_and_cleanup(tmp_path):
    import asyncio

    adapter = OpenTelemetryDemoAdapter(workspace=tmp_path)

    async def scenario():
        try:
            await adapter.install(OTEL_DEMO_ENVIRONMENT.release())
            baseline = await adapter.wait_for_healthy_baseline(timedelta(minutes=15))
            assert baseline.healthy
            await adapter.apply_load(OTEL_DEMO_ENVIRONMENT.smoke_load)
            fault = await adapter.inject_fault(OTEL_DEMO_ENVIRONMENT.smoke_fault)
            assert fault.active
            state = await adapter.observe_state()
            assert state.workloads
            await adapter.reset()
        finally:
            await adapter.cleanup()

    asyncio.run(scenario())
