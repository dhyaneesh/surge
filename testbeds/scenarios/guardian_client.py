"""Typed Guardian ingestion and observation boundary for scenario execution."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Protocol
from urllib import error, request

from pydantic import Field, ValidationError

from apps.guardian_api.models import (
    ActionType,
    CriticalIntegrityFailure,
    GuardianProjection,
    IncidentSubmission,
    ObservationUpdate,
)
from testbeds.scenarios.models import StrictModel


class GuardianUnavailableError(ConnectionError):
    """The configured real Guardian endpoint could not be reached."""


class GuardianResponseError(RuntimeError):
    """The Guardian endpoint returned a response that did not match the schema."""


_TOKEN_ENV = "GUARDIAN_SCENARIO_TOKEN"
_ENVELOPE_KEYS = ("incident_id", "workflow_id", "projection")


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
    workflow_id: str = ""
    projection: GuardianProjection | None = None
    response_metadata: dict[str, Any] = Field(default_factory=dict)


_SCALE_DIRECTIONS = {
    ActionType.SCALE_UP: "up",
    ActionType.SCALE_DOWN: "down",
}


def _action_to_dict(action: ActionType) -> dict[str, Any]:
    if action in _SCALE_DIRECTIONS:
        return {"actionType": "scale", "scaleDirection": _SCALE_DIRECTIONS[action]}
    return {"actionType": action.value.replace("-", "_")}


def _validate_projection(data: Any) -> GuardianProjection:
    try:
        return GuardianProjection.model_validate(data, strict=False)
    except ValidationError as exc:
        raise GuardianResponseError(
            "Guardian response envelope projection invalid"
        ) from exc


def _projection_to_snapshot(envelope: dict[str, Any]) -> GuardianSnapshot:
    projection = _validate_projection(envelope["projection"])
    integrity = projection.integrity_failures
    if projection.telemetry_healthy:
        telemetry_quality = "healthy"
    elif CriticalIntegrityFailure.SAMPLE_STALE in integrity:
        telemetry_quality = "stale"
    else:
        telemetry_quality = "failed"
    actionable = (
        bool(projection.eligible_actions) or projection.proposed_action is not None
    )
    supporting = envelope.get("supporting_evidence") or ()
    contradicting = envelope.get("contradicting_evidence") or ()
    required_fresh = envelope.get("required_fresh_evidence") or ()
    audit_counts = envelope.get("audit_event_counts") or {}
    safety_gates = envelope.get("safety_gates") or ()
    workflow_states = envelope.get("workflow_states") or (
        projection.workflow_state.value,
    )
    parent_count = int(envelope.get("parent_count", 1))
    proposal_count = int(
        envelope.get(
            "proposal_count",
            1 if projection.proposed_action is not None else 0,
        )
    )
    approval_count = int(envelope.get("approval_count", 0))
    permitted = envelope.get("permitted_operations") or tuple(
        item.value for item in projection.permitted_actions
    )
    forbidden_ops = envelope.get("forbidden_operations") or tuple(
        item.value for item in projection.forbidden_actions
    )
    mutations = envelope.get("mutations") or ()
    return GuardianSnapshot(
        incident_class=projection.incident_class.value
        if projection.incident_class
        else None,
        actionable=actionable,
        telemetry_quality=telemetry_quality,
        supporting_evidence=tuple(supporting),
        contradicting_evidence=tuple(contradicting),
        required_fresh_evidence=tuple(required_fresh),
        eligible_actions=tuple(
            _action_to_dict(item) for item in projection.eligible_actions
        ),
        forbidden_actions=tuple(
            _action_to_dict(item) for item in projection.forbidden_actions
        ),
        proposed_action=(
            _action_to_dict(projection.proposed_action)
            if projection.proposed_action
            else None
        ),
        policy_decision=projection.policy_decision.value,
        policy_fail_closed=True,
        policy_bundle_state=envelope.get("policy_bundle_state"),
        permitted_operations=tuple(permitted),
        forbidden_operations=tuple(forbidden_ops),
        workflow_states=tuple(workflow_states),
        terminal_reason=projection.terminal_reason,
        parent_count=parent_count,
        proposal_count=proposal_count,
        approval_count=approval_count,
        mutation_count=projection.executed_mutations,
        mutations=tuple(mutations),
        audit_event_counts=dict(audit_counts),
        tenant_isolation=envelope.get("tenant_isolation"),
        safety_gates=tuple(safety_gates),
        scaler_result=projection.scaler_result.value
        if projection.scaler_result
        else None,
        scaler_scale_down_permitted=False,
        recovery_state="healthy" if projection.recovery_verified else None,
    )


class GuardianClient(Protocol):
    async def submit_incident(
        self,
        submission: IncidentSubmission,
        *,
        idempotency_key: str,
    ) -> GuardianSubmission: ...

    async def submit_observation(
        self,
        observation: ObservationUpdate,
        *,
        idempotency_key: str,
    ) -> GuardianSubmission: ...

    async def observe(self, incident_id: str) -> GuardianSnapshot: ...


class ScriptedGuardianClient:
    """Explicit test client; it is never registered as a production service."""

    def __init__(
        self,
        snapshot: GuardianSnapshot,
        *,
        response_metadata: dict[str, Any] | None = None,
        projection: GuardianProjection | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.response_metadata = response_metadata or {}
        self.projection = projection
        self.submissions: list[tuple[IncidentSubmission, str]] = []
        self.observations: list[tuple[ObservationUpdate, str]] = []

    async def submit_incident(
        self,
        submission: IncidentSubmission,
        *,
        idempotency_key: str,
    ) -> GuardianSubmission:
        self.submissions.append((submission, idempotency_key))
        return GuardianSubmission(
            incident_id=f"test-{idempotency_key}",
            workflow_id=f"guardian/test-tenant/incident/test-{idempotency_key}",
            projection=self.projection,
            response_metadata=self.response_metadata,
        )

    async def submit_observation(
        self,
        observation: ObservationUpdate,
        *,
        idempotency_key: str,
    ) -> GuardianSubmission:
        self.observations.append((observation, idempotency_key))
        return GuardianSubmission(
            incident_id=observation.incident_id,
            workflow_id=f"guardian/test-tenant/incident/{observation.incident_id}",
            projection=self.projection,
            response_metadata=self.response_metadata,
        )

    async def observe(self, incident_id: str) -> GuardianSnapshot:
        return self.snapshot


class HttpGuardianClient:
    """Authenticated HTTP client that fails explicitly when Guardian is unavailable.

    The bearer token is read from ``GUARDIAN_SCENARIO_TOKEN`` (or supplied
    directly), carried only in the in-memory request ``Authorization`` header,
    and never persisted into artifacts, submissions, or logs.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        resolved = token if token is not None else os.environ.get(_TOKEN_ENV)
        if not isinstance(resolved, str) or not resolved.strip():
            raise GuardianUnavailableError(
                "Guardian scenario token is required (set GUARDIAN_SCENARIO_TOKEN)"
            )
        self._token = resolved.strip()

    @property
    def bearer_token(self) -> str:
        return self._token

    async def submit_incident(
        self,
        submission: IncidentSubmission,
        *,
        idempotency_key: str,
    ) -> GuardianSubmission:
        response = await self._json_request(
            "/v1/incidents",
            method="POST",
            payload=submission.model_dump(mode="json"),
            headers={"Idempotency-Key": idempotency_key},
        )
        return self._envelope_submission(response)

    async def submit_observation(
        self,
        observation: ObservationUpdate,
        *,
        idempotency_key: str,
    ) -> GuardianSubmission:
        response = await self._json_request(
            f"/v1/incidents/{observation.incident_id}/observations",
            method="POST",
            payload=observation.model_dump(mode="json"),
            headers={"Idempotency-Key": idempotency_key},
        )
        return self._envelope_submission(response)

    async def observe(self, incident_id: str) -> GuardianSnapshot:
        response = await self._json_request(
            f"/v1/incidents/{incident_id}/scenario-snapshot", method="GET"
        )
        return _projection_to_snapshot(response)

    def _envelope_submission(self, response: dict[str, Any]) -> GuardianSubmission:
        incident_id, workflow_id, projection = self._parse_envelope(response)
        return GuardianSubmission(
            incident_id=incident_id,
            workflow_id=workflow_id,
            projection=projection,
            response_metadata={},
        )

    @staticmethod
    def _parse_envelope(response: Any) -> tuple[str, str, GuardianProjection]:
        if not isinstance(response, dict):
            raise GuardianResponseError("Guardian response envelope is not an object")
        missing = [key for key in _ENVELOPE_KEYS if key not in response]
        if missing:
            raise GuardianResponseError(
                "Guardian response envelope missing keys: " + ", ".join(missing)
            )
        projection = _validate_projection(response["projection"])
        return response["incident_id"], response["workflow_id"], projection

    async def _json_request(
        self,
        path: str,
        *,
        method: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        token = self._token

        def invoke() -> dict[str, Any]:
            body = json.dumps(payload).encode() if payload is not None else None
            http_request = request.Request(
                self.base_url + path,
                data=body,
                method=method,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                    **(headers or {}),
                },
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
                raise GuardianResponseError("Guardian returned a non-object response")
            return decoded

        return await asyncio.to_thread(invoke)
