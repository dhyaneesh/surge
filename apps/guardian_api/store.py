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

from pydantic import AwareDatetime, BaseModel, model_validator

from apps.guardian_api.domain import evaluate_incident
from apps.guardian_api.models import (
    ActionType,
    CriticalIntegrityFailure,
    GuardianProjection,
    IncidentClass,
    IncidentFacts,
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
        expected = len(self.observations) + 1
        if len(self.evaluation_times) != expected:
            raise ValueError(
                "evaluation history must align with facts and observations"
            )
        if len(self.projection_history) != expected:
            raise ValueError(
                "projection history must align with facts and observations"
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


def incident_facts_hash(facts: IncidentFacts) -> str:
    """Hash the canonical IncidentFacts representation."""

    canonical = canonical_incident_facts(facts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_incident_snapshot(snapshot: IncidentSnapshot) -> str:
    """Serialize a complete incident record for deterministic replay comparison."""

    if not isinstance(snapshot, IncidentSnapshot):
        raise TypeError("snapshot must be a validated IncidentSnapshot")
    return canonical_json(snapshot)


def _observation_identity(observation: ObservationUpdate) -> tuple[str, str, int]:
    return (
        observation.observation_id,
        observation.window_key,
        observation.sequence,
    )


def _observation_sort_key(observation: ObservationUpdate) -> tuple[Any, ...]:
    return (
        observation.observed_at,
        observation.sequence,
        observation.window_key,
        observation.observation_id,
        canonical_json(observation),
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
            "incident_class": IncidentClass.TELEMETRY_FAILURE,
            "telemetry_healthy": False,
            "integrity_failures": failures,
            "eligible_actions": (),
            "permitted_actions": (ActionType.INVESTIGATE, ActionType.ALERT),
            "forbidden_actions": forbidden,
            "proposed_action": None,
            "workflow_state": WorkflowState.TELEMETRY_FAILURE,
            "policy_decision": PolicyDecision.DENIED,
            "terminal_reason": "observation-conflict",
            "recovery_verified": False,
        }
    )


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
    projections = [evaluate_incident(facts, now=evaluation_times[0])]
    projections.extend(
        evaluate_incident(facts, now=evaluated_at, observation=observation)
        for observation, evaluated_at in zip(
            observations, evaluation_times[1:], strict=True
        )
    )
    if conflicts:
        projections[-1] = _fail_closed_conflict_projection(projections[-1])
    return tuple(projections)


def _copy_snapshot(snapshot: IncidentSnapshot) -> IncidentSnapshot:
    return IncidentSnapshot.model_validate_json(snapshot.model_dump_json())


class InMemoryIncidentStore:
    """One-lock local store with only store-owned incident mutations."""

    def __init__(
        self, *, projector: ProjectionReplayer = replay_incident_history
    ) -> None:
        self._lock = RLock()
        self._idempotency: dict[tuple[str, str], _IdempotencyRecord] = {}
        self._incidents: dict[tuple[str, str], IncidentSnapshot] = {}
        self._projector = projector
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
        if (
            not isinstance(history, tuple)
            or len(history) != len(observations) + 1
            or any(not isinstance(item, GuardianProjection) for item in history)
        ):
            raise ProjectionInvariantError("projector returned an invalid history")
        return history

    def create_idempotent(
        self,
        *,
        authenticated_tenant: str,
        idempotency_key: str,
        facts: IncidentFacts,
        now: datetime,
    ) -> IncidentSnapshot:
        """Atomically claim a key and create or return its incident."""

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

    def append_observation(
        self,
        authenticated_tenant: str,
        incident_id: str,
        observation: ObservationUpdate,
        *,
        now: datetime,
    ) -> IncidentSnapshot:
        """Append one ordered observation and atomically replay complete history."""

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
            ordered_entries = sorted(
                (
                    *zip(
                        current.observations,
                        current.evaluation_times[1:],
                        strict=True,
                    ),
                    (observation, now),
                ),
                key=lambda entry: _observation_sort_key(entry[0]),
            )
            observations = tuple(item for item, _ in ordered_entries)
            conflicts = _observation_conflicts(observations)
            conflicting_identities = {
                (item.observation_id, item.window_key, item.sequence)
                for item in conflicts
            }
            latest_conflict_evaluation = {
                identity: max(
                    evaluated_at
                    for item, evaluated_at in ordered_entries
                    if _observation_identity(item) == identity
                )
                for identity in conflicting_identities
            }
            evaluation_times = (
                current.evaluation_times[0],
                *(
                    latest_conflict_evaluation.get(
                        _observation_identity(item), evaluated_at
                    )
                    for item, evaluated_at in ordered_entries
                ),
            )
            projection_history = self._project(
                current.facts, observations, evaluation_times, conflicts
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
