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
    CriticalIntegrityFailure,
    EvidenceFreshness,
    EvidencePass,
    EvidenceSource,
    IncidentFacts,
    IncidentClass,
    IncidentSeverity,
    NumericEvidence,
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
    ObservationOrderError,
    ReentrantStoreWriteError,
    canonical_incident_snapshot,
    canonical_incident_facts,
    canonical_json,
    incident_facts_hash,
    replay_incident_history,
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
    control: ControlFacts | None = None,
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
        control=control or ControlFacts(),
    )


def observation(
    *,
    observed_at: datetime,
    tenant_id: str = "tenant-a",
    incident_id: str = "incident-1",
    healthy: bool = True,
    observation_id: str = "observation-1",
    sequence: int = 1,
    window_key: str = "window-1",
) -> ObservationUpdate:
    return ObservationUpdate(
        tenant_id=tenant_id,
        incident_id=incident_id,
        observation_id=observation_id,
        sequence=sequence,
        window_key=window_key,
        observed_at=observed_at,
        window_started_at=observed_at - timedelta(seconds=30),
        telemetry=telemetry(observed_at),
        service_healthy=healthy,
        required_conditions_satisfied=healthy,
        provenance_ref=f"query-contract/{observed_at.isoformat()}",
    )


def numeric_evidence(
    value: float, *, baseline: float | None, group: str
) -> NumericEvidence:
    return NumericEvidence(
        tenant_id="tenant-a",
        subject_role="request-processor",
        environment="production",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="processor",
        service_name="processor",
        value=value,
        baseline_value=baseline,
        observed_at=NOW,
        freshness=EvidenceFreshness.FRESH,
        source=EvidenceSource.QUERY_CONTRACT,
        provenance_ref=f"query-contract/{group}",
        independence_group=group,
        expected_samples=10,
        usable_samples=10,
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


def test_canonical_json_normalizes_negative_zero_and_rejects_non_string_keys() -> None:
    assert canonical_json({"value": -0.0}) == '{"value":0.0}'
    negative_zero = facts().model_copy(
        update={
            "telemetry": facts().telemetry.model_copy(
                update={"clock_skew_seconds": -0.0}
            )
        }
    )
    assert incident_facts_hash(negative_zero) == incident_facts_hash(facts())
    with pytest.raises(TypeError, match="mapping keys must be strings"):
        canonical_json({1: "invalid"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", "tenant/a"),
        ("incident_id", "incident/a"),
        ("tenant_id", "guardian/tenant/incident/id"),
    ],
)
def test_incident_scope_identifiers_reject_workflow_delimiters(
    field: str, value: str
) -> None:
    payload = facts().model_dump()
    payload[field] = value
    with pytest.raises(ValidationError):
        IncidentFacts.model_validate(payload)


def test_service_rejects_invalid_authenticated_tenant_identifier() -> None:
    service = GuardianService(InMemoryIncidentStore())
    with pytest.raises(ValueError, match="authenticated tenant"):
        service.get_incident("tenant/a", "incident-1")


def test_create_and_append_reject_naive_operation_times_without_persistence() -> None:
    store = InMemoryIncidentStore()
    service = GuardianService(store)
    naive_now = NOW.replace(tzinfo=None)

    with pytest.raises(ValueError, match="timezone-aware"):
        service.submit_incident("tenant-a", "request-1", facts(), now=naive_now)
    assert store.snapshot().incidents == ()

    created = service.submit_incident("tenant-a", "request-1", facts(), now=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        service.append_observation(
            "tenant-a",
            "incident-1",
            observation(observed_at=NOW + timedelta(minutes=1)),
            now=naive_now,
        )
    assert service.get_incident("tenant-a", "incident-1") == created


def test_delayed_submission_evaluates_stale_facts_at_operation_time() -> None:
    operation_time = NOW + timedelta(minutes=10)
    snapshot = GuardianService(InMemoryIncidentStore()).submit_incident(
        "tenant-a", "request-1", facts(), now=operation_time
    )

    assert snapshot.evaluation_times == (operation_time,)
    assert snapshot.projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert (
        CriticalIntegrityFailure.SAMPLE_STALE in snapshot.projection.integrity_failures
    )


def test_delayed_observation_is_stale_at_append_operation_time() -> None:
    service = GuardianService(InMemoryIncidentStore())
    incident_facts = facts(
        control=ControlFacts(action_completed_at=NOW - timedelta(minutes=1))
    )
    service.submit_incident("tenant-a", "request-1", incident_facts, now=NOW)
    stale_observation = observation(
        observed_at=NOW + timedelta(minutes=1), healthy=True
    )
    operation_time = NOW + timedelta(minutes=10)

    snapshot = service.append_observation(
        "tenant-a", "incident-1", stale_observation, now=operation_time
    )

    assert snapshot.evaluation_times == (NOW, operation_time)
    assert not snapshot.projection.recovery_verified
    assert (
        CriticalIntegrityFailure.SAMPLE_STALE in snapshot.projection.integrity_failures
    )


def test_policy_freshness_is_evaluated_at_submission_operation_time() -> None:
    incident_facts = facts().model_copy(
        update={
            "signals": SignalFacts(
                request_rate=numeric_evidence(200.0, baseline=100.0, group="load"),
                cpu_utilization=numeric_evidence(
                    0.85, baseline=None, group="utilization"
                ),
            )
        }
    )
    operation_time = NOW + timedelta(seconds=31)

    snapshot = GuardianService(InMemoryIncidentStore()).submit_incident(
        "tenant-a", "request-1", incident_facts, now=operation_time
    )

    assert snapshot.evaluation_times == (operation_time,)
    assert snapshot.projection.proposed_action is None
    assert snapshot.projection.terminal_reason == "policy-unusable"


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


def test_same_key_different_facts_race_has_one_winner_and_conflicts() -> None:
    store = InMemoryIncidentStore()
    service = GuardianService(store)
    workers = 12
    barrier = Barrier(workers)

    def submit(index: int):
        barrier.wait()
        request = facts(incident_id=f"incident-{index}")
        try:
            return service.submit_incident(
                "tenant-a", "racing-request", request, now=NOW
            )
        except IdempotencyConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = tuple(executor.map(submit, range(workers)))

    successes = tuple(item for item in results if not isinstance(item, Exception))
    conflicts = tuple(
        item for item in results if isinstance(item, IdempotencyConflictError)
    )
    assert len(successes) == 1
    assert len(conflicts) == workers - 1
    assert len(store.snapshot().incidents) == 1


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


def test_store_exposes_only_narrow_append_not_arbitrary_update() -> None:
    store = InMemoryIncidentStore()
    assert not hasattr(store, "update")


def test_projector_failure_rolls_back_append() -> None:
    def failing_projector(facts, observations, evaluation_times, conflicts):
        if observations:
            raise RuntimeError("projection failed")
        return replay_incident_history(facts, observations, evaluation_times, conflicts)

    store = InMemoryIncidentStore(projector=failing_projector)
    service = GuardianService(store)
    before = service.submit_incident("tenant-a", "request-1", facts(), now=NOW)

    with pytest.raises(RuntimeError, match="projection failed"):
        service.append_observation(
            "tenant-a",
            "incident-1",
            observation(observed_at=NOW + timedelta(minutes=1)),
            now=NOW + timedelta(minutes=1),
        )

    assert store.get("tenant-a", "incident-1") == before


def test_projector_reentry_is_rejected_and_rolls_back_outer_append() -> None:
    store_reference: dict[str, InMemoryIncidentStore] = {}

    def reentrant_projector(facts, observations, evaluation_times, conflicts):
        if observations:
            try:
                store_reference["store"].append_observation(
                    "tenant-a",
                    "incident-1",
                    observation(observed_at=NOW + timedelta(minutes=2)),
                    now=NOW + timedelta(minutes=2),
                )
            except ReentrantStoreWriteError:
                pass
        return replay_incident_history(facts, observations, evaluation_times, conflicts)

    store = InMemoryIncidentStore(projector=reentrant_projector)
    store_reference["store"] = store
    service = GuardianService(store)
    before = service.submit_incident("tenant-a", "request-1", facts(), now=NOW)

    with pytest.raises(ReentrantStoreWriteError):
        service.append_observation(
            "tenant-a",
            "incident-1",
            observation(observed_at=NOW + timedelta(minutes=1)),
            now=NOW + timedelta(minutes=1),
        )

    assert store.get("tenant-a", "incident-1") == before


def test_chronologically_older_observation_is_rejected_without_persistence() -> None:
    store = InMemoryIncidentStore()
    service = GuardianService(store)
    service.submit_incident("tenant-a", "request-1", facts(), now=NOW)
    before = service.append_observation(
        "tenant-a",
        "incident-1",
        observation(observed_at=NOW + timedelta(minutes=2)),
        now=NOW + timedelta(minutes=2),
    )

    with pytest.raises(ObservationOrderError):
        service.append_observation(
            "tenant-a",
            "incident-1",
            observation(observed_at=NOW + timedelta(minutes=1)),
            now=NOW + timedelta(minutes=2),
        )

    assert store.get("tenant-a", "incident-1") == before


def test_first_observation_cannot_predate_incident_facts() -> None:
    store = InMemoryIncidentStore()
    service = GuardianService(store)
    before = service.submit_incident("tenant-a", "request-1", facts(), now=NOW)

    with pytest.raises(ObservationOrderError):
        service.append_observation(
            "tenant-a",
            "incident-1",
            observation(observed_at=NOW - timedelta(microseconds=1)),
            now=NOW,
        )

    assert store.get("tenant-a", "incident-1") == before


def test_first_observation_may_equal_incident_observed_at() -> None:
    service = GuardianService(InMemoryIncidentStore())
    service.submit_incident("tenant-a", "request-1", facts(), now=NOW)

    snapshot = service.append_observation(
        "tenant-a",
        "incident-1",
        observation(observed_at=NOW),
        now=NOW,
    )

    assert snapshot.observations[0].observed_at == NOW
    assert not snapshot.observation_conflicts


def test_pure_replay_rejects_reordered_history() -> None:
    later = observation(observed_at=NOW + timedelta(minutes=2))
    earlier = observation(observed_at=NOW + timedelta(minutes=1))

    with pytest.raises(ObservationOrderError):
        replay_incident_history(
            facts(),
            (later, earlier),
            (NOW, NOW + timedelta(minutes=2), NOW + timedelta(minutes=2)),
        )


def test_duplicate_observation_delivery_is_idempotent() -> None:
    store = InMemoryIncidentStore()
    service = GuardianService(store)
    service.submit_incident("tenant-a", "request-1", facts(), now=NOW)
    update = observation(observed_at=NOW + timedelta(minutes=1))
    first = service.append_observation(
        "tenant-a",
        "incident-1",
        update,
        now=NOW + timedelta(minutes=1),
    )

    duplicate = service.append_observation(
        "tenant-a",
        "incident-1",
        update,
        now=NOW + timedelta(minutes=2),
    )

    assert canonical_incident_snapshot(duplicate) == canonical_incident_snapshot(first)
    assert len(duplicate.observations) == 1
    assert len(duplicate.projection_history) == 2


def test_simultaneous_appends_have_no_lost_updates() -> None:
    store = InMemoryIncidentStore()
    service = GuardianService(store)
    service.submit_incident("tenant-a", "request-1", facts(), now=NOW)
    workers = 10
    barrier = Barrier(workers)

    def append(index: int):
        barrier.wait()
        update = observation(
            observed_at=NOW + timedelta(minutes=1),
            observation_id=f"observation-{index}",
            sequence=index,
        ).model_copy(update={"provenance_ref": f"query-contract/concurrent-{index}"})
        return service.append_observation(
            "tenant-a",
            "incident-1",
            update,
            now=NOW + timedelta(minutes=1),
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        tuple(executor.map(append, range(workers)))

    final = service.get_incident("tenant-a", "incident-1")
    assert len(final.observations) == workers
    assert len(final.projection_history) == workers + 1
    assert {item.provenance_ref for item in final.observations} == {
        f"query-contract/concurrent-{index}" for index in range(workers)
    }


def test_conflicting_window_is_fail_closed_and_arrival_order_independent() -> None:
    incident_facts = facts(
        control=ControlFacts(action_completed_at=NOW - timedelta(minutes=1))
    )
    healthy = observation(observed_at=NOW + timedelta(minutes=1), healthy=True)
    unhealthy = observation(observed_at=NOW + timedelta(minutes=1), healthy=False)

    def run(first: ObservationUpdate, second: ObservationUpdate):
        service = GuardianService(InMemoryIncidentStore())
        service.submit_incident("tenant-a", "request-1", incident_facts, now=NOW)
        first_snapshot = service.append_observation(
            "tenant-a", "incident-1", first, now=NOW + timedelta(minutes=1)
        )
        final_snapshot = service.append_observation(
            "tenant-a", "incident-1", second, now=NOW + timedelta(minutes=2)
        )
        return first_snapshot, final_snapshot

    healthy_only, healthy_first = run(healthy, unhealthy)
    unhealthy_only, unhealthy_first = run(unhealthy, healthy)

    assert healthy_first.observations != unhealthy_first.observations
    assert canonical_incident_snapshot(healthy_first) != canonical_incident_snapshot(
        unhealthy_first
    )
    assert healthy_first.observation_conflicts == unhealthy_first.observation_conflicts
    assert healthy_first.projection == unhealthy_first.projection
    assert len(healthy_first.observation_conflicts) == 1
    assert healthy_first.projection.terminal_reason == "observation-conflict"
    assert not healthy_first.projection.telemetry_healthy
    assert not healthy_first.projection.recovery_verified
    assert healthy_first.projection.proposed_action is None
    assert not healthy_first.projection.eligible_actions
    assert healthy_first.evaluation_times == (
        NOW,
        NOW + timedelta(minutes=1),
        NOW + timedelta(minutes=2),
    )
    assert unhealthy_first.evaluation_times == healthy_first.evaluation_times
    assert healthy_first.projection_history[: len(healthy_only.projection_history)] == (
        healthy_only.projection_history
    )
    assert (
        unhealthy_first.projection_history[: len(unhealthy_only.projection_history)]
        == unhealthy_only.projection_history
    )


def test_conflicting_window_makes_every_hypothesis_ineligible() -> None:
    incident_facts = facts().model_copy(
        update={
            "signals": SignalFacts(
                request_rate=numeric_evidence(200.0, baseline=100.0, group="load"),
                cpu_utilization=numeric_evidence(
                    0.85, baseline=None, group="utilization"
                ),
            )
        }
    )
    healthy = observation(observed_at=NOW + timedelta(minutes=1), healthy=True)
    unhealthy = observation(observed_at=NOW + timedelta(minutes=1), healthy=False)
    service = GuardianService(InMemoryIncidentStore())

    initial = service.submit_incident("tenant-a", "request-1", incident_facts, now=NOW)
    service.append_observation(
        "tenant-a", "incident-1", healthy, now=NOW + timedelta(minutes=1)
    )
    conflicted = service.append_observation(
        "tenant-a", "incident-1", unhealthy, now=NOW + timedelta(minutes=2)
    )

    assert any(item.eligible for item in initial.projection.hypotheses)
    assert all(not item.eligible for item in conflicted.projection.hypotheses)
    assert conflicted.projection.eligible_actions == ()
    assert conflicted.projection.permitted_actions == ()
    assert conflicted.projection.proposed_action is None
    assert not conflicted.projection.recovery_verified


def test_concurrent_conflicting_window_matches_canonical_fail_closed_state() -> None:
    incident_facts = facts(
        control=ControlFacts(action_completed_at=NOW - timedelta(minutes=1))
    )
    healthy = observation(observed_at=NOW + timedelta(minutes=1), healthy=True)
    unhealthy = observation(observed_at=NOW + timedelta(minutes=1), healthy=False)

    expected_service = GuardianService(InMemoryIncidentStore())
    expected_service.submit_incident("tenant-a", "request-1", incident_facts, now=NOW)
    expected_service.append_observation(
        "tenant-a", "incident-1", healthy, now=NOW + timedelta(minutes=1)
    )
    expected = expected_service.append_observation(
        "tenant-a", "incident-1", unhealthy, now=NOW + timedelta(minutes=2)
    )

    store = InMemoryIncidentStore()
    service = GuardianService(store)
    service.submit_incident("tenant-a", "request-1", incident_facts, now=NOW)
    barrier = Barrier(2)

    def append(update: ObservationUpdate):
        barrier.wait()
        return service.append_observation(
            "tenant-a",
            "incident-1",
            update,
            now=NOW + timedelta(minutes=2),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        tuple(executor.map(append, (healthy, unhealthy)))

    actual = service.get_incident("tenant-a", "incident-1")
    assert actual.observation_conflicts == expected.observation_conflicts
    assert actual.projection == expected.projection
    assert actual.projection.terminal_reason == "observation-conflict"


def test_observations_are_append_only_and_reevaluate_history_deterministically() -> (
    None
):
    first_observation = observation(
        observed_at=NOW + timedelta(minutes=1), healthy=False
    )
    second_observation = observation(
        observed_at=NOW + timedelta(minutes=1, seconds=50),
        observation_id="observation-2",
        sequence=2,
        window_key="window-2",
    )

    incident_facts = facts(
        control=ControlFacts(action_completed_at=NOW - timedelta(minutes=1))
    )

    def run_sequence():
        service = GuardianService(InMemoryIncidentStore())
        initial = service.submit_incident(
            "tenant-a",
            "request-1",
            incident_facts,
            now=NOW + timedelta(seconds=10),
        )
        first = service.append_observation(
            "tenant-a",
            "incident-1",
            first_observation,
            now=NOW + timedelta(minutes=1, seconds=10),
        )
        second = service.append_observation(
            "tenant-a",
            "incident-1",
            second_observation,
            now=NOW + timedelta(minutes=1, seconds=55),
        )
        return initial, first, second

    initial, first, second = run_sequence()
    _, _, repeated = run_sequence()

    assert initial.observations == ()
    assert first.observations == (first_observation,)
    assert second.observations == (first_observation, second_observation)
    assert initial.evaluation_times == (NOW + timedelta(seconds=10),)
    assert first.evaluation_times == (
        NOW + timedelta(seconds=10),
        NOW + timedelta(minutes=1, seconds=10),
    )
    assert second.evaluation_times[: len(first.evaluation_times)] == (
        first.evaluation_times
    )
    assert second.evaluation_times[-1] == NOW + timedelta(minutes=1, seconds=55)
    assert second.projection_history[: len(first.projection_history)] == (
        first.projection_history
    )
    assert len(second.projection_history) == 3
    assert not second.projection_history[1].recovery_verified
    assert second.projection_history[2].recovery_verified
    assert second.projection == second.projection_history[-1]
    assert canonical_incident_snapshot(first) != canonical_incident_snapshot(second)
    assert first_observation.provenance_ref in canonical_incident_snapshot(second)
    assert canonical_incident_snapshot(second) == canonical_incident_snapshot(repeated)


def test_returned_snapshots_are_frozen_copies_not_store_objects() -> None:
    service = GuardianService(InMemoryIncidentStore())
    created = service.submit_incident("tenant-a", "request-1", facts(), now=NOW)

    with pytest.raises(ValidationError):
        created.workflow_id = "guardian/tenant-b/incident/incident-1"  # type: ignore[misc]

    confidence = created.projection.hypotheses[0].required_group_confidence
    with pytest.raises(TypeError):
        confidence["caller-mutation"] = 0.0  # type: ignore[index]
    assert not isinstance(confidence, dict)
    with pytest.raises(TypeError):
        dict.__setitem__(
            confidence,  # type: ignore[arg-type]
            "base-class-mutation",
            0.0,
        )
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
