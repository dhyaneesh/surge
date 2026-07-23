"""Transport-independent operations for the minimal Guardian runtime."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from apps.guardian_api.domain import evaluate_incident
from apps.guardian_api.models import (
    GuardianProjection,
    IncidentFacts,
    ObservationUpdate,
)
from apps.guardian_api.store import (
    IdempotencyConflictError,
    InMemoryIncidentStore,
    IncidentNotFoundError,
    IncidentSnapshot,
    incident_facts_hash,
)


class TenantMismatchError(PermissionError):
    """Authenticated and submitted tenant identities do not agree."""


Evaluator = Callable[..., GuardianProjection]


def _require_identity(value: str, *, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


class GuardianService:
    """Tenant-scoped incident lifecycle without infrastructure side effects."""

    def __init__(
        self,
        store: InMemoryIncidentStore | None = None,
        *,
        evaluator: Evaluator = evaluate_incident,
    ) -> None:
        self._store = store or InMemoryIncidentStore()
        self._evaluator = evaluator

    def _evaluate_history(
        self,
        facts: IncidentFacts,
        observations: tuple[ObservationUpdate, ...],
        *,
        now: datetime,
    ) -> GuardianProjection:
        if not observations:
            return self._evaluator(facts, now=now)
        projection: GuardianProjection | None = None
        for observation in observations:
            projection = self._evaluator(facts, now=now, observation=observation)
        assert projection is not None
        return projection

    def submit_incident(
        self,
        authenticated_tenant: str,
        idempotency_key: str,
        facts: IncidentFacts,
        *,
        now: datetime,
    ) -> IncidentSnapshot:
        """Create one incident projection for a tenant-scoped idempotency key."""

        _require_identity(authenticated_tenant, name="authenticated tenant")
        _require_identity(idempotency_key, name="idempotency key")
        if not isinstance(facts, IncidentFacts):
            raise TypeError("facts must be validated IncidentFacts")
        if facts.tenant_id != authenticated_tenant:
            raise TenantMismatchError("authenticated tenant does not match incident")

        candidate = IncidentSnapshot(
            tenant_id=authenticated_tenant,
            incident_id=facts.incident_id,
            workflow_id=(
                f"guardian/{authenticated_tenant}/incident/{facts.incident_id}"
            ),
            facts=facts,
            projection=self._evaluate_history(facts, (), now=now),
        )
        return self._store.create_idempotent(
            authenticated_tenant=authenticated_tenant,
            idempotency_key=idempotency_key,
            facts_hash=incident_facts_hash(facts),
            candidate=candidate,
        )

    def get_incident(
        self, authenticated_tenant: str, incident_id: str
    ) -> IncidentSnapshot:
        """Return an opaque not-found result outside the authenticated tenant."""

        _require_identity(authenticated_tenant, name="authenticated tenant")
        _require_identity(incident_id, name="incident ID")
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

        _require_identity(authenticated_tenant, name="authenticated tenant")
        _require_identity(incident_id, name="incident ID")
        if not isinstance(observation, ObservationUpdate):
            raise TypeError("observation must be a validated ObservationUpdate")

        def append(current: IncidentSnapshot) -> IncidentSnapshot:
            if observation.tenant_id != current.tenant_id:
                raise TenantMismatchError(
                    "observation tenant does not match authenticated incident"
                )
            if observation.incident_id != current.incident_id:
                raise IncidentNotFoundError
            history = (*current.observations, observation)
            return current.model_copy(
                update={
                    "observations": history,
                    "projection": self._evaluate_history(
                        current.facts, history, now=now
                    ),
                }
            )

        return self._store.update(authenticated_tenant, incident_id, append)

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
