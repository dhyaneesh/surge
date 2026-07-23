"""Prove legitimate-demand-scale-up against controlled OTel Demo runners + real HTTP API."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import Any, Iterator

import pytest

from apps.guardian_api.http import create_server
from apps.guardian_api.models import ActionType, IncidentClass, IncidentSubmission
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


def _otel_state(*, namespace: str = "guardian-otel-demo-test") -> EnvironmentState:
    return EnvironmentState(
        environment="otel-demo",
        namespace=namespace,
        release=OTEL_DEMO_ENVIRONMENT.release(),
        services=(
            ObservedServiceIdentity(
                "transaction-processor", "checkout", "1.2.3", DIGEST
            ),
            ObservedServiceIdentity("cache", "redis", "1.0.0", DIGEST),
        ),
        workloads=(
            WorkloadState("transaction-processor", "checkout", 3, 3),
            WorkloadState("cache", "redis", 1, 1),
        ),
        healthy=True,
    )


class ControlledOtelDemoAdapter(OpenTelemetryDemoAdapter):
    """OpenTelemetryDemoAdapter with controlled allowlisted lifecycle responses."""

    def __init__(self, *, workspace: Path, namespace: str = "guardian-otel-demo-test"):
        self._workspace = Path(workspace)
        self.namespace = namespace
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
        self.calls: list[str] = []

    async def install(self, release: EnvironmentRelease) -> EnvironmentState:
        self.calls.append("install")
        self._installed = True
        return _otel_state(namespace=self.namespace)

    async def reset(self) -> None:
        self.calls.append("reset")
        self._active_load = False

    async def wait_for_healthy_baseline(self, timeout: timedelta) -> BaselineState:
        self.calls.append("baseline")
        return BaselineState(True, environment=_otel_state(namespace=self.namespace))

    async def apply_load(self, profile: LoadProfile) -> LoadExecution:
        self.calls.append(f"load:{profile.concurrent_users}")
        self._active_load = True
        return LoadExecution(profile, active=True)

    async def observe_state(self) -> EnvironmentState:
        self.calls.append("observe")
        return _otel_state(namespace=self.namespace)

    async def cleanup(self) -> None:
        self.calls.append("cleanup")
        self._cleaned = True


def _scale_up_samples(observed_at: datetime) -> tuple[EvidenceSample, ...]:
    return (
        EvidenceSample(
            EvidenceSourceKind.SIGNOZ_TELEMETRY,
            observed_at=observed_at,
            provenance_ref="signoz/telemetry-quality",
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
            provenance_ref="endpoint-probe/checkout",
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
            provenance_ref="metrics-api/checkout",
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
            provenance_ref="rollout-state/redis",
            values={
                "dependency_healthy": True,
                "usable_samples": 10,
                "required_samples": 10,
            },
        ),
    )


def _recovery_samples(observed_at: datetime) -> tuple[EvidenceSample, ...]:
    """Distinct healthy post-reset samples (no elevated request_rate/cpu)."""

    return (
        EvidenceSample(
            EvidenceSourceKind.SIGNOZ_TELEMETRY,
            observed_at=observed_at,
            provenance_ref="signoz/recovery-telemetry-quality",
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


def test_legitimate_demand_scale_up_with_controlled_otel_demo_and_real_api(
    server, tmp_path
) -> None:
    scenario = load_guardian_scenario(
        "testbeds/scenarios/legitimate-demand-scale-up.yaml"
    )
    adapter = ControlledOtelDemoAdapter(workspace=tmp_path / "otel")
    observed_at = datetime.now(UTC)
    samples = _scale_up_samples(observed_at)
    recovery = _recovery_samples(observed_at)
    registration = AdapterRegistration(
        environment="otel-demo",
        adapter=adapter,
        release=OTEL_DEMO_ENVIRONMENT.release(),
        declaration=ENVIRONMENT_DECLARATIONS["otel-demo"],
        role_bindings={"request-processor": "transaction-processor"},
        fault_role_bindings={},
        deployment_bindings={},
        evidence_samples=samples,
        recovery_evidence_samples=recovery,
    )
    client = HttpGuardianClient(
        f"http://{server.listening_address[0]}:{server.listening_address[1]}",
        token=TOKEN,
    )

    result = asyncio.run(
        ScenarioExecutor(client).execute(
            scenario,
            registration,
            ExecutionSettings(
                tmp_path / "artifacts",
                baseline_timeout=timedelta(seconds=1),
                operation_timeout=timedelta(seconds=5),
            ),
        )
    )

    assert result.reset_completed is True
    assert result.cleanup_completed is True
    assert result.environment_invalidated is False
    assert "install" in adapter.calls
    assert any(call.startswith("load:") for call in adapter.calls)
    assert result.status is ExecutionStatus.PASSED
    failed = [item for item in result.assertions if not item.passed]
    assert failed == [], [item.model_dump(mode="json") for item in failed]

    payloads = json.loads(
        (result.artifact_directory / "incident-payloads.json").read_text()
    )
    assert len(payloads) == 1
    submission = IncidentSubmission.model_validate(payloads[0], strict=False)
    assert submission.signals.request_rate is not None
    assert submission.signals.cpu_utilization is not None
    assert submission.identity is not None
    assert submission.identity.target_role == "request-processor"
    assert submission.identity.service_name == "checkout"
    assert submission.identity.workload_name == "checkout"
    assert submission.signals.request_rate.service_name == "checkout"
    assert submission.signals.request_rate.subject_role == "request-processor"
    # Successful load injection alone is not the rate evidence.
    assert submission.signals.request_rate.provenance_ref.startswith("endpoint-probe")

    # Re-submit the same normalized facts to prove classification against the
    # live loopback API (executor already submitted once under its own key).
    created = asyncio.run(
        client.submit_incident(submission, idempotency_key="proof-direct-1")
    )
    assert created.projection is not None
    assert created.projection.incident_class is IncidentClass.LOAD_SPIKE
    assert created.projection.proposed_action is ActionType.SCALE_UP
    assert ActionType.SCALE_UP in created.projection.eligible_actions
    assert created.projection.executed_mutations == 0

    snapshot = asyncio.run(client.observe(created.incident_id))
    assert snapshot.incident_class == "load_spike"
    assert snapshot.proposed_action == {
        "actionType": "scale",
        "scaleDirection": "up",
    }
    assert snapshot.mutation_count == 0
    assert snapshot.audit_event_counts.get("observation-recorded", 0) >= 1
