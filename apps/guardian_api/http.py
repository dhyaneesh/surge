"""Authenticated loopback HTTP transport for the minimal Guardian runtime."""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import math
import re
import socket
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore, Condition
from types import MappingProxyType
from typing import Any, cast, overload
from urllib.parse import urlsplit

from pydantic import Field, BaseModel, ValidationError

from apps.guardian_api.models import (
    SCOPED_IDENTIFIER_PATTERN,
    ActionType,
    GuardianProjection,
    IncidentFacts,
    IncidentSubmission,
    ObservationUpdate,
    ScopedIdentifier,
    StrictModel,
)
from apps.guardian_api.rules import Polarity, evidence_contributions
from apps.guardian_api.service import GuardianService, TenantMismatchError
from apps.guardian_api.store import (
    IdempotencyConflictError,
    IncidentInvariantError,
    IncidentNotFoundError,
    IncidentSnapshot,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_MAX_REQUEST_BODY = 1_048_576
DEFAULT_CONNECTION_READ_TIMEOUT = 5.0
DEFAULT_DRAIN_TIMEOUT = 1.0
DEFAULT_MAX_CONCURRENT_REQUESTS = 32

_LOGGER = logging.getLogger("guardian.http")
_INCIDENT_PATH = re.compile(
    rf"^/v1/incidents/(?P<incident_id>{SCOPED_IDENTIFIER_PATTERN[1:-1]})/"
    r"scenario-snapshot$"
)
_OBSERVATION_PATH = re.compile(
    rf"^/v1/incidents/(?P<incident_id>{SCOPED_IDENTIFIER_PATTERN[1:-1]})/"
    r"observations$"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b(authorization|cookie|credential|password|secret|token)"
    r"(\s*[:=]\s*)[^\s,;]+"
)
_SUPPORTED_HTTP_METHODS = frozenset({"GET", "POST"})
_SATURATED_BODY = b'{"error":"service unavailable"}'
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_MINIMUM_TOKEN_DIVERSITY = 8


class _DuplicateJSONKey(ValueError):
    pass


def _strict_json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey
        result[key] = value
    return result


def _validate_strict_json(raw: bytes) -> None:
    decoded = raw.decode("utf-8")
    json.loads(
        decoded,
        object_pairs_hook=_strict_json_object_pairs,
        parse_constant=_reject_json_constant,
    )


def _reject_json_constant(_value: str) -> None:
    raise ValueError


_SIGNAL_EVIDENCE_TYPE = {
    "request_rate": "load",
    "cpu_utilization": "resource-utilization",
    "memory_utilization": "resource-utilization",
    "error_rate": "exceptions",
    "p95_latency_ms": "metrics",
    "deployment_version": "deployment-event",
    "topology_edge": "topology",
    "dependency_healthy": "dependency-health",
    "restart_delta": "resource-utilization",
    "throttling_ratio": "resource-utilization",
    "oom_killed": "resource-utilization",
}

_GROUP_SIGNAL = {
    "load": "request_rate",
    "utilization": "cpu_utilization",
    "pressure": "restart_delta",
    "deployment": "deployment_version",
    "exceptions": "error_rate",
    "latency": "p95_latency_ms",
    "topology": "topology_edge",
    "dependency": "dependency_healthy",
}


def _evidence_descriptor(
    facts: IncidentFacts, signal_name: str
) -> dict[str, Any] | None:
    evidence_type = _SIGNAL_EVIDENCE_TYPE.get(signal_name)
    item = getattr(facts.signals, signal_name, None)
    if evidence_type is None or item is None:
        return None
    tenant_relation = (
        "foreign-tenant" if item.tenant_id != facts.tenant_id else "same-tenant"
    )
    return {
        "evidenceType": evidence_type,
        "subjectRole": item.subject_role,
        "tenantRelation": tenant_relation,
        "freshness": item.freshness.value,
    }


def _scenario_evidence_lists(
    snapshot: IncidentSnapshot,
) -> tuple[
    tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]
]:
    facts = snapshot.facts
    now = snapshot.evaluation_times[-1]
    contributions = evidence_contributions(
        facts.signals,
        now=now,
        freshness_seconds=facts.telemetry.freshness_seconds,
    )
    supporting: list[dict[str, Any]] = []
    contradicting: list[dict[str, Any]] = []
    seen_support: set[str] = set()
    seen_contradict: set[str] = set()
    for contribution in contributions:
        signal_name = _GROUP_SIGNAL.get(contribution.group)
        if signal_name is None:
            continue
        descriptor = _evidence_descriptor(facts, signal_name)
        if descriptor is None:
            continue
        key = json.dumps(descriptor, sort_keys=True)
        if contribution.polarity is Polarity.SUPPORT:
            if key not in seen_support:
                supporting.append(descriptor)
                seen_support.add(key)
        elif key not in seen_contradict:
            contradicting.append(descriptor)
            seen_contradict.add(key)
    # Metrics evidence for healthy observation when telemetry is present.
    if facts.telemetry.quality >= 0.80 and facts.identity is not None:
        metrics = {
            "evidenceType": "metrics",
            "subjectRole": facts.identity.target_role,
            "tenantRelation": "same-tenant",
            "freshness": "fresh",
        }
        key = json.dumps(metrics, sort_keys=True)
        if key not in seen_support:
            supporting.append(metrics)
    required_fresh: list[dict[str, Any]] = []
    if snapshot.projection.recovery_verified and facts.identity is not None:
        required_fresh.append(
            {
                "evidenceType": "recovery-telemetry",
                "subjectRole": facts.identity.target_role,
                "tenantRelation": "same-tenant",
                "freshness": "fresh",
            }
        )
    return tuple(supporting), tuple(contradicting), tuple(required_fresh)


