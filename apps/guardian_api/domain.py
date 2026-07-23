"""Deterministic Guardian incident evaluation."""

from __future__ import annotations

from datetime import datetime, timedelta

from apps.guardian_api.models import (
    ActionType,
    CriticalIntegrityFailure,
    EvidenceFreshness,
    GuardianProjection,
    IncidentClass,
    IncidentFacts,
    IncidentSeverity,
    ObservationUpdate,
    PolicyDecision,
    PolicyState,
    ScalerResult,
    TelemetryFacts,
    WorkflowState,
)
from apps.guardian_api.rules import (
    ALLOWED_SOURCES_BY_SIGNAL,
    RULE_DEFINITION,
    score_hypotheses,
)

TARGET_CORRELATED_SIGNALS = {
    "request_rate",
    "cpu_utilization",
    "memory_utilization",
    "throttling_ratio",
    "oom_killed",
    "restart_delta",
    "deployment_version",
    "error_rate",
    "p95_latency_ms",
    "topology_edge",
}


def _integrity_failures(
    telemetry: TelemetryFacts,
    *,
    identity_resolved: bool,
    observed_at: datetime,
    now: datetime,
) -> tuple[CriticalIntegrityFailure, ...]:
    failures: list[CriticalIntegrityFailure] = []
    if not identity_resolved:
        failures.append(CriticalIntegrityFailure.IDENTITY_MISSING)
    if telemetry.identity_conflict:
        failures.append(CriticalIntegrityFailure.IDENTITY_CONFLICT)
    if now - telemetry.newest_required_sample_at > timedelta(
        seconds=2 * telemetry.freshness_seconds
    ):
        failures.append(CriticalIntegrityFailure.SAMPLE_STALE)
    if (
        telemetry.newest_required_sample_at - observed_at > timedelta(seconds=60)
        or telemetry.newest_required_sample_at - now > timedelta(seconds=60)
        or observed_at - now > timedelta(seconds=60)
        or abs(telemetry.clock_skew_seconds) > 60
    ):
        failures.append(CriticalIntegrityFailure.TIMESTAMP_SKEW)
    if telemetry.usable_sample_count == 0:
        failures.append(CriticalIntegrityFailure.ZERO_SAMPLES)
    if not telemetry.pipeline_available:
        failures.append(CriticalIntegrityFailure.PIPELINE_UNAVAILABLE)
    if not telemetry.comparison_valid:
        failures.append(CriticalIntegrityFailure.COMPARISON_INVALID)
    return tuple(failures)


def _has_conflict(actions: tuple[ActionType, ...]) -> bool:
    return len(set(actions)) > 1


def _discriminatory_groups(hypotheses: tuple) -> tuple[str, ...]:
    groups: list[str] = []
    for hypothesis in sorted(hypotheses, key=lambda item: item.name.value):
        groups.extend(RULE_DEFINITION.hypotheses[hypothesis.name].discriminatory_groups)
    return tuple(dict.fromkeys(groups))


