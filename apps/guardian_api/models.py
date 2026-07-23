"""Strict normalized models for Guardian's deterministic domain."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_serializer,
    model_validator,
)


NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SCOPED_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
ScopedIdentifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=SCOPED_IDENTIFIER_PATTERN),
]
ImageDigest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
UnitFloat = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class StrictModel(BaseModel):
    """Immutable input and output model with an exact schema."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ImmutableFloatMapping(Mapping[str, float]):
    """Detached immutable mapping with no mutable dictionary base class."""

    __slots__ = ("__data",)

    def __init__(self, values: Mapping[str, float]) -> None:
        self.__data = MappingProxyType(dict(values))

    def __getitem__(self, key: str) -> float:
        return self.__data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.__data)

    def __len__(self) -> int:
        return len(self.__data)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({dict(self.__data)!r})"


class EvidenceFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"
    CONFLICTING = "conflicting"


class EvidenceSource(StrEnum):
    QUERY_CONTRACT = "query-contract"
    ADAPTER_OBSERVATION = "adapter-observation"
    CONTROL_PLANE = "control-plane"
    DEPLOYMENT_EVENT = "deployment-event"
    FAULT_EXECUTION = "fault-execution"
    LOAD_EXECUTION = "load-execution"
    POLICY_CONTROL = "policy-control"
    SCALER_GATEWAY = "scaler-gateway"
    RECOVERY_OBSERVATION = "recovery-observation"


class CriticalIntegrityFailure(StrEnum):
    IDENTITY_MISSING = "identity-missing"
    IDENTITY_CONFLICT = "identity-conflict"
    SAMPLE_STALE = "sample-stale"
    TIMESTAMP_SKEW = "timestamp-skew"
    ZERO_SAMPLES = "zero-samples"
    PIPELINE_UNAVAILABLE = "pipeline-unavailable"
    COMPARISON_INVALID = "comparison-invalid"


class RequiredSignal(StrEnum):
    TELEMETRY_QUALITY = "telemetry_quality"
    REQUEST_RATE = "request_rate"
    CPU_UTILIZATION = "cpu_utilization"
    MEMORY_UTILIZATION = "memory_utilization"
    THROTTLING_RATIO = "throttling_ratio"
    OOM_KILLED = "oom_killed"
    RESTART_DELTA = "restart_delta"
    DEPLOYMENT_VERSION = "deployment_version"
    ERROR_RATE = "error_rate"
    P95_LATENCY_MS = "p95_latency_ms"
    TOPOLOGY_EDGE = "topology_edge"
    DEPENDENCY_HEALTHY = "dependency_healthy"


class PolicyState(StrEnum):
    FRESH = "fresh"
    RESTRICTED = "restricted"
    FAIL_CLOSED = "fail-closed"


class HypothesisName(StrEnum):
    LOAD_SPIKE = "load_spike"
    DEPLOYMENT_REGRESSION = "deployment_regression"
    RESOURCE_SATURATION = "resource_saturation"
    DEPENDENCY_FAILURE = "dependency_failure"


class IncidentClass(StrEnum):
    LOAD_SPIKE = "load_spike"
    DEPLOYMENT_REGRESSION = "deployment_regression"
    RESOURCE_SATURATION = "resource_saturation"
    DEPENDENCY_FAILURE = "dependency_failure"
    TELEMETRY_FAILURE = "telemetry_failure"
    UNKNOWN = "unknown"


class IncidentSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ActionType(StrEnum):
    SCALE_UP = "scale-up"
    SCALE_DOWN = "scale-down"
    ROLLBACK = "rollback"
    PROTECT_DEPENDENCY = "protect-dependency"
    CONTINUE_INVESTIGATION = "continue-investigation"
    INVESTIGATE = "investigate"
    ALERT = "alert"
    SCALER_PAUSE = "scaler-pause"


class WorkflowState(StrEnum):
    ASSESSMENT = "assessment"
    CLASSIFIED = "classified"
    TELEMETRY_FAILURE = "telemetry-failure"
    CONFLICT_RESOLUTION = "conflict-resolution"
    UNKNOWN = "unknown"


class PolicyDecision(StrEnum):
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval-required"


class ScalerDirection(StrEnum):
    UP = "up"
    DOWN = "down"
    HOLD = "hold"


class ScalerResult(StrEnum):
    FRESH_VALUE = "fresh-value"
    SAFE_HOLD = "safe-hold"


class TargetIdentity(StrictModel):
    target_role: NonEmptyString
    environment: NonEmptyString
    namespace: NonEmptyString
    workload_kind: NonEmptyString
    workload_name: NonEmptyString
    service_name: NonEmptyString
    service_version: NonEmptyString | None = None
    image_digest: ImageDigest | None = None


