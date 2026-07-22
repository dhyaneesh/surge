"""Canonical Pydantic schema for environment-neutral Guardian scenarios."""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PositiveFloat,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.alias_generators import to_camel

from testbeds.models import FaultType
from testbeds.scenarios.validation import (
    is_mutating_action,
    parse_positive_duration,
    require_unique,
    validate_dns_name,
    validate_semantic_labels,
)


Duration = Annotated[timedelta, BeforeValidator(parse_positive_duration)]
NormalizedToken = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=253,
        pattern=r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$",
    ),
]
NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class IncidentClass(StrEnum):
    LOAD_SPIKE = "load_spike"
    DEPLOYMENT_REGRESSION = "deployment_regression"
    DEPENDENCY_FAILURE = "dependency_failure"
    RESOURCE_SATURATION = "resource_saturation"
    TELEMETRY_FAILURE = "telemetry_failure"
    UNKNOWN = "unknown"


class ActionType(StrEnum):
    SCALE = "scale"
    ROLLBACK = "rollback"
    ALERT_ONLY = "alert_only"
    CONTINUE_INVESTIGATION = "continue_investigation"
    PAUSE_SCALER = "pause_scaler"


class ServiceRole(StrEnum):
    REQUEST_PROCESSOR = "request-processor"
    API_GATEWAY = "api-gateway"
    BACKGROUND_WORKER = "background-worker"
    DATA_STORE = "data-store"
    CACHE = "cache"
    MESSAGE_BROKER = "message-broker"
    DEPENDENCY = "dependency"


class ServiceCapability(StrEnum):
    HORIZONTALLY_SCALABLE = "horizontally-scalable"
    VERSION_DEPLOYABLE = "version-deployable"
    FAULT_INJECTABLE = "fault-injectable"
    QUEUE_BACKED = "queue-backed"
    STATEFUL = "stateful"
    STATELESS = "stateless"


class EvidenceType(StrEnum):
    METRICS = "metrics"
    TRACES = "traces"
    LOGS = "logs"
    EXCEPTIONS = "exceptions"
    DEPLOYMENT_EVENT = "deployment-event"
    SERVICE_IDENTITY = "service-identity"
    WORKLOAD_STATE = "workload-state"
    TOPOLOGY = "topology"
    LOAD = "load"
    RESOURCE_UTILIZATION = "resource-utilization"
    DEPENDENCY_HEALTH = "dependency-health"
    TELEMETRY_QUALITY = "telemetry-quality"
    POLICY_DECISION = "policy-decision"
    ACTION_RESULT = "action-result"
    RECOVERY_TELEMETRY = "recovery-telemetry"
    IDENTITY_CONFLICT = "identity-conflict"


class PolicyDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval-required"


class WorkflowState(StrEnum):
    ACTIVE = "active"
    TELEMETRY_VALIDATION = "telemetry-validation"
    ASSESSMENT = "assessment"
    CLASSIFIED = "classified"
    TELEMETRY_FAILURE = "telemetry-failure"
    UNKNOWN = "unknown"
    CONFLICT_RESOLUTION = "conflict-resolution"
    ACTION_PROPOSED = "action-proposed"
    POLICY_ALLOWED = "policy-allowed"
    POLICY_DENIED = "policy-denied"
    APPROVAL_PENDING = "approval-pending"
    EXECUTING = "executing"
    RECOVERY_VERIFICATION = "recovery-verification"
    RECOVERED = "recovered"
    CLOSED = "closed"
    SUPERSEDED_BY_OPERATOR = "superseded-by-operator"


class ScenarioMetadata(StrictModel):
    name: str

    @field_validator("name")
    @classmethod
    def name_is_dns_style(cls, value: str) -> str:
        return validate_dns_name(value)


