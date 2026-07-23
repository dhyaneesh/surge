"""Unit contracts for the authenticated Guardian scenario HTTP client."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any

import pytest

from apps.guardian_api.models import (
    ControlFacts,
    EvidencePass,
    GuardianProjection,
    IncidentSubmission,
    ObservationUpdate,
    PolicyDecision,
    PolicyFacts,
    PolicyState,
    RequiredSignal,
    SignalFacts,
    TargetIdentity,
    TelemetryFacts,
    WorkflowState,
)
from testbeds.scenarios.guardian_client import (
    GuardianResponseError,
    GuardianSubmission,
    GuardianUnavailableError,
    HttpGuardianClient,
)

DIGEST = "sha256:" + "a" * 64
TOKEN = "guardian_scenario_token_AA_0123456789"


def _submission() -> IncidentSubmission:
    observed_at = datetime.now(UTC)
    return IncidentSubmission(
        tenant_id="tenant-a",
        observed_at=observed_at,
        identity=TargetIdentity(
            target_role="request-processor",
            environment="production",
            namespace="payments",
            workload_kind="Deployment",
            workload_name="processor",
            service_name="processor",
            image_digest=DIGEST,
        ),
        telemetry=TelemetryFacts(
            quality=1.0,
            newest_required_sample_at=observed_at,
            freshness_seconds=60,
            required_signals=frozenset({RequiredSignal.TELEMETRY_QUALITY}),
            clock_skew_seconds=0,
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


def _observation(incident_id: str) -> ObservationUpdate:
    observed_at = datetime.now(UTC) + timedelta(seconds=1)
    return ObservationUpdate(
        tenant_id="tenant-a",
        incident_id=incident_id,
        observation_id="observation-1",
        sequence=1,
        window_key="window-1",
        observed_at=observed_at,
        window_started_at=observed_at - timedelta(seconds=30),
        telemetry=TelemetryFacts(
            quality=1.0,
            newest_required_sample_at=observed_at,
            freshness_seconds=60,
            required_signals=frozenset({RequiredSignal.TELEMETRY_QUALITY}),
            clock_skew_seconds=0,
            required_sample_count=10,
            usable_sample_count=10,
            pipeline_available=True,
            comparison_valid=True,
        ),
        service_healthy=True,
        required_conditions_satisfied=True,
        provenance_ref="query-contract/observation-1",
    )


def _projection_payload() -> dict[str, Any]:
    return GuardianProjection(
        rules_version="guardian-rules/v1",
        incident_class=None,
        telemetry_healthy=True,
        integrity_failures=(),
        hypotheses=(),
        eligible_actions=(),
        permitted_actions=(),
        forbidden_actions=(),
        proposed_action=None,
        workflow_state=WorkflowState.ASSESSMENT,
        policy_decision=PolicyDecision.DENIED,
        terminal_reason=None,
        requested_evidence_groups=(),
        proposal_expires_at=None,
        approval_expires_at=None,
        approval_nonce_expires_at=None,
        foreign_evidence_rejected=False,
        scaler_result=None,
        recovery_verified=False,
        escalation_required=False,
    ).model_dump(mode="json")


def _envelope() -> dict[str, Any]:
    return {
        "incident_id": "incident-server-1",
        "workflow_id": "guardian/tenant-a/incident/incident-server-1",
        "projection": _projection_payload(),
    }


class _RecordingHandler(BaseHTTPRequestHandler):
    server_version = "TestGuardian"
    sys_version = ""
    received: dict[str, Any] = {}

    def log_message(self, *args: Any) -> None:  # type: ignore[reportIncompatibleMethodOverride]
        pass

    def _serve(self, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_POST(self) -> None:
        cls = type(self)
        cls.received["authorization"] = self.headers.get("Authorization")
        cls.received["idempotency_key"] = self.headers.get("Idempotency-Key")
        cls.received["path"] = self.path
        length = int(self.headers.get("Content-Length", "0"))
        cls.received["body"] = self.rfile.read(length).decode() if length else ""
        if self.headers.get("Idempotency-Key") == "malformed":
            self._serve({"unexpected": "shape"})
            return
        self._serve(_envelope())

    def do_GET(self) -> None:
        type(self).received["authorization"] = self.headers.get("Authorization")
        type(self).received["path"] = self.path
        self._serve(_envelope())

    def do_HEAD(self) -> None:
        self._serve({})


@pytest.fixture()
def guardian_server(monkeypatch):
    monkeypatch.delenv("GUARDIAN_SCENARIO_TOKEN", raising=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
    _RecordingHandler.received = {}
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base_url, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_client_requires_a_bearer_token_when_none_is_configured(
    guardian_server,
) -> None:
    base_url, _ = guardian_server
    with pytest.raises(GuardianUnavailableError, match="token"):
        HttpGuardianClient(base_url)


def test_client_reads_token_from_guardian_scenario_token_env(guardian_server) -> None:
    base_url, _ = guardian_server
    os.environ["GUARDIAN_SCENARIO_TOKEN"] = TOKEN
    try:
        client = HttpGuardianClient(base_url)
        assert client.bearer_token == TOKEN
    finally:
        os.environ.pop("GUARDIAN_SCENARIO_TOKEN", None)


def test_submit_incident_sends_bearer_token_and_parses_envelope(
    guardian_server,
) -> None:
    base_url, _ = guardian_server
    client = HttpGuardianClient(base_url, token=TOKEN)

    result = asyncio.run(
        client.submit_incident(_submission(), idempotency_key="create-1")
    )

    assert _RecordingHandler.received["authorization"] == f"Bearer {TOKEN}"
    assert _RecordingHandler.received["idempotency_key"] == "create-1"
    assert isinstance(result, GuardianSubmission)
    assert result.incident_id == "incident-server-1"
    assert result.workflow_id == "guardian/tenant-a/incident/incident-server-1"
    assert result.projection is not None
    assert result.projection.incident_class is None
    assert TOKEN not in json.dumps(result.model_dump(mode="json"))


def test_submit_incident_serializes_incident_submission_not_raw_stimulus(
    guardian_server,
) -> None:
    base_url, _ = guardian_server
    client = HttpGuardianClient(base_url, token=TOKEN)
    submission = _submission()

    asyncio.run(client.submit_incident(submission, idempotency_key="create-1"))

    body = json.loads(_RecordingHandler.received["body"])
    assert body["schema_version"] == "guardian.incident-facts/v1"
    assert body["tenant_id"] == "tenant-a"
    assert "scenario_id" not in json.dumps(body)
    assert "expected" not in json.dumps(body)
    assert "stimulus" not in json.dumps(body)


def test_submit_observation_posts_to_the_observation_endpoint(guardian_server) -> None:
    base_url, _ = guardian_server
    client = HttpGuardianClient(base_url, token=TOKEN)

    result = asyncio.run(
        client.submit_observation(
            _observation("incident-server-1"), idempotency_key="observation-1"
        )
    )

    assert _RecordingHandler.received["path"] == (
        "/v1/incidents/incident-server-1/observations"
    )
    assert _RecordingHandler.received["authorization"] == f"Bearer {TOKEN}"
    assert isinstance(result, GuardianSubmission)
    assert result.incident_id == "incident-server-1"


def test_response_schema_mismatch_fails_closed(guardian_server) -> None:
    base_url, _ = guardian_server
    client = HttpGuardianClient(base_url, token=TOKEN)
    with pytest.raises(GuardianResponseError, match="envelope"):
        asyncio.run(client.submit_incident(_submission(), idempotency_key="malformed"))


def test_connection_unavailability_is_an_explicit_error() -> None:
    client = HttpGuardianClient("http://127.0.0.1:1", token=TOKEN)
    with pytest.raises(GuardianUnavailableError):
        asyncio.run(client.submit_incident(_submission(), idempotency_key="create-1"))