def _scenario_workflow_states(snapshot: IncidentSnapshot) -> tuple[str, ...]:
    states = ["active"]
    for projection in snapshot.projection_history:
        value = projection.workflow_state.value
        if value not in states:
            states.append(value)
    if "assessment" not in states:
        states.insert(1, "assessment")
    if snapshot.projection.terminal_reason or snapshot.projection.recovery_verified:
        if "closed" not in states:
            states.append("closed")
    elif "closed" not in states and snapshot.projection.proposed_action is None:
        states.append("closed")
    return tuple(states)


def _scenario_safety_gates(snapshot: IncidentSnapshot) -> tuple[str, ...]:
    gates: list[str] = []
    if snapshot.projection.recovery_verified:
        gates.append("post-action-evidence-for-recovery")
    return tuple(gates)


def _scenario_audit_counts(snapshot: IncidentSnapshot) -> dict[str, int]:
    return {"observation-recorded": 1 + len(snapshot.observations)}


def _action_dict(action: ActionType) -> dict[str, Any]:
    if action is ActionType.SCALE_UP:
        return {"actionType": "scale", "scaleDirection": "up"}
    if action is ActionType.SCALE_DOWN:
        return {"actionType": "scale", "scaleDirection": "down"}
    return {"actionType": action.value.replace("-", "_")}


def _build_scenario_observation_payload(snapshot: IncidentSnapshot) -> dict[str, Any]:
    supporting, contradicting, required_fresh = _scenario_evidence_lists(snapshot)
    projection = snapshot.projection
    return HTTPScenarioObservationSnapshot(
        incident_id=snapshot.incident_id,
        workflow_id=snapshot.workflow_id,
        projection=projection,
        supporting_evidence=supporting,
        contradicting_evidence=contradicting,
        required_fresh_evidence=required_fresh,
        audit_event_counts=_scenario_audit_counts(snapshot),
        safety_gates=_scenario_safety_gates(snapshot),
        workflow_states=_scenario_workflow_states(snapshot),
        parent_count=1,
        proposal_count=1 if projection.proposed_action is not None else 0,
        approval_count=1
        if snapshot.facts.control.approval_issued_at is not None
        else 0,
        permitted_operations=tuple(item.value for item in projection.permitted_actions),
        forbidden_operations=tuple(item.value for item in projection.forbidden_actions),
        policy_bundle_state=snapshot.facts.policy.state.value,
        tenant_isolation=(
            {"foreignEvidenceRejected": True}
            if projection.foreign_evidence_rejected
            else {"foreignEvidenceRejected": False}
        ),
        mutations=tuple(
            _action_dict(item) for item in ()
        ),  # local runtime never executes
    ).model_dump(mode="json")


