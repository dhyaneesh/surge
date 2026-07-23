"""Fail-closed security contracts for the local Guardian HTTP API."""

from __future__ import annotations

import json
import logging
from http.client import HTTPConnection
from threading import Event, Thread
from typing import Any, cast

import pytest

from apps.guardian_api import __main__ as guardian_main
from apps.guardian_api.__main__ import parse_runtime_config, run_runtime
from apps.guardian_api.http import (
    HTTPIncidentSnapshot,
    _redact,
    create_server,
    load_token_tenants,
)
from apps.guardian_api.service import GuardianService
from apps.guardian_api.store import InMemoryIncidentStore, replay_incident_history
from tests.integration.test_guardian_http import (
    incident_payload,
    request,
    running_server,
    TOKEN_A,
    TOKEN_B,
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
            token=TOKEN_A,
            idempotency_key="override",
            payload=incident_payload(tenant_id="tenant-b"),
        )
        assert response.status == 403
        assert body == {"error": "forbidden"}


def test_untrusted_tenant_header_cannot_override_token_identity() -> None:
    store = InMemoryIncidentStore()
    with running_server(service=GuardianService(store)) as server:
        response, body = request(
            server,
            "POST",
            "/v1/incidents",
            token=TOKEN_A,
            idempotency_key="header-override",
            payload=incident_payload(),
            extra_headers={"X-Guardian-Tenant": "tenant-b"},
        )
        assert response.status == 200
        assert set(body) == {"incident_id", "workflow_id", "projection"}
    assert store.snapshot().incidents[0].facts.tenant_id == "tenant-a"


