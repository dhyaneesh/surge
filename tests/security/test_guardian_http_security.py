"""Fail-closed security contracts for the local Guardian HTTP API."""

from __future__ import annotations

import json
import logging
from http.client import HTTPConnection
from threading import Thread
from typing import Any, cast

import pytest

from apps.guardian_api.__main__ import parse_runtime_config, run_runtime
from apps.guardian_api.http import create_server, load_token_tenants
from apps.guardian_api.service import GuardianService
from apps.guardian_api.store import InMemoryIncidentStore, replay_incident_history
from tests.integration.test_guardian_http import (
    incident_payload,
    request,
    running_server,
)


@pytest.mark.parametrize(
    "authorization",
    [None, "", "opaque-a", "Basic opaque-a", "Bearer invalid"],
)
def test_missing_or_invalid_authentication_is_401(authorization: str | None) -> None:
    with running_server() as server:
        response, body = request(
            server,
            "GET",
            "/v1/incidents/missing/scenario-snapshot",
            authorization=authorization,
        )
        assert response.status == 401
        assert response.getheader("WWW-Authenticate") == "Bearer"
        assert body == {"error": "authentication required"}


def test_body_tenant_cannot_override_authenticated_tenant() -> None:
    with running_server() as server:
        response, body = request(
            server,
            "POST",
            "/v1/incidents",
            token="opaque-a",
            idempotency_key="override",
            payload=incident_payload(tenant_id="tenant-b"),
        )
        assert response.status == 403
        assert body == {"error": "forbidden"}


def test_untrusted_tenant_header_cannot_override_token_identity() -> None:
    with running_server() as server:
        response, body = request(
            server,
            "POST",
            "/v1/incidents",
            token="opaque-a",
            idempotency_key="header-override",
            payload=incident_payload(),
            extra_headers={"X-Guardian-Tenant": "tenant-b"},
        )
        assert response.status == 200
        assert body["tenant_id"] == "tenant-a"
        assert body["facts"]["tenant_id"] == "tenant-a"


def test_caller_supplied_incident_id_is_rejected() -> None:
    payload = incident_payload()
    payload["incident_id"] = "caller-chosen-id"
    with running_server() as server:
        response, body = request(
            server,
            "POST",
            "/v1/incidents",
            token="opaque-a",
            idempotency_key="caller-id",
            payload=payload,
        )
        assert response.status == 400
        assert body == {"error": "invalid request body"}


def test_foreign_evidence_is_rejected_before_evaluation() -> None:
    evaluations = 0

    def counting_projector(*args: Any, **kwargs: Any):
        nonlocal evaluations
        evaluations += 1
        return replay_incident_history(*args, **kwargs)

    store = InMemoryIncidentStore(projector=counting_projector)
    payload = incident_payload()
    observed_at = payload["observed_at"]
    payload["signals"]["request_rate"] = {
        "tenant_id": "tenant-b",
        "subject_role": "request-processor",
        "environment": "production",
        "namespace": "payments",
        "workload_kind": "Deployment",
        "workload_name": "processor",
        "service_name": "processor",
        "observed_at": observed_at,
        "freshness": "fresh",
        "source": "query-contract",
        "provenance_ref": "query-contract/request-rate",
        "independence_group": "load",
        "expected_samples": 10,
        "usable_samples": 10,
        "value": 200.0,
        "baseline_value": 100.0,
    }
    with running_server(service=GuardianService(store)) as server:
        response, body = request(
            server,
            "POST",
            "/v1/incidents",
            token="opaque-a",
            idempotency_key="foreign-evidence",
            payload=payload,
        )
    assert response.status == 403
    assert body == {"error": "forbidden"}
    assert evaluations == 0
    assert store.snapshot().incidents == ()


def test_cross_tenant_lookup_is_a_non_disclosing_404() -> None:
    with running_server() as server:
        created, created_body = request(
            server,
            "POST",
            "/v1/incidents",
            token="opaque-a",
            idempotency_key="create-a",
            payload=incident_payload(),
        )
        own_missing, own_body = request(
            server,
            "GET",
            "/v1/incidents/missing/scenario-snapshot",
            token="opaque-a",
        )
        foreign, foreign_body = request(
            server,
            "GET",
            f"/v1/incidents/{created_body['incident_id']}/scenario-snapshot",
            token="opaque-b",
        )
        assert created.status == 200
        assert own_missing.status == foreign.status == 404
        assert own_body == foreign_body == {"error": "incident not found"}


