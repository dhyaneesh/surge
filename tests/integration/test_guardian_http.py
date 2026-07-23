"""Real-socket integration contracts for the local Guardian HTTP API."""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection, HTTPResponse
from threading import Thread
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from apps.guardian_api.http import GuardianHTTPServer, create_server
from apps.guardian_api.models import (
    ControlFacts,
    EvidencePass,
    IncidentFacts,
    ObservationUpdate,
    PolicyFacts,
    PolicyState,
    RequiredSignal,
    SignalFacts,
    TargetIdentity,
    TelemetryFacts,
)


DIGEST = "sha256:" + "a" * 64


def telemetry(observed_at: datetime) -> TelemetryFacts:
    return TelemetryFacts(
        quality=1.0,
        newest_required_sample_at=observed_at,
        freshness_seconds=60,
        required_signals=frozenset({RequiredSignal.TELEMETRY_QUALITY}),
        clock_skew_seconds=0,
        required_sample_count=10,
        usable_sample_count=10,
        pipeline_available=True,
        comparison_valid=True,
    )


def incident_payload(
    *, tenant_id: str = "tenant-a", incident_id: str = "incident-1"
) -> dict[str, Any]:
    observed_at = datetime.now(UTC)
    facts = IncidentFacts(
        tenant_id=tenant_id,
        incident_id=incident_id,
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
        telemetry=telemetry(observed_at),
        evidence_pass=EvidencePass(completed_passes=1, started_at=observed_at),
        signals=SignalFacts(),
        policy=PolicyFacts(state=PolicyState.FRESH, evaluated_at=observed_at),
        control=ControlFacts(),
    )
    return facts.model_dump(mode="json")


def observation_payload(
    *,
    tenant_id: str = "tenant-a",
    incident_id: str = "incident-1",
    sequence: int = 1,
) -> dict[str, Any]:
    observed_at = datetime.now(UTC) + timedelta(seconds=1)
    observation = ObservationUpdate(
        tenant_id=tenant_id,
        incident_id=incident_id,
        observation_id=f"observation-{sequence}",
        sequence=sequence,
        window_key=f"window-{sequence}",
        observed_at=observed_at,
        window_started_at=observed_at - timedelta(seconds=30),
        telemetry=telemetry(observed_at),
        service_healthy=True,
        required_conditions_satisfied=True,
        provenance_ref=f"query-contract/observation-{sequence}",
    )
    return observation.model_dump(mode="json")


@contextmanager
def running_server(**kwargs: Any) -> Iterator[GuardianHTTPServer]:
    server = create_server(
        token_tenants={"opaque-a": "tenant-a", "opaque-b": "tenant-b"},
        port=0,
        **kwargs,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def request(
    server: GuardianHTTPServer,
    method: str,
    path: str,
    *,
    token: str | None = None,
    authorization: str | None = None,
    idempotency_key: str | None = None,
    payload: dict[str, Any] | bytes | None = None,
    content_type: str | None = None,
) -> tuple[HTTPResponse, dict[str, Any]]:
    headers: dict[str, str] = {}
    if authorization is not None:
        headers["Authorization"] = authorization
    elif token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    if isinstance(payload, dict):
        body = json.dumps(payload).encode()
        headers["Content-Type"] = content_type or "application/json"
    else:
        body = payload
        if content_type is not None:
            headers["Content-Type"] = content_type
    connection = HTTPConnection(*server.listening_address, timeout=2)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    decoded = json.loads(raw) if raw else {}
    connection.close()
    return response, decoded


def test_health_create_duplicate_observe_and_snapshot_lifecycle() -> None:
    with running_server() as server:
        health, health_body = request(server, "GET", "/health")
        assert health.status == 200
        assert health_body == {"status": "ok"}

        payload = incident_payload()
        created, created_body = request(
            server,
            "POST",
            "/v1/incidents",
            token="opaque-a",
            idempotency_key="create-1",
            payload=payload,
        )
        duplicate, duplicate_body = request(
            server,
            "POST",
            "/v1/incidents",
            token="opaque-a",
            idempotency_key="create-1",
            payload=payload,
            content_type="application/json; charset=utf-8",
        )
        assert created.status == duplicate.status == 200
        assert created_body == duplicate_body
        assert created_body["incident_id"] == "incident-1"

        observed, observed_body = request(
            server,
            "POST",
            "/v1/incidents/incident-1/observations",
            token="opaque-a",
            payload=observation_payload(),
        )
        assert observed.status == 200
        assert len(observed_body["observations"]) == 1

        snapshot, snapshot_body = request(
            server,
            "GET",
            "/v1/incidents/incident-1/scenario-snapshot",
            token="opaque-a",
        )
        assert snapshot.status == 200
        assert snapshot_body == observed_body


def test_malformed_requests_and_unknown_paths_are_bounded() -> None:
    with running_server() as server:
        malformed, malformed_body = request(
            server,
            "POST",
            "/v1/incidents",
            token="opaque-a",
            idempotency_key="create-1",
            payload=b"{not-json",
            content_type="application/json",
        )
        assert malformed.status == 400
        assert malformed_body == {"error": "invalid request body"}

        missing_key, _ = request(
            server,
            "POST",
            "/v1/incidents",
            token="opaque-a",
            payload=incident_payload(),
        )
        missing_path, _ = request(server, "GET", "/v1/missing", token="opaque-a")
        wrong_method, wrong_method_body = request(server, "POST", "/health")
        assert missing_key.status == 400
        assert missing_path.status == 404
        assert wrong_method.status == 405
        assert wrong_method_body == {"error": "method not allowed"}


def test_server_shutdown_releases_the_loopback_listener() -> None:
    server = create_server(token_tenants={"opaque-a": "tenant-a"}, port=0)
    host, port = server.listening_address
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    response, _ = request(server, "GET", "/health")
    assert response.status == 200

    server.shutdown()
    server.server_close()
    thread.join(timeout=2)

    assert host == "127.0.0.1"
    assert not thread.is_alive()
    connection = HTTPConnection(host, port, timeout=0.2)
    try:
        connection.connect()
    except OSError:
        pass
    else:
        raise AssertionError("shutdown listener still accepted a connection")
    finally:
        connection.close()


def test_cli_prints_only_readiness_and_shuts_down_cleanly_on_sigterm() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    token = "never-print-this-token"
    environment = {
        **os.environ,
        "GUARDIAN_LOCAL_TOKENS_JSON": json.dumps({token: "tenant-a"}),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "apps.guardian_api", "--port", "0"],
        cwd=repository_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=5), "Guardian CLI did not become ready"
        readiness = process.stdout.readline().strip()
        selector.close()
        parsed = urlsplit(readiness)
        assert parsed.path == "/health"
        connection = HTTPConnection(parsed.hostname, parsed.port, timeout=2)
        connection.request("GET", parsed.path)
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.status == 200

        process.terminate()
        assert process.wait(timeout=5) == 0
        stdout, stderr = process.communicate(timeout=1)
        rendered = readiness + stdout + stderr
        assert token not in rendered
        assert rendered.strip() == readiness
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
