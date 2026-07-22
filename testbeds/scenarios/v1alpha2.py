"""Strict, environment-neutral GuardianScenario v1alpha2 test contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, PositiveInt, StringConstraints, model_validator

from testbeds.scenarios.models import (
    ActionType,
    BaselineRequirements,
    EvidenceType,
    IncidentClass,
    PolicyDecision,
    ScenarioMetadata,
    DeploymentStimulus,
    FaultStimulus,
    LoadStimulus,
    ScenarioTarget,
    StrictModel,
    WorkflowState,
)

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EnvironmentCapability(StrEnum):
    HEALTHY_BASELINE = "healthy-baseline"
    LOAD_GENERATION = "load-generation"
    FAULT_INJECTION = "fault-injection"
    DEPLOYMENT_TRANSITION = "deployment-transition"
    PROGRESSIVE_DELIVERY = "progressive-delivery"
    HORIZONTAL_SCALING = "horizontal-scaling"
    SCALE_TO_ZERO = "scale-to-zero"
    DEPENDENCY_OBSERVATION = "dependency-observation"
    RESOURCE_PRESSURE = "resource-pressure"
    TELEMETRY_INTERRUPTION = "telemetry-interruption"
    POLICY_BUNDLE_CONTROL = "policy-bundle-control"
    MANUAL_WORKLOAD_MUTATION = "manual-workload-mutation"
    MULTI_TENANT_FIXTURE = "multi-tenant-fixture"
    WORKFLOW_OBSERVATION = "workflow-observation"
    APPROVAL_CONTROL = "approval-control"
    RECOVERY_OBSERVATION = "recovery-observation"
    MUTATION_OBSERVATION = "mutation-observation"
    SCALER_OBSERVATION = "scaler-observation"
    INCIDENT_INGRESS_CONTROL = "incident-ingress-control"
    AMBIGUOUS_SYMPTOM = "ambiguous-symptom"


class ScaleDirection(StrEnum):
    UP = "up"
    DOWN = "down"
    ANY = "any"


class EvidenceFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    CONFLICTING = "conflicting"


class TenantRelation(StrEnum):
    SAME = "same-tenant"
    FOREIGN = "foreign-tenant"


class TelemetryQuality(StrEnum):
    HEALTHY = "healthy"
    FAILED = "failed"
    STALE = "stale"
    INCOMPLETE = "incomplete"


class SafetyGate(StrEnum):
    IDENTITY = "identity-before-version-action"
    FRESH_EVIDENCE = "fresh-evidence-before-eligibility"
    EXPIRY = "expiry-before-mutation"
    POLICY = "policy-before-mutation"
    DRIFT = "drift-before-each-mutation"
    TENANT_SCORING = "tenant-before-scoring"
    TENANT_IO = "tenant-before-external-io"
    RECOVERY = "post-action-evidence-for-recovery"


class RecoveryCondition(StrEnum):
    SERVICE_HEALTHY = "service-healthy"
    CAPACITY_CONVERGED = "capacity-converged"
    ERROR_IMPROVED = "error-improved"
    LATENCY_IMPROVED = "latency-improved"
    VERSION_RESTORED = "desired-version-restored"
    PRESSURE_CLEARED = "resource-pressure-cleared"
    POLICY_RESTORED = "policy-restored"


class AuditEventType(StrEnum):
    OBSERVATION = "observation-recorded"
    MUTATION = "mutation-executed"
    REJECTION = "action-rejected"
    DUPLICATE = "duplicate-event-received"
    TENANT_REJECTION = "tenant-reference-rejected"


class PolicyBundleState(StrEnum):
    FRESH = "fresh"
    RESTRICTED = "restricted"
    FAIL_CLOSED = "fail-closed"


class PolicyOperation(StrEnum):
    READ_ONLY_INVESTIGATION = "read-only-investigation"
    ALERT_ONLY = "alert-only"
    ROLLBACK = "rollback"
    SCALE_UP = "scale-up"
    SCALE_DOWN = "scale-down"
    SCALER_PAUSE = "scaler-pause"
    POLICY_ACTIVATION = "policy-activation"
    APPROVAL_ISSUANCE = "approval-issuance"


class WorkflowReason(StrEnum):
    APPROVAL_EXPIRED = "approval-expired"
    OPERATOR_DRIFT = "operator-drift"
    POLICY_UNUSABLE = "policy-unusable"
    CONFLICT_UNRESOLVED = "conflict-unresolved"
    TELEMETRY_UNUSABLE = "telemetry-unusable"
    TENANT_MISMATCH = "tenant-mismatch"


class TelemetryMode(StrEnum):
    INTERRUPTED = "interrupted"
    STALE = "stale"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"


class DeliveryMode(StrEnum):
    DUPLICATE = "duplicate"
    RETRIED = "retried"
    REORDERED = "reordered"


class TelemetryStimulus(StrictModel):
    mode: TelemetryMode


class IncidentDeliveryStimulus(StrictModel):
    count: int = Field(ge=2)
    mode: DeliveryMode


class ApprovalStimulus(StrictModel):
    attempt_after_expiry: Literal[True]


class OperatorDriftStimulus(StrictModel):
    protected_field_change_before_mutation: Literal[True]


class PolicyBundleStimulus(StrictModel):
    state: PolicyBundleState


class TenantInjectionStimulus(StrictModel):
    evidence_tenant_relation: Literal[TenantRelation.FOREIGN]


class AmbiguousSymptomStimulus(StrictModel):
    preserve_healthy_telemetry: Literal[True]


class ScenarioStimulusV1Alpha2(StrictModel):
    load: LoadStimulus | None = None
    fault: FaultStimulus | None = None
    deployment: DeploymentStimulus | None = None
    telemetry: TelemetryStimulus | None = None
    incident_delivery: IncidentDeliveryStimulus | None = None
    approval: ApprovalStimulus | None = None
    operator_drift: OperatorDriftStimulus | None = None
    policy_bundle: PolicyBundleStimulus | None = None
    tenant_injection: TenantInjectionStimulus | None = None
    ambiguous_symptom: AmbiguousSymptomStimulus | None = None


class EnvironmentRequirements(StrictModel):
    capabilities: frozenset[EnvironmentCapability]

    @model_validator(mode="after")
    def non_empty(self) -> Self:
        if not self.capabilities:
            raise ValueError("environment capabilities must not be empty")
        return self


class ScenarioTraceability(StrictModel):
    normative_requirements: tuple[NonEmptyString, ...] = ()
    acceptance_tests: tuple[NonEmptyString, ...] = ()


class EvidenceAssertion(StrictModel):
    evidence_type: EvidenceType
    subject_role: NonEmptyString | None = None
    tenant_relation: TenantRelation = TenantRelation.SAME
    freshness: EvidenceFreshness


class EvidenceExpectation(StrictModel):
    supporting: tuple[EvidenceAssertion, ...] = ()
    contradicting: tuple[EvidenceAssertion, ...] = ()
    required_fresh: tuple[EvidenceAssertion, ...] = ()


class ActionAssertion(StrictModel):
    action_type: ActionType
    scale_direction: ScaleDirection | None = None

    @model_validator(mode="after")
    def direction_matches_action(self) -> Self:
        if (self.action_type is ActionType.SCALE) != (self.scale_direction is not None):
            raise ValueError("scaleDirection is required only for scale actions")
        return self


class ActionExpectation(StrictModel):
    eligible: tuple[ActionAssertion, ...] = ()
    forbidden: tuple[ActionAssertion, ...] = ()
    proposed: ActionAssertion | None = None


class CardinalityExpectation(StrictModel):
    exact: int | None = Field(default=None, ge=0)
    at_most: int | None = Field(default=None, ge=0)
    at_least: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def exactly_one_bound(self) -> Self:
        if (
            sum(item is not None for item in (self.exact, self.at_most, self.at_least))
            != 1
        ):
            raise ValueError("cardinality requires exactly one bound")
        return self


class IncidentExpectation(StrictModel):
    incident_class: IncidentClass | None = None
    actionable: bool
    telemetry_quality: TelemetryQuality | None = None


class PolicyExpectation(StrictModel):
    decision: PolicyDecision
    bundle_state: PolicyBundleState | None = None
    fail_closed: bool
    permitted_operations: tuple[PolicyOperation, ...] = ()
    forbidden_operations: tuple[PolicyOperation, ...] = ()

    @model_validator(mode="after")
    def policy_is_fail_closed(self) -> Self:
        if set(self.permitted_operations) & set(self.forbidden_operations):
            raise ValueError("permitted and forbidden policy operations overlap")
        writes = {
            PolicyOperation.ROLLBACK,
            PolicyOperation.SCALE_UP,
            PolicyOperation.SCALE_DOWN,
            PolicyOperation.SCALER_PAUSE,
            PolicyOperation.POLICY_ACTIVATION,
            PolicyOperation.APPROVAL_ISSUANCE,
        }
        if self.bundle_state is PolicyBundleState.FAIL_CLOSED and (
            self.decision is not PolicyDecision.DENIED
            or not self.fail_closed
            or set(self.permitted_operations) & writes
        ):
            raise ValueError("fail-closed policy cannot authorize writes")
        return self


class WorkflowExpectation(StrictModel):
    required_states: tuple[WorkflowState, ...] = ()
    parent_count: CardinalityExpectation
    proposal_count: CardinalityExpectation
    approval_count: CardinalityExpectation
    terminal_reason: WorkflowReason | None = None


class MutationExpectation(StrictModel):
    count: CardinalityExpectation
    actions: tuple[ActionAssertion, ...] = ()
    target: ScenarioTarget | None = None


class AuditEventExpectation(StrictModel):
    event_type: AuditEventType
    count: CardinalityExpectation


class AuditExpectation(StrictModel):
    events: tuple[AuditEventExpectation, ...] = ()


class TenantIsolationExpectation(StrictModel):
    reject_foreign_evidence_before_scoring: bool
    reject_mismatch_before_external_io: bool
    tenant_scoped_workflow_identity: bool
    tenant_scoped_deduplication: bool
    tenant_scoped_cache_and_topology: bool
    tenant_scoped_approval: bool


class ScalerResultKind(StrEnum):
    ERROR = "error"
    SAFE_HOLD = "safe-hold"


class ScalerExpectation(StrictModel):
    result: ScalerResultKind
    fabricated_zero_forbidden: Literal[True]
    scale_down_forbidden: Literal[True]
    gateway_convergence_required: bool = False


class RecoveryExpectationV1Alpha2(StrictModel):
    contract_ref: NonEmptyString
    contract_version: PositiveInt
    registry_version: NonEmptyString
    require_fresh_telemetry: Literal[True]
    evidence: tuple[EvidenceAssertion, ...]
    conditions: tuple[RecoveryCondition, ...]
    minimum_post_action_windows: PositiveInt

    @model_validator(mode="after")
    def observable(self) -> Self:
        if not self.evidence or not self.conditions:
            raise ValueError("recovery requires evidence and conditions")
        if any(item.freshness is not EvidenceFreshness.FRESH for item in self.evidence):
            raise ValueError("recovery evidence must be fresh")
        return self


class ExpectedOutcomeV1Alpha2(StrictModel):
    incident: IncidentExpectation
    evidence: EvidenceExpectation = Field(default_factory=EvidenceExpectation)
    actions: ActionExpectation = Field(default_factory=ActionExpectation)
    policy: PolicyExpectation
    workflow: WorkflowExpectation
    mutations: MutationExpectation
    audit: AuditExpectation = Field(default_factory=AuditExpectation)
    tenant_isolation: TenantIsolationExpectation | None = None
    safety_gates: tuple[SafetyGate, ...] = ()
    scaler: ScalerExpectation | None = None
    recovery: RecoveryExpectationV1Alpha2 | None = None

    @model_validator(mode="after")
    def safety_invariants(self) -> Self:
        def overlaps(left: ActionAssertion, right: ActionAssertion) -> bool:
            if left.action_type is not right.action_type:
                return False
            if left.action_type is not ActionType.SCALE:
                return True
            return (
                ScaleDirection.ANY in {left.scale_direction, right.scale_direction}
                or left.scale_direction is right.scale_direction
            )

        if any(
            overlaps(a, b)
            for a in self.actions.eligible
            for b in self.actions.forbidden
        ):
            raise ValueError("eligible and forbidden actions overlap")
        if self.actions.proposed and not any(
            self.actions.proposed == item for item in self.actions.eligible
        ):
            raise ValueError("proposed action must be eligible")
        if any(item not in self.actions.eligible for item in self.mutations.actions):
            raise ValueError("observed mutation must exactly match an eligible action")
        mutating = bool(self.mutations.actions) or (
            self.actions.proposed is not None
            and self.actions.proposed.action_type
            in {ActionType.SCALE, ActionType.ROLLBACK}
        )
        if mutating and (
            self.recovery is None or SafetyGate.RECOVERY not in self.safety_gates
        ):
            raise ValueError("mutating actions require pinned fresh recovery")
        if (
            self.incident.incident_class is IncidentClass.UNKNOWN
            and self.incident.telemetry_quality is not TelemetryQuality.HEALTHY
        ):
            raise ValueError("unknown requires healthy telemetry")
        if not self.incident.actionable and self.mutations.count.exact != 0:
            raise ValueError("non-actionable incidents require zero mutations")
        if (
            self.policy.bundle_state is PolicyBundleState.FAIL_CLOSED
            and self.mutations.count.exact != 0
        ):
            raise ValueError("fail-closed policy requires zero mutations")
        if (
            self.workflow.terminal_reason is WorkflowReason.APPROVAL_EXPIRED
            and SafetyGate.EXPIRY not in self.safety_gates
        ):
            raise ValueError("approval expiry must be checked before mutation")
        if (
            self.workflow.terminal_reason is WorkflowReason.OPERATOR_DRIFT
            and SafetyGate.DRIFT not in self.safety_gates
        ):
            raise ValueError("operator drift must be checked before mutation")
        rejects_tenant = self.tenant_isolation is not None and (
            self.tenant_isolation.reject_foreign_evidence_before_scoring
            or self.tenant_isolation.reject_mismatch_before_external_io
        )
        if rejects_tenant and not {
            SafetyGate.TENANT_SCORING,
            SafetyGate.TENANT_IO,
        }.issubset(self.safety_gates):
            raise ValueError("tenant rejection requires pre-scoring and pre-I/O gates")
        return self


class ScenarioSpecV1Alpha2(StrictModel):
    description: NonEmptyString
    candidate_environments: tuple[NonEmptyString, ...] = ()
    environment_requirements: EnvironmentRequirements
    traceability: ScenarioTraceability
    target: ScenarioTarget
    baseline: BaselineRequirements
    stimulus: ScenarioStimulusV1Alpha2 = Field(default_factory=ScenarioStimulusV1Alpha2)
    expected: ExpectedOutcomeV1Alpha2


class GuardianScenarioV1Alpha2(StrictModel):
    api_version: Literal["tests.guardian.io/v1alpha2"]
    kind: Literal["GuardianScenario"]
    metadata: ScenarioMetadata
    spec: ScenarioSpecV1Alpha2
