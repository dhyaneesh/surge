"""Tenant-scoped, process-local storage for the minimal Guardian runtime.

This store is intentionally neither durable nor a transactional-outbox
implementation. It provides one in-process atomic boundary for the local slice.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, model_validator

from apps.guardian_api.domain import evaluate_incident
from apps.guardian_api.models import (
    ActionType,
    CriticalIntegrityFailure,
    GuardianProjection,
    IncidentClass,
    IncidentFacts,
    IncidentSubmission,
    ObservationUpdate,
    PolicyDecision,
    ScopedIdentifier,
    StrictModel,
    WorkflowState,
)


class IdempotencyConflictError(RuntimeError):
    """The same tenant-scoped key was reused for different canonical facts."""


class IncidentInvariantError(RuntimeError):
    """An incident mutation violated its immutable identity or history."""


class ProjectionInvariantError(IncidentInvariantError):
    """A trusted projector returned an invalid replay history."""


class ObservationOrderError(IncidentInvariantError):
    """An observation arrived before the latest persisted observation."""


class ReentrantStoreWriteError(IncidentInvariantError):
    """A projector attempted a nested write in the active transaction."""


class IncidentNotFoundError(LookupError):
    """An incident is absent from the authenticated tenant's namespace."""

    def __init__(self) -> None:
        super().__init__("incident not found")


class ObservationConflict(StrictModel):
    """Immutable marker for divergent payloads sharing one logical window."""

    state: Literal["conflicting"] = "conflicting"
    observation_id: ScopedIdentifier
    sequence: int
    window_key: ScopedIdentifier
    payload_hashes: tuple[str, ...]


class IncidentSnapshot(StrictModel):
    """Immutable application state returned without exposing store-owned objects."""

    tenant_id: ScopedIdentifier
    incident_id: ScopedIdentifier
    workflow_id: str
    facts: IncidentFacts
    observations: tuple[ObservationUpdate, ...] = ()
    observation_conflicts: tuple[ObservationConflict, ...] = ()
    evaluation_times: tuple[AwareDatetime, ...]
    projection_history: tuple[GuardianProjection, ...]
    projection: GuardianProjection

    @model_validator(mode="after")
    def histories_are_aligned(self) -> IncidentSnapshot:
        expected_evaluations = len(self.observations) + 1
        if len(self.evaluation_times) != expected_evaluations:
            raise ValueError(
                "evaluation history must align with facts and observations"
            )
        # Projection history may include an assessment preamble for the first
        # evaluation, so it is allowed to be longer than evaluation_times.
        if len(self.projection_history) < expected_evaluations:
            raise ValueError(
                "projection history must cover facts and every observation"
            )
        if self.projection != self.projection_history[-1]:
            raise ValueError("final projection must be the latest history entry")
        return self


class StoreSnapshot(StrictModel):
    """Stable diagnostic view of local store contents."""

    incidents: tuple[IncidentSnapshot, ...]
    idempotency_count: int


@dataclass(frozen=True)
class _IdempotencyRecord:
    facts_hash: str
    incident_key: tuple[str, str]


ProjectionReplayer = Callable[
    [
        IncidentFacts,
        tuple[ObservationUpdate, ...],
        tuple[datetime, ...],
        tuple[ObservationConflict, ...],
    ],
    tuple[GuardianProjection, ...],
]


