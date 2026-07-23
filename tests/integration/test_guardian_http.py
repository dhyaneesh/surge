"""Real-socket integration contracts for the local Guardian HTTP API."""

from __future__ import annotations

import json
import logging
import os
import re
import selectors
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection, HTTPResponse
from threading import Event, Thread
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import pytest

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
from apps.guardian_api.service import GuardianService


DIGEST = "sha256:" + "a" * 64
TOKEN_A = "guardian_local_token_A_0123456789"
TOKEN_B = "guardian_local_token_B_9876543210"


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


def incident_payload(*, tenant_id: str = "tenant-a") -> dict[str, Any]:
    observed_at = datetime.now(UTC)
    facts = IncidentFacts(
        tenant_id=tenant_id,
        incident_id="caller-chosen-id",
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
    payload = facts.model_dump(mode="json")
    del payload["incident_id"]
    return payload


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
    token_tenants = kwargs.pop(
        "token_tenants", {TOKEN_A: "tenant-a", TOKEN_B: "tenant-b"}
    )
    server = create_server(
        token_tenants=token_tenants,
        port=0,
        **kwargs,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.drain_and_close()
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
    extra_headers: dict[str, str] | None = None,
) -> tuple[HTTPResponse, dict[str, Any]]:
    headers: dict[str, str] = dict(extra_headers or {})
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
            token=TOKEN_A,
            idempotency_key="create-1",
            payload=payload,
        )
        duplicate, duplicate_body = request(
            server,
            "POST",
            "/v1/incidents",
            token=TOKEN_A,
            idempotency_key="create-1",
            payload=payload,
            content_type="application/json; charset=utf-8",
        )
        assert created.status == duplicate.status == 200
        assert created_body == duplicate_body
        incident_id = created_body["incident_id"]
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", incident_id)
        assert incident_id not in {"caller-chosen-id", "tenant-a", "create-1"}
        assert created_body["workflow_id"] == (
            f"guardian/tenant-a/incident/{incident_id}"
        )

        observed, observed_body = request(
            server,
            "POST",
            f"/v1/incidents/{incident_id}/observations",
            token=TOKEN_A,
            payload=observation_payload(incident_id=incident_id),
        )
        assert observed.status == 200
        assert set(observed_body) == {"incident_id", "workflow_id", "projection"}

        snapshot, snapshot_body = request(
            server,
            "GET",
            f"/v1/incidents/{incident_id}/scenario-snapshot",
            token=TOKEN_A,
        )
        assert snapshot.status == 200
        assert snapshot_body["incident_id"] == observed_body["incident_id"]
        assert snapshot_body["workflow_id"] == observed_body["workflow_id"]
        assert snapshot_body["projection"] == observed_body["projection"]
        assert "supporting_evidence" in snapshot_body
        assert "audit_event_counts" in snapshot_body
        assert snapshot_body["audit_event_counts"].get("observation-recorded", 0) >= 1
        assert "workflow_states" in snapshot_body
        assert "safety_gates" in snapshot_body


@pytest.mark.parametrize(
    "content_length_headers",
    [
        b"Content-Length: +0\r\n",
        b"Content-Length: -0\r\n",
        b"Content-Length: 1_0\r\n",
        b"Content-Length: 1, 1\r\n",
        b"Content-Length: \xd9\xa1\r\n",
        b"Content-Length: 0\r\nContent-Length: 0\r\n",
    ],
)
def test_content_length_requires_one_ascii_digit_header(
    content_length_headers: bytes,
) -> None:
    with running_server() as server:
        connection = socket.create_connection(server.listening_address, timeout=1)
        try:
            connection.sendall(
                b"GET /health HTTP/1.1\r\nHost: localhost\r\n"
                + content_length_headers
                + b"\r\n"
            )
            response = connection.recv(4096)
        finally:
            connection.close()

    assert response.startswith(b"HTTP/1.1 400 ")
    assert b"\r\nContent-Type: application/json\r\n" in response


def test_oversized_ascii_content_length_is_rejected_without_integer_overflow() -> None:
    with running_server() as server:
        connection = socket.create_connection(server.listening_address, timeout=1)
        try:
            connection.sendall(
                b"GET /health HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
                + b"9" * 5000
                + b"\r\n\r\n"
            )
            response = connection.recv(4096)
        finally:
            connection.close()

    assert response.startswith(b"HTTP/1.1 413 ")


def test_partial_body_timeout_is_a_safe_request_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="guardian.http")
    with running_server(connection_read_timeout=0.05) as server:
        connection = socket.create_connection(server.listening_address, timeout=1)
        try:
            connection.sendall(
                b"POST /v1/incidents HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                + f"Authorization: Bearer {TOKEN_A}\r\n".encode("ascii")
                + b"Idempotency-Key: partial\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 10\r\n\r\n{"
            )
            response_parts = []
            while part := connection.recv(4096):
                response_parts.append(part)
            response = b"".join(response_parts)
        finally:
            connection.close()

    assert response.startswith(b"HTTP/1.1 408 ")
    assert b'{"error":"request timeout"}' in response
    assert "guardian HTTP request failed safely" not in caplog.text
    assert all(getattr(record, "http_status", None) != 500 for record in caplog.records)


