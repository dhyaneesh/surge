"""Replay contracts for deterministic Guardian projections.

TST-GRD-MLO-002-REPLAY
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from apps.guardian_api.models import (
    ActionType,
    ControlFacts,
    EvidenceFreshness,
    EvidencePass,
    EvidenceSource,
    HypothesisName,
    IncidentSeverity,
    IncidentSubmission,
    NumericEvidence,
    ObservationUpdate,
    PolicyFacts,
    PolicyState,
    RequiredSignal,
    SignalFacts,
    TargetIdentity,
    TelemetryFacts,
    WorkflowState,
)
from apps.guardian_api.service import GuardianService
from apps.guardian_api.store import (
    InMemoryIncidentStore,
    IncidentSnapshot,
    canonical_json,
)


NOW = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
TENANT_ID = "replay-tenant"
INCIDENT_ID = "replay-incident"
IDEMPOTENCY_KEY = "replay-submission"


def _telemetry(observed_at: datetime, *, quality: float = 1.0) -> TelemetryFacts:
    return TelemetryFacts(
        quality=quality,
        newest_required_sample_at=observed_at,
        freshness_seconds=60,
        required_signals=frozenset({RequiredSignal.TELEMETRY_QUALITY}),
        clock_skew_seconds=0,
        required_sample_count=10,
        usable_sample_count=10,
        pipeline_available=True,
        comparison_valid=True,
    )


def _numeric(
    value: float,
    *,
    baseline: float | None = None,
    group: str,
) -> NumericEvidence:
    return NumericEvidence(
        tenant_id=TENANT_ID,
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
        provenance_ref=f"query-contract/replay-{group}",
        independence_group=group,
        expected_samples=10_000,
        usable_samples=10_000,
    )


def _submission(
    *,
    telemetry_quality: float = 1.0,
    signals: SignalFacts | None = None,
) -> IncidentSubmission:
    return IncidentSubmission(
        tenant_id=TENANT_ID,
        severity=IncidentSeverity.WARNING,
        observed_at=NOW,
        identity=TargetIdentity(
            target_role="request-processor",
            environment="production",
            namespace="payments",
            workload_kind="Deployment",
            workload_name="processor",
            service_name="processor",
            image_digest="sha256:" + "a" * 64,
        ),
        telemetry=_telemetry(NOW, quality=telemetry_quality),
        evidence_pass=EvidencePass(completed_passes=1, started_at=NOW),
        signals=signals if signals is not None else SignalFacts(),
        policy=PolicyFacts(state=PolicyState.FRESH, evaluated_at=NOW),
        control=ControlFacts(),
    )


def _observation(
    *,
    sequence: int,
    healthy: bool,
    observed_at: datetime,
) -> ObservationUpdate:
    return ObservationUpdate(
        tenant_id=TENANT_ID,
        incident_id=INCIDENT_ID,
        observation_id=f"replay-observation-{sequence}",
        sequence=sequence,
        window_key=f"replay-window-{sequence}",
        observed_at=observed_at,
        window_started_at=observed_at - timedelta(seconds=30),
        telemetry=_telemetry(observed_at),
        service_healthy=healthy,
        required_conditions_satisfied=healthy,
        provenance_ref=f"query-contract/replay-{sequence}",
    )


def _canonical_events() -> tuple[IncidentSubmission | ObservationUpdate, ...]:
    return (
        _submission(),
        _observation(
            sequence=1,
            healthy=False,
            observed_at=NOW + timedelta(minutes=1),
        ),
        _observation(
            sequence=2,
            healthy=True,
            observed_at=NOW + timedelta(minutes=2),
        ),
    )


def _eligible_action_events() -> tuple[IncidentSubmission, ...]:
    return (
        _submission(
            signals=SignalFacts(
                request_rate=_numeric(200, baseline=100, group="load"),
                cpu_utilization=_numeric(0.85, group="util"),
            )
        ),
    )


def _replay(
    events: tuple[IncidentSubmission | ObservationUpdate, ...],
) -> tuple[tuple[IncidentSnapshot, ...], InMemoryIncidentStore]:
    store = InMemoryIncidentStore(incident_id_factory=lambda: INCIDENT_ID)
    service = GuardianService(store)
    snapshots: list[IncidentSnapshot] = []

    for event in events:
        if isinstance(event, IncidentSubmission):
            snapshots.append(
                service.submit_incident(
                    TENANT_ID,
                    IDEMPOTENCY_KEY,
                    event,
                    now=NOW,
                )
            )
        else:
            snapshots.append(
                service.append_observation(
                    TENANT_ID,
                    INCIDENT_ID,
                    event,
                    now=event.observed_at,
                )
            )
    for snapshot in snapshots:
        for projection in snapshot.projection_history:
            assert projection.model_participated is False
    return tuple(snapshots), store


def _projection_hashes(snapshot: IncidentSnapshot) -> tuple[str, ...]:
    return tuple(
        hashlib.sha256(canonical_json(projection).encode("utf-8")).hexdigest()
        for projection in snapshot.projection_history
    )


def test_empty_store_replay_records_hashable_projection_history() -> None:
    snapshots, store = _replay(_canonical_events())
    final = snapshots[-1]

    assert len(store.snapshot().incidents) == 1
    assert len(final.projection_history) == len(_canonical_events())
    assert all(len(digest) == 64 for digest in _projection_hashes(final))


@pytest.mark.parametrize("event_count", (1, 2, 3))
def test_prefix_replays_match_full_history_prefix(event_count: int) -> None:
    events = _canonical_events()
    full_snapshots, _ = _replay(events)
    prefix_snapshots, _ = _replay(events[:event_count])

    assert (
        _projection_hashes(prefix_snapshots[-1])
        == _projection_hashes(full_snapshots[-1])[
            : len(_projection_hashes(prefix_snapshots[-1]))
        ]
    )


def test_repeated_full_replays_are_identical_and_fact_change_is_observable() -> None:
    events = _canonical_events()
    first_snapshots, _ = _replay(events)
    second_snapshots, _ = _replay(events)
    changed_snapshots, _ = _replay((_submission(telemetry_quality=0.79),))

    assert _projection_hashes(first_snapshots[-1]) == _projection_hashes(
        second_snapshots[-1]
    )
    assert _projection_hashes(changed_snapshots[-1]) != _projection_hashes(
        first_snapshots[0]
    )


def test_eligible_action_replays_preserve_scores_transitions_and_gates() -> None:
    first_snapshots, _ = _replay(_eligible_action_events())
    second_snapshots, _ = _replay(_eligible_action_events())
    first = first_snapshots[-1].projection_history
    second = second_snapshots[-1].projection_history

    assert _projection_hashes(first_snapshots[-1]) == _projection_hashes(
        second_snapshots[-1]
    )
    assert tuple(
        tuple(
            (hypothesis.name, hypothesis.deterministic_score, hypothesis.eligible)
            for hypothesis in projection.hypotheses
        )
        for projection in first
    ) == tuple(
        tuple(
            (hypothesis.name, hypothesis.deterministic_score, hypothesis.eligible)
            for hypothesis in projection.hypotheses
        )
        for projection in second
    )
    assert tuple(projection.workflow_state for projection in first) == (
        WorkflowState.ASSESSMENT,
        WorkflowState.CLASSIFIED,
    )
    assert tuple(projection.workflow_state for projection in first) == tuple(
        projection.workflow_state for projection in second
    )
    assert tuple(
        (
            projection.permitted_actions,
            projection.forbidden_actions,
            projection.proposed_action,
        )
        for projection in first
    ) == tuple(
        (
            projection.permitted_actions,
            projection.forbidden_actions,
            projection.proposed_action,
        )
        for projection in second
    )
    assert all(projection.model_participated is False for projection in first)
    assert all(projection.model_participated is False for projection in second)

    load_spike = next(
        hypothesis
        for hypothesis in first[-1].hypotheses
        if hypothesis.name is HypothesisName.LOAD_SPIKE
    )
    assert load_spike.eligible is True
    assert first[-1].proposed_action is ActionType.SCALE_UP
    assert set(first[-1].forbidden_actions) == {
        ActionType.SCALE_DOWN,
        ActionType.ROLLBACK,
        ActionType.PROTECT_DEPENDENCY,
    }


def test_duplicate_idempotent_delivery_leaves_projection_and_parent_unchanged() -> None:
    submission = _submission()
    snapshots, store = _replay((submission, submission))

    assert _projection_hashes(snapshots[1]) == _projection_hashes(snapshots[0])
    assert {snapshot.workflow_id for snapshot in snapshots} == {
        f"guardian/{TENANT_ID}/incident/{INCIDENT_ID}"
    }
    assert len(store.snapshot().incidents) == 1
    assert store.snapshot().idempotency_count == 1


def test_interleaved_duplicate_delivery_converges_to_one_parent_and_same_hashes() -> (
    None
):
    submission, first, second = _canonical_events()
    canonical_snapshots, canonical_store = _replay((submission, first, second))
    reordered_snapshots, reordered_store = _replay(
        (submission, first, first, second, submission)
    )

    assert _projection_hashes(reordered_snapshots[-1]) == _projection_hashes(
        canonical_snapshots[-1]
    )
    assert len(canonical_store.snapshot().incidents) == 1
    assert len(reordered_store.snapshot().incidents) == 1
    assert {
        snapshot.workflow_id for snapshot in reordered_store.snapshot().incidents
    } == {f"guardian/{TENANT_ID}/incident/{INCIDENT_ID}"}