def _validated_operation_time(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("operation time must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("operation time must be timezone-aware")
    return value


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, float) and value == 0.0:
        return 0.0
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical JSON mapping keys must be strings")
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Return deterministic JSON for validated Guardian values."""

    return json.dumps(
        _canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_incident_facts(facts: IncidentFacts) -> str:
    """Serialize strict facts to sorted, whitespace-free canonical JSON."""

    if not isinstance(facts, IncidentFacts):
        raise TypeError("facts must be validated IncidentFacts")
    return canonical_json(facts)


def canonical_incident_submission(submission: IncidentSubmission) -> str:
    """Serialize caller-supplied facts without a caller-controlled incident ID."""

    if not isinstance(submission, IncidentSubmission):
        raise TypeError("submission must be validated IncidentSubmission")
    return canonical_json(submission)


def incident_facts_hash(facts: IncidentFacts) -> str:
    """Hash the canonical IncidentFacts representation."""

    canonical = canonical_incident_facts(facts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def incident_submission_hash(submission: IncidentSubmission) -> str:
    """Hash canonical submitted facts before server identity assignment."""

    canonical = canonical_incident_submission(submission)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_incident_snapshot(snapshot: IncidentSnapshot) -> str:
    """Serialize the complete persisted incident audit record."""

    if not isinstance(snapshot, IncidentSnapshot):
        raise TypeError("snapshot must be a validated IncidentSnapshot")
    return canonical_json(snapshot)


def _observation_identity(observation: ObservationUpdate) -> tuple[str, str, int]:
    return (
        observation.observation_id,
        observation.window_key,
        observation.sequence,
    )


def _observation_conflicts(
    observations: tuple[ObservationUpdate, ...],
) -> tuple[ObservationConflict, ...]:
    payloads_by_identity: dict[tuple[str, str, int], set[str]] = {}
    for observation in observations:
        payloads_by_identity.setdefault(_observation_identity(observation), set()).add(
            canonical_json(observation)
        )
    conflicts = []
    for (observation_id, window_key, sequence), payloads in sorted(
        payloads_by_identity.items()
    ):
        if len(payloads) < 2:
            continue
        conflicts.append(
            ObservationConflict(
                observation_id=observation_id,
                sequence=sequence,
                window_key=window_key,
                payload_hashes=tuple(
                    hashlib.sha256(payload.encode("utf-8")).hexdigest()
                    for payload in sorted(payloads)
                ),
            )
        )
    return tuple(conflicts)


def _fail_closed_conflict_projection(
    projection: GuardianProjection,
) -> GuardianProjection:
    failures = tuple(
        dict.fromkeys(
            (
                *projection.integrity_failures,
                CriticalIntegrityFailure.COMPARISON_INVALID,
            )
        )
    )
    forbidden = tuple(
        dict.fromkeys(
            (
                *projection.forbidden_actions,
                ActionType.SCALE_UP,
                ActionType.SCALE_DOWN,
                ActionType.ROLLBACK,
                ActionType.PROTECT_DEPENDENCY,
            )
        )
    )
    return projection.model_copy(
        update={
            "hypotheses": tuple(
                hypothesis.model_copy(update={"eligible": False})
                for hypothesis in projection.hypotheses
            ),
            "incident_class": IncidentClass.TELEMETRY_FAILURE,
            "telemetry_healthy": False,
            "integrity_failures": failures,
            "eligible_actions": (),
            "permitted_actions": (),
            "forbidden_actions": forbidden,
            "proposed_action": None,
            "requested_evidence_groups": (),
            "workflow_state": WorkflowState.TELEMETRY_FAILURE,
            "policy_decision": PolicyDecision.DENIED,
            "terminal_reason": "observation-conflict",
            "recovery_verified": False,
        }
    )


def _with_assessment_preamble(
    projection: GuardianProjection,
) -> tuple[GuardianProjection, ...]:
    """Record assessment as the mandatory pre-classification phase.

    The assessment entry is the same evaluation with workflow_state=ASSESSMENT;
    scores and actions are unchanged.
    """

    if projection.workflow_state in {
        WorkflowState.CLASSIFIED,
        WorkflowState.CONFLICT_RESOLUTION,
        WorkflowState.UNKNOWN,
        WorkflowState.TELEMETRY_FAILURE,
        WorkflowState.CLOSED,
    }:
        assessment = projection.model_copy(
            update={
                "workflow_state": WorkflowState.ASSESSMENT,
                "recovery_verified": False,
            }
        )
        return (assessment, projection)
    return (projection,)


def replay_incident_history(
    facts: IncidentFacts,
    observations: tuple[ObservationUpdate, ...],
    evaluation_times: tuple[datetime, ...],
    conflicts: tuple[ObservationConflict, ...] = (),
) -> tuple[GuardianProjection, ...]:
    """Purely recompute the initial and observation-aligned projection history."""

    if len(evaluation_times) != len(observations) + 1:
        raise ProjectionInvariantError("evaluation times do not align with history")
    if any(
        current.observed_at < previous.observed_at
        for previous, current in zip(observations, observations[1:])
    ):
        raise ObservationOrderError("replay history must be chronological")
    if observations and observations[0].observed_at < facts.observed_at:
        raise ObservationOrderError("observation cannot predate incident facts")
    first = evaluate_incident(facts, now=evaluation_times[0])
    projections = list(_with_assessment_preamble(first))
    for index, (observation, evaluated_at) in enumerate(
        zip(observations, evaluation_times[1:], strict=True),
        start=1,
    ):
        projection = evaluate_incident(
            facts,
            now=evaluated_at,
            observation=observation,
        )
        if _observation_conflicts(observations[:index]):
            projection = _fail_closed_conflict_projection(projection)
        projections.append(projection)
    if conflicts:
        projections[-1] = _fail_closed_conflict_projection(projections[-1])
    return tuple(projections)


def _copy_snapshot(snapshot: IncidentSnapshot) -> IncidentSnapshot:
    return IncidentSnapshot.model_validate_json(snapshot.model_dump_json())


class InMemoryIncidentStore:
    """One-lock local store with only store-owned incident mutations."""

    def __init__(
        self,
        *,
        projector: ProjectionReplayer = replay_incident_history,
        incident_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._lock = RLock()
        self._idempotency: dict[tuple[str, str], _IdempotencyRecord] = {}
        self._incidents: dict[tuple[str, str], IncidentSnapshot] = {}
        self._projector = projector
        self._incident_id_factory = incident_id_factory or (
            lambda: f"inc-{uuid4().hex}"
        )
        self._transaction_depth = 0
        self._reentry_attempted = False

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        with self._lock:
            if self._transaction_depth:
                self._reentry_attempted = True
                raise ReentrantStoreWriteError("nested store writes are prohibited")
            self._transaction_depth = 1
            self._reentry_attempted = False
            try:
                yield
                if self._reentry_attempted:
                    raise ReentrantStoreWriteError("projector attempted a nested write")
            finally:
                self._transaction_depth = 0
                self._reentry_attempted = False

    def _project(
        self,
        facts: IncidentFacts,
        observations: tuple[ObservationUpdate, ...],
        evaluation_times: tuple[datetime, ...],
        conflicts: tuple[ObservationConflict, ...],
    ) -> tuple[GuardianProjection, ...]:
        history = self._projector(facts, observations, evaluation_times, conflicts)
        if self._reentry_attempted:
            raise ReentrantStoreWriteError("projector attempted a nested write")
        # History may include an assessment preamble before the first evaluation
        # result, so length is at least observations+1 (not necessarily equal).
        if (
            not isinstance(history, tuple)
            or len(history) < len(observations) + 1
            or any(not isinstance(item, GuardianProjection) for item in history)
        ):
            raise ProjectionInvariantError("projector returned an invalid history")
        return history

    def restore_correlated_idempotent(
        self,
        *,
        authenticated_tenant: str,
        idempotency_key: str,
        facts: IncidentFacts,
        now: datetime,
    ) -> IncidentSnapshot:
        """Atomically restore trusted facts that already carry correlation."""

        now = _validated_operation_time(now)
        if facts.tenant_id != authenticated_tenant:
            raise IncidentInvariantError("facts tenant must match authenticated tenant")
        idempotency_identity = (authenticated_tenant, idempotency_key)
        incident_identity = (authenticated_tenant, facts.incident_id)
        facts_hash = incident_facts_hash(facts)
        with self._write_transaction():
            claimed = self._idempotency.get(idempotency_identity)
            if claimed is not None:
                if claimed.facts_hash != facts_hash:
                    raise IdempotencyConflictError(
                        "idempotency key already belongs to different incident facts"
                    )
                return _copy_snapshot(self._incidents[claimed.incident_key])

            existing = self._incidents.get(incident_identity)
            if existing is not None:
                if incident_facts_hash(existing.facts) != facts_hash:
                    raise IdempotencyConflictError(
                        "incident identity already belongs to different facts"
                    )
                stored = existing
            else:
                evaluation_times = (now,)
                projection_history = self._project(facts, (), evaluation_times, ())
                stored = IncidentSnapshot(
                    tenant_id=authenticated_tenant,
                    incident_id=facts.incident_id,
                    workflow_id=(
                        f"guardian/{authenticated_tenant}/incident/{facts.incident_id}"
                    ),
                    facts=facts,
                    evaluation_times=evaluation_times,
                    projection_history=projection_history,
                    projection=projection_history[-1],
                )
                stored = _copy_snapshot(stored)
                self._incidents[incident_identity] = stored

            self._idempotency[idempotency_identity] = _IdempotencyRecord(
                facts_hash=facts_hash,
                incident_key=incident_identity,
            )
            return _copy_snapshot(stored)

    def create_new_idempotent(
        self,
        *,
        authenticated_tenant: str,
        idempotency_key: str,
        submission: IncidentSubmission,
        now: datetime,
    ) -> IncidentSnapshot:
        """Atomically assign one opaque incident ID after claiming a new key."""

        now = _validated_operation_time(now)
        if submission.tenant_id != authenticated_tenant:
            raise IncidentInvariantError(
                "submission tenant must match authenticated tenant"
            )
        idempotency_identity = (authenticated_tenant, idempotency_key)
        payload_hash = incident_submission_hash(submission)
        with self._write_transaction():
            claimed = self._idempotency.get(idempotency_identity)
            if claimed is not None:
                if claimed.facts_hash != payload_hash:
                    raise IdempotencyConflictError(
                        "idempotency key already belongs to different submitted facts"
                    )
                return _copy_snapshot(self._incidents[claimed.incident_key])

            incident_id = ""
            facts: IncidentFacts | None = None
            for _attempt in range(16):
                candidate = self._incident_id_factory()
                try:
                    facts = IncidentFacts.model_validate(
                        {
                            **submission.model_dump(mode="python"),
                            "incident_id": candidate,
                        }
                    )
                except (TypeError, ValueError) as error:
                    raise IncidentInvariantError(
                        "incident ID generator returned an invalid identifier"
                    ) from error
                incident_identity = (authenticated_tenant, facts.incident_id)
                if incident_identity not in self._incidents:
                    incident_id = facts.incident_id
                    break
            if not incident_id or facts is None:
                raise IncidentInvariantError(
                    "incident ID generator did not produce a unique identifier"
                )

            evaluation_times = (now,)
            projection_history = self._project(facts, (), evaluation_times, ())
            stored = IncidentSnapshot(
                tenant_id=authenticated_tenant,
                incident_id=incident_id,
                workflow_id=f"guardian/{authenticated_tenant}/incident/{incident_id}",
                facts=facts,
                evaluation_times=evaluation_times,
                projection_history=projection_history,
                projection=projection_history[-1],
            )
            stored = _copy_snapshot(stored)
            self._incidents[(authenticated_tenant, incident_id)] = stored
            self._idempotency[idempotency_identity] = _IdempotencyRecord(
                facts_hash=payload_hash,
                incident_key=(authenticated_tenant, incident_id),
            )
            return _copy_snapshot(stored)

    def append_observation(
        self,
        authenticated_tenant: str,
        incident_id: str,
        observation: ObservationUpdate,
        *,
        now: datetime,
    ) -> IncidentSnapshot:
        """Append one ordered observation and atomically replay complete history."""

        now = _validated_operation_time(now)
        identity = (authenticated_tenant, incident_id)
        with self._write_transaction():
            current = self._incidents.get(identity)
            if current is None:
                raise IncidentNotFoundError
            if (
                observation.tenant_id != authenticated_tenant
                or observation.incident_id != incident_id
            ):
                raise IncidentInvariantError(
                    "observation identity must match its incident"
                )
            if observation in current.observations:
                return _copy_snapshot(current)
            if observation.observed_at < current.facts.observed_at:
                raise ObservationOrderError("observation cannot predate incident facts")
            if (
                current.observations
                and observation.observed_at < current.observations[-1].observed_at
            ):
                raise ObservationOrderError(
                    "observations must be appended in chronological order"
                )
            observations = (*current.observations, observation)
            conflicts = _observation_conflicts(observations)
            evaluation_times = (
                *current.evaluation_times,
                now,
            )
            replayed_history = self._project(
                current.facts, observations, evaluation_times, conflicts
            )
            if replayed_history[: len(current.projection_history)] != (
                current.projection_history
            ):
                raise ProjectionInvariantError(
                    "projector rewrote persisted projection history"
                )
            projection_history = (
                *current.projection_history,
                replayed_history[-1],
            )
            replacement = IncidentSnapshot(
                tenant_id=current.tenant_id,
                incident_id=current.incident_id,
                workflow_id=current.workflow_id,
                facts=current.facts,
                observations=observations,
                observation_conflicts=conflicts,
                evaluation_times=evaluation_times,
                projection_history=projection_history,
                projection=projection_history[-1],
            )
            stored = _copy_snapshot(replacement)
            self._incidents[identity] = stored
            return _copy_snapshot(stored)

    def get(self, authenticated_tenant: str, incident_id: str) -> IncidentSnapshot:
        """Load only from the authenticated tenant's incident namespace."""

        with self._lock:
            snapshot = self._incidents.get((authenticated_tenant, incident_id))
            if snapshot is None:
                raise IncidentNotFoundError
            return _copy_snapshot(snapshot)

    def snapshot(self) -> StoreSnapshot:
        """Return a sorted, detached copy of all local incident projections."""

        with self._lock:
            incidents = tuple(
                _copy_snapshot(self._incidents[key]) for key in sorted(self._incidents)
            )
            return StoreSnapshot(
                incidents=incidents,
                idempotency_count=len(self._idempotency),
            )


InMemoryGuardianStore = InMemoryIncidentStore
IdempotencyConflict = IdempotencyConflictError
IncidentNotFound = IncidentNotFoundError