def test_malformed_requests_and_unknown_paths_are_bounded() -> None:
    with running_server() as server:
        malformed, malformed_body = request(
            server,
            "POST",
            "/v1/incidents",
            token=TOKEN_A,
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
            token=TOKEN_A,
            payload=incident_payload(),
        )
        missing_path, _ = request(server, "GET", "/v1/missing", token=TOKEN_A)
        wrong_method, wrong_method_body = request(server, "POST", "/health")
        assert missing_key.status == 400
        assert missing_path.status == 404
        assert wrong_method.status == 405
        assert wrong_method_body == {"error": "method not allowed"}


def test_universal_envelope_caps_gets_and_unknown_methods_use_json_405() -> None:
    with running_server(max_request_body=64) as server:
        oversized, oversized_body = request(
            server,
            "GET",
            "/health",
            payload=b"x" * 65,
        )
        unsupported, unsupported_body = request(server, "PROPFIND", "/health")

        assert oversized.status == 413
        assert oversized_body == {"error": "request body too large"}
        assert oversized.getheader("Content-Type") == "application/json"
        assert unsupported.status == 405
        assert unsupported.getheader("Allow") == "GET"
        assert unsupported.getheader("Content-Type") == "application/json"
        assert unsupported_body == {"error": "method not allowed"}


def test_server_shutdown_releases_the_loopback_listener() -> None:
    server = create_server(token_tenants={TOKEN_A: "tenant-a"}, port=0)
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


def test_shutdown_waits_for_active_request_handlers() -> None:
    entered = Event()
    release = Event()

    class BlockingService(GuardianService):
        def get_incident(self, authenticated_tenant: str, incident_id: str):
            entered.set()
            assert release.wait(timeout=5)
            return super().get_incident(authenticated_tenant, incident_id)

    server = create_server(
        token_tenants={TOKEN_A: "tenant-a"},
        service=BlockingService(),
        port=0,
    )
    serve_thread = Thread(target=server.serve_forever)
    request_thread = Thread(
        target=lambda: request(
            server,
            "GET",
            "/v1/incidents/missing/scenario-snapshot",
            token=TOKEN_A,
        )
    )
    serve_thread.start()
    request_thread.start()
    drained = False
    try:
        assert entered.wait(timeout=2)
        releaser = Thread(target=lambda: (Event().wait(0.1), release.set()))
        releaser.start()
        started = time.monotonic()
        server.drain_and_close(timeout_seconds=1.0)
        elapsed = time.monotonic() - started
        drained = True
        releaser.join(timeout=1)
        serve_thread.join(timeout=1)
        request_thread.join(timeout=2)
        assert elapsed < 1.0
        assert not serve_thread.is_alive()
        assert not request_thread.is_alive()
    finally:
        release.set()
        if not drained:
            server.shutdown()
            server.server_close()
        serve_thread.join(timeout=2)
        request_thread.join(timeout=2)


def test_request_concurrency_limit_rejects_saturation_then_recovers() -> None:
    entered = Event()
    release = Event()

    class BlockingService(GuardianService):
        def get_incident(self, authenticated_tenant: str, incident_id: str):
            entered.set()
            assert release.wait(timeout=3)
            return super().get_incident(authenticated_tenant, incident_id)

    first_result: list[int] = []
    with running_server(service=BlockingService(), max_concurrent_requests=1) as server:
        first = Thread(
            target=lambda: first_result.append(
                request(
                    server,
                    "GET",
                    "/v1/incidents/missing/scenario-snapshot",
                    token=TOKEN_A,
                )[0].status
            )
        )
        first.start()
        assert entered.wait(timeout=1)

        saturated, saturated_body = request(server, "GET", "/health")
        assert saturated.status == 503
        assert saturated_body == {"error": "service unavailable"}

        release.set()
        first.join(timeout=2)
        recovered, recovered_body = request(server, "GET", "/health")
        assert recovered.status == 200
        assert recovered_body == {"status": "ok"}

    assert not first.is_alive()
    assert first_result == [404]


def test_cli_prints_only_readiness_and_shuts_down_cleanly_on_sigterm() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    token = "never_print_this_token_A_0123456789"
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


def test_cli_sigterm_closes_idle_pre_request_socket_within_bound() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    environment = {
        **os.environ,
        "GUARDIAN_LOCAL_TOKENS_JSON": json.dumps({TOKEN_A: "tenant-a"}),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "apps.guardian_api", "--port", "0"],
        cwd=repository_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    idle_socket: socket.socket | None = None
    try:
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout=5), "Guardian CLI did not become ready"
        readiness = urlsplit(process.stdout.readline().strip())
        selector.close()

        idle_socket = socket.create_connection(
            (readiness.hostname or "127.0.0.1", readiness.port or 0), timeout=1
        )
        idle_socket.sendall(b"G")
        Event().wait(0.1)

        started = time.monotonic()
        process.terminate()
        assert process.wait(timeout=3) == 0
        assert time.monotonic() - started < 3

        idle_socket.settimeout(1)
        try:
            remaining = idle_socket.recv(1)
        except ConnectionResetError:
            remaining = b""
        assert remaining == b""
    finally:
        if idle_socket is not None:
            idle_socket.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
