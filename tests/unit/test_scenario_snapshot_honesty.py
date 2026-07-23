"""Honest scenario-snapshot projection contracts (no invented evidence)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apps.guardian_api.domain import evaluate_incident
from apps.guardian_api.http import (
    _scenario_evidence_lists,
    _scenario_safety_gates,
    _scenario_workflow_states,
)
from apps.guardian_api.models import (
    ActionType,
    ControlFacts,
    EvidencePass,
    EvidenceSource,
    IncidentClass,
    IncidentFacts,
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
from apps.guardian_api.store import IncidentSnapshot
from apps.guardian_api.models import EvidenceFreshness

NOW = datetime(2026, 7, 23, 18, 0, tzinfo=UTC)
DIGEST = "sha256:" + "a" * 64


def _telemetry(*, quality: float = 1.0) -> TelemetryFacts:
    return TelemetryFacts(
        quality=quality,
        newest_required_sample_at=NOW,
        freshness_seconds=60,
        required_signals=frozenset({RequiredSignal.TELEMETRY_QUALITY}),
        clock_skew_seconds=0.0,
        required_sample_count=10,
        usable_sample_count=10 if quality >= 0.80 else 0,
        pipeline_available=quality >= 0.80,
        comparison_valid=quality >= 0.80,
    )


def _identity() -> TargetIdentity:
    return TargetIdentity(
        target_role="request-processor",
        environment="otel-demo",
        namespace="guardian-test",
        workload_kind="Deployment",
        workload_name="checkout",
        service_name="checkout",
        image_digest=DIGEST,
    )


def _numeric(
    value: float, *, baseline: float | None = None, group: str
) -> NumericEvidence:
    return NumericEvidence(
        tenant_id="tenant-a",
        subject_role="request-processor",
        environment="otel-demo",
        namespace="guardian-test",
        workload_kind="Deployment",
        workload_name="checkout",
        service_name="checkout",
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


def _facts(**changes: object) -> IncidentFacts:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "incident_id": "incident-1",
        "observed_at": NOW,
        "identity": _identity(),
        "telemetry": _telemetry(),
        "evidence_pass": EvidencePass(completed_passes=1, started_at=NOW),
        "signals": SignalFacts(),
        "policy": PolicyFacts(state=PolicyState.FRESH, evaluated_at=NOW),
        "control": ControlFacts(),
    }
    values.update(changes)
    return IncidentFacts.model_validate(values)


def _snapshot(
    *,
    facts: IncidentFacts | None = None,
    observations: tuple[ObservationUpdate, ...] = (),
    projection_history: tuple | None = None,
) -> IncidentSnapshot:
    facts = facts or _facts()
    if projection_history is None:
        projection = evaluate_incident(facts, now=NOW)
        projection_history = (projection,)
        evaluation_times = (NOW,)
    else:
        evaluation_times = tuple(
            NOW + timedelta(seconds=index) for index in range(len(projection_history))
        )
        # Allow preamble: times length must equal observations+1 for store rules
        # used by tests that build snapshots directly — align times to history length
        # only when matching observation count+1; tests override as needed.
        if len(evaluation_times) != len(observations) + 1:
            evaluation_times = (NOW,) * (len(observations) + 1)
            # If history longer than times, pad times to history length for direct unit tests
            if len(projection_history) > len(evaluation_times):
                evaluation_times = tuple(
                    NOW + timedelta(seconds=i) for i in range(len(projection_history))
                )
    # IncidentSnapshot validator requires times == obs+1 and history == times.
    # For honest-history unit tests that include assessment preamble, build via store.
    return IncidentSnapshot(
        tenant_id="tenant-a",
        incident_id=facts.incident_id,
        workflow_id=f"guardian/tenant-a/incident/{facts.incident_id}",
        facts=facts,
        observations=observations,
        evaluation_times=(NOW,)
        if not observations and len(projection_history) == 1
        else evaluation_times,
        projection_history=projection_history,
        projection=projection_history[-1],
    )


def test_supporting_evidence_does_not_invent_metrics_from_telemetry_alone() -> None:
    facts = _facts(signals=SignalFacts())
    projection = evaluate_incident(facts, now=NOW)
    snapshot = IncidentSnapshot(
        tenant_id="tenant-a",
        incident_id="incident-1",
        workflow_id="guardian/tenant-a/incident/incident-1",
        facts=facts,
        evaluation_times=(NOW,),
        projection_history=(projection,),
        projection=projection,
    )

    supporting, contradicting, required_fresh = _scenario_evidence_lists(snapshot)

    assert supporting == ()
    assert contradicting == ()
    assert required_fresh == ()
    assert all(item.get("evidenceType") != "metrics" for item in supporting)


def test_supporting_evidence_comes_from_real_signal_contributions() -> None:
    facts = _facts(
        signals=SignalFacts(
            request_rate=_numeric(200, baseline=100, group="load"),
            cpu_utilization=_numeric(0.90, group="utilization"),
        )
    )
    projection = evaluate_incident(facts, now=NOW)
    snapshot = IncidentSnapshot(
        tenant_id="tenant-a",
        incident_id="incident-1",
        workflow_id="guardian/tenant-a/incident/incident-1",
        facts=facts,
        evaluation_times=(NOW,),
        projection_history=(projection,),
        projection=projection,
    )

    supporting, _, _ = _scenario_evidence_lists(snapshot)
    types = {item["evidenceType"] for item in supporting}
    assert "load" in types
    assert "resource-utilization" in types
    assert "metrics" not in types


def test_recovery_telemetry_absent_without_post_reset_observation() -> None:
    facts = _facts(
        control=ControlFacts(action_completed_at=NOW - timedelta(minutes=1)),
        signals=SignalFacts(
            request_rate=_numeric(200, baseline=100, group="load"),
            cpu_utilization=_numeric(0.90, group="utilization"),
        ),
    )
    projection = evaluate_incident(facts, now=NOW)
    # Force recovery_verified true without observations to prove we do not invent.
    projection = projection.model_copy(update={"recovery_verified": True})
    snapshot = IncidentSnapshot(
        tenant_id="tenant-a",
        incident_id="incident-1",
        workflow_id="guardian/tenant-a/incident/incident-1",
        facts=facts,
        evaluation_times=(NOW,),
        projection_history=(projection,),
        projection=projection,
    )

    _, _, required_fresh = _scenario_evidence_lists(snapshot)
    gates = _scenario_safety_gates(snapshot)

    assert required_fresh == ()
    assert gates == ()


def test_recovery_telemetry_derived_from_post_reset_observation() -> None:
    handoff = NOW - timedelta(minutes=2)
    facts = _facts(
        control=ControlFacts(action_completed_at=handoff),
        signals=SignalFacts(
            request_rate=_numeric(200, baseline=100, group="load"),
            cpu_utilization=_numeric(0.90, group="utilization"),
        ),
    )
    observed_at = NOW
    observation = ObservationUpdate(
        tenant_id="tenant-a",
        incident_id="incident-1",
        observation_id="observation-1",
        sequence=1,
        window_key="post-reset-1",
        observed_at=observed_at,
        window_started_at=handoff + timedelta(seconds=1),
        telemetry=_telemetry(),
        service_healthy=True,
        required_conditions_satisfied=True,
        provenance_ref="adapter-observation/post-reset-window",
    )
    projection = evaluate_incident(facts, now=observed_at, observation=observation)
    assert projection.recovery_verified is True
    assert projection.workflow_state is WorkflowState.CLOSED
    snapshot = IncidentSnapshot(
        tenant_id="tenant-a",
        incident_id="incident-1",
        workflow_id="guardian/tenant-a/incident/incident-1",
        facts=facts,
        observations=(observation,),
        evaluation_times=(NOW - timedelta(minutes=1), observed_at),
        projection_history=(
            evaluate_incident(facts, now=NOW - timedelta(minutes=1)),
            projection,
        ),
        projection=projection,
    )

    _, _, required_fresh = _scenario_evidence_lists(snapshot)
    gates = _scenario_safety_gates(snapshot)

    assert required_fresh == (
        {
            "evidenceType": "recovery-telemetry",
            "subjectRole": "request-processor",
            "tenantRelation": "same-tenant",
            "freshness": "fresh",
        },
    )
    assert gates == ("post-action-evidence-for-recovery",)


def test_workflow_states_do_not_invent_closed_without_closed_projection() -> None:
    facts = _facts(
        signals=SignalFacts(
            request_rate=_numeric(200, baseline=100, group="load"),
            cpu_utilization=_numeric(0.90, group="utilization"),
        )
    )
    projection = evaluate_incident(facts, now=NOW)
    assert projection.proposed_action is ActionType.SCALE_UP
    assert projection.workflow_state is WorkflowState.CLASSIFIED
    snapshot = IncidentSnapshot(
        tenant_id="tenant-a",
        incident_id="incident-1",
        workflow_id="guardian/tenant-a/incident/incident-1",
        facts=facts,
        evaluation_times=(NOW,),
        projection_history=(projection,),
        projection=projection,
    )

    states = _scenario_workflow_states(snapshot)
    assert "active" in states
    assert "closed" not in states
    assert "assessment" not in states or "assessment" in {
        item.workflow_state.value for item in snapshot.projection_history
    }


def test_store_create_records_assessment_preamble_before_classified() -> None:
    from apps.guardian_api.service import GuardianService
    from apps.guardian_api.models import IncidentSubmission

    service = GuardianService()
    facts = _facts(
        signals=SignalFacts(
            request_rate=_numeric(200, baseline=100, group="load"),
            cpu_utilization=_numeric(0.90, group="utilization"),
        )
    )
    payload = facts.model_dump(mode="python")
    payload.pop("incident_id", None)
    submission = IncidentSubmission.model_validate(payload)

    created = service.submit_incident(
        "tenant-a", "honest-history-1", submission, now=NOW
    )
    assert created.projection.incident_class is IncidentClass.LOAD_SPIKE
    assert created.projection.workflow_state is WorkflowState.CLASSIFIED
    states = [item.workflow_state for item in created.projection_history]
    assert WorkflowState.ASSESSMENT in states
    assert states[-1] is WorkflowState.CLASSIFIED
    snapshot_states = _scenario_workflow_states(created)
    assert snapshot_states[0] == "active"
    assert "assessment" in snapshot_states
    assert "classified" in snapshot_states
    assert "closed" not in snapshot_states