def test_caller_supplied_incident_id_is_rejected() -> None:
    payload = incident_payload()
    payload["incident_id"] = "caller-chosen-id"
    with running_server() as server:
        response, body = request(
            server,
            "POST",
            "/v1/incidents",
            token=TOKEN_A,
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
            token=TOKEN_A,
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
            token=TOKEN_A,
            idempotency_key="create-a",
            payload=incident_payload(),
        )
        own_missing, own_body = request(
            server,
            "GET",
            "/v1/incidents/missing/scenario-snapshot",
            token=TOKEN_A,
        )
        foreign, foreign_body = request(
            server,
            "GET",
            f"/v1/incidents/{created_body['incident_id']}/scenario-snapshot",
            token=TOKEN_B,
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
            token=TOKEN_A,
            idempotency_key="same-key",
            payload=first_payload,
        )
        conflict, body = request(
            server,
            "POST",
            "/v1/incidents",
            token=TOKEN_A,
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
            token=TOKEN_A,
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
    assert TOKEN_A not in rendered


def test_embedded_configured_bearer_in_valid_input_is_rejected_before_persistence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "valid_nested_secret_A_0123456789"
    payload = incident_payload()
    payload["identity"]["service_name"] = f"processor-{secret}-shadow"
    store = InMemoryIncidentStore()
    caplog.set_level(logging.INFO, logger="guardian.http")

    with running_server(
        token_tenants={secret: "tenant-a"}, service=GuardianService(store)
    ) as server:
        response, body = request(
            server,
            "POST",
            "/v1/incidents",
            token=secret,
            idempotency_key="secret-input",
            payload=payload,
        )

    rendered = json.dumps(body, sort_keys=True) + caplog.text
    assert response.status == 400
    assert body == {"error": "invalid request body"}
    assert secret not in rendered
    assert store.snapshot().incidents == ()


def test_embedded_configured_bearer_is_not_persisted_as_idempotency_key() -> None:
    secret = "valid_idempotency_secret_0123456789"
    store = InMemoryIncidentStore()
    with running_server(
        token_tenants={secret: "tenant-a"}, service=GuardianService(store)
    ) as server:
        response, body = request(
            server,
            "POST",
            "/v1/incidents",
            token=secret,
            idempotency_key=f"retry-{secret}-one",
            payload=incident_payload(),
        )

    assert response.status == 400
    assert secret not in json.dumps(body)
    assert store.snapshot().incidents == ()
    assert store.snapshot().idempotency_count == 0


def test_http_snapshot_dto_excludes_raw_incident_and_observation_inputs() -> None:
    with running_server() as server:
        created, body = request(
            server,
            "POST",
            "/v1/incidents",
            token=TOKEN_A,
            idempotency_key="minimal-snapshot",
            payload=incident_payload(),
        )

    assert created.status == 200
    assert set(body) == {"incident_id", "workflow_id", "projection"}
    assert not {"facts", "observations", "projection_history"}.intersection(body)


def test_configured_token_value_is_redacted_from_valid_snapshot_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "response_value_secret_A_0123456789"

    class SecretProjectionService(GuardianService):
        def submit_incident(self, *args: Any, **kwargs: Any):
            snapshot = super().submit_incident(*args, **kwargs)
            projection = snapshot.projection.model_copy(
                update={"rules_version": secret}
            )
            return snapshot.model_copy(update={"projection": projection})

    caplog.set_level(logging.INFO, logger="guardian.http")
    with running_server(
        token_tenants={secret: "tenant-a"}, service=SecretProjectionService()
    ) as server:
        response, body = request(
            server,
            "POST",
            "/v1/incidents",
            token=secret,
            idempotency_key="redacted-output",
            payload=incident_payload(),
        )

    rendered = json.dumps(body, sort_keys=True) + caplog.text
    assert response.status == 200
    assert body["projection"]["rules_version"] == "[REDACTED]"
    assert set(body) == {"incident_id", "workflow_id", "projection"}
    HTTPIncidentSnapshot.model_validate_json(json.dumps(body))
    assert secret not in rendered


def test_exact_token_redaction_preserves_json_keys() -> None:
    secret = "response_value_secret_A_0123456789"

    redacted = _redact(
        {secret: secret, "api_token": "fixed-schema-value"},
        exact_secrets=frozenset({secret}),
    )

    assert redacted == {
        secret: "[REDACTED]",
        "api_token": "fixed-schema-value",
    }


def test_attacker_controlled_method_is_normalized_in_structured_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attacker_method = "TOKEN-do-not-disclose"
    caplog.set_level(logging.INFO, logger="guardian.http")
    with running_server() as server:
        response, _ = request(server, attacker_method, "/health")

    method_fields = [
        cast(str, getattr(record, "http_method"))
        for record in caplog.records
        if hasattr(record, "http_method")
    ]
    assert response.status == 405
    assert method_fields
    assert set(method_fields) == {"unsupported"}
    assert attacker_method not in method_fields


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
            token=TOKEN_A,
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
            token=TOKEN_A,
            idempotency_key="large",
            payload=b"x" * 65,
            content_type="application/json",
        )
        wrong_type, _ = request(
            server,
            "POST",
            "/v1/incidents",
            token=TOKEN_A,
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
        load_token_tenants(json.dumps({TOKEN_A: "tenant/a"}))
    with pytest.raises(ValueError, match="token map"):
        load_token_tenants('{"tökén":"tenant-a"}')
    with pytest.raises(ValueError, match="token map"):
        load_token_tenants('{"short-token":"tenant-a"}')
    with pytest.raises(ValueError, match="token map"):
        load_token_tenants(json.dumps({"a" * 32: "tenant-a"}))
    with pytest.raises(ValueError, match="token map"):
        load_token_tenants(json.dumps({"invalid.token." + "a" * 32: "tenant-a"}))
    with pytest.raises(ValueError, match="non-loopback"):
        create_server(
            token_tenants={TOKEN_A: "tenant-a"},
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
        {"GUARDIAN_LOCAL_TOKENS_JSON": json.dumps({TOKEN_A: "tenant-a"})}, []
    )
    assert config.host == "127.0.0.1"
    assert config.port == 8080
    assert not config.allow_non_loopback
    assert config.token_tenants == {TOKEN_A: "tenant-a"}
    assert TOKEN_A not in repr(config)

    with pytest.raises(ValueError, match="port"):
        parse_runtime_config(
            {
                "GUARDIAN_LOCAL_TOKENS_JSON": json.dumps({TOKEN_A: "tenant-a"}),
                "GUARDIAN_PORT": "not-a-port",
            },
            [],
        )
    with pytest.raises(ValueError, match="opt-in"):
        parse_runtime_config(
            {
                "GUARDIAN_LOCAL_TOKENS_JSON": json.dumps({TOKEN_A: "tenant-a"}),
                "GUARDIAN_ALLOW_NON_LOOPBACK": "sometimes",
            },
            [],
        )


def test_cli_non_loopback_bind_needs_explicit_local_runtime_opt_in() -> None:
    environment = {
        "GUARDIAN_LOCAL_TOKENS_JSON": json.dumps({TOKEN_A: "tenant-a"}),
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
        {"GUARDIAN_LOCAL_TOKENS_JSON": json.dumps({TOKEN_A: "tenant-a"})},
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
        {"GUARDIAN_LOCAL_TOKENS_JSON": json.dumps({TOKEN_A: "tenant-a"})},
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


class _FailingRuntimeServer:
    drain_timeout = 0.1
    listening_address = ("127.0.0.1", 1)

    def __init__(self, serve: Any) -> None:
        self.serve_forever = serve
        self.closed = False

    def drain_and_close(self) -> None:
        self.closed = True

    def server_close(self) -> None:
        self.closed = True


def test_cli_does_not_publish_readiness_when_serving_thread_dies_at_startup() -> None:
    config = parse_runtime_config(
        {"GUARDIAN_LOCAL_TOKENS_JSON": json.dumps({TOKEN_A: "tenant-a"})},
        ["--port", "0"],
    )
    readiness: list[str] = []
    server = _FailingRuntimeServer(
        lambda: (_ for _ in ()).throw(RuntimeError("unexpected serving failure"))
    )

    with pytest.raises(guardian_main.GuardianServerRuntimeError):
        run_runtime(
            config,
            server_factory=lambda **_kwargs: cast(Any, server),
            signal_installer=lambda _number, _handler: None,
            readiness_writer=readiness.append,
        )

    assert readiness == []
    assert server.closed


def test_cli_surfaces_serving_thread_death_after_readiness() -> None:
    config = parse_runtime_config(
        {"GUARDIAN_LOCAL_TOKENS_JSON": json.dumps({TOKEN_A: "tenant-a"})},
        ["--port", "0"],
    )
    release = Event()
    failed = Event()
    installed_handler: list[Any] = []

    def serve() -> None:
        assert release.wait(timeout=2)
        failed.set()
        raise RuntimeError("unexpected serving failure")

    server = _FailingRuntimeServer(serve)

    def install(_number: int, handler: Any) -> None:
        installed_handler.append(handler)

    def publish_readiness(_message: str) -> None:
        release.set()
        assert failed.wait(timeout=1)
        installed_handler[0](15, None)

    with pytest.raises(guardian_main.GuardianServerRuntimeError):
        run_runtime(
            config,
            server_factory=lambda **_kwargs: cast(Any, server),
            signal_installer=install,
            readiness_writer=publish_readiness,
        )

    assert server.closed


def test_cli_restores_previous_sigterm_handler() -> None:
    config = parse_runtime_config(
        {"GUARDIAN_LOCAL_TOKENS_JSON": json.dumps({TOKEN_A: "tenant-a"})},
        ["--port", "0"],
    )
    previous_handler = object()
    installed: list[Any] = []

    def install(_number: int, handler: Any) -> object:
        installed.append(handler)
        return previous_handler

    def stop_after_readiness(_message: str) -> None:
        installed[0](15, None)

    assert (
        run_runtime(
            config,
            signal_installer=install,
            readiness_writer=stop_after_readiness,
        )
        == 0
    )
    assert installed[1] is previous_handler


def test_cli_serving_thread_entry_uses_configurable_one_second_bound() -> None:
    assert guardian_main.DEFAULT_SERVING_STARTUP_TIMEOUT >= 1.0
    config = parse_runtime_config(
        {"GUARDIAN_LOCAL_TOKENS_JSON": json.dumps({TOKEN_A: "tenant-a"})},
        ["--port", "0"],
    )
    installed: list[Any] = []

    def install(_number: int, handler: Any) -> None:
        installed.append(handler)

    def delayed_thread_factory(**kwargs: Any) -> Thread:
        target = kwargs.pop("target")

        def delayed_target() -> None:
            Event().wait(0.2)
            target()

        return Thread(target=delayed_target, **kwargs)

    def stop_after_readiness(_message: str) -> None:
        installed[0](15, None)

    assert (
        run_runtime(
            config,
            signal_installer=install,
            thread_factory=delayed_thread_factory,
            readiness_writer=stop_after_readiness,
            serving_startup_timeout=guardian_main.DEFAULT_SERVING_STARTUP_TIMEOUT,
        )
        == 0
    )
