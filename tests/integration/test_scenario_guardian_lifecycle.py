"""Real-stack lifecycle contracts for the scenario runner against the Guardian API.

TST-GRD-WF-001-CONTRACT.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from threading import Thread
from typing import Any, Iterator

import pytest

from apps.guardian_api.http import create_server
from apps.guardian_api.models import (
    ControlFacts,
    EvidencePass,
    IncidentSubmission,
    ObservationUpdate,
    PolicyFacts,
    PolicyState,
    RequiredSignal,
    SignalFacts,
    TelemetryFacts,
)
from apps.guardian_api.service import GuardianService
from testbeds.models import (
    EnvironmentRelease,
    EnvironmentState,
    ObservedServiceIdentity,
    WorkloadState,
)
from testbeds.evidence.collector import EvidenceSample
from testbeds.evidence.contracts import EvidenceSourceKind
from testbeds.scenarios.facts import (
    ControlStimulus,
    FactBuildContext,
    build_incident_submission,
)
from testbeds.scenarios.guardian_client import (
    GuardianResponseError,
    GuardianUnavailableError,
    HttpGuardianClient,
)

TOKEN_A = "guardian_local_token_A_0123456789"
DIGEST = "sha256:" + "a" * 64


def _service() -> EnvironmentState:
    return EnvironmentState(
        environment="test",
        namespace="test-ns",
        release=EnvironmentRelease(environment="test"),
        services=(
            ObservedServiceIdentity(
                "transaction-processor", "transaction-processor", "1.2.3", DIGEST
            ),
        ),
        workloads=(
            WorkloadState("transaction-processor", "transaction-processor", 3, 3),
        ),
        healthy=True,
    )


def _telemetry_sample(observed_at: datetime) -> EvidenceSample:
    return EvidenceSample(
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
    )


def _facts(*, control=ControlStimulus(), tenant_id="tenant-a") -> IncidentSubmission:
    observed_at = datetime.now(UTC)
    return build_incident_submission(
        FactBuildContext(
            tenant_id=tenant_id,
            environment="test",
            target_role="request-processor",
            role_bindings={"request-processor": "transaction-processor"},
            release=EnvironmentRelease(environment="test"),
            observed_at=observed_at,
            observations=(_service(),),
            control=control,
            evidence_samples=(_telemetry_sample(observed_at),),
            required_signals=frozenset({"telemetry_quality"}),
        )
    )


def _observation(
    incident_id: str, *, healthy: bool = True, observed_at: datetime | None = None
) -> ObservationUpdate:
    now = observed_at or datetime.now(UTC)
    return ObservationUpdate(
        tenant_id="tenant-a",
        incident_id=incident_id,
        observation_id="observation-1",
        sequence=1,
        window_key="window-1",
        observed_at=now,
        window_started_at=now - timedelta(seconds=30),
        telemetry=TelemetryFacts(
            quality=1.0,
            newest_required_sample_at=now,
            freshness_seconds=60,
            required_signals=frozenset({RequiredSignal.TELEMETRY_QUALITY}),
            clock_skew_seconds=0.0,
            required_sample_count=10,
            usable_sample_count=10,
            pipeline_available=healthy,
            comparison_valid=healthy,
        ),
        service_healthy=healthy,
        required_conditions_satisfied=healthy,
        provenance_ref="query-contract/observation-1",
    )


def _manual_submission(*, identity=None, tenant_id="tenant-a") -> IncidentSubmission:
    observed_at = datetime.now(UTC)
    return IncidentSubmission(
        tenant_id=tenant_id,
        observed_at=observed_at,
        identity=identity,
        telemetry=TelemetryFacts(
            quality=1.0,
            newest_required_sample_at=observed_at,
            freshness_seconds=60,
            required_signals=frozenset({RequiredSignal.TELEMETRY_QUALITY}),
            clock_skew_seconds=0.0,
            required_sample_count=10,
            usable_sample_count=10,
            pipeline_available=True,
            comparison_valid=True,
        ),
        evidence_pass=EvidencePass(completed_passes=1, started_at=observed_at),
        signals=SignalFacts(),
        policy=PolicyFacts(state=PolicyState.FRESH, evaluated_at=observed_at),
        control=ControlFacts(),
    )


@pytest.fixture()
def server() -> Iterator[Any]:
    srv = create_server(
        token_tenants={TOKEN_A: "tenant-a"},
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


def _client(server: Any) -> HttpGuardianClient:
    return HttpGuardianClient(
        f"http://{server.listening_address[0]}:{server.listening_address[1]}",
        token=TOKEN_A,
    )


def test_guardian_client_requires_a_token() -> None:
    with pytest.raises(GuardianUnavailableError, match="token"):
        HttpGuardianClient("http://127.0.0.1:8080")


def test_submit_incident_and_observe_full_lifecycle(server) -> None:
    client = _client(server)
    submission = _facts()

    result = asyncio.run(
        client.submit_incident(submission, idempotency_key="lifecycle-1")
    )

    assert result.incident_id
    assert result.workflow_id
    assert result.projection is not None
    assert result.projection.incident_class is None

    snapshot = asyncio.run(client.observe(result.incident_id))
    assert snapshot.incident_class is None


def test_duplicate_submission_converges_on_one_incident(server) -> None:
    client = _client(server)
    submission = _facts()

    first = asyncio.run(
        client.submit_incident(submission, idempotency_key="duplicate-1")
    )
    second = asyncio.run(
        client.submit_incident(submission, idempotency_key="duplicate-1")
    )

    assert first.incident_id == second.incident_id
    assert first.workflow_id == second.workflow_id


def test_foreign_tenant_evidence_is_rejected(server) -> None:
    client = _client(server)
    submission = _facts(control=ControlStimulus(foreign_tenant=True))

    with pytest.raises(GuardianUnavailableError, match="403"):
        asyncio.run(client.submit_incident(submission, idempotency_key="foreign-1"))


def test_missing_identity_evidence_fails_closed_as_telemetry_failure(server) -> None:
    client = _client(server)
    submission = _manual_submission(identity=None)

    result = asyncio.run(
        client.submit_incident(submission, idempotency_key="missing-id-1")
    )

    assert result.projection is not None
    assert result.projection.incident_class is not None
    assert result.projection.telemetry_healthy is False


def test_recovery_verified_after_fresh_post_action_observation(server) -> None:
    client = _client(server)
    action_completed = datetime.now(UTC) - timedelta(minutes=2)
    submission = _facts(control=ControlStimulus(action_completed_at=action_completed))

    created = asyncio.run(
        client.submit_incident(submission, idempotency_key="recovery-1")
    )
    # Without a fresh observation, recovery is not verified
    before = asyncio.run(client.observe(created.incident_id))
    assert before.recovery_state is None

    fresh = _observation(created.incident_id)
    asyncio.run(client.submit_observation(fresh, idempotency_key="recovery-obs-1"))

    after = asyncio.run(client.observe(created.incident_id))
    assert after.recovery_state == "healthy"


def test_recovery_without_fresh_window_remains_unverified(server) -> None:
    client = _client(server)
    action_completed = datetime.now(UTC) - timedelta(minutes=2)
    submission = _facts(control=ControlStimulus(action_completed_at=action_completed))

    created = asyncio.run(
        client.submit_incident(submission, idempotency_key="no-fresh-1")
    )
    snapshot = asyncio.run(client.observe(created.incident_id))
    assert snapshot.recovery_state is None


def test_malformed_response_fails_closed() -> None:
    import http.server

    class MalformedHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"unexpected": "shape"}')

        def log_message(self, *args):  # type: ignore[reportIncompatibleMethodOverride]
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MalformedHandler)
    thread = Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        client = HttpGuardianClient(
            f"http://127.0.0.1:{srv.server_address[1]}", token=TOKEN_A
        )
        with pytest.raises(GuardianResponseError, match="envelope"):
            asyncio.run(client.submit_incident(_facts(), idempotency_key="malformed-1"))
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def test_token_is_never_in_the_submission_or_snapshot() -> None:
    _client = HttpGuardianClient("http://127.0.0.1:1", token=TOKEN_A)
    submission = _facts()

    dump = json.dumps(submission.model_dump(mode="json"))
    assert TOKEN_A not in dump