class NormalizedSelector(StrictModel):
    role: ServiceRole
    capabilities: frozenset[ServiceCapability] = frozenset()
    semantic_labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("semantic_labels")
    @classmethod
    def labels_are_portable(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_semantic_labels(value)


class ScenarioTarget(StrictModel):
    service_selector: NormalizedSelector
    workload_selector: NormalizedSelector | None = None


class UnsupportedEnvironment(StrictModel):
    environment: NormalizedToken
    reason: NonEmptyString


class BaselineRequirements(StrictModel):
    healthy_for: Duration


class LoadStimulus(StrictModel):
    pattern: NormalizedToken
    multiplier: Annotated[float, Field(gt=1)]
    duration: Duration


class FaultStimulus(StrictModel):
    fault_type: FaultType = Field(alias="type")
    duration: Duration | None = None
    magnitude: PositiveFloat = 1.0


class DeploymentStimulus(StrictModel):
    from_version: NonEmptyString
    to_version: NonEmptyString
    record_deployment_event: bool = True

    @model_validator(mode="after")
    def versions_are_distinct(self) -> Self:
        if self.from_version == self.to_version:
            raise ValueError("deployment fromVersion and toVersion must be different")
        return self


class ScenarioStimulus(StrictModel):
    load: LoadStimulus | None = None
    fault: FaultStimulus | None = None
    deployment: DeploymentStimulus | None = None


class ProposedAction(StrictModel):
    action_type: ActionType = Field(alias="type")
    approval_required: bool = False


class RecoveryExpectation(StrictModel):
    contract_ref: NonEmptyString | None = None
    require_fresh_telemetry: bool = False
    expected_result: Literal["healthy", "recovered"] | None = None

    @model_validator(mode="after")
    def fresh_telemetry_has_observable_condition(self) -> Self:
        if self.require_fresh_telemetry and self.expected_result is None:
            raise ValueError(
                "fresh telemetry recovery requires a healthy or recovered expectedResult"
            )
        return self


class ExpectedOutcome(StrictModel):
    incident_class: IncidentClass | None = None
    proposed_action: ProposedAction | None = None
    allowed_actions: tuple[ActionType, ...] = ()
    forbidden_actions: tuple[ActionType, ...] = ()
    evidence_types: tuple[EvidenceType, ...] = ()
    policy_decision: PolicyDecision | None = None
    workflow_states: tuple[WorkflowState, ...] = ()
    recovery: RecoveryExpectation | None = None

    @model_validator(mode="after")
    def action_and_recovery_expectations_are_consistent(self) -> Self:
        require_unique(self.workflow_states, "workflowStates")
        overlap = set(self.allowed_actions) & set(self.forbidden_actions)
        if overlap:
            rendered = ", ".join(sorted(action.value for action in overlap))
            raise ValueError(f"allowedActions and forbiddenActions overlap: {rendered}")
        proposed_mutation = self.proposed_action is not None and is_mutating_action(
            self.proposed_action.action_type
        )
        allowed_mutation = any(
            is_mutating_action(action) for action in self.allowed_actions
        )
        if (proposed_mutation or allowed_mutation) and self.recovery is None:
            raise ValueError("mutating proposed or allowed actions require recovery")
        return self


class ScenarioSpec(StrictModel):
    applicable_environments: tuple[NormalizedToken, ...]
    unsupported_environments: tuple[UnsupportedEnvironment, ...] = ()
    required_capabilities: frozenset[ServiceCapability] = frozenset()
    required_faults: frozenset[FaultType] = frozenset()
    required_telemetry: frozenset[EvidenceType] = frozenset()
    required_providers: frozenset[NormalizedToken] = frozenset()
    target: ScenarioTarget
    baseline: BaselineRequirements | None = None
    stimulus: ScenarioStimulus = Field(default_factory=ScenarioStimulus)
    expected: ExpectedOutcome = Field(default_factory=ExpectedOutcome)

    @field_validator("applicable_environments")
    @classmethod
    def applicable_environments_are_non_empty_and_unique(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not value:
            raise ValueError("applicableEnvironments must not be empty")
        require_unique(value, "applicableEnvironments")
        return value

    @model_validator(mode="after")
    def environment_sets_are_unique_and_disjoint(self) -> Self:
        unsupported = tuple(item.environment for item in self.unsupported_environments)
        require_unique(unsupported, "unsupportedEnvironments")
        overlap = set(self.applicable_environments) & set(unsupported)
        if overlap:
            rendered = ", ".join(sorted(overlap))
            raise ValueError(
                f"applicableEnvironments and unsupportedEnvironments overlap: {rendered}"
            )
        return self


class GuardianScenario(StrictModel):
    api_version: Literal["tests.guardian.io/v1alpha1"]
    kind: Literal["GuardianScenario"]
    metadata: ScenarioMetadata
    spec: ScenarioSpec
