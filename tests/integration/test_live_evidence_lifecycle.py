"""Prove assessment and recovery evidence are independently collected."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import Any, Iterator

import pytest

from apps.guardian_api.http import create_server
from apps.guardian_api.service import GuardianService
from testbeds.adapters.otel_demo import OpenTelemetryDemoAdapter
from testbeds.evidence.collector import EvidenceSample
from testbeds.evidence.contracts import EvidenceSourceKind
from testbeds.environments.capabilities import ENVIRONMENT_DECLARATIONS
from testbeds.environments.otel_demo import OTEL_DEMO_ENVIRONMENT
from testbeds.models import (
    BaselineState,
    EnvironmentRelease,
    EnvironmentState,
    LoadExecution,
    LoadProfile,
    ObservedServiceIdentity,
    WorkloadState,
)
from testbeds.scenarios.execution import (
    AdapterRegistration,
    ExecutionSettings,
    ExecutionStatus,
    ScenarioExecutor,
)
from testbeds.scenarios.guardian_client import HttpGuardianClient
from testbeds.scenarios.loader import load_guardian_scenario

TOKEN = "guardian_local_token_A_0123456789"
DIGEST = "sha256:" + "a" * 64


def _state() -> EnvironmentState:
    return EnvironmentState(
        environment="otel-demo",
        namespace="guardian-live-evidence",
        release=OTEL_DEMO_ENVIRONMENT.release(),
        services=(
            ObservedServiceIdentity("request-processor", "checkout", "1.2.3", DIGEST),
            ObservedServiceIdentity("dependency", "redis", "1.0.0", DIGEST),
        ),
        workloads=(
            WorkloadState("request-processor", "checkout", 3, 3),
            WorkloadState("dependency", "redis", 1, 1),
        ),
        healthy=True,
    )


class ControlledOtelDemoAdapter(OpenTelemetryDemoAdapter):
    """Loopback-safe adapter implementing the scenario lifecycle."""

    def __init__(self, *, workspace: Path) -> None:
        self._workspace = workspace
        self.namespace = "guardian-live-evidence"
        self._release = OTEL_DEMO_ENVIRONMENT.release()
        self._active_load = False
        self._active_faults: set = set()
        self._created_resources: set = set()
        self._changed: list = []
        self._diagnostics: list = []
        self._command_history: list = []
        self._installed = False
        self._cleaned = False
        self.contaminated = False

    async def install(self, release: EnvironmentRelease) -> EnvironmentState:
        self._installed = True
        return _state()

    async def reset(self) -> None:
        self._active_load = False

    async def wait_for_healthy_baseline(self, timeout: timedelta) -> BaselineState:
        return BaselineState(True, environment=_state())

    async def apply_load(self, profile: LoadProfile) -> LoadExecution:
        self._active_load = True
        return LoadExecution(profile, active=True)

    async def observe_state(self) -> EnvironmentState:
        return _state()

    async def cleanup(self) -> None:
        self._cleaned = True


@dataclass
class RecordingEvidenceProvider:
    assessment_calls: int = 0
    recovery_calls: int = 0

    async def collect_assessment_evidence(self, **kwargs) -> tuple[EvidenceSample, ...]:
        self.assessment_calls += 1
        observed_at = datetime.now(UTC)
        return (
            EvidenceSample(
                EvidenceSourceKind.SIGNOZ_TELEMETRY,
                observed_at=observed_at,
                provenance_ref="query-contract/assessment-telemetry",
                values={
                    "quality": 1.0,
                    "usable_samples": 10,
                    "required_samples": 10,
                    "pipeline_available": True,
                    "comparison_valid": True,
                },
            ),
            EvidenceSample(
                EvidenceSourceKind.ENDPOINT_PROBE,
                observed_at=observed_at,
                provenance_ref="endpoint-probe/assessment-checkout",
                values={
                    "request_rate": 240.0,
                    "baseline_request_rate": 100.0,
                    "error_rate": 0.01,
                    "baseline_error_rate": 0.01,
                    "usable_samples": 10,
                    "required_samples": 10,
                },
            ),
            EvidenceSample(
                EvidenceSourceKind.METRICS_API,
                observed_at=observed_at,
                provenance_ref="metrics-api/assessment-checkout",
                values={
                    "cpu_utilization": 0.90,
                    "memory_utilization": 0.55,
                    "usable_samples": 10,
                    "required_samples": 10,
                },
            ),
            EvidenceSample(
                EvidenceSourceKind.ROLLOUT_STATE,
                observed_at=observed_at,
                provenance_ref="rollout-state/assessment-redis",
                values={
                    "dependency_healthy": True,
                    "usable_samples": 10,
                    "required_samples": 10,
                },
            ),
        )

    async def collect_recovery_evidence(self, **kwargs) -> tuple[EvidenceSample, ...]:
        self.recovery_calls += 1
        observed_at = datetime.now(UTC)
        return (
            EvidenceSample(
                EvidenceSourceKind.SIGNOZ_TELEMETRY,
                observed_at=observed_at,
                provenance_ref="query-contract/recovery-telemetry",
                values={
                    "quality": 1.0,
                    "usable_samples": 10,
                    "required_samples": 10,
                    "pipeline_available": True,
                    "comparison_valid": True,
                },
            ),
        )


@pytest.fixture()
def server() -> Iterator[Any]:
    srv = create_server(
        token_tenants={TOKEN: "tenant-a"},
        service=GuardianService(),
        port=0,
    )
    thread = Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.drain_and_close()
        thread.join(timeout=2)


def test_lifecycle_persists_distinct_assessment_and_recovery_evidence(
    server, tmp_path: Path
) -> None:
    provider = RecordingEvidenceProvider()
    registration = AdapterRegistration(
        environment="otel-demo",
        adapter=ControlledOtelDemoAdapter(workspace=tmp_path / "otel"),
        release=OTEL_DEMO_ENVIRONMENT.release(),
        declaration=ENVIRONMENT_DECLARATIONS["otel-demo"],
        role_bindings={"request-processor": "checkout"},
        fault_role_bindings={},
        deployment_bindings={},
        evidence_provider=provider,
    )
    client = HttpGuardianClient(
        f"http://{server.listening_address[0]}:{server.listening_address[1]}",
        token=TOKEN,
    )

    result = asyncio.run(
        ScenarioExecutor(client).execute(
            load_guardian_scenario(
                "testbeds/scenarios/legitimate-demand-scale-up.yaml"
            ),
            registration,
            ExecutionSettings(
                tmp_path / "artifacts",
                baseline_timeout=timedelta(seconds=1),
                operation_timeout=timedelta(seconds=5),
            ),
        )
    )

    artifact = result.artifact_directory
    assessment_path = artifact / "assessment-evidence.json"
    recovery_path = artifact / "recovery-evidence.json"
    assert result.status is ExecutionStatus.PASSED
    assert assessment_path.exists()
    assert recovery_path.exists()

    assessment = json.loads(assessment_path.read_text())
    recovery = json.loads(recovery_path.read_text())
    assert assessment[0]["observed_at"] != recovery[0]["observed_at"]
    assert assessment[0]["provenance_ref"] != recovery[0]["provenance_ref"]
    assert provider.assessment_calls == 1
    assert provider.recovery_calls == 1