class HTTPIncidentSnapshot(StrictModel):
    """Minimal transport DTO that excludes persisted caller and audit inputs."""

    incident_id: ScopedIdentifier
    workflow_id: str
    projection: GuardianProjection


class HTTPScenarioObservationSnapshot(StrictModel):
    """Test-observation projection with assertion-ready fields for scenarios."""

    incident_id: ScopedIdentifier
    workflow_id: str
    projection: GuardianProjection
    supporting_evidence: tuple[dict[str, Any], ...] = ()
    contradicting_evidence: tuple[dict[str, Any], ...] = ()
    required_fresh_evidence: tuple[dict[str, Any], ...] = ()
    audit_event_counts: dict[str, int] = Field(default_factory=dict)
    safety_gates: tuple[str, ...] = ()
    workflow_states: tuple[str, ...] = ()
    parent_count: int = 1
    proposal_count: int = 0
    approval_count: int = 0
    permitted_operations: tuple[str, ...] = ()
    forbidden_operations: tuple[str, ...] = ()
    policy_bundle_state: str | None = None
    tenant_isolation: dict[str, bool] | None = None
    mutations: tuple[dict[str, Any], ...] = ()


def _redact_string(value: str, exact_secrets: frozenset[str]) -> str:
    for secret in sorted(exact_secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    value = _BEARER_VALUE.sub("Bearer [REDACTED]", value)
    return _ASSIGNED_SECRET.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value
    )


def _redact(value: Any, *, exact_secrets: frozenset[str] = frozenset()) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _redact(item, exact_secrets=exact_secrets)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item, exact_secrets=exact_secrets) for item in value]
    if isinstance(value, str):
        return _redact_string(value, exact_secrets)
    return value


def _contains_configured_token_substring(
    value: Any, exact_secrets: frozenset[str]
) -> bool:
    if isinstance(value, BaseModel):
        return _contains_configured_token_substring(
            value.model_dump(mode="python"), exact_secrets
        )
    if isinstance(value, Mapping):
        return any(
            _contains_configured_token_substring(key, exact_secrets)
            or _contains_configured_token_substring(item, exact_secrets)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple | set | frozenset):
        return any(
            _contains_configured_token_substring(item, exact_secrets) for item in value
        )
    return isinstance(value, str) and any(secret in value for secret in exact_secrets)


def _normalized_http_method(
    value: object, *, exact_secrets: frozenset[str] = frozenset()
) -> str:
    if value in _SUPPORTED_HTTP_METHODS and value not in exact_secrets:
        return cast(str, value)
    return "unsupported"


