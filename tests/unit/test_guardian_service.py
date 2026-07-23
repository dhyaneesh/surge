"""Unit contracts for the tenant-scoped local Guardian service."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from threading import Barrier

import pytest
from pydantic import ValidationError

from apps.guardian_api.models import (
    ControlFacts,
    EvidencePass,
    IncidentFacts,
    IncidentSeverity,
    ObservationUpdate,
    PolicyFacts,
    PolicyState,
    RequiredSignal,
    SignalFacts,
    TargetIdentity,
    TelemetryFacts,
)
from apps.guardian_api.service import (
    GuardianService,
    IdempotencyConflictError,
    IncidentNotFoundError,
    TenantMismatchError,
)
from apps.guardian_api.store import (
    InMemoryIncidentStore,
    IncidentInvariantError,
    canonical_incident_facts,
    incident_facts_hash,
)


NOW = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def telemetry(observed_at: datetime = NOW) -> TelemetryFacts:
    return TelemetryFacts(
        quality=1.0,
        newest_required_sample_at=observed_at,
        freshness_seconds=60,
        required_signals=frozenset({RequiredSignal.TELEMETRY_QUALITY}),
        clock_skew_seconds=0,
        required_sample_count=10,
        usable_sample_count=10,
        pipeline_available=True,
        comparison_valid=True,
    )


def facts(
    *,
    tenant_id: str = "tenant-a",
    incident_id: str = "incident-1",
    severity: IncidentSeverity = IncidentSeverity.WARNING,
) -> IncidentFacts:
    return IncidentFacts(
        tenant_id=tenant_id,
        incident_id=incident_id,
        severity=severity,
        observed_at=NOW,
        identity=TargetIdentity(
            target_role="request-processor",
            environment="production",
            namespace="payments",
            workload_kind="Deployment",
            workload_name="processor",
            service_name="processor",
            image_digest=DIGEST,
        ),
        telemetry=telemetry(),
        evidence_pass=EvidencePass(completed_passes=1, started_at=NOW),
        signals=SignalFacts(),
        policy=PolicyFacts(state=PolicyState.FRESH, evaluated_at=NOW),
        control=ControlFacts(),
    )


def observation(
    *,
    observed_at: datetime,
    tenant_id: str = "tenant-a",
    incident_id: str = "incident-1",
    healthy: bool = True,
) -> ObservationUpdate:
    return ObservationUpdate(
        tenant_id=tenant_id,
        incident_id=incident_id,
        observed_at=observed_at,
        window_started_at=observed_at - timedelta(seconds=30),
        telemetry=telemetry(observed_at),
        service_healthy=healthy,
        required_conditions_satisfied=healthy,
        provenance_ref=f"query-contract/{observed_at.isoformat()}",
    )


def test_canonical_facts_use_sorted_stable_json_before_hashing() -> None:
    original = facts()
    reordered_payload = dict(reversed(list(original.model_dump().items())))
    reordered = IncidentFacts.model_validate(reordered_payload)

    canonical = canonical_incident_facts(original)

    assert canonical == canonical_incident_facts(reordered)
    assert canonical == json.dumps(
        json.loads(canonical), sort_keys=True, separators=(",", ":")
    )
    assert incident_facts_hash(original) == incident_facts_hash(reordered)


def test_concurrent_duplicate_submissions_create_one_incident_and_workflow() -> None:
    store = InMemoryIncidentStore()
    service = GuardianService(store)
    request = facts()
    workers = 12
    barrier = Barrier(workers)

    def submit():
        barrier.wait()
        return service.submit_incident(
            authenticated_tenant="tenant-a",
            idempotency_key="request-1",
            facts=request,
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = tuple(executor.map(lambda _: submit(), range(workers)))

    stored = store.snapshot()
    assert len(stored.incidents) == 1
    assert stored.idempotency_count == 1
    assert {item.incident_id for item in results} == {"incident-1"}
    assert {item.workflow_id for item in results} == {
        "guardian/tenant-a/incident/incident-1"
    }
    assert all(item == results[0] for item in results)


def test_same_tenant_and_key_with_different_facts_conflicts() -> None:
    service = GuardianService(InMemoryIncidentStore())
    service.submit_incident("tenant-a", "request-1", facts(), now=NOW)

    with pytest.raises(IdempotencyConflictError):
        service.submit_incident(
            "tenant-a",
            "request-1",
            facts(severity=IncidentSeverity.CRITICAL),
            now=NOW,
        )


def test_same_instant_with_different_offsets_is_the_same_idempotent_request() -> None:
    store = InMemoryIncidentStore()
    service = GuardianService(store)
    original = facts()
    shifted_at = NOW.astimezone(timezone(timedelta(hours=2)))
    shifted_payload = original.model_dump()
    shifted_payload["observed_at"] = shifted_at
    shifted_payload["telemetry"]["newest_required_sample_at"] = shifted_at
    shifted_payload["evidence_pass"]["started_at"] = shifted_at
    shifted_payload["policy"]["evaluated_at"] = shifted_at
    shifted = IncidentFacts.model_validate(shifted_payload)

    first = service.submit_incident("tenant-a", "request-1", original, now=NOW)
    duplicate = service.submit_incident("tenant-a", "request-1", shifted, now=NOW)

    assert incident_facts_hash(original) == incident_facts_hash(shifted)
    assert duplicate == first
    assert len(store.snapshot().incidents) == 1


def test_cross_tenant_lookup_and_update_are_non_disclosing() -> None:
    service = GuardianService(InMemoryIncidentStore())
    service.submit_incident("tenant-a", "request-1", facts(), now=NOW)

    with pytest.raises(IncidentNotFoundError, match="incident not found"):
        service.get_incident("tenant-b", "incident-1")
    with pytest.raises(IncidentNotFoundError, match="incident not found"):
        service.append_observation(
            "tenant-b",
            "incident-1",
            observation(
                observed_at=NOW + timedelta(minutes=1),
                tenant_id="tenant-b",
            ),
            now=NOW + timedelta(minutes=1),
        )

    assert service.get_incident("tenant-a", "incident-1").observations == ()


def test_observation_cannot_change_the_incident_tenant_or_identity() -> None:
    service = GuardianService(InMemoryIncidentStore())
    service.submit_incident("tenant-a", "request-1", facts(), now=NOW)

    with pytest.raises(TenantMismatchError):
        service.append_observation(
            "tenant-a",
            "incident-1",
            observation(observed_at=NOW + timedelta(minutes=1), tenant_id="tenant-b"),
            now=NOW + timedelta(minutes=1),
        )
    with pytest.raises(IncidentNotFoundError):
        service.append_observation(
            "tenant-a",
            "incident-missing",
            observation(
                observed_at=NOW + timedelta(minutes=1),
                incident_id="incident-missing",
            ),
            now=NOW + timedelta(minutes=1),
        )

    assert service.get_incident("tenant-a", "incident-1").observations == ()


@pytest.mark.parametrize(
    "replacement_facts",
    [
        facts(tenant_id="tenant-b"),
        facts(severity=IncidentSeverity.CRITICAL),
    ],
)
def test_store_rejects_replaced_facts_without_persisting(
    replacement_facts: IncidentFacts,
) -> None:
    store = InMemoryIncidentStore()
    service = GuardianService(store)
    before = service.submit_incident("tenant-a", "request-1", facts(), now=NOW)

    with pytest.raises(IncidentInvariantError):
        store.update(
            "tenant-a",
            "incident-1",
            lambda current: current.model_copy(update={"facts": replacement_facts}),
        )

    assert store.get("tenant-a", "incident-1") == before


@pytest.mark.parametrize("field", ["tenant_id", "incident_id", "workflow_id"])
def test_store_rejects_outer_identity_changes_with_typed_error(field: str) -> None:
    store = InMemoryIncidentStore()
    service = GuardianService(store)
    before = service.submit_incident("tenant-a", "request-1", facts(), now=NOW)
    changed = {
        "tenant_id": "tenant-b",
        "incident_id": "incident-2",
        "workflow_id": "guardian/tenant-a/incident/incident-2",
    }

    with pytest.raises(IncidentInvariantError):
        store.update(
            "tenant-a",
            "incident-1",
            lambda current: current.model_copy(update={field: changed[field]}),
        )

    assert store.get("tenant-a", "incident-1") == before


def test_store_rejects_truncated_or_modified_observation_history() -> None:
    store = InMemoryIncidentStore()
    service = GuardianService(store)
    service.submit_incident("tenant-a", "request-1", facts(), now=NOW)
    first_observation = observation(
        observed_at=NOW + timedelta(minutes=1), healthy=False
    )
    before = service.append_observation(
        "tenant-a",
        "incident-1",
        first_observation,
        now=NOW + timedelta(minutes=1),
    )
    invalid_histories = (
        (),
        (first_observation.model_copy(update={"service_healthy": True}),),
    )

    for invalid_history in invalid_histories:
        with pytest.raises(IncidentInvariantError):
            store.update(
                "tenant-a",
                "incident-1",
                lambda current, history=invalid_history: current.model_copy(
                    update={"observations": history}
                ),
            )
        assert store.get("tenant-a", "incident-1") == before


def test_observations_are_append_only_and_reevaluate_history_deterministically() -> (
    None
):
    first_observation = observation(
        observed_at=NOW + timedelta(minutes=1), healthy=False
    )
    second_observation = observation(observed_at=NOW + timedelta(minutes=2))

    def run_sequence():
        service = GuardianService(InMemoryIncidentStore())
        initial = service.submit_incident("tenant-a", "request-1", facts(), now=NOW)
        first = service.append_observation(
            "tenant-a",
            "incident-1",
            first_observation,
            now=NOW + timedelta(minutes=1),
        )
        second = service.append_observation(
            "tenant-a",
            "incident-1",
            second_observation,
            now=NOW + timedelta(minutes=2),
        )
        return initial, first, second

    initial, first, second = run_sequence()
    _, _, repeated = run_sequence()

    assert initial.observations == ()
    assert first.observations == (first_observation,)
    assert second.observations == (first_observation, second_observation)
    assert second.model_dump_json() == repeated.model_dump_json()


def test_returned_snapshots_are_frozen_copies_not_store_objects() -> None:
    service = GuardianService(InMemoryIncidentStore())
    created = service.submit_incident("tenant-a", "request-1", facts(), now=NOW)

    with pytest.raises(ValidationError):
        created.workflow_id = "guardian/tenant-b/incident/incident-1"  # type: ignore[misc]

    confidence = created.projection.hypotheses[0].required_group_confidence
    with pytest.raises(TypeError):
        confidence["caller-mutation"] = 0.0
    loaded = service.get_incident("tenant-a", "incident-1")

    assert (
        "caller-mutation"
        not in loaded.projection.hypotheses[0].required_group_confidence
    )
    assert loaded is not created
    assert loaded.facts is not created.facts


def test_strict_models_reject_malformed_and_naive_time_inputs_before_service() -> None:
    payload = facts().model_dump()
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        IncidentFacts.model_validate(payload)

    update = observation(observed_at=NOW + timedelta(minutes=1)).model_dump()
    update["observed_at"] = datetime(2026, 7, 23, 10, 1)
    with pytest.raises(ValidationError):
        ObservationUpdate.model_validate(update)
