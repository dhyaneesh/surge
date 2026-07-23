"""Testbed-to-production normalization of adapter evidence into Guardian facts.

This module is the only scenario-aware normalization boundary. It converts
successful adapter operation results and independently observed environment
state into the strict ``guardian.incident-facts/v1`` schema consumed by the
production Guardian API.

It never imports production decision logic, never accepts scenario identifiers,
expected assertions, raw stimulus magnitudes, secrets, or demo-specific service
names as incident evidence. Missing required evidence fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping

from apps.guardian_api.models import (
    BooleanEvidence,
    ControlFacts,
    EvidenceFreshness,
    EvidencePass,
    EvidenceSource,
    IncidentSeverity,
    IncidentSubmission,
    NumericEvidence,
    ObservationUpdate,
    PolicyFacts,
    PolicyState,
    RequiredSignal,
    ScalerDirection,
    ScalerFacts,
    SignalFacts,
    TargetIdentity,
    TelemetryFacts,
    VersionEvidence,
)
from testbeds.models import (
    DeploymentEvent,
    EnvironmentRelease,
    EnvironmentState,
    FaultExecution,
    LoadExecution,
    ObservedServiceIdentity,
    RolloutState,
    ScalingState,
    WorkloadState,
)


class FactNormalizationError(ValueError):
    """Raised when adapter evidence cannot be normalized safely."""


class MissingTenantError(FactNormalizationError):
    """The authenticated tenant identity is absent before normalization."""


class MissingEvidenceError(FactNormalizationError):
    """A required evidence contract cannot be satisfied from observations."""


_WORKLOAD_KIND = "Deployment"
_HEALTHY_QUALITY = 1.0
_UNHEALTHY_QUALITY = 0.0
_SAMPLE_BUDGET = 10
_CONTROL_PROVENANCE = "test-control"


@dataclass(frozen=True, slots=True)
class ControlStimulus:
    """Typed test-control fixtures translated into provenance-tagged facts."""

    telemetry_mode: str | None = None
    policy_bundle_state: str | None = None
    approval_after_expiry: bool = False
    operator_drift: bool = False
    foreign_tenant: bool = False
    action_completed_at: datetime | None = None

    @property
    def provenance(self) -> str:
        return _CONTROL_PROVENANCE


@dataclass(frozen=True, slots=True)
class FactBuildContext:
    tenant_id: str
    environment: str
    target_role: str
    role_bindings: Mapping[str, str]
    release: EnvironmentRelease
    observed_at: datetime
    observations: tuple[EnvironmentState, ...]
    load_result: LoadExecution | None = None
    fault_result: FaultExecution | None = None
    deployment_result: DeploymentEvent | None = None
    control: ControlStimulus = field(default_factory=ControlStimulus)
    severity: str = IncidentSeverity.WARNING.value
    freshness_seconds: int = 60


def _require_tenant(tenant_id: str) -> str:
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise MissingTenantError("incident facts require an authenticated tenant")
    return tenant_id.strip()


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FactNormalizationError("observed_at must be timezone-aware")
    return value


def _bound_service(ctx: FactBuildContext) -> str:
    return ctx.role_bindings.get(ctx.target_role, ctx.target_role)


def _latest_observation(ctx: FactBuildContext) -> EnvironmentState:
    if not ctx.observations:
        raise MissingEvidenceError("at least one independent observation is required")
    return ctx.observations[-1]


def _find_service(
    observation: EnvironmentState, bound: str
) -> ObservedServiceIdentity | None:
    for candidate in observation.services:
        if candidate.service_name == bound or candidate.role == bound:
            return candidate
    return None


def _find_workload(observation: EnvironmentState, bound: str) -> WorkloadState | None:
    for candidate in observation.workloads:
        if candidate.name == bound or candidate.role == bound:
            return candidate
    return None


def _resolve_identity(ctx: FactBuildContext) -> TargetIdentity:
    observation = _latest_observation(ctx)
    bound = _bound_service(ctx)
    service = _find_service(observation, bound)
    if service is None:
        raise MissingEvidenceError(
            "required identity evidence is missing: target service not observed"
        )
    return TargetIdentity(
        target_role=ctx.target_role,
        environment=ctx.environment,
        namespace=observation.namespace or ctx.environment,
        workload_kind=_WORKLOAD_KIND,
        workload_name=service.service_name,
        service_name=service.service_name,
        service_version=service.version,
        image_digest=service.image_digest,
    )


def _evidence_common(
    ctx: FactBuildContext,
    *,
    subject_role: str,
    observed_at: datetime,
    source: EvidenceSource,
    provenance_ref: str,
    independence_group: str,
    usable: int,
) -> dict:
    return {
        "tenant_id": ctx.tenant_id,
        "subject_role": subject_role,
        "environment": ctx.environment,
        "namespace": _latest_observation(ctx).namespace or ctx.environment,
        "workload_kind": _WORKLOAD_KIND,
        "workload_name": subject_role,
        "service_name": subject_role,
        "observed_at": observed_at,
        "freshness": EvidenceFreshness.FRESH,
        "source": source,
        "provenance_ref": provenance_ref,
        "independence_group": independence_group,
        "expected_samples": _SAMPLE_BUDGET,
        "usable_samples": usable,
    }


def _build_telemetry(ctx: FactBuildContext) -> TelemetryFacts:
    observation = _latest_observation(ctx)
    mode = ctx.control.telemetry_mode
    observed_at = _aware(ctx.observed_at)
    quality = _HEALTHY_QUALITY if observation.healthy else _UNHEALTHY_QUALITY
    pipeline_available = observation.healthy
    comparison_valid = observation.healthy
    usable = _SAMPLE_BUDGET if observation.healthy else 0
    newest = observed_at
    if mode in {"interrupted", "unavailable"}:
        quality = _UNHEALTHY_QUALITY
        pipeline_available = False
        usable = 0
    elif mode == "incomplete":
        quality = 0.70
        usable = 0
        comparison_valid = False
    elif mode == "stale":
        newest = observed_at - timedelta(seconds=2 * ctx.freshness_seconds + 10)
    return TelemetryFacts(
        quality=quality,
        newest_required_sample_at=newest,
        freshness_seconds=ctx.freshness_seconds,
        required_signals=frozenset({RequiredSignal.TELEMETRY_QUALITY}),
        clock_skew_seconds=0.0,
        required_sample_count=_SAMPLE_BUDGET,
        usable_sample_count=usable,
        pipeline_available=pipeline_available,
        comparison_valid=comparison_valid,
        identity_conflict=False,
    )


def _deployment_version_evidence(
    ctx: FactBuildContext, identity: TargetIdentity
) -> VersionEvidence | None:
    if len(ctx.observations) < 2:
        return None
    first = _find_service(ctx.observations[0], identity.service_name)
    last = _find_service(ctx.observations[-1], identity.service_name)
    if first is None or last is None:
        return None
    if not first.image_digest or not last.image_digest:
        return None
    if first.image_digest == last.image_digest:
        return None
    common = _evidence_common(
        ctx,
        subject_role=ctx.target_role,
        observed_at=_aware(ctx.observed_at),
        source=EvidenceSource.ADAPTER_OBSERVATION,
        provenance_ref="adapter-observation/version-transition",
        independence_group="deployment-version-transition",
        usable=_SAMPLE_BUDGET,
    )
    return VersionEvidence(
        **common,
        previous_service_version=first.version,
        current_service_version=last.version,
        previous_digest=first.image_digest,
        current_digest=last.image_digest,
    )


def _dependency_health_evidence(ctx: FactBuildContext) -> BooleanEvidence | None:
    observation = _latest_observation(ctx)
    bound = _bound_service(ctx)
    dependency: RolloutState | None = None
    for rollout in observation.rollouts:
        if rollout.name == bound:
            continue
        dependency = rollout
        break
    if dependency is None:
        return None
    common = _evidence_common(
        ctx,
        subject_role="dependency",
        observed_at=_aware(ctx.observed_at),
        source=EvidenceSource.ADAPTER_OBSERVATION,
        provenance_ref=f"adapter-observation/rollout/{dependency.name}",
        independence_group=f"dependency-{dependency.name}",
        usable=_SAMPLE_BUDGET,
    )
    healthy = bool(dependency.recovery_healthy) and dependency.unavailable_replicas == 0
    return BooleanEvidence(**common, value=healthy)


def _scaler_facts(ctx: FactBuildContext) -> ScalerFacts | None:
    observation = _latest_observation(ctx)
    scaling: ScalingState | None = observation.scaling
    if scaling is None or not scaling.scaler_active and scaling.current_replicas == 0:
        return None
    if scaling.desired_replicas > scaling.current_replicas:
        direction = ScalerDirection.UP
    elif scaling.desired_replicas < scaling.current_replicas:
        direction = ScalerDirection.DOWN
    else:
        direction = ScalerDirection.HOLD
    source_value = (
        float(scaling.queue_depth)
        if scaling.queue_depth is not None
        else float(max(0, scaling.desired_replicas - scaling.current_replicas))
    )
    return ScalerFacts(
        tenant_id=ctx.tenant_id,
        source_value=source_value,
        source_expires_at=_aware(ctx.observed_at)
        + timedelta(seconds=ctx.freshness_seconds),
        requested_direction=direction,
    )


def _policy_facts(ctx: FactBuildContext) -> PolicyFacts:
    state = PolicyState.FRESH
    if ctx.control.policy_bundle_state == "restricted":
        state = PolicyState.RESTRICTED
    elif ctx.control.policy_bundle_state == "fail-closed":
        state = PolicyState.FAIL_CLOSED
    return PolicyFacts(state=state, evaluated_at=_aware(ctx.observed_at))


def _control_facts(ctx: FactBuildContext) -> ControlFacts:
    kwargs: dict = {}
    if ctx.control.approval_after_expiry:
        issued = _aware(ctx.observed_at) - timedelta(minutes=20)
        kwargs = {
            "proposal_created_at": issued,
            "approval_issued_at": issued,
            "approval_expires_at": issued + timedelta(minutes=10),
        }
    if ctx.control.operator_drift:
        kwargs["protected_fingerprint"] = "protected-resource-v1"
        kwargs["current_fingerprint"] = "operator-changed-v2"
    if ctx.control.action_completed_at is not None:
        kwargs["action_completed_at"] = _aware(ctx.control.action_completed_at)
    return ControlFacts(**kwargs)


def _foreign_evidence(
    ctx: FactBuildContext, identity: TargetIdentity
) -> NumericEvidence | None:
    if not ctx.control.foreign_tenant:
        return None
    common = _evidence_common(
        ctx,
        subject_role=ctx.target_role,
        observed_at=_aware(ctx.observed_at),
        source=EvidenceSource.QUERY_CONTRACT,
        provenance_ref=f"{_CONTROL_PROVENANCE}/foreign-tenant-evidence",
        independence_group="foreign-tenant-control",
        usable=_SAMPLE_BUDGET,
    )
    foreign = {
        **common,
        "tenant_id": "foreign-tenant",
        "workload_name": identity.workload_name,
        "service_name": identity.service_name,
    }
    return NumericEvidence(**foreign, value=200.0, baseline_value=100.0)


def build_incident_submission(ctx: FactBuildContext) -> IncidentSubmission:
    """Normalize adapter evidence into a strict incident submission.

    The returned facts never carry a scenario identifier, expected assertions,
    raw stimulus magnitudes, secrets, or demo-specific service names. Evidence
    originates only from successful adapter operation results and independently
    observed environment state.
    """

    _require_tenant(ctx.tenant_id)
    _aware(ctx.observed_at)
    identity = _resolve_identity(ctx)
    telemetry = _build_telemetry(ctx)
    deployment_version = _deployment_version_evidence(ctx, identity)
    dependency_healthy = _dependency_health_evidence(ctx)
    scaler = _scaler_facts(ctx)
    foreign = _foreign_evidence(ctx, identity)
    signals_kwargs: dict = {}
    if deployment_version is not None:
        signals_kwargs["deployment_version"] = deployment_version
    if dependency_healthy is not None:
        signals_kwargs["dependency_healthy"] = dependency_healthy
    if foreign is not None:
        signals_kwargs["request_rate"] = foreign
    signals = SignalFacts(**signals_kwargs)
    policy = _policy_facts(ctx)
    control = _control_facts(ctx)
    evidence_pass = EvidencePass(completed_passes=1, started_at=_aware(ctx.observed_at))
    severity = IncidentSeverity(ctx.severity)
    return IncidentSubmission(
        tenant_id=ctx.tenant_id,
        severity=severity,
        observed_at=_aware(ctx.observed_at),
        identity=identity,
        telemetry=telemetry,
        evidence_pass=evidence_pass,
        signals=signals,
        policy=policy,
        control=control,
        scaler=scaler,
    )


def build_observation_update(
    ctx: FactBuildContext,
    *,
    incident_id: str,
    observation_id: str,
    sequence: int,
    window_key: str,
    observed_at: datetime,
    window_started_at: datetime,
    observation_state: EnvironmentState,
) -> ObservationUpdate:
    """Normalize a fresh post-reset observation window into an update.

    Recovery may be evaluated only from a window that starts at or after the
    reset point and does not predate the observation timestamp.
    """

    _require_tenant(ctx.tenant_id)
    observed_at = _aware(observed_at)
    window_started_at = _aware(window_started_at)
    if window_started_at > observed_at:
        raise FactNormalizationError(
            "observation window cannot start after the observation timestamp"
        )
    identity = _resolve_identity(
        FactBuildContext(
            tenant_id=ctx.tenant_id,
            environment=ctx.environment,
            target_role=ctx.target_role,
            role_bindings=ctx.role_bindings,
            release=ctx.release,
            observed_at=observed_at,
            observations=(observation_state,),
            control=ControlStimulus(),
            severity=ctx.severity,
            freshness_seconds=ctx.freshness_seconds,
        )
    )
    healthy = bool(observation_state.healthy) and bool(observation_state.services)
    telemetry = TelemetryFacts(
        quality=_HEALTHY_QUALITY if healthy else _UNHEALTHY_QUALITY,
        newest_required_sample_at=observed_at,
        freshness_seconds=ctx.freshness_seconds,
        required_signals=frozenset({RequiredSignal.TELEMETRY_QUALITY}),
        clock_skew_seconds=0.0,
        required_sample_count=_SAMPLE_BUDGET,
        usable_sample_count=_SAMPLE_BUDGET if healthy else 0,
        pipeline_available=healthy,
        comparison_valid=healthy,
        identity_conflict=False,
    )
    del identity
    return ObservationUpdate(
        tenant_id=ctx.tenant_id,
        incident_id=incident_id,
        observation_id=observation_id,
        sequence=sequence,
        window_key=window_key,
        observed_at=observed_at,
        window_started_at=window_started_at,
        telemetry=telemetry,
        service_healthy=healthy,
        required_conditions_satisfied=healthy,
        provenance_ref="adapter-observation/post-reset-window",
    )


def normalize_observed_at(value: datetime, *, reference: datetime) -> datetime:
    """Return a timezone-aware observation timestamp no earlier than reference."""

    normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if normalized < reference:
        raise FactNormalizationError("observation cannot predate the incident")
    return normalized