def test_conflicting_idempotent_payload_is_409() -> None:
    first_payload = incident_payload()
    conflicting_payload = dict(first_payload)
    conflicting_payload["severity"] = "critical"
    with running_server() as server:
        first, _ = request(
            server,
            "POST",
            "/v1/incidents",
            token="opaque-a",
            idempotency_key="same-key",
            payload=first_payload,
        )
        conflict, body = request(
            server,
            "POST",
            "/v1/incidents",
            token="opaque-a",
            idempotency_key="same-key",
            payload=conflicting_payload,
        )
        assert first.status == 200
        assert conflict.status == 409
        assert body == {"error": "conflict"}


def test_secrets_are_not_echoed_or_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="guardian.http")
    secret = "do-not-disclose"
    with running_server() as server:
        response, body = request(
            server,
            "POST",
            "/v1/incidents",
            token="opaque-a",
            idempotency_key="redaction",
            payload=(
                b'{"password":"'
                + secret.encode()
                + b'","Authorization":"Bearer '
                + secret.encode()
                + b'"}'
            ),
            content_type="application/json",
        )
    rendered = json.dumps(body) + caplog.text
    assert response.status == 400
    assert secret not in rendered
    assert "opaque-a" not in rendered


def test_internal_errors_return_a_safe_500(caplog: pytest.LogCaptureFixture) -> None:
    class ExplodingService(GuardianService):
        def get_incident(self, authenticated_tenant: str, incident_id: str):
            raise RuntimeError("Authorization: Bearer do-not-disclose password=hunter2")

    caplog.set_level(logging.INFO, logger="guardian.http")
    with running_server(service=ExplodingService()) as server:
        response, body = request(
            server,
            "GET",
            "/v1/incidents/incident-1/scenario-snapshot",
            token="opaque-a",
        )
    rendered = json.dumps(body) + caplog.text
    assert response.status == 500
    assert body == {"error": "internal server error"}
    assert "do-not-disclose" not in rendered
    assert "hunter2" not in rendered


def test_request_body_cap_and_json_content_type_are_enforced() -> None:
    with running_server(max_request_body=64) as server:
        too_large, _ = request(
            server,
            "POST",
            "/v1/incidents",
            token="opaque-a",
            idempotency_key="large",
            payload=b"x" * 65,
            content_type="application/json",
        )
        wrong_type, _ = request(
            server,
            "POST",
            "/v1/incidents",
            token="opaque-a",
            idempotency_key="wrong-type",
            payload=b"{}",
            content_type="text/plain",
        )
        assert too_large.status == 413
        assert wrong_type.status == 400
        assert wrong_type.getheader("Connection") == "close"


def test_uncaught_server_errors_suppress_raw_exception_output(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.INFO, logger="guardian.http")
    with running_server() as server:
        try:
            raise RuntimeError("password=do-not-disclose")
        except RuntimeError:
            server.handle_error(cast(Any, None), ("127.0.0.1", 1))
    captured = capsys.readouterr()
    rendered = caplog.text + captured.out + captured.err
    assert "do-not-disclose" not in rendered


@pytest.mark.parametrize("raw", [None, "", "{}", "[]", "not-json"])
def test_startup_rejects_missing_or_malformed_token_maps(raw: str | None) -> None:
    with pytest.raises(ValueError, match="token map"):
        load_token_tenants(raw)


def test_startup_rejects_invalid_token_entries_and_unsafe_binding() -> None:
    with pytest.raises(ValueError, match="token map"):
        load_token_tenants('{"":"tenant-a"}')
    with pytest.raises(ValueError, match="token map"):
        load_token_tenants('{"opaque-a":"tenant/a"}')
    with pytest.raises(ValueError, match="token map"):
        load_token_tenants('{"tökén":"tenant-a"}')
    with pytest.raises(ValueError, match="non-loopback"):
        create_server(
            token_tenants={"opaque-a": "tenant-a"},
            host="0.0.0.0",
            port=0,
        )


