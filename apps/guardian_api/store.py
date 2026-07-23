"""Tenant-scoped, process-local storage for the minimal Guardian runtime.

This store is intentionally neither durable nor a transactional-outbox
implementation. It provides one in-process atomic boundary for the local slice.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from threading import RLock
from typing import Any, NoReturn, Self

from pydantic import BaseModel

from apps.guardian_api.models import (
    GuardianProjection,
    IncidentFacts,
    NonEmptyString,
    ObservationUpdate,
    StrictModel,
)


class IdempotencyConflictError(RuntimeError):
    """The same tenant-scoped key was reused for different canonical facts."""


class IncidentInvariantError(RuntimeError):
    """A store update attempted to rewrite immutable incident history."""


class IncidentNotFoundError(LookupError):
    """An incident is absent from the authenticated tenant's namespace."""

    def __init__(self) -> None:
        super().__init__("incident not found")


class IncidentSnapshot(StrictModel):
    """Immutable application state returned without exposing store-owned objects."""

    tenant_id: NonEmptyString
    incident_id: NonEmptyString
    workflow_id: NonEmptyString
    facts: IncidentFacts
    observations: tuple[ObservationUpdate, ...] = ()
    projection: GuardianProjection


class StoreSnapshot(StrictModel):
    """Stable diagnostic view of local store contents."""

    incidents: tuple[IncidentSnapshot, ...]
    idempotency_count: int


class _FrozenDict(dict[str, float]):
    """JSON-serializable immutable dictionary for detached projections."""

    @staticmethod
    def _immutable() -> NoReturn:
        raise TypeError("snapshot mappings are immutable")

    def __setitem__(self, key: str, value: float) -> None:
        self._immutable()

    def __delitem__(self, key: str) -> None:
        self._immutable()

    def clear(self) -> None:
        self._immutable()

    def pop(self, *args: Any, **kwargs: Any) -> Any:
        self._immutable()

    def popitem(self) -> tuple[str, float]:
        self._immutable()

    def setdefault(self, *args: Any, **kwargs: Any) -> Any:
        self._immutable()

    def update(self, *args: Any, **kwargs: Any) -> None:
        self._immutable()

    def __ior__(self, value: Any) -> Self:
        self._immutable()


@dataclass(frozen=True)
class _IdempotencyRecord:
    facts_hash: str
    incident_key: tuple[str, str]


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (set, frozenset)):
        normalized = [_canonical_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def canonical_incident_facts(facts: IncidentFacts) -> str:
    """Serialize strict facts to sorted, whitespace-free canonical JSON."""

    if not isinstance(facts, IncidentFacts):
        raise TypeError("facts must be validated IncidentFacts")
    return json.dumps(
        _canonical_value(facts),
        sort_keys=True,
        separators=(",", ":"),
    )


def incident_facts_hash(facts: IncidentFacts) -> str:
    """Hash the canonical IncidentFacts representation."""

    canonical = canonical_incident_facts(facts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _copy_snapshot(snapshot: IncidentSnapshot) -> IncidentSnapshot:
    copied = IncidentSnapshot.model_validate_json(snapshot.model_dump_json())
    for hypothesis in copied.projection.hypotheses:
        object.__setattr__(
            hypothesis,
            "required_group_confidence",
            _FrozenDict(hypothesis.required_group_confidence),
        )
    return copied


def _validate_update_invariants(
    authenticated_tenant: str,
    current: IncidentSnapshot,
    replacement: IncidentSnapshot,
) -> None:
    immutable_identity_changed = (
        current.tenant_id != authenticated_tenant
        or current.facts.tenant_id != authenticated_tenant
        or replacement.tenant_id != authenticated_tenant
        or replacement.incident_id != current.incident_id
        or replacement.workflow_id != current.workflow_id
    )
    if immutable_identity_changed:
        raise IncidentInvariantError("incident identity is immutable")
    if replacement.facts != current.facts:
        raise IncidentInvariantError("initial incident facts are immutable")

    prior_count = len(current.observations)
    if (
        len(replacement.observations) < prior_count
        or replacement.observations[:prior_count] != current.observations
    ):
        raise IncidentInvariantError("observation history is append-only")
    if any(
        item.tenant_id != authenticated_tenant
        or item.incident_id != current.incident_id
        for item in replacement.observations[prior_count:]
    ):
        raise IncidentInvariantError("observation identity must match its incident")


class InMemoryIncidentStore:
    """One-lock local store keyed by authenticated tenant identity."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._idempotency: dict[tuple[str, str], _IdempotencyRecord] = {}
        self._incidents: dict[tuple[str, str], IncidentSnapshot] = {}

    def create_idempotent(
        self,
        *,
        authenticated_tenant: str,
        idempotency_key: str,
        facts_hash: str,
        candidate: IncidentSnapshot,
    ) -> IncidentSnapshot:
        """Atomically claim a key and create or return its incident."""

        idempotency_identity = (authenticated_tenant, idempotency_key)
        incident_identity = (authenticated_tenant, candidate.incident_id)
        with self._lock:
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
                stored = _copy_snapshot(candidate)
                self._incidents[incident_identity] = stored

            self._idempotency[idempotency_identity] = _IdempotencyRecord(
                facts_hash=facts_hash,
                incident_key=incident_identity,
            )
            return _copy_snapshot(stored)

    def get(self, authenticated_tenant: str, incident_id: str) -> IncidentSnapshot:
        """Load only from the authenticated tenant's incident namespace."""

        with self._lock:
            snapshot = self._incidents.get((authenticated_tenant, incident_id))
            if snapshot is None:
                raise IncidentNotFoundError
            return _copy_snapshot(snapshot)

    def update(
        self,
        authenticated_tenant: str,
        incident_id: str,
        update: Callable[[IncidentSnapshot], IncidentSnapshot],
    ) -> IncidentSnapshot:
        """Apply a pure update while holding the same lock as all store state."""

        identity = (authenticated_tenant, incident_id)
        with self._lock:
            current = self._incidents.get(identity)
            if current is None:
                raise IncidentNotFoundError
            replacement = update(_copy_snapshot(current))
            _validate_update_invariants(authenticated_tenant, current, replacement)
            stored = _copy_snapshot(replacement)
            self._incidents[identity] = stored
            return _copy_snapshot(stored)

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