class EvidenceFact(StrictModel):
    tenant_id: ScopedIdentifier
    subject_role: NonEmptyString
    environment: NonEmptyString
    namespace: NonEmptyString
    workload_kind: NonEmptyString
    workload_name: NonEmptyString
    service_name: NonEmptyString
    observed_at: AwareDatetime
    freshness: EvidenceFreshness
    source: EvidenceSource
    provenance_ref: NonEmptyString
    independence_group: NonEmptyString
    expected_samples: int = Field(ge=1)
    usable_samples: int = Field(ge=0)

    @model_validator(mode="after")
    def sample_counts_are_consistent(self) -> Self:
        if self.usable_samples > self.expected_samples:
            raise ValueError("usable samples cannot exceed expected samples")
        return self

    @property
    def usable_confidence(self) -> float:
        return min(1.0, self.usable_samples / self.expected_samples)


EvidenceBase = EvidenceFact


class NumericEvidence(EvidenceFact):
    value: NonNegativeFloat
    baseline_value: NonNegativeFloat | None = None


class BooleanEvidence(EvidenceFact):
    value: bool


class VersionEvidence(EvidenceFact):
    previous_service_version: NonEmptyString | None = None
    current_service_version: NonEmptyString | None = None
    previous_digest: ImageDigest
    current_digest: ImageDigest


class TelemetryFacts(StrictModel):
    quality: UnitFloat
    newest_required_sample_at: AwareDatetime
    freshness_seconds: int = Field(gt=0)
    required_signals: frozenset[RequiredSignal]
    clock_skew_seconds: float = Field(allow_inf_nan=False)
    required_sample_count: int = Field(ge=1)
    usable_sample_count: int = Field(ge=0)
    pipeline_available: bool
    comparison_valid: bool
    identity_conflict: bool = False

    @model_validator(mode="after")
    def sample_counts_are_consistent(self) -> Self:
        if self.usable_sample_count > self.required_sample_count:
            raise ValueError("usable samples cannot exceed required samples")
        return self


class EvidencePass(StrictModel):
    completed_passes: int = Field(ge=0)
    started_at: AwareDatetime
    completed_conflict_passes: int = Field(default=0, ge=0, le=2)
    conflict_started_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def conflict_timing_is_complete(self) -> Self:
        if self.completed_conflict_passes and self.conflict_started_at is None:
            raise ValueError("conflict pass timing is required")
        return self


class PolicyFacts(StrictModel):
    state: PolicyState
    evaluated_at: AwareDatetime


class ControlFacts(StrictModel):
    proposal_created_at: AwareDatetime | None = None
    proposal_ttl_seconds: int = Field(default=900, gt=0, le=1800)
    approval_issued_at: AwareDatetime | None = None
    approval_expires_at: AwareDatetime | None = None
    approval_nonce_issued_at: AwareDatetime | None = None
    approval_nonce_expires_at: AwareDatetime | None = None
    action_attempted_at: AwareDatetime | None = None
    action_completed_at: AwareDatetime | None = None
    protected_fingerprint: NonEmptyString | None = None
    current_fingerprint: NonEmptyString | None = None

    @model_validator(mode="after")
    def complete_pairs(self) -> Self:
        if (self.approval_issued_at is None) != (self.approval_expires_at is None):
            raise ValueError("approval issuance and expiry must be supplied together")
        if self.approval_issued_at is not None and self.proposal_created_at is None:
            raise ValueError("approval requires an active proposal")
        if (self.approval_nonce_issued_at is None) != (
            self.approval_nonce_expires_at is None
        ):
            raise ValueError(
                "approval nonce issuance and expiry must be supplied together"
            )
        if (self.protected_fingerprint is None) != (self.current_fingerprint is None):
            raise ValueError("both protected-resource fingerprints are required")
        return self


class ScalerFacts(StrictModel):
    tenant_id: ScopedIdentifier
    source_value: float = Field(allow_inf_nan=False)
    source_expires_at: AwareDatetime
    requested_direction: ScalerDirection