def _evidence_integrity_failures(
    facts: IncidentFacts, *, now: datetime
) -> tuple[CriticalIntegrityFailure, ...]:
    failures: list[CriticalIntegrityFailure] = []
    evidence_items = facts.signals.evidence_items()
    evidence_by_signal = dict(evidence_items)
    required_signals = {
        signal.value if hasattr(signal, "value") else str(signal)
        for signal in facts.telemetry.required_signals
    }
    if any(item.usable_samples == 0 for _, item in evidence_items):
        failures.append(CriticalIntegrityFailure.ZERO_SAMPLES)
    for signal_name, evidence in evidence_items:
        if evidence.source not in ALLOWED_SOURCES_BY_SIGNAL[signal_name]:
            failures.append(CriticalIntegrityFailure.COMPARISON_INVALID)
        if evidence.freshness in {
            EvidenceFreshness.CONFLICTING,
            EvidenceFreshness.MISSING,
        }:
            failures.append(CriticalIntegrityFailure.COMPARISON_INVALID)
        if evidence.freshness is EvidenceFreshness.STALE:
            failures.append(CriticalIntegrityFailure.SAMPLE_STALE)
        if evidence.observed_at - now > timedelta(
            seconds=60
        ) or evidence.observed_at - facts.observed_at > timedelta(seconds=60):
            failures.append(CriticalIntegrityFailure.TIMESTAMP_SKEW)
    for signal_name in required_signals:
        if signal_name == "telemetry_quality":
            continue
        evidence = evidence_by_signal.get(signal_name)
        if (
            evidence is None
            or evidence.usable_samples == 0
            or evidence.freshness is EvidenceFreshness.MISSING
            or evidence.source not in ALLOWED_SOURCES_BY_SIGNAL[signal_name]
        ):
            if CriticalIntegrityFailure.ZERO_SAMPLES not in failures:
                failures.append(CriticalIntegrityFailure.ZERO_SAMPLES)
            continue
        if evidence.observed_at - now > timedelta(
            seconds=60
        ) or evidence.observed_at - facts.observed_at > timedelta(seconds=60):
            if CriticalIntegrityFailure.TIMESTAMP_SKEW not in failures:
                failures.append(CriticalIntegrityFailure.TIMESTAMP_SKEW)
        if (
            evidence.freshness is EvidenceFreshness.STALE
            or now - evidence.observed_at
            > timedelta(seconds=2 * facts.telemetry.freshness_seconds)
        ):
            if CriticalIntegrityFailure.SAMPLE_STALE not in failures:
                failures.append(CriticalIntegrityFailure.SAMPLE_STALE)
        if evidence.freshness is EvidenceFreshness.CONFLICTING:
            failures.append(CriticalIntegrityFailure.COMPARISON_INVALID)
    if facts.identity is not None and any(
        name in TARGET_CORRELATED_SIGNALS
        and (
            item.subject_role != facts.identity.target_role
            or item.environment != facts.identity.environment
            or item.namespace != facts.identity.namespace
            or item.workload_kind != facts.identity.workload_kind
            or item.workload_name != facts.identity.workload_name
            or item.service_name != facts.identity.service_name
        )
        for name, item in evidence_items
    ):
        failures.append(CriticalIntegrityFailure.IDENTITY_CONFLICT)
    dependency = facts.signals.dependency_healthy
    if (
        dependency is not None
        and dependency.subject_role != "dependency"
        and CriticalIntegrityFailure.IDENTITY_CONFLICT not in failures
    ):
        failures.append(CriticalIntegrityFailure.IDENTITY_CONFLICT)
    deployment = facts.signals.deployment_version
    identity = facts.identity
    if deployment is not None and (
        identity is None
        or identity.image_digest is None
        and identity.service_version is None
        or deployment.subject_role != identity.target_role
        or deployment.environment != identity.environment
        or deployment.namespace != identity.namespace
        or deployment.workload_kind != identity.workload_kind
        or deployment.workload_name != identity.workload_name
        or deployment.service_name != identity.service_name
        or identity.image_digest is not None
        and deployment.current_digest != identity.image_digest
        or identity.service_version is not None
        and deployment.current_service_version != identity.service_version
    ):
        failure = (
            CriticalIntegrityFailure.IDENTITY_MISSING
            if identity is None
            or identity.image_digest is None
            and identity.service_version is None
            else CriticalIntegrityFailure.IDENTITY_CONFLICT
        )
        if failure not in failures:
            failures.append(failure)
    return tuple(failures)


def _recovery_verified(
    facts: IncidentFacts,
    observation: ObservationUpdate | None,
    *,
    now: datetime,
    assessment_healthy: bool,
    foreign_evidence: bool,
) -> bool:
    completed = facts.control.action_completed_at
    if (
        observation is None
        or completed is None
        or not assessment_healthy
        or foreign_evidence
    ):
        return False
    if (
        observation.tenant_id != facts.tenant_id
        or observation.incident_id != facts.incident_id
        or observation.observed_at <= completed
        or observation.window_started_at <= completed
        or observation.window_started_at > observation.observed_at
        or observation.window_started_at > now
        or observation.telemetry.newest_required_sample_at
        < observation.window_started_at
        or observation.telemetry.newest_required_sample_at > observation.observed_at
        or observation.telemetry.newest_required_sample_at > now
        or observation.observed_at > now
        or not observation.service_healthy
        or not observation.required_conditions_satisfied
    ):
        return False
    failures = _integrity_failures(
        observation.telemetry,
        identity_resolved=facts.identity is not None,
        observed_at=observation.observed_at,
        now=now,
    )
    return observation.telemetry.quality >= 0.80 and not failures


