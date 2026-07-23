"""Transport-independent operations for the minimal Guardian runtime."""

from __future__ import annotations

from datetime import datetime
from re import fullmatch

from apps.guardian_api.models import (
    SCOPED_IDENTIFIER_PATTERN,
    IncidentFacts,
    ObservationUpdate,
)
from apps.guardian_api.store import (
    IdempotencyConflictError,
    InMemoryIncidentStore,
    IncidentNotFoundError,
    IncidentSnapshot,
)


class TenantMismatchError(PermissionError):
    """Authenticated and submitted tenant identities do not agree."""


def _require_scoped_identifier(value: str, *, name: str) -> None:
    if (
        not isinstance(value, str)
        or fullmatch(SCOPED_IDENTIFIER_PATTERN, value) is None
    ):
        raise ValueError(f"{name} must be a delimiter-safe scoped identifier")


def _require_nonempty(value: str, *, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


class GuardianService:
    """Tenant-scoped incident lifecycle without infrastructure side effects."""

    def __init__(
        self,
        store: InMemoryIncidentStore | None = None,
    ) -> None:
        self._store = store or InMemoryIncidentStore()

    def submit_incident(
        self,
        authenticated_tenant: str,
        idempotency_key: str,
        facts: IncidentFacts,
        *,
        now: datetime,
    ) -> IncidentSnapshot:
        """Create one incident projection for a tenant-scoped idempotency key."""

        _require_scoped_identifier(authenticated_tenant, name="authenticated tenant")
        _require_nonempty(idempotency_key, name="idempotency key")
        if not isinstance(facts, IncidentFacts):
            raise TypeError("facts must be validated IncidentFacts")
        if facts.tenant_id != authenticated_tenant:
            raise TenantMismatchError("authenticated tenant does not match incident")

        return self._store.create_idempotent(
            authenticated_tenant=authenticated_tenant,
            idempotency_key=idempotency_key,
            facts=facts,
            now=now,
        )

    def get_incident(
        self, authenticated_tenant: str, incident_id: str
    ) -> IncidentSnapshot:
        """Return an opaque not-found result outside the authenticated tenant."""

        _require_scoped_identifier(authenticated_tenant, name="authenticated tenant")
        _require_scoped_identifier(incident_id, name="incident ID")
        return self._store.get(authenticated_tenant, incident_id)

    def append_observation(
        self,
        authenticated_tenant: str,
        incident_id: str,
        observation: ObservationUpdate,
        *,
        now: datetime,
    ) -> IncidentSnapshot:
        """Append an observation and deterministically replay the complete history."""

        _require_scoped_identifier(authenticated_tenant, name="authenticated tenant")
        _require_scoped_identifier(incident_id, name="incident ID")
        if not isinstance(observation, ObservationUpdate):
            raise TypeError("observation must be a validated ObservationUpdate")

        current = self._store.get(authenticated_tenant, incident_id)
        if observation.tenant_id != current.tenant_id:
            raise TenantMismatchError(
                "observation tenant does not match authenticated incident"
            )
        if observation.incident_id != current.incident_id:
            raise IncidentNotFoundError
        return self._store.append_observation(
            authenticated_tenant,
            incident_id,
            observation,
            now=now,
        )

    def create_incident(
        self,
        authenticated_tenant: str,
        idempotency_key: str,
        facts: IncidentFacts,
        *,
        now: datetime,
    ) -> IncidentSnapshot:
        """Compatibility name for submit_incident."""

        return self.submit_incident(
            authenticated_tenant, idempotency_key, facts, now=now
        )

    def update_observation(
        self,
        authenticated_tenant: str,
        incident_id: str,
        observation: ObservationUpdate,
        *,
        now: datetime,
    ) -> IncidentSnapshot:
        """Compatibility name for append_observation."""

        return self.append_observation(
            authenticated_tenant, incident_id, observation, now=now
        )


IdempotencyConflict = IdempotencyConflictError
IncidentNotFound = IncidentNotFoundError
