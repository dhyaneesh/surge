"""Authenticated loopback HTTP transport for the minimal Guardian runtime."""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import re
import socket
from collections.abc import Mapping
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import MappingProxyType
from typing import Any, cast, overload
from urllib.parse import urlsplit

from pydantic import ValidationError

from apps.guardian_api.models import (
    SCOPED_IDENTIFIER_PATTERN,
    IncidentFacts,
    ObservationUpdate,
)
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

_LOGGER = logging.getLogger("guardian.http")
_INCIDENT_PATH = re.compile(
    rf"^/v1/incidents/(?P<incident_id>{SCOPED_IDENTIFIER_PATTERN[1:-1]})/"
    r"scenario-snapshot$"
)
_OBSERVATION_PATH = re.compile(
    rf"^/v1/incidents/(?P<incident_id>{SCOPED_IDENTIFIER_PATTERN[1:-1]})/"
    r"observations$"
)
_SECRET_KEY = re.compile(
    r"(?:authorization|cookie|credential|password|secret|token)", re.IGNORECASE
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_ASSIGNED_SECRET = re.compile(
    r"(?i)\b(authorization|cookie|credential|password|secret|token)"
    r"(\s*[:=]\s*)[^\s,;]+"
)


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


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]" if _SECRET_KEY.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        value = _BEARER_VALUE.sub("Bearer [REDACTED]", value)
        return _ASSIGNED_SECRET.sub(
            lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value
        )
    return value


def _validated_token_tenants(token_tenants: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(token_tenants, Mapping) or not token_tenants:
        raise ValueError("invalid local token map")
    validated: dict[str, str] = {}
    for token, tenant in token_tenants.items():
        if (
            not isinstance(token, str)
            or not token
            or token != token.strip()
            or any(
                character.isspace()
                or ord(character) < 33
                or ord(character) == 127
                or character == ","
                for character in token
            )
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
    block_on_close = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        token_tenants: Mapping[str, str],
        service: GuardianService,
        max_request_body: int,
    ) -> None:
        self.token_tenants = token_tenants
        self.guardian_service = service
        self.max_request_body = max_request_body
        super().__init__(server_address, GuardianRequestHandler)

    def resolve_tenant(self, candidate: str) -> str | None:
        for token, tenant in self.token_tenants.items():
            if hmac.compare_digest(candidate, token):
                return tenant
        return None

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

    def _send_json(
        self,
        status: HTTPStatus,
        payload: Mapping[str, Any] | list[Any],
        *,
        headers: Mapping[str, str] | None = None,
        close_connection: bool = False,
    ) -> None:
        body = json.dumps(
            _redact(payload), sort_keys=True, separators=(",", ":"), allow_nan=False
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
        if self.command != "HEAD":
            self.wfile.write(body)
        _LOGGER.info(
            "guardian HTTP request completed",
            extra={"http_method": self.command, "http_status": status.value},
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
        ):
            self._error(HTTPStatus.BAD_REQUEST, "idempotency key required")
            return None
        return values[0]

    @overload
    def _read_model(self, model: type[IncidentFacts]) -> IncidentFacts | None: ...

    @overload
    def _read_model(
        self, model: type[ObservationUpdate]
    ) -> ObservationUpdate | None: ...

    def _read_model(
        self, model: type[IncidentFacts] | type[ObservationUpdate]
    ) -> IncidentFacts | ObservationUpdate | None:
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
        if self.headers.get_all("Transfer-Encoding", []):
            self._error(
                HTTPStatus.BAD_REQUEST,
                "invalid request body",
                close_connection=True,
            )
            return None
        lengths = self.headers.get_all("Content-Length", [])
        try:
            length = int(lengths[0]) if len(lengths) == 1 else -1
        except ValueError:
            length = -1
        if length > self.guardian_server.max_request_body:
            self._error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "request body too large",
                close_connection=True,
            )
            return None
        if length <= 0:
            self._error(HTTPStatus.BAD_REQUEST, "invalid request body")
            return None
        raw = self.rfile.read(length)
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
        return snapshot.model_dump(mode="json")

    def _handle_create(self, tenant: str) -> None:
        idempotency_key = self._idempotency_key()
        if idempotency_key is None:
            return
        facts = self._read_model(IncidentFacts)
        if facts is None:
            return
        if (
            facts.tenant_id != tenant
            or any(item.tenant_id != tenant for item in facts.signals.all_evidence())
            or facts.scaler is not None
            and facts.scaler.tenant_id != tenant
        ):
            self._error(HTTPStatus.FORBIDDEN, "forbidden")
            return
        snapshot = self.guardian_server.guardian_service.submit_incident(
            tenant,
            idempotency_key,
            facts,
            now=datetime.now(UTC),
        )
        self._send_json(HTTPStatus.OK, self._snapshot_payload(snapshot))

    def _handle_observation(self, tenant: str, incident_id: str) -> None:
        observation = self._read_model(ObservationUpdate)
        if observation is None:
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
        self._send_json(HTTPStatus.OK, self._snapshot_payload(snapshot))

    def _dispatch(self) -> None:
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
        if route_name == "health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        tenant = self._authenticated_tenant()
        if tenant is None:
            return
        try:
            if route_name == "incidents":
                self._handle_create(tenant)
            elif route_name == "observations":
                assert incident_id is not None
                self._handle_observation(tenant, incident_id)
            else:
                assert incident_id is not None
                self._handle_snapshot(tenant, incident_id)
        except TenantMismatchError:
            self._error(HTTPStatus.FORBIDDEN, "forbidden")
        except IncidentNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, "incident not found")
        except (IdempotencyConflictError, IncidentInvariantError):
            self._error(HTTPStatus.CONFLICT, "conflict")
        except Exception:
            _LOGGER.error(
                "guardian HTTP request failed safely",
                extra={"http_method": self.command, "http_status": 500},
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
    return GuardianHTTPServer(
        (host, port),
        token_tenants=_validated_token_tenants(token_tenants),
        service=service or GuardianService(),
        max_request_body=max_request_body,
    )