def evaluate_incident(
    facts: IncidentFacts,
    *,
    now: datetime,
    observation: ObservationUpdate | None = None,
) -> GuardianProjection:
    """Produce a fail-closed, side-effect-free incident projection."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("evaluation time must be timezone-aware")

    identity_resolved = facts.identity is not None
    failures = _integrity_failures(
        facts.telemetry,
        identity_resolved=identity_resolved,
        observed_at=facts.observed_at,
        now=now,
    )
    for failure in _evidence_integrity_failures(facts, now=now):
        if failure not in failures:
            failures = (*failures, failure)
    foreign_evidence = any(
        item.tenant_id != facts.tenant_id for item in facts.signals.all_evidence()
    ) or (facts.scaler is not None and facts.scaler.tenant_id != facts.tenant_id)
    if foreign_evidence and CriticalIntegrityFailure.IDENTITY_CONFLICT not in failures:
        failures = (*failures, CriticalIntegrityFailure.IDENTITY_CONFLICT)
    telemetry_healthy = facts.telemetry.quality >= 0.80 and not failures
    score_input = facts.signals if not foreign_evidence else type(facts.signals)()
    hypotheses = score_hypotheses(
        score_input,
        telemetry_healthy=telemetry_healthy,
        identity_resolved=identity_resolved and not foreign_evidence,
        now=now,
        freshness_seconds=facts.telemetry.freshness_seconds,
    )
    eligible = tuple(item for item in hypotheses if item.eligible)
    eligible_actions = tuple(
        dict.fromkeys(RULE_DEFINITION.hypotheses[item.name].action for item in eligible)
    )
    forbidden_actions: list[ActionType] = []
    permitted_actions: tuple[ActionType, ...] = ()
    proposed_action: ActionType | None = None
    incident_class: IncidentClass | None = None
    terminal_reason: str | None = None
    requested_evidence_groups: tuple[str, ...] = ()
    escalation_required = False
    workflow_state = WorkflowState.ASSESSMENT
    policy_decision = PolicyDecision.DENIED

    if failures or facts.telemetry.quality < 0.80:
        incident_class = IncidentClass.TELEMETRY_FAILURE
        workflow_state = WorkflowState.TELEMETRY_FAILURE
        if foreign_evidence:
            terminal_reason = "tenant-mismatch"
        forbidden_actions.extend(
            (
                ActionType.SCALE_UP,
                ActionType.SCALE_DOWN,
                ActionType.ROLLBACK,
                ActionType.PROTECT_DEPENDENCY,
            )
        )
        permitted_actions = (
            ActionType.INVESTIGATE,
            ActionType.ALERT,
            ActionType.SCALER_PAUSE,
        )
    elif foreign_evidence:
        terminal_reason = "tenant-mismatch"
    elif eligible:
        ranked = sorted(
            eligible,
            key=lambda item: (-item.deterministic_score, item.name.value),
        )
        winner = ranked[0]
        near_tie = (
            len(ranked) > 1
            and winner.deterministic_score - ranked[1].deterministic_score <= 0.03
        )
        incident_class = IncidentClass(winner.name.value)
        workflow_state = WorkflowState.CLASSIFIED
        if _has_conflict(eligible_actions) or near_tie:
            workflow_state = WorkflowState.CONFLICT_RESOLUTION
            requested_evidence_groups = _discriminatory_groups(eligible)
            conflict_started = facts.evidence_pass.conflict_started_at
            conflict_budget_exhausted = (
                facts.evidence_pass.completed_conflict_passes >= 2
                or conflict_started is not None
                and now - conflict_started >= timedelta(minutes=10)
            )
            if conflict_budget_exhausted:
                proposed_action = ActionType.CONTINUE_INVESTIGATION
                terminal_reason = "conflict-unresolved"
                escalation_required = facts.severity is IncidentSeverity.CRITICAL
        else:
            proposed_action = RULE_DEFINITION.hypotheses[winner.name].action
            policy_decision = PolicyDecision.APPROVAL_REQUIRED
    elif (
        facts.evidence_pass.completed_passes >= 2
        or now - facts.evidence_pass.started_at >= timedelta(minutes=10)
    ):
        incident_class = IncidentClass.UNKNOWN
        workflow_state = WorkflowState.UNKNOWN

    action_time = facts.control.action_attempted_at or now
    policy_current = (
        facts.policy.state is PolicyState.FRESH
        and facts.policy.evaluated_at <= now
        and facts.policy.evaluated_at <= action_time
        and action_time - facts.policy.evaluated_at <= timedelta(seconds=30)
    )
    if proposed_action not in {None, ActionType.CONTINUE_INVESTIGATION}:
        if not policy_current:
            proposed_action = None
            policy_decision = PolicyDecision.DENIED
            terminal_reason = "policy-unusable"

    proposal_expires_at = None
    if facts.control.proposal_created_at is not None:
        proposal_expires_at = facts.control.proposal_created_at + timedelta(
            seconds=facts.control.proposal_ttl_seconds
        )
    approval_expires_at = None
    if facts.control.approval_issued_at is not None:
        assert facts.control.approval_expires_at is not None
        candidates = [
            facts.control.approval_expires_at,
            facts.control.approval_issued_at + timedelta(minutes=10),
        ]
        if proposal_expires_at is not None:
            candidates.append(proposal_expires_at)
        approval_expires_at = min(candidates)

    approval_nonce_expires_at = None
    if facts.control.approval_nonce_issued_at is not None:
        assert facts.control.approval_nonce_expires_at is not None
        approval_nonce_expires_at = min(
            facts.control.approval_nonce_expires_at,
            facts.control.approval_nonce_issued_at + timedelta(minutes=5),
        )

    attempted = action_time
    expired = (
        (proposal_expires_at is not None and attempted >= proposal_expires_at)
        or (approval_expires_at is not None and attempted >= approval_expires_at)
        or (
            approval_nonce_expires_at is not None
            and attempted >= approval_nonce_expires_at
        )
    )
    if expired:
        proposed_action = None
        policy_decision = PolicyDecision.DENIED
        terminal_reason = "approval-expired"

    fingerprints = facts.control
    if (
        fingerprints.protected_fingerprint is not None
        and fingerprints.protected_fingerprint != fingerprints.current_fingerprint
    ):
        proposed_action = None
        policy_decision = PolicyDecision.DENIED
        terminal_reason = "operator-drift"

    scaler_result = None
    if facts.scaler is not None:
        scaler_policy_current = (
            facts.policy.state is PolicyState.FRESH
            and facts.policy.evaluated_at <= now
            and now - facts.policy.evaluated_at <= timedelta(seconds=30)
        )
        if (
            facts.scaler.tenant_id != facts.tenant_id
            or facts.scaler.source_expires_at <= now
            or not scaler_policy_current
        ):
            scaler_result = ScalerResult.SAFE_HOLD
            forbidden_actions.append(ActionType.SCALE_DOWN)
        else:
            scaler_result = ScalerResult.FRESH_VALUE

    return GuardianProjection(
        rules_version=RULE_DEFINITION.version,
        incident_class=incident_class,
        telemetry_healthy=telemetry_healthy,
        integrity_failures=failures,
        hypotheses=hypotheses,
        eligible_actions=eligible_actions,
        permitted_actions=permitted_actions,
        forbidden_actions=tuple(dict.fromkeys(forbidden_actions)),
        proposed_action=proposed_action,
        workflow_state=workflow_state,
        policy_decision=policy_decision,
        terminal_reason=terminal_reason,
        requested_evidence_groups=requested_evidence_groups,
        proposal_expires_at=proposal_expires_at,
        approval_expires_at=approval_expires_at,
        approval_nonce_expires_at=approval_nonce_expires_at,
        foreign_evidence_rejected=foreign_evidence,
        scaler_result=scaler_result,
        recovery_verified=_recovery_verified(
            facts,
            observation,
            now=now,
            assessment_healthy=telemetry_healthy,
            foreign_evidence=foreign_evidence,
        ),
        escalation_required=escalation_required,
    )