def _validated_token_tenants(token_tenants: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(token_tenants, Mapping) or not token_tenants:
        raise ValueError("invalid local token map")
    validated: dict[str, str] = {}
    for token, tenant in token_tenants.items():
        if (
            not isinstance(token, str)
            or _TOKEN_PATTERN.fullmatch(token) is None
            or len(set(token)) < _MINIMUM_TOKEN_DIVERSITY
            or not isinstance(tenant, str)
            or re.fullmatch(SCOPED_IDENTIFIER_PATTERN, tenant) is None
        ):
            raise ValueError("invalid local token map")
        validated[token] = tenant
    return MappingProxyType(validated)


def load_token_tenants(raw_json: str | None) -> Mapping[str, str]:
    """Load the mandatory opaque-token-to-tenant map from one JSON value."""

    if not isinstance(raw_json, str) or not raw_json.strip():
        raise ValueError("invalid local token map")
    try:
        parsed = json.loads(
            raw_json,
            object_pairs_hook=_strict_json_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("invalid local token map") from error
    if not isinstance(parsed, dict):
        raise ValueError("invalid local token map")
    return _validated_token_tenants(parsed)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        if host.lower() != "localhost":
            return False
        try:
            addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError:
            return False
        return bool(addresses) and all(
            ipaddress.ip_address(item[4][0]).is_loopback for item in addresses
        )


class GuardianHTTPServer(ThreadingHTTPServer):
    """Thread-per-request server carrying only local Guardian dependencies."""

    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        token_tenants: Mapping[str, str],
        service: GuardianService,
        max_request_body: int,
        connection_read_timeout: float,
        drain_timeout: float,
        max_concurrent_requests: int,
    ) -> None:
        self.token_tenants = token_tenants
        self.token_values = frozenset(token_tenants)
        self.guardian_service = service
        self.max_request_body = max_request_body
        self.connection_read_timeout = connection_read_timeout
        self.drain_timeout = drain_timeout
        self.max_concurrent_requests = max_concurrent_requests
        self._request_slots = BoundedSemaphore(max_concurrent_requests)
        self._connection_condition = Condition()
        self._active_connections: set[socket.socket] = set()
        super().__init__(server_address, GuardianRequestHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            try:
                response = (
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(_SATURATED_BODY)}\r\n".encode("ascii")
                    + b"Cache-Control: no-store\r\n"
                    b"X-Content-Type-Options: nosniff\r\n"
                    b"Connection: close\r\n\r\n" + _SATURATED_BODY
                )
                request.sendall(response)
                _LOGGER.info(
                    "guardian HTTP request completed",
                    extra={"http_method": "unsupported", "http_status": 503},
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(self.connection_read_timeout)
        with self._connection_condition:
            self._active_connections.add(request)
        return request, client_address

    def shutdown_request(self, request: Any) -> None:
        try:
            super().shutdown_request(request)
        finally:
            with self._connection_condition:
                self._active_connections.discard(request)
                self._connection_condition.notify_all()

    def drain_and_close(self, *, timeout_seconds: float | None = None) -> None:
        """Stop accepting, drain bounded handlers, then close stalled sockets."""

        timeout = self.drain_timeout if timeout_seconds is None else timeout_seconds
        if (
            not isinstance(timeout, int | float)
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("drain timeout must be a positive finite number")
        self.shutdown()
        deadline = time.monotonic() + timeout
        with self._connection_condition:
            while self._active_connections:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._connection_condition.wait(timeout=remaining)
            stalled = tuple(self._active_connections)
        for connection in stalled:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        self.server_close()

    def resolve_tenant(self, candidate: str) -> str | None:
        if not candidate.isascii():
            return None
        candidate_bytes = candidate.encode("ascii")
        for token, tenant in self.token_tenants.items():
            if hmac.compare_digest(candidate_bytes, token.encode("ascii")):
                return tenant
        return None

    def contains_configured_token(self, value: Any) -> bool:
        return _contains_configured_token_substring(value, self.token_values)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Suppress SocketServer tracebacks that could contain request secrets."""

        del request, client_address
        _LOGGER.error("guardian HTTP connection failed safely")

    @property
    def listening_address(self) -> tuple[str, int]:
        """Return the bound IPv4 host and port with a narrow public type."""

        return str(self.server_address[0]), int(self.server_address[1])


class GuardianRequestHandler(BaseHTTPRequestHandler):
    """Strict JSON routes with fail-closed authentication and safe errors."""

    protocol_version = "HTTP/1.1"
    server_version = "GuardianLocal"
    sys_version = ""
    _request_content_length: int | None = None

    def __getattr__(self, name: str):
        if name.startswith("do_"):
            return self._dispatch
        raise AttributeError(name)

    @property
    def guardian_server(self) -> GuardianHTTPServer:
        return cast(GuardianHTTPServer, self.server)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress BaseHTTPRequestHandler's raw request/header-style logging."""

        del format, args

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """Suppress the default raw request target log."""

    def log_error(self, format: str, *args: Any) -> None:
        """Suppress default exception interpolation."""

        del format, args

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Replace BaseHTTPRequestHandler HTML errors with bounded JSON."""

        del message, explain
        try:
            status = HTTPStatus(code)
        except ValueError:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self._error(status, status.phrase.lower())

    def _send_json(
        self,
        status: HTTPStatus,
        payload: Mapping[str, Any] | list[Any],
        *,
        headers: Mapping[str, str] | None = None,
        close_connection: bool = False,
    ) -> None:
        body = json.dumps(
            _redact(payload, exact_secrets=self.guardian_server.token_values),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        self.send_response_only(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if close_connection:
            self.send_header("Connection", "close")
            self.close_connection = True
        if headers is not None:
            for name, value in headers.items():
                self.send_header(name, value)
        self.end_headers()
        if getattr(self, "command", "") != "HEAD":
            self.wfile.write(body)
        _LOGGER.info(
            "guardian HTTP request completed",
            extra={
                "http_method": _normalized_http_method(
                    getattr(self, "command", "unknown"),
                    exact_secrets=self.guardian_server.token_values,
                ),
                "http_status": status.value,
            },
        )

    def _error(
        self,
        status: HTTPStatus,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
        close_connection: bool = True,
    ) -> None:
        self._send_json(
            status,
            {"error": message},
            headers=headers,
            close_connection=close_connection,
        )

    def _route(self) -> tuple[str, str | None] | None:
        parsed = urlsplit(self.path)
        if parsed.query or parsed.fragment or parsed.scheme or parsed.netloc:
            return None
        if parsed.path == "/health":
            return ("health", None)
        if parsed.path == "/v1/incidents":
            return ("incidents", None)
        if match := _OBSERVATION_PATH.fullmatch(parsed.path):
            return ("observations", match.group("incident_id"))
        if match := _INCIDENT_PATH.fullmatch(parsed.path):
            return ("snapshot", match.group("incident_id"))
        return None

    def _validate_request_envelope(self) -> bool:
        if self.headers.get_all("Transfer-Encoding", []):
            self._error(HTTPStatus.BAD_REQUEST, "invalid request body")
            return False
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self._request_content_length = None
            return True
        if len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None:
            self._error(HTTPStatus.BAD_REQUEST, "invalid request body")
            return False
        normalized_length = lengths[0].lstrip("0") or "0"
        maximum_length = str(self.guardian_server.max_request_body)
        if len(normalized_length) > len(maximum_length) or (
            len(normalized_length) == len(maximum_length)
            and normalized_length > maximum_length
        ):
            self._error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request body too large",
            )
            return False
        length = int(normalized_length)
        self._request_content_length = length
        return True

    def _authenticated_tenant(self) -> str | None:
        values = self.headers.get_all("Authorization", [])
        if len(values) != 1:
            self._error(
                HTTPStatus.UNAUTHORIZED,
                "authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
            return None
        match = re.fullmatch(r"Bearer ([^\s,]+)", values[0], re.IGNORECASE)
        tenant = self.guardian_server.resolve_tenant(match.group(1)) if match else None
        if tenant is None:
            self._error(
                HTTPStatus.UNAUTHORIZED,
                "authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return tenant

    def _idempotency_key(self) -> str | None:
        values = self.headers.get_all("Idempotency-Key", [])
        if (
            len(values) != 1
            or not values[0].strip()
            or values[0] != values[0].strip()
            or len(values[0]) > 256
            or any(ord(character) < 33 for character in values[0])
            or any(token in values[0] for token in self.guardian_server.token_values)
        ):
            self._error(HTTPStatus.BAD_REQUEST, "idempotency key required")
            return None
        return values[0]

    @overload
    def _read_model(self, model: type[IncidentFacts]) -> IncidentFacts | None: ...

    @overload
    def _read_model(
        self, model: type[IncidentSubmission]
    ) -> IncidentSubmission | None: ...

    @overload
    def _read_model(
        self, model: type[ObservationUpdate]
    ) -> ObservationUpdate | None: ...

    def _read_model(
        self,
        model: type[IncidentFacts] | type[IncidentSubmission] | type[ObservationUpdate],
    ) -> IncidentFacts | IncidentSubmission | ObservationUpdate | None:
        content_types = self.headers.get_all("Content-Type", [])
        if len(content_types) != 1:
            self._error(HTTPStatus.BAD_REQUEST, "JSON content type required")
            return None
        media_type = content_types[0].split(";", 1)[0].strip().lower()
        if (
            not (
                media_type == "application/json"
                or media_type.startswith("application/")
                and media_type.endswith("+json")
            )
            or self.headers.get_content_charset("utf-8").lower() != "utf-8"
        ):
            self._error(HTTPStatus.BAD_REQUEST, "JSON content type required")
            return None
        length = getattr(self, "_request_content_length", None)
        if length is None or length <= 0:
            self._error(HTTPStatus.BAD_REQUEST, "invalid request body")
            return None
        try:
            raw = self.rfile.read(length)
        except TimeoutError:
            try:
                self._error(
                    HTTPStatus.REQUEST_TIMEOUT,
                    "request timeout",
                    close_connection=True,
                )
            except OSError:
                self.close_connection = True
            return None
        if len(raw) != length:
            self._error(
                HTTPStatus.BAD_REQUEST,
                "invalid request body",
                close_connection=True,
            )
            return None
        try:
            _validate_strict_json(raw)
            return model.model_validate_json(raw)
        except (UnicodeDecodeError, ValueError, ValidationError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid request body")
            return None

    def _snapshot_payload(self, snapshot: IncidentSnapshot) -> dict[str, Any]:
        return HTTPIncidentSnapshot(
            incident_id=snapshot.incident_id,
            workflow_id=snapshot.workflow_id,
            projection=snapshot.projection,
        ).model_dump(mode="json")

    def _handle_create(self, tenant: str) -> None:
        idempotency_key = self._idempotency_key()
        if idempotency_key is None:
            return
        submission = self._read_model(IncidentSubmission)
        if submission is None:
            return
        if self.guardian_server.contains_configured_token(submission):
            self._error(HTTPStatus.BAD_REQUEST, "invalid request body")
            return
        if (
            submission.tenant_id != tenant
            or any(
                item.tenant_id != tenant for item in submission.signals.all_evidence()
            )
            or submission.scaler is not None
            and submission.scaler.tenant_id != tenant
        ):
            self._error(HTTPStatus.FORBIDDEN, "forbidden")
            return
        snapshot = self.guardian_server.guardian_service.submit_incident(
            tenant,
            idempotency_key,
            submission,
            now=datetime.now(UTC),
        )
        self._send_json(HTTPStatus.OK, self._snapshot_payload(snapshot))

    def _handle_observation(self, tenant: str, incident_id: str) -> None:
        observation = self._read_model(ObservationUpdate)
        if observation is None:
            return
        if self.guardian_server.contains_configured_token(observation):
            self._error(HTTPStatus.BAD_REQUEST, "invalid request body")
            return
        if observation.tenant_id != tenant:
            self._error(HTTPStatus.FORBIDDEN, "forbidden")
            return
        if observation.incident_id != incident_id:
            self._error(HTTPStatus.NOT_FOUND, "incident not found")
            return
        snapshot = self.guardian_server.guardian_service.append_observation(
            tenant,
            incident_id,
            observation,
            now=datetime.now(UTC),
        )
        self._send_json(HTTPStatus.OK, self._snapshot_payload(snapshot))

    def _handle_snapshot(self, tenant: str, incident_id: str) -> None:
        snapshot = self.guardian_server.guardian_service.get_incident(
            tenant, incident_id
        )
        self._send_json(HTTPStatus.OK, _build_scenario_observation_payload(snapshot))

    def _dispatch_request(self) -> None:
        if not self._validate_request_envelope():
            return
        if self.command not in _SUPPORTED_HTTP_METHODS:
            route = self._route()
            allow = (
                {
                    "health": "GET",
                    "incidents": "POST",
                    "observations": "POST",
                    "snapshot": "GET",
                }[route[0]]
                if route is not None
                else "GET, POST"
            )
            self._error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "method not allowed",
                headers={"Allow": allow},
            )
            return
        route = self._route()
        if route is None:
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        route_name, incident_id = route
        expected_method = {
            "health": "GET",
            "incidents": "POST",
            "observations": "POST",
            "snapshot": "GET",
        }[route_name]
        if self.command != expected_method:
            self._error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "method not allowed",
                headers={"Allow": expected_method},
            )
            return
        if expected_method == "GET" and (self._request_content_length or 0) > 0:
            self._error(HTTPStatus.BAD_REQUEST, "request body not allowed")
            return
        if route_name == "health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        tenant = self._authenticated_tenant()
        if tenant is None:
            return
        if route_name == "incidents":
            self._handle_create(tenant)
        elif route_name == "observations":
            assert incident_id is not None
            self._handle_observation(tenant, incident_id)
        else:
            assert incident_id is not None
            self._handle_snapshot(tenant, incident_id)

    def _dispatch(self) -> None:
        try:
            self._dispatch_request()
        except TenantMismatchError:
            self._error(HTTPStatus.FORBIDDEN, "forbidden")
        except IncidentNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, "incident not found")
        except (IdempotencyConflictError, IncidentInvariantError):
            self._error(HTTPStatus.CONFLICT, "conflict")
        except Exception:
            _LOGGER.error(
                "guardian HTTP request failed safely",
                extra={
                    "http_method": _normalized_http_method(
                        self.command,
                        exact_secrets=self.guardian_server.token_values,
                    ),
                    "http_status": 500,
                },
            )
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")

    do_GET = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_PATCH = _dispatch
    do_DELETE = _dispatch
    do_OPTIONS = _dispatch
    do_HEAD = _dispatch
    do_TRACE = _dispatch
    do_CONNECT = _dispatch


def create_server(
    *,
    token_tenants: Mapping[str, str],
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    service: GuardianService | None = None,
    max_request_body: int = DEFAULT_MAX_REQUEST_BODY,
    connection_read_timeout: float = DEFAULT_CONNECTION_READ_TIMEOUT,
    drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
    max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS,
    allow_non_loopback: bool = False,
) -> GuardianHTTPServer:
    """Create a configured server without starting its request loop."""

    if not isinstance(host, str) or not host:
        raise ValueError("host is required")
    if not allow_non_loopback and not _is_loopback(host):
        raise ValueError("non-loopback bind requires explicit opt-in")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if (
        not isinstance(max_request_body, int)
        or isinstance(max_request_body, bool)
        or max_request_body <= 0
    ):
        raise ValueError("request body limit must be positive")
    if (
        not isinstance(max_concurrent_requests, int)
        or isinstance(max_concurrent_requests, bool)
        or max_concurrent_requests <= 0
    ):
        raise ValueError("request concurrency limit must be positive")
    for name, value in (
        ("connection read timeout", connection_read_timeout),
        ("drain timeout", drain_timeout),
    ):
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a positive finite number")
    return GuardianHTTPServer(
        (host, port),
        token_tenants=_validated_token_tenants(token_tenants),
        service=service or GuardianService(),
        max_request_body=max_request_body,
        connection_read_timeout=float(connection_read_timeout),
        drain_timeout=float(drain_timeout),
        max_concurrent_requests=max_concurrent_requests,
    )
