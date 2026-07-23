"""Test-only protocol for controlling a Guardian validation environment."""

from datetime import timedelta
from typing import Protocol, runtime_checkable

from testbeds.models import (
    BaselineState,
    DeploymentEvent,
    DeploymentSpecification,
    EnvironmentRelease,
    EnvironmentState,
    FaultExecution,
    FaultSpecification,
    LoadExecution,
    LoadProfile,
)


@runtime_checkable
class EnvironmentAdapter(Protocol):
    async def install(self, release: EnvironmentRelease) -> EnvironmentState: ...

    async def reset(self) -> None: ...

    async def wait_for_healthy_baseline(self, timeout: timedelta) -> BaselineState: ...

    async def apply_load(self, profile: LoadProfile) -> LoadExecution: ...

    async def inject_fault(self, fault: FaultSpecification) -> FaultExecution: ...

    async def deploy_version(
        self, deployment: DeploymentSpecification
    ) -> DeploymentEvent: ...

    async def observe_state(self) -> EnvironmentState: ...

    async def cleanup(self) -> None: ...
