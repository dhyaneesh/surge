"""Typed Guardian ingestion and observation boundary for scenario execution."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol
from urllib import error, request

from pydantic import Field

from testbeds.scenarios.models import StrictModel


class GuardianUnavailableError(ConnectionError):
    """The configured real Guardian endpoint could not be reached."""


class GuardianSnapshot(StrictModel):
    incident_class: str | None = None
    actionable: bool
    telemetry_quality: str | None = None
    supporting_evidence: tuple[dict[str, Any], ...] = ()
    contradicting_evidence: tuple[dict[str, Any], ...] = ()
    required_fresh_evidence: tuple[dict[str, Any], ...] = ()
    eligible_actions: tuple[dict[str, Any], ...] = ()
    forbidden_actions: tuple[dict[str, Any], ...] = ()
    proposed_action: dict[str, Any] | None = None
    policy_decision: str
    policy_fail_closed: bool
    policy_bundle_state: str | None = None
    permitted_operations: tuple[str, ...] = ()
    forbidden_operations: tuple[str, ...] = ()
    workflow_states: tuple[str, ...]
    terminal_reason: str | None = None
    parent_count: int = Field(ge=0)
    proposal_count: int = Field(ge=0)
    approval_count: int = Field(ge=0)
    mutation_count: int = Field(ge=0)
    executed_mutations: tuple[dict[str, Any], ...] = Field(
        default=(), alias="mutations"
    )
    audit_event_counts: dict[str, int] = Field(default_factory=dict)
    tenant_isolation: dict[str, bool] | None = None
    safety_gates: tuple[str, ...] = ()
    scaler_result: str | None = None
    scaler_fabricated_zero: bool = False
    scaler_scale_down_permitted: bool = False
    recovery_state: str | None = None


class GuardianSubmission(StrictModel):
    incident_id: str
    response_metadata: dict[str, Any] = Field(default_factory=dict)


class GuardianClient(Protocol):
    async def submit_incident(
        self, payload: dict[str, Any], *, idempotency_key: str
    ) -> GuardianSubmission: ...

    async def observe(self, incident_id: str) -> GuardianSnapshot: ...


class ScriptedGuardianClient:
    """Explicit test client; it is never registered as a production service."""

    def __init__(
        self,
        snapshot: GuardianSnapshot,
        *,
        response_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.response_metadata = response_metadata or {}
        self.submissions: list[tuple[dict[str, Any], str]] = []

    async def submit_incident(
        self, payload: dict[str, Any], *, idempotency_key: str
    ) -> GuardianSubmission:
        self.submissions.append((payload, idempotency_key))
        return GuardianSubmission(
            incident_id=f"test-{idempotency_key}",
            response_metadata=self.response_metadata,
        )

    async def observe(self, incident_id: str) -> GuardianSnapshot:
        return self.snapshot


class HttpGuardianClient:
    """Real HTTP client that fails explicitly when Guardian is unavailable."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def submit_incident(
        self, payload: dict[str, Any], *, idempotency_key: str
    ) -> GuardianSubmission:
        response = await self._json_request(
            "/v1/incidents",
            method="POST",
            payload=payload,
            headers={"Idempotency-Key": idempotency_key},
        )
        return GuardianSubmission.model_validate(response)

    async def observe(self, incident_id: str) -> GuardianSnapshot:
        response = await self._json_request(
            f"/v1/incidents/{incident_id}/scenario-snapshot", method="GET"
        )
        return GuardianSnapshot.model_validate(response)

    async def _json_request(
        self,
        path: str,
        *,
        method: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        def invoke() -> dict[str, Any]:
            body = json.dumps(payload).encode() if payload is not None else None
            http_request = request.Request(
                self.base_url + path,
                data=body,
                method=method,
                headers={"Content-Type": "application/json", **(headers or {})},
            )
            try:
                with request.urlopen(
                    http_request, timeout=self.timeout_seconds
                ) as response:
                    decoded = json.loads(response.read().decode("utf-8"))
            except (error.URLError, TimeoutError, OSError) as exc:
                raise GuardianUnavailableError(
                    f"Guardian endpoint unavailable at {self.base_url}: {exc}"
                ) from exc
            if not isinstance(decoded, dict):
                raise GuardianUnavailableError(
                    "Guardian returned a non-object response"
                )
            return decoded

        return await asyncio.to_thread(invoke)
