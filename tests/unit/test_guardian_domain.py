"""Unit contracts for the deterministic Guardian incident evaluator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

import apps.guardian_api.models as guardian_models
from apps.guardian_api.domain import evaluate_incident
from apps.guardian_api.models import (
    ActionType,
    BooleanEvidence,
    ControlFacts,
    CriticalIntegrityFailure,
    EvidenceFreshness,
    EvidencePass,
    EvidenceSource,
    GuardianProjection,
    HypothesisName,
    IncidentClass,
    IncidentFacts,
    IncidentSeverity,
    NumericEvidence,
    ObservationUpdate,
    PolicyFacts,
    PolicyState,
    ScalerDirection,
    ScalerFacts,
    ScalerResult,
    SignalFacts,
    TargetIdentity,
    TelemetryFacts,
    VersionEvidence,
    WorkflowState,
)
from apps.guardian_api.rules import RULE_DEFINITION


NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def numeric(
    value: float,
    *,
    baseline: float | None = None,
    group: str = "metric",
    confidence: float = 1.0,
    tenant: str = "tenant-a",
    subject_role: str = "request-processor",
    source: EvidenceSource = EvidenceSource.QUERY_CONTRACT,
) -> NumericEvidence:
    expected_samples = 10_000
    return NumericEvidence(
        tenant_id=tenant,
        subject_role=subject_role,
        environment="production",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="processor",
        service_name="processor",
        value=value,
        baseline_value=baseline,
        observed_at=NOW,
        freshness=EvidenceFreshness.FRESH,
        source=source,
        provenance_ref="query-contract/sample",
        independence_group=group,
        expected_samples=expected_samples,
        usable_samples=round(confidence * expected_samples),
    )


def boolean(
    value: bool,
    *,
    group: str,
    tenant: str = "tenant-a",
) -> BooleanEvidence:
    return BooleanEvidence(
        tenant_id=tenant,
        subject_role="dependency" if group == "dependency" else "request-processor",
        environment="production",
        namespace="dependencies" if group == "dependency" else "payments",
        workload_kind="Deployment",
        workload_name="database" if group == "dependency" else "processor",
        service_name="database" if group == "dependency" else "processor",
        value=value,
        observed_at=NOW,
        freshness=EvidenceFreshness.FRESH,
        source=EvidenceSource.CONTROL_PLANE,
        provenance_ref=f"control/{group}",
        independence_group=group,
        expected_samples=10,
        usable_samples=10,
    )


def version(previous: str = DIGEST_A, current: str = DIGEST_B) -> VersionEvidence:
    return VersionEvidence(
        tenant_id="tenant-a",
        subject_role="request-processor",
        environment="production",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="processor",
        service_name="processor",
        previous_digest=previous,
        current_digest=current,
        observed_at=NOW,
        freshness=EvidenceFreshness.FRESH,
        source=EvidenceSource.DEPLOYMENT_EVENT,
        provenance_ref="deployment/revision-2",
        independence_group="deployment",
        expected_samples=1,
        usable_samples=1,
    )


def base_facts(**changes: object) -> IncidentFacts:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "incident_id": "incident-1",
        "observed_at": NOW,
        "identity": TargetIdentity(
            target_role="request-processor",
            environment="production",
            namespace="payments",
            workload_kind="Deployment",
            workload_name="processor",
            service_name="processor",
            image_digest=DIGEST_B,
        ),
        "telemetry": TelemetryFacts(
            quality=1.0,
            newest_required_sample_at=NOW,
            freshness_seconds=60,
            required_signals=frozenset(
                {guardian_models.RequiredSignal.TELEMETRY_QUALITY}
            ),
            clock_skew_seconds=0,
            required_sample_count=10,
            usable_sample_count=10,
            pipeline_available=True,
            comparison_valid=True,
        ),
        "evidence_pass": EvidencePass(completed_passes=1, started_at=NOW),
        "signals": SignalFacts(),
        "policy": PolicyFacts(state=PolicyState.FRESH, evaluated_at=NOW),
        "control": ControlFacts(),
    }
    values.update(changes)
    return IncidentFacts.model_validate(values)


def score(projection: GuardianProjection, name: HypothesisName):
    return next(item for item in projection.hypotheses if item.name is name)


def test_projection_and_hypotheses_use_the_versioned_rule_definition() -> None:
    projection = evaluate_incident(base_facts(), now=NOW)
    assert projection.rules_version == RULE_DEFINITION.version
    assert set(RULE_DEFINITION.hypotheses) == set(HypothesisName)


def test_healthy_elevated_load_has_no_action() -> None:
    facts = base_facts(
        signals=SignalFacts(
            request_rate=numeric(200, baseline=100, group="traffic"),
            cpu_utilization=numeric(0.50, group="resource"),
            memory_utilization=numeric(0.55, group="resource"),
        )
    )
    projection = evaluate_incident(facts, now=NOW)
    assert projection.incident_class is None
    assert projection.proposed_action is None
    assert projection.executed_mutations == 0
    assert score(projection, HypothesisName.LOAD_SPIKE).deterministic_score == 0


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        (
            SignalFacts(
                request_rate=numeric(200, baseline=100, group="load"),
                cpu_utilization=numeric(0.85, group="util"),
            ),
            HypothesisName.LOAD_SPIKE,
        ),
        (
            SignalFacts(
                deployment_version=version(),
                error_rate=numeric(0.05, baseline=0.01, group="errors"),
            ),
            HypothesisName.DEPLOYMENT_REGRESSION,
        ),
        (
            SignalFacts(
                cpu_utilization=numeric(0.85, group="util"),
                throttling_ratio=numeric(0.20, group="pressure"),
            ),
            HypothesisName.RESOURCE_SATURATION,
        ),
        (
            SignalFacts(
                topology_edge=boolean(True, group="topology"),
                dependency_healthy=boolean(False, group="dependency"),
            ),
            HypothesisName.DEPENDENCY_FAILURE,
        ),
    ],
)
def test_supported_hypotheses_are_deterministically_eligible(
    signals: SignalFacts, expected: HypothesisName
) -> None:
    """TST-GRD-REA-001-UNIT; TST-GRD-REA-002-UNIT; TST-GRD-REA-003-UNIT."""
    projection = evaluate_incident(base_facts(signals=signals), now=NOW)
    hypothesis = score(projection, expected)
    assert hypothesis.eligible
    assert hypothesis.evidence_confidence >= 0.85
    assert projection.incident_class == IncidentClass(expected.value)
    assert projection.executed_mutations == 0


def test_rollback_requires_correlated_version_identity() -> None:
    identity = base_facts().identity
    assert identity is not None
    unresolved = identity.model_copy(
        update={"image_digest": None, "service_version": None}
    )
    projection = evaluate_incident(
        base_facts(
            identity=unresolved,
            signals=SignalFacts(
                deployment_version=version(),
                error_rate=numeric(0.05, baseline=0.01, group="errors"),
            ),
        ),
        now=NOW,
    )
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert projection.proposed_action is not ActionType.ROLLBACK

    conflicting = evaluate_incident(
        base_facts(
            signals=SignalFacts(
                deployment_version=version().model_copy(
                    update={"environment": "staging"}
                ),
                error_rate=numeric(0.05, baseline=0.01, group="errors"),
            )
        ),
        now=NOW,
    )
    assert conflicting.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert CriticalIntegrityFailure.IDENTITY_CONFLICT in conflicting.integrity_failures

    version_only_identity = identity.model_copy(
        update={"image_digest": None, "service_version": "v2"}
    )
    uncorrelated_version = evaluate_incident(
        base_facts(
            identity=version_only_identity,
            signals=SignalFacts(
                deployment_version=version().model_copy(
                    update={"current_service_version": "v3"}
                ),
                error_rate=numeric(0.05, baseline=0.01, group="errors"),
            ),
        ),
        now=NOW,
    )
    assert uncorrelated_version.incident_class is IncidentClass.TELEMETRY_FAILURE

    dual_identity = identity.model_copy(update={"service_version": "v2"})
    dual_conflict = evaluate_incident(
        base_facts(
            identity=dual_identity,
            signals=SignalFacts(
                deployment_version=version().model_copy(
                    update={"current_service_version": "v3"}
                ),
                error_rate=numeric(0.05, baseline=0.01, group="errors"),
            ),
        ),
        now=NOW,
    )
    assert dual_conflict.incident_class is IncidentClass.TELEMETRY_FAILURE


def test_optional_conflicting_or_future_evidence_fails_closed() -> None:
    conflicting = numeric(1).model_copy(
        update={"freshness": EvidenceFreshness.CONFLICTING}
    )
    future = numeric(1).model_copy(update={"observed_at": NOW + timedelta(seconds=61)})
    for signals in (
        SignalFacts(request_rate=conflicting),
        SignalFacts(request_rate=future),
    ):
        projection = evaluate_incident(base_facts(signals=signals), now=NOW)
        assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE


@pytest.mark.parametrize(
    "freshness", [EvidenceFreshness.STALE, EvidenceFreshness.MISSING]
)
def test_optional_unusable_evidence_blocks_otherwise_executable_action(
    freshness: EvidenceFreshness,
) -> None:
    projection = evaluate_incident(
        base_facts(
            signals=SignalFacts(
                request_rate=numeric(200, baseline=100, group="load"),
                cpu_utilization=numeric(0.85, group="util"),
                error_rate=numeric(0.05, baseline=0.01, group="errors").model_copy(
                    update={"freshness": freshness}
                ),
            )
        ),
        now=NOW,
    )
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert projection.proposed_action is None


def test_sample_counts_cannot_exceed_expected() -> None:
    payload = numeric(1).model_dump()
    payload["usable_samples"] = payload["expected_samples"] + 1
    with pytest.raises(ValidationError):
        NumericEvidence.model_validate(payload)
    telemetry = base_facts().telemetry.model_copy(
        update={"usable_sample_count": 11, "required_sample_count": 10}
    )
    with pytest.raises(ValidationError):
        TelemetryFacts.model_validate(telemetry.model_dump())


def test_policy_from_future_and_naive_evaluation_time_fail_closed() -> None:
    signals = SignalFacts(
        request_rate=numeric(200, baseline=100, group="load"),
        cpu_utilization=numeric(0.85, group="util"),
    )
    future_policy = evaluate_incident(
        base_facts(
            signals=signals,
            policy=PolicyFacts(
                state=PolicyState.FRESH,
                evaluated_at=NOW + timedelta(seconds=1),
            ),
        ),
        now=NOW,
    )
    assert future_policy.proposed_action is None
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_incident(base_facts(signals=signals), now=NOW.replace(tzinfo=None))


def test_score_lead_above_point_zero_three_is_final() -> None:
    """TST-GRD-REA-006-UNIT."""
    projection = evaluate_incident(
        base_facts(
            signals=SignalFacts(
                request_rate=numeric(200, baseline=100, group="load"),
                cpu_utilization=numeric(0.85, group="util"),
                throttling_ratio=numeric(0.20, group="pressure"),
            )
        ),
        now=NOW,
    )
    assert score(projection, HypothesisName.LOAD_SPIKE).deterministic_score == 0.8
    assert (
        score(projection, HypothesisName.RESOURCE_SATURATION).deterministic_score
        == 0.75
    )
    assert projection.workflow_state is WorkflowState.CLASSIFIED


def test_telemetry_failure_action_allowlist_is_non_mutating() -> None:
    """TST-GRD-CLS-005-UNIT."""
    telemetry = base_facts().telemetry.model_copy(update={"pipeline_available": False})
    projection = evaluate_incident(base_facts(telemetry=telemetry), now=NOW)
    assert set(projection.permitted_actions) == {
        ActionType.INVESTIGATE,
        ActionType.ALERT,
        ActionType.SCALER_PAUSE,
    }
    assert ActionType.PROTECT_DEPENDENCY in projection.forbidden_actions


def test_fault_execution_record_cannot_supply_exception_symptom() -> None:
    projection = evaluate_incident(
        base_facts(
            signals=SignalFacts(
                deployment_version=version(),
                error_rate=numeric(
                    0.10,
                    baseline=0.01,
                    group="errors",
                    source=EvidenceSource.FAULT_EXECUTION,
                ),
            )
        ),
        now=NOW,
    )
    hypothesis = score(projection, HypothesisName.DEPLOYMENT_REGRESSION)
    assert not hypothesis.eligible
    assert projection.proposed_action is not ActionType.ROLLBACK


def test_independence_group_contributes_only_once() -> None:
    """TST-GRD-REA-007-UNIT."""
    signals = SignalFacts(
        cpu_utilization=numeric(0.90, group="same-scrape"),
        memory_utilization=numeric(0.90, group="same-scrape"),
        throttling_ratio=numeric(0.20, group="pressure"),
    )
    hypothesis = score(
        evaluate_incident(base_facts(signals=signals), now=NOW),
        HypothesisName.RESOURCE_SATURATION,
    )
    assert hypothesis.support == pytest.approx(0.75)
    assert hypothesis.deterministic_score == pytest.approx(0.75)


@pytest.mark.parametrize(
    ("signals", "hypothesis", "eligible"),
    [
        (
            SignalFacts(
                request_rate=numeric(199.9, baseline=100, group="load"),
                cpu_utilization=numeric(0.85, group="util"),
            ),
            HypothesisName.LOAD_SPIKE,
            False,
        ),
        (
            SignalFacts(
                request_rate=numeric(200, baseline=100, group="load"),
                cpu_utilization=numeric(0.85, group="util"),
            ),
            HypothesisName.LOAD_SPIKE,
            True,
        ),
        (
            SignalFacts(
                deployment_version=version(),
                p95_latency_ms=numeric(300, baseline=200, group="latency"),
            ),
            HypothesisName.DEPLOYMENT_REGRESSION,
            True,
        ),
        (
            SignalFacts(
                cpu_utilization=numeric(0.85, group="util"),
                restart_delta=numeric(1, group="pressure"),
            ),
            HypothesisName.RESOURCE_SATURATION,
            True,
        ),
    ],
)
def test_rule_threshold_boundaries(
    signals: SignalFacts, hypothesis: HypothesisName, eligible: bool
) -> None:
    result = score(evaluate_incident(base_facts(signals=signals), now=NOW), hypothesis)
    assert result.eligible is eligible


def test_low_group_confidence_blocks_eligibility() -> None:
    weak_load = numeric(200, baseline=100, group="load", confidence=0.84)
    facts = base_facts(
        signals=SignalFacts(
            request_rate=weak_load,
            cpu_utilization=numeric(0.85, group="util"),
        )
    )
    hypothesis = score(evaluate_incident(facts, now=NOW), HypothesisName.LOAD_SPIKE)
    assert hypothesis.deterministic_score == pytest.approx(0.736)
    assert hypothesis.evidence_confidence == pytest.approx(0.84)
    assert not hypothesis.eligible


def test_marked_fresh_evidence_older_than_contract_cannot_support() -> None:
    old_rate = numeric(200, baseline=100, group="load").model_copy(
        update={"observed_at": NOW - timedelta(hours=24)}
    )
    projection = evaluate_incident(
        base_facts(
            signals=SignalFacts(
                request_rate=old_rate,
                cpu_utilization=numeric(0.85, group="util"),
            )
        ),
        now=NOW,
    )
    assert not score(projection, HypothesisName.LOAD_SPIKE).eligible


def test_zero_samples_on_candidate_evidence_is_critical() -> None:
    empty_rate = numeric(200, baseline=100, group="load").model_copy(
        update={"usable_samples": 0}
    )
    projection = evaluate_incident(
        base_facts(
            signals=SignalFacts(
                request_rate=empty_rate,
                cpu_utilization=numeric(0.85, group="util"),
            )
        ),
        now=NOW,
    )
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert CriticalIntegrityFailure.ZERO_SAMPLES in projection.integrity_failures


def test_absent_required_signal_is_critical_even_with_aggregate_samples() -> None:
    telemetry = base_facts().telemetry.model_copy(
        update={"required_signals": frozenset({"memory_utilization"})}
    )
    projection = evaluate_incident(
        base_facts(
            telemetry=telemetry,
            signals=SignalFacts(
                request_rate=numeric(200, baseline=100, group="load"),
                cpu_utilization=numeric(0.85, group="util"),
            ),
        ),
        now=NOW,
    )
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert CriticalIntegrityFailure.ZERO_SAMPLES in projection.integrity_failures


def test_required_signal_set_uses_a_typed_enum() -> None:
    required_signal = getattr(guardian_models, "RequiredSignal", None)
    assert required_signal is not None
    telemetry = base_facts().telemetry.model_copy(
        update={"required_signals": frozenset({required_signal.REQUEST_RATE})}
    )
    assert telemetry.required_signals == frozenset({required_signal.REQUEST_RATE})


def test_executable_incident_rejects_empty_required_signal_contract() -> None:
    payload = base_facts().model_dump()
    payload["telemetry"]["required_signals"] = frozenset()
    with pytest.raises(ValidationError):
        IncidentFacts.model_validate(payload)


def test_required_signal_marked_missing_is_not_present() -> None:
    required_signal = guardian_models.RequiredSignal
    telemetry = base_facts().telemetry.model_copy(
        update={"required_signals": frozenset({required_signal.MEMORY_UTILIZATION})}
    )
    missing_memory = numeric(0.5, group="memory").model_copy(
        update={"freshness": EvidenceFreshness.MISSING}
    )
    projection = evaluate_incident(
        base_facts(
            telemetry=telemetry,
            signals=SignalFacts(
                request_rate=numeric(200, baseline=100, group="load"),
                cpu_utilization=numeric(0.85, group="util"),
                memory_utilization=missing_memory,
            ),
        ),
        now=NOW,
    )
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert CriticalIntegrityFailure.ZERO_SAMPLES in projection.integrity_failures


def test_required_signal_more_than_sixty_seconds_in_future_has_clock_skew() -> None:
    required_signal = guardian_models.RequiredSignal
    telemetry = base_facts().telemetry.model_copy(
        update={"required_signals": frozenset({required_signal.MEMORY_UTILIZATION})}
    )
    future_memory = numeric(0.5, group="memory").model_copy(
        update={"observed_at": NOW + timedelta(seconds=61)}
    )
    projection = evaluate_incident(
        base_facts(
            telemetry=telemetry,
            signals=SignalFacts(
                request_rate=numeric(200, baseline=100, group="load"),
                cpu_utilization=numeric(0.85, group="util"),
                memory_utilization=future_memory,
            ),
        ),
        now=NOW,
    )
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert CriticalIntegrityFailure.TIMESTAMP_SKEW in projection.integrity_failures


@pytest.mark.parametrize(
    ("changes", "failure"),
    [
        ({"identity": None}, CriticalIntegrityFailure.IDENTITY_MISSING),
        (
            {
                "telemetry": TelemetryFacts(
                    quality=1,
                    newest_required_sample_at=NOW - timedelta(seconds=121),
                    freshness_seconds=60,
                    required_signals=frozenset(
                        {guardian_models.RequiredSignal.TELEMETRY_QUALITY}
                    ),
                    clock_skew_seconds=0,
                    required_sample_count=10,
                    usable_sample_count=10,
                    pipeline_available=True,
                    comparison_valid=True,
                )
            },
            CriticalIntegrityFailure.SAMPLE_STALE,
        ),
        (
            {
                "telemetry": TelemetryFacts(
                    quality=1,
                    newest_required_sample_at=NOW + timedelta(seconds=61),
                    freshness_seconds=60,
                    required_signals=frozenset(
                        {guardian_models.RequiredSignal.TELEMETRY_QUALITY}
                    ),
                    clock_skew_seconds=0,
                    required_sample_count=10,
                    usable_sample_count=10,
                    pipeline_available=True,
                    comparison_valid=True,
                )
            },
            CriticalIntegrityFailure.TIMESTAMP_SKEW,
        ),
        (
            {
                "telemetry": TelemetryFacts(
                    quality=1,
                    newest_required_sample_at=NOW,
                    freshness_seconds=60,
                    required_signals=frozenset(
                        {guardian_models.RequiredSignal.TELEMETRY_QUALITY}
                    ),
                    clock_skew_seconds=0,
                    required_sample_count=10,
                    usable_sample_count=0,
                    pipeline_available=True,
                    comparison_valid=True,
                )
            },
            CriticalIntegrityFailure.ZERO_SAMPLES,
        ),
    ],
)
def test_critical_telemetry_failure_precedes_causal_hypotheses(
    changes: dict[str, object], failure: CriticalIntegrityFailure
) -> None:
    """TST-GRD-CLS-001-UNIT; TST-GRD-CLS-002-UNIT; TST-GRD-CLS-003-UNIT."""
    changes["signals"] = SignalFacts(
        request_rate=numeric(200, baseline=100, group="load"),
        cpu_utilization=numeric(0.85, group="util"),
    )
    projection = evaluate_incident(base_facts(**changes), now=NOW)
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert failure in projection.integrity_failures
    assert projection.proposed_action is None


def test_quality_boundary_is_healthy_at_point_eight() -> None:
    telemetry = base_facts().telemetry.model_copy(update={"quality": 0.80})
    assert evaluate_incident(base_facts(telemetry=telemetry), now=NOW).telemetry_healthy
    assert not evaluate_incident(
        base_facts(telemetry=telemetry.model_copy(update={"quality": 0.799})),
        now=NOW,
    ).telemetry_healthy


def test_unknown_requires_two_passes_or_ten_minutes() -> None:
    """TST-GRD-CLS-004-UNIT; TST-GRD-CLS-006-UNIT."""
    initial = evaluate_incident(
        base_facts(
            evidence_pass=EvidencePass(
                completed_passes=1, started_at=NOW - timedelta(minutes=9)
            )
        ),
        now=NOW,
    )
    assert initial.incident_class is None
    by_pass = evaluate_incident(
        base_facts(evidence_pass=EvidencePass(completed_passes=2, started_at=NOW)),
        now=NOW + timedelta(minutes=1),
    )
    by_time = evaluate_incident(
        base_facts(
            evidence_pass=EvidencePass(
                completed_passes=1, started_at=NOW - timedelta(minutes=10)
            )
        ),
        now=NOW,
    )
    assert by_pass.incident_class is IncidentClass.UNKNOWN
    assert by_time.incident_class is IncidentClass.UNKNOWN


def test_unhealthy_telemetry_never_becomes_unknown() -> None:
    telemetry = base_facts().telemetry.model_copy(update={"pipeline_available": False})
    projection = evaluate_incident(
        base_facts(
            telemetry=telemetry,
            evidence_pass=EvidencePass(completed_passes=2, started_at=NOW),
        ),
        now=NOW + timedelta(minutes=10),
    )
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE


@pytest.mark.parametrize(
    ("update", "failure"),
    [
        (
            {"pipeline_available": False},
            CriticalIntegrityFailure.PIPELINE_UNAVAILABLE,
        ),
        (
            {"comparison_valid": False},
            CriticalIntegrityFailure.COMPARISON_INVALID,
        ),
    ],
)
def test_remaining_critical_integrity_statuses_fail_closed(
    update: dict[str, object], failure: CriticalIntegrityFailure
) -> None:
    telemetry = base_facts().telemetry.model_copy(update=update)
    projection = evaluate_incident(base_facts(telemetry=telemetry), now=NOW)
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert failure in projection.integrity_failures


def test_mutually_exclusive_actions_enter_conflict_resolution() -> None:
    """TST-GRD-ACT-001-UNIT; TST-GRD-ACT-002-UNIT; TST-GRD-ACT-005-UNIT."""
    signals = SignalFacts(
        request_rate=numeric(200, baseline=100, group="load"),
        cpu_utilization=numeric(0.85, group="util"),
        deployment_version=version(),
        error_rate=numeric(0.05, baseline=0.01, group="errors"),
    )
    projection = evaluate_incident(
        base_facts(
            signals=signals,
            evidence_pass=EvidencePass(
                completed_passes=1,
                started_at=NOW,
                completed_conflict_passes=2,
                conflict_started_at=NOW - timedelta(minutes=1),
            ),
            severity=IncidentSeverity.CRITICAL,
        ),
        now=NOW,
    )
    assert projection.workflow_state is WorkflowState.CONFLICT_RESOLUTION
    assert set(projection.eligible_actions) == {
        ActionType.SCALE_UP,
        ActionType.ROLLBACK,
    }
    assert projection.proposed_action is ActionType.CONTINUE_INVESTIGATION
    assert projection.executed_mutations == 0
    assert projection.escalation_required


def test_near_tie_without_model_cannot_choose_rollback_over_dependency() -> None:
    signals = SignalFacts(
        deployment_version=version(),
        error_rate=numeric(0.05, baseline=0.01, group="errors", confidence=0.95),
        topology_edge=boolean(True, group="topology"),
        dependency_healthy=boolean(False, group="dependency"),
    )
    projection = evaluate_incident(base_facts(signals=signals), now=NOW)
    assert projection.workflow_state is WorkflowState.CONFLICT_RESOLUTION
    assert projection.proposed_action is None


def test_near_tied_same_action_hypotheses_do_not_select_without_model() -> None:
    signals = SignalFacts(
        request_rate=numeric(200, baseline=100, group="load", confidence=0.85),
        cpu_utilization=numeric(0.85, group="util"),
        throttling_ratio=numeric(0.20, group="pressure", confidence=0.9143),
    )
    projection = evaluate_incident(base_facts(signals=signals), now=NOW)
    assert score(
        projection, HypothesisName.LOAD_SPIKE
    ).deterministic_score == pytest.approx(0.74)
    assert score(
        projection, HypothesisName.RESOURCE_SATURATION
    ).deterministic_score == pytest.approx(0.720005)
    assert projection.workflow_state is WorkflowState.CONFLICT_RESOLUTION
    assert projection.proposed_action is None


def test_conflict_collects_discriminatory_evidence_before_budget_exhaustion() -> None:
    """TST-GRD-ACT-003-UNIT; TST-GRD-ACT-004-UNIT."""
    signals = SignalFacts(
        request_rate=numeric(200, baseline=100, group="load"),
        cpu_utilization=numeric(0.85, group="util"),
        deployment_version=version(),
        error_rate=numeric(0.05, baseline=0.01, group="errors"),
    )
    projection = evaluate_incident(
        base_facts(
            signals=signals,
            evidence_pass=EvidencePass(
                completed_passes=1,
                started_at=NOW,
                completed_conflict_passes=1,
                conflict_started_at=NOW,
            ),
        ),
        now=NOW,
    )
    assert projection.workflow_state is WorkflowState.CONFLICT_RESOLUTION
    assert projection.proposed_action is None
    assert projection.terminal_reason is None
    assert projection.requested_evidence_groups == (
        "deployment",
        "exceptions|latency",
        "load",
        "utilization",
    )


def test_policy_is_fail_closed_and_must_be_recent() -> None:
    signals = SignalFacts(
        request_rate=numeric(200, baseline=100, group="load"),
        cpu_utilization=numeric(0.85, group="util"),
    )
    stale = evaluate_incident(
        base_facts(
            signals=signals,
            policy=PolicyFacts(
                state=PolicyState.FRESH,
                evaluated_at=NOW - timedelta(seconds=31),
            ),
        ),
        now=NOW,
    )
    unusable = evaluate_incident(
        base_facts(
            signals=signals,
            policy=PolicyFacts(state=PolicyState.FAIL_CLOSED, evaluated_at=NOW),
        ),
        now=NOW,
    )
    assert stale.proposed_action is None
    assert stale.terminal_reason == "policy-unusable"
    assert unusable.proposed_action is None


def test_policy_is_rechecked_against_execution_start() -> None:
    """TST-GRD-TTL-006-UNIT."""
    projection = evaluate_incident(
        base_facts(
            signals=SignalFacts(
                request_rate=numeric(200, baseline=100, group="load"),
                cpu_utilization=numeric(0.85, group="util"),
            ),
            control=ControlFacts(action_attempted_at=NOW + timedelta(seconds=31)),
        ),
        now=NOW,
    )
    assert projection.proposed_action is None
    assert projection.terminal_reason == "policy-unusable"


def test_proposal_and_approval_expiry_cannot_be_revived() -> None:
    """TST-GRD-TTL-003-UNIT; TST-GRD-TTL-005-UNIT."""
    signals = SignalFacts(
        request_rate=numeric(200, baseline=100, group="load"),
        cpu_utilization=numeric(0.85, group="util"),
    )
    control = ControlFacts(
        proposal_created_at=NOW,
        proposal_ttl_seconds=900,
        approval_issued_at=NOW + timedelta(minutes=8),
        approval_expires_at=NOW + timedelta(minutes=30),
        action_attempted_at=NOW + timedelta(minutes=16),
    )
    projection = evaluate_incident(
        base_facts(signals=signals, control=control),
        now=NOW + timedelta(minutes=16),
    )
    assert projection.proposal_expires_at == NOW + timedelta(minutes=15)
    assert projection.approval_expires_at == NOW + timedelta(minutes=15)
    assert projection.proposed_action is None
    assert projection.terminal_reason == "approval-expired"
    with pytest.raises(ValidationError):
        ControlFacts(proposal_ttl_seconds=1801)


def test_approval_nonce_is_valid_for_at_most_five_minutes() -> None:
    """TST-GRD-TTL-004-UNIT."""
    projection = evaluate_incident(
        base_facts(
            signals=SignalFacts(
                request_rate=numeric(200, baseline=100, group="load"),
                cpu_utilization=numeric(0.85, group="util"),
            ),
            control=ControlFacts(
                proposal_created_at=NOW,
                approval_issued_at=NOW,
                approval_expires_at=NOW + timedelta(minutes=10),
                approval_nonce_issued_at=NOW,
                approval_nonce_expires_at=NOW + timedelta(minutes=10),
                action_attempted_at=NOW + timedelta(minutes=6),
            ),
        ),
        now=NOW + timedelta(minutes=6),
    )
    assert projection.approval_nonce_expires_at == NOW + timedelta(minutes=5)
    assert projection.proposed_action is None
    assert projection.terminal_reason == "approval-expired"


def test_operator_drift_denies_stale_proposal() -> None:
    signals = SignalFacts(
        deployment_version=version(),
        error_rate=numeric(0.05, baseline=0.01, group="errors"),
    )
    projection = evaluate_incident(
        base_facts(
            signals=signals,
            control=ControlFacts(
                protected_fingerprint="before",
                current_fingerprint="after",
            ),
        ),
        now=NOW,
    )
    assert projection.proposed_action is None
    assert projection.terminal_reason == "operator-drift"


def test_foreign_tenant_evidence_is_rejected_before_scoring() -> None:
    signals = SignalFacts(
        request_rate=numeric(
            200, baseline=100, group="foreign-load", tenant="tenant-b"
        ),
        cpu_utilization=numeric(0.85, group="util"),
    )
    projection = evaluate_incident(base_facts(signals=signals), now=NOW)
    assert projection.foreign_evidence_rejected
    assert projection.terminal_reason == "tenant-mismatch"
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert CriticalIntegrityFailure.IDENTITY_CONFLICT in projection.integrity_failures
    assert not projection.telemetry_healthy
    assert all(not item.eligible for item in projection.hypotheses)


def test_stale_scaler_source_returns_safe_hold() -> None:
    projection = evaluate_incident(
        base_facts(
            scaler=ScalerFacts(
                tenant_id="tenant-a",
                source_value=0,
                source_expires_at=NOW - timedelta(seconds=1),
                requested_direction=ScalerDirection.DOWN,
            )
        ),
        now=NOW,
    )
    assert projection.scaler_result is ScalerResult.SAFE_HOLD
    assert ActionType.SCALE_DOWN in projection.forbidden_actions


def test_scaler_source_fails_closed_when_policy_is_not_fresh() -> None:
    projection = evaluate_incident(
        base_facts(
            policy=PolicyFacts(state=PolicyState.RESTRICTED, evaluated_at=NOW),
            scaler=ScalerFacts(
                tenant_id="tenant-a",
                source_value=3,
                source_expires_at=NOW + timedelta(minutes=1),
                requested_direction=ScalerDirection.DOWN,
            ),
        ),
        now=NOW,
    )
    assert projection.scaler_result is ScalerResult.SAFE_HOLD
    assert ActionType.SCALE_DOWN in projection.forbidden_actions


def test_scaler_policy_freshness_is_evaluated_at_poll_time() -> None:
    poll_time = NOW + timedelta(hours=1)
    telemetry = base_facts().telemetry.model_copy(
        update={"newest_required_sample_at": poll_time}
    )
    projection = evaluate_incident(
        base_facts(
            telemetry=telemetry,
            control=ControlFacts(action_attempted_at=NOW),
            scaler=ScalerFacts(
                tenant_id="tenant-a",
                source_value=3,
                source_expires_at=poll_time + timedelta(minutes=1),
                requested_direction=ScalerDirection.DOWN,
            ),
        ),
        now=poll_time,
    )
    assert projection.scaler_result is ScalerResult.SAFE_HOLD


def test_foreign_scaler_facts_are_rejected() -> None:
    projection = evaluate_incident(
        base_facts(
            scaler=ScalerFacts(
                tenant_id="tenant-b",
                source_value=3,
                source_expires_at=NOW + timedelta(minutes=1),
                requested_direction=ScalerDirection.UP,
            )
        ),
        now=NOW,
    )
    assert projection.scaler_result is ScalerResult.SAFE_HOLD
    assert projection.foreign_evidence_rejected


def test_fresh_later_observation_can_verify_recovery() -> None:
    facts = base_facts(
        control=ControlFacts(action_completed_at=NOW - timedelta(minutes=1))
    )
    update = ObservationUpdate(
        tenant_id="tenant-a",
        incident_id="incident-1",
        observation_id="recovery-2",
        sequence=2,
        window_key="recovery-window",
        observed_at=NOW,
        window_started_at=NOW - timedelta(seconds=30),
        telemetry=base_facts().telemetry,
        service_healthy=True,
        required_conditions_satisfied=True,
        provenance_ref="recovery/window-2",
    )
    assert evaluate_incident(facts, now=NOW, observation=update).recovery_verified
    stale = update.model_copy(update={"observed_at": NOW - timedelta(minutes=2)})
    assert not evaluate_incident(facts, now=NOW, observation=stale).recovery_verified

    repackaged = update.model_copy(
        update={
            "telemetry": update.telemetry.model_copy(
                update={"newest_required_sample_at": NOW - timedelta(minutes=2)}
            )
        }
    )
    assert not evaluate_incident(
        facts, now=NOW, observation=repackaged
    ).recovery_verified

    future_sample = update.model_copy(
        update={
            "telemetry": update.telemetry.model_copy(
                update={"newest_required_sample_at": NOW + timedelta(seconds=1)}
            )
        }
    )
    assert not evaluate_incident(
        facts, now=NOW, observation=future_sample
    ).recovery_verified


def test_foreign_assessment_evidence_cannot_verify_recovery() -> None:
    facts = base_facts(
        signals=SignalFacts(
            request_rate=numeric(200, baseline=100, group="load", tenant="tenant-b")
        ),
        control=ControlFacts(action_completed_at=NOW - timedelta(minutes=1)),
    )
    update = ObservationUpdate(
        tenant_id="tenant-a",
        incident_id="incident-1",
        observation_id="recovery-2",
        sequence=2,
        window_key="recovery-window",
        observed_at=NOW,
        window_started_at=NOW - timedelta(seconds=30),
        telemetry=base_facts().telemetry,
        service_healthy=True,
        required_conditions_satisfied=True,
        provenance_ref="recovery/window-2",
    )
    assert not evaluate_incident(facts, now=NOW, observation=update).recovery_verified


def test_one_independence_group_contributes_to_only_one_polarity() -> None:
    projection = evaluate_incident(
        base_facts(
            signals=SignalFacts(
                request_rate=numeric(200, baseline=100, group="shared"),
                cpu_utilization=numeric(0.50, group="shared"),
            )
        ),
        now=NOW,
    )
    hypothesis = score(projection, HypothesisName.LOAD_SPIKE)
    assert hypothesis.support == pytest.approx(0.4)
    assert hypothesis.contradiction == 0


def test_models_reject_scenario_fields_naive_time_and_mutable_identity() -> None:
    payload = base_facts().model_dump()
    payload["scenario_id"] = "expected-load-spike"
    with pytest.raises(ValidationError):
        IncidentFacts.model_validate(payload)
    payload = base_facts().model_dump()
    payload["expected_result"] = "rollback"
    with pytest.raises(ValidationError):
        IncidentFacts.model_validate(payload)
    with pytest.raises(ValidationError):
        base_facts(observed_at=datetime(2026, 7, 23, 8, 0))
    with pytest.raises(ValidationError):
        TargetIdentity(
            target_role="request-processor",
            environment="production",
            namespace="payments",
            workload_kind="Deployment",
            workload_name="processor",
            service_name="processor",
            image_digest="processor:latest",
        )
    evidence_payload = numeric(1).model_dump()
    evidence_payload["value"] = "1"
    with pytest.raises(ValidationError):
        NumericEvidence.model_validate(evidence_payload)


def test_numeric_evidence_rejects_negative_values_and_baselines() -> None:
    payload = numeric(1, baseline=1).model_dump()
    payload["value"] = -0.01
    with pytest.raises(ValidationError):
        NumericEvidence.model_validate(payload)
    payload["value"] = 1.0
    payload["baseline_value"] = -0.01
    with pytest.raises(ValidationError):
        NumericEvidence.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    [
        "cpu_utilization",
        "memory_utilization",
        "throttling_ratio",
        "error_rate",
    ],
)
def test_ratio_signals_reject_values_above_one(field: str) -> None:
    with pytest.raises(ValidationError):
        SignalFacts.model_validate({field: numeric(1.01).model_dump()})


def test_timestamp_skew_is_derived_from_sample_time() -> None:
    telemetry = base_facts().telemetry.model_copy(
        update={"newest_required_sample_at": NOW + timedelta(seconds=61)}
    )
    projection = evaluate_incident(base_facts(telemetry=telemetry), now=NOW)
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert CriticalIntegrityFailure.TIMESTAMP_SKEW in projection.integrity_failures


@pytest.mark.parametrize("clock_skew_seconds", [-61.0, 61.0])
def test_independently_measured_clock_skew_fails_in_either_direction(
    clock_skew_seconds: float,
) -> None:
    telemetry = base_facts().telemetry.model_copy(
        update={"clock_skew_seconds": clock_skew_seconds}
    )
    projection = evaluate_incident(base_facts(telemetry=telemetry), now=NOW)
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert CriticalIntegrityFailure.TIMESTAMP_SKEW in projection.integrity_failures


def test_observation_window_and_samples_cannot_be_future_relative_to_envelope() -> None:
    telemetry = base_facts().telemetry
    with pytest.raises(ValidationError):
        ObservationUpdate(
            tenant_id="tenant-a",
            incident_id="incident-1",
            observation_id="invalid-window",
            sequence=1,
            window_key="recovery-window",
            observed_at=NOW,
            window_started_at=NOW + timedelta(seconds=1),
            telemetry=telemetry,
            service_healthy=True,
            required_conditions_satisfied=True,
            provenance_ref="recovery/invalid-window",
        )
    with pytest.raises(ValidationError):
        ObservationUpdate(
            tenant_id="tenant-a",
            incident_id="incident-1",
            observation_id="invalid-sample",
            sequence=1,
            window_key="recovery-window",
            observed_at=NOW,
            window_started_at=NOW - timedelta(seconds=1),
            telemetry=telemetry.model_copy(
                update={"newest_required_sample_at": NOW + timedelta(seconds=1)}
            ),
            service_healthy=True,
            required_conditions_satisfied=True,
            provenance_ref="recovery/invalid-sample",
        )


@pytest.mark.parametrize(
    "missing",
    ["provenance_ref", "independence_group"],
)
def test_evidence_requires_provenance_and_independence(missing: str) -> None:
    payload = numeric(1).model_dump()
    del payload[missing]
    with pytest.raises(ValidationError):
        NumericEvidence.model_validate(payload)


def test_evidence_confidence_is_derived_only_from_sample_counts() -> None:
    payload = numeric(1).model_dump()
    payload["expected_samples"] = 4
    payload["usable_samples"] = 2
    evidence = NumericEvidence.model_validate(payload)
    assert evidence.usable_confidence == 0.5
    payload["confidence"] = 0.1
    with pytest.raises(ValidationError):
        NumericEvidence.model_validate(payload)
    del payload["confidence"]
    payload["usable_samples"] = 5
    with pytest.raises(ValidationError):
        NumericEvidence.model_validate(payload)


def test_identity_and_evidence_roles_are_explicit_and_digest_is_optional() -> None:
    identity = TargetIdentity(
        target_role="request-processor",
        environment="production",
        namespace="payments",
        workload_kind="Deployment",
        workload_name="processor",
        service_name="processor",
        service_version="2026.07.23",
    )
    payload = numeric(1).model_dump()
    payload["subject_role"] = "request-processor"
    evidence = NumericEvidence.model_validate(payload)
    assert identity.image_digest is None
    assert evidence.subject_role == identity.target_role


def test_target_role_mismatch_is_an_identity_conflict() -> None:
    payload = numeric(200, baseline=100, group="load").model_dump()
    payload["subject_role"] = "unrelated-role"
    rate = NumericEvidence.model_validate(payload)
    base_identity = base_facts().identity
    assert base_identity is not None
    identity_payload = base_identity.model_dump()
    identity_payload["target_role"] = "request-processor"
    identity = TargetIdentity.model_validate(identity_payload)
    projection = evaluate_incident(
        base_facts(
            identity=identity,
            signals=SignalFacts(
                request_rate=rate,
                cpu_utilization=numeric(0.85, group="util"),
            ),
        ),
        now=NOW,
    )
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert CriticalIntegrityFailure.IDENTITY_CONFLICT in projection.integrity_failures


@pytest.mark.parametrize(
    "identity_change",
    [
        {"environment": "staging"},
        {"namespace": "other"},
        {"workload_kind": "StatefulSet"},
        {"workload_name": "other-processor"},
        {"service_name": "other-service"},
    ],
)
def test_same_role_cross_target_evidence_is_an_identity_conflict(
    identity_change: dict[str, str],
) -> None:
    rate = numeric(200, baseline=100, group="load").model_copy(update=identity_change)
    projection = evaluate_incident(
        base_facts(
            signals=SignalFacts(
                request_rate=rate,
                cpu_utilization=numeric(0.85, group="util"),
            )
        ),
        now=NOW,
    )
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert CriticalIntegrityFailure.IDENTITY_CONFLICT in projection.integrity_failures


def test_dependency_health_requires_dependency_subject_role() -> None:
    projection = evaluate_incident(
        base_facts(
            signals=SignalFacts(
                topology_edge=boolean(True, group="topology"),
                dependency_healthy=boolean(False, group="dependency").model_copy(
                    update={"subject_role": "request-processor"}
                ),
            )
        ),
        now=NOW,
    )
    assert projection.incident_class is IncidentClass.TELEMETRY_FAILURE
    assert CriticalIntegrityFailure.IDENTITY_CONFLICT in projection.integrity_failures


def test_model_never_authorizes_or_executes_mutations() -> None:
    projection = evaluate_incident(
        base_facts(
            signals=SignalFacts(
                request_rate=numeric(200, baseline=100, group="load"),
                cpu_utilization=numeric(0.85, group="util"),
            )
        ),
        now=NOW,
    )
    assert projection.model_participated is False
    assert projection.executed_mutations == 0