class SignalFacts(StrictModel):
    request_rate: NumericEvidence | None = None
    cpu_utilization: NumericEvidence | None = None
    memory_utilization: NumericEvidence | None = None
    throttling_ratio: NumericEvidence | None = None
    oom_killed: BooleanEvidence | None = None
    restart_delta: NumericEvidence | None = None
    deployment_version: VersionEvidence | None = None
    error_rate: NumericEvidence | None = None
    p95_latency_ms: NumericEvidence | None = None
    topology_edge: BooleanEvidence | None = None
    dependency_healthy: BooleanEvidence | None = None

    @model_validator(mode="after")
    def ratios_are_bounded(self) -> Self:
        for name in (
            "cpu_utilization",
            "memory_utilization",
            "throttling_ratio",
            "error_rate",
        ):
            evidence = getattr(self, name)
            if evidence is not None and (
                evidence.value > 1
                or evidence.baseline_value is not None
                and evidence.baseline_value > 1
            ):
                raise ValueError(f"{name} values must be within [0, 1]")
        return self

    def evidence_items(self) -> tuple[tuple[str, EvidenceFact], ...]:
        return tuple(
            (name, value)
            for name in type(self).model_fields
            if isinstance((value := getattr(self, name)), EvidenceFact)
        )

    def all_evidence(self) -> tuple[EvidenceFact, ...]:
        return tuple(value for _, value in self.evidence_items())


class IncidentSubmission(StrictModel):
    """Caller-supplied facts before the server assigns incident identity."""

    schema_version: Literal["guardian.incident-facts/v1"] = "guardian.incident-facts/v1"
    tenant_id: ScopedIdentifier
    severity: IncidentSeverity = IncidentSeverity.WARNING
    observed_at: AwareDatetime
    identity: TargetIdentity | None
    telemetry: TelemetryFacts
    evidence_pass: EvidencePass
    signals: SignalFacts
    policy: PolicyFacts
    control: ControlFacts
    scaler: ScalerFacts | None = None

    @model_validator(mode="after")
    def required_signal_contract_is_nonempty(self) -> Self:
        if not self.telemetry.required_signals:
            raise ValueError("executable incident facts require a signal contract")
        return self


class IncidentFacts(IncidentSubmission):
    """Persisted facts with an incident identity assigned by a trusted boundary."""

    incident_id: ScopedIdentifier


class ObservationUpdate(StrictModel):
    schema_version: Literal["guardian.observation-update/v1"] = (
        "guardian.observation-update/v1"
    )
    tenant_id: ScopedIdentifier
    incident_id: ScopedIdentifier
    observation_id: ScopedIdentifier
    sequence: int = Field(ge=0)
    window_key: ScopedIdentifier
    observed_at: AwareDatetime
    window_started_at: AwareDatetime
    telemetry: TelemetryFacts
    service_healthy: bool
    required_conditions_satisfied: bool
    provenance_ref: NonEmptyString

    @model_validator(mode="after")
    def observation_times_are_ordered(self) -> Self:
        if self.window_started_at > self.observed_at:
            raise ValueError("observation window cannot start in the future")
        if self.telemetry.newest_required_sample_at > self.observed_at:
            raise ValueError("telemetry sample cannot be newer than its observation")
        return self


class HypothesisScore(StrictModel):
    name: HypothesisName
    support: UnitFloat
    contradiction: UnitFloat
    deterministic_score: UnitFloat
    evidence_confidence: UnitFloat
    required_group_confidence: Mapping[NonEmptyString, UnitFloat]
    eligible: bool

    @model_validator(mode="after")
    def confidence_mapping_is_immutable(self) -> Self:
        object.__setattr__(
            self,
            "required_group_confidence",
            ImmutableFloatMapping(self.required_group_confidence),
        )
        return self

    @field_serializer("required_group_confidence")
    def serialize_required_group_confidence(
        self, value: Mapping[str, float]
    ) -> dict[str, float]:
        return dict(value)


class GuardianProjection(StrictModel):
    schema_version: Literal["guardian.projection/v1"] = "guardian.projection/v1"
    rules_version: NonEmptyString
    incident_class: IncidentClass | None
    telemetry_healthy: bool
    integrity_failures: tuple[CriticalIntegrityFailure, ...]
    hypotheses: tuple[HypothesisScore, ...]
    eligible_actions: tuple[ActionType, ...]
    permitted_actions: tuple[ActionType, ...]
    forbidden_actions: tuple[ActionType, ...]
    proposed_action: ActionType | None
    workflow_state: WorkflowState
    policy_decision: PolicyDecision
    terminal_reason: NonEmptyString | None
    requested_evidence_groups: tuple[NonEmptyString, ...]
    proposal_expires_at: datetime | None
    approval_expires_at: datetime | None
    approval_nonce_expires_at: datetime | None
    foreign_evidence_rejected: bool
    scaler_result: ScalerResult | None
    recovery_verified: bool
    escalation_required: bool
    model_participated: Literal[False] = False
    executed_mutations: Literal[0] = 0