def test_non_ascii_bearer_is_a_safe_json_401() -> None:
    with running_server() as server:
        response, body = request(
            server,
            "GET",
            "/v1/incidents/missing/scenario-snapshot",
            authorization="Bearer \xff",
        )
        assert response.status == 401
        assert response.getheader("Content-Type") == "application/json"
        assert body == {"error": "authentication required"}


def test_cli_configuration_defaults_to_loopback_and_requires_valid_values() -> None:
    config = parse_runtime_config(
        {"GUARDIAN_LOCAL_TOKENS_JSON": '{"opaque-a":"tenant-a"}'}, []
    )
    assert config.host == "127.0.0.1"
    assert config.port == 8080
    assert not config.allow_non_loopback
    assert config.token_tenants == {"opaque-a": "tenant-a"}
    assert "opaque-a" not in repr(config)

    with pytest.raises(ValueError, match="port"):
        parse_runtime_config(
            {
                "GUARDIAN_LOCAL_TOKENS_JSON": '{"opaque-a":"tenant-a"}',
                "GUARDIAN_PORT": "not-a-port",
            },
            [],
        )
    with pytest.raises(ValueError, match="opt-in"):
        parse_runtime_config(
            {
                "GUARDIAN_LOCAL_TOKENS_JSON": '{"opaque-a":"tenant-a"}',
                "GUARDIAN_ALLOW_NON_LOOPBACK": "sometimes",
            },
            [],
        )


def test_cli_non_loopback_bind_needs_explicit_local_runtime_opt_in() -> None:
    environment = {
        "GUARDIAN_LOCAL_TOKENS_JSON": '{"opaque-a":"tenant-a"}',
        "GUARDIAN_HOST": "0.0.0.0",
        "GUARDIAN_PORT": "0",
    }
    denied = parse_runtime_config(environment, [])
    with pytest.raises(ValueError, match="non-loopback"):
        create_server(
            token_tenants=denied.token_tenants,
            host=denied.host,
            port=denied.port,
            allow_non_loopback=denied.allow_non_loopback,
        )

    allowed = parse_runtime_config(environment, ["--allow-non-loopback"])
    server = create_server(
        token_tenants=allowed.token_tenants,
        host=allowed.host,
        port=allowed.port,
        allow_non_loopback=allowed.allow_non_loopback,
    )
    server.server_close()


def test_cli_signal_setup_failure_closes_bound_listener() -> None:
    config = parse_runtime_config(
        {"GUARDIAN_LOCAL_TOKENS_JSON": '{"opaque-a":"tenant-a"}'},
        ["--port", "0"],
    )
    servers = []

    def server_factory(**kwargs: Any):
        server = create_server(**kwargs)
        servers.append(server)
        return server

    def fail_signal_setup(_number: int, _handler: Any) -> None:
        raise RuntimeError("injected signal failure")

    with pytest.raises(RuntimeError, match="injected signal failure"):
        run_runtime(
            config,
            server_factory=server_factory,
            signal_installer=fail_signal_setup,
        )

    host, port = servers[0].listening_address
    connection = HTTPConnection(host, port, timeout=0.2)
    with pytest.raises(OSError):
        connection.connect()
    connection.close()


def test_cli_readiness_failure_stops_server_and_joins_thread() -> None:
    config = parse_runtime_config(
        {"GUARDIAN_LOCAL_TOKENS_JSON": '{"opaque-a":"tenant-a"}'},
        ["--port", "0"],
    )
    servers = []
    threads: list[Thread] = []

    def server_factory(**kwargs: Any):
        server = create_server(**kwargs)
        servers.append(server)
        return server

    def thread_factory(**kwargs: Any) -> Thread:
        thread = Thread(**kwargs)
        threads.append(thread)
        return thread

    def fail_readiness(_message: str) -> None:
        raise RuntimeError("injected readiness failure")

    with pytest.raises(RuntimeError, match="injected readiness failure"):
        run_runtime(
            config,
            server_factory=server_factory,
            signal_installer=lambda _number, _handler: None,
            thread_factory=thread_factory,
            readiness_writer=fail_readiness,
        )

    assert len(threads) == 1
    assert not threads[0].is_alive()
    host, port = servers[0].listening_address
    connection = HTTPConnection(host, port, timeout=0.2)
    with pytest.raises(OSError):
        connection.connect()
    connection.close()
