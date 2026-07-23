"""Testbed-to-production normalization of collector evidence into Guardian facts.

This module is the only scenario-aware normalization boundary. It converts
independently sampled collector evidence and typed control fixtures into the
strict ``guardian.incident-facts/v1`` schema consumed by the production
Guardian API.

It never imports production decision logic, never accepts scenario identifiers,
expected assertions, raw stimulus magnitudes, secrets, or demo-specific service
names as incident evidence. Missing required evidence fails closed.
``EnvironmentState.healthy``, successful load injection, and successful fault
injection are never treated as telemetry or symptom proof.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

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
from testbeds.evidence.collector import EvidenceSample, UnavailableEvidence
from testbeds.evidence.contracts import EvidenceSourceKind
from testbeds.models import (
    DeploymentEvent,
    EnvironmentRelease,
    EnvironmentState,
    FaultExecution,
    LoadExecution,
    ObservedServiceIdentity,
    ScalingState,
)


class FactNormalizationError(ValueError):
    """Raised when adapter evidence cannot be normalized safely."""


class MissingTenantError(FactNormalizationError):
    """The authenticated tenant identity is absent before normalization."""


class MissingEvidenceError(FactNormalizationError):
    """A required evidence contract cannot be satisfied from observations."""


_WORKLOAD_KIND = "Deployment"
_SAMPLE_BUDGET = 10
_CONTROL_PROVENANCE = "test-control"

_SIGNAL_SOURCE: dict[str, EvidenceSource] = {
    "request_rate": EvidenceSource.QUERY_CONTRACT,
    "cpu_utilization": EvidenceSource.QUERY_CONTRACT,
    "memory_utilization": EvidenceSource.QUERY_CONTRACT,
    "error_rate": EvidenceSource.QUERY_CONTRACT,
    "p95_latency_ms": EvidenceSource.QUERY_CONTRACT,
    "restart_delta": EvidenceSource.ADAPTER_OBSERVATION,
    "topology_edge": EvidenceSource.ADAPTER_OBSERVATION,
    "dependency_healthy": EvidenceSource.ADAPTER_OBSERVATION,
    "deployment_version": EvidenceSource.ADAPTER_OBSERVATION,
}

_NUMERIC_BASELINE_KEYS = {
    "request_rate": "baseline_request_rate",
    "error_rate": "baseline_error_rate",
    "p95_latency_ms": "baseline_p95_latency_ms",
}


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
    evidence_samples: tuple[EvidenceSample | UnavailableEvidence, ...] = ()
    required_signals: frozenset[str] = frozenset({"telemetry_quality"})


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
    identity: TargetIdentity,
    *,
    subject_role: str,
    observed_at: datetime,
    source: EvidenceSource,
    provenance_ref: str,
    independence_group: str,
    usable: int,
    expected: int = _SAMPLE_BUDGET,
) -> dict:
    return {
        "tenant_id": ctx.tenant_id,
        "subject_role": subject_role,
        "environment": identity.environment,
        "namespace": identity.namespace,
        "workload_kind": identity.workload_kind,
        "workload_name": identity.workload_name,
        "service_name": identity.service_name,
        "observed_at": observed_at,
        "freshness": EvidenceFreshness.FRESH,
        "source": source,
        "provenance_ref": provenance_ref,
        "independence_group": independence_group,
        "expected_samples": expected,
        "usable_samples": usable,
    }


def _usable_from_values(
    values: Mapping[str, object], default: int = _SAMPLE_BUDGET
) -> int:
    raw = values.get("usable_samples", default)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _expected_from_values(
    values: Mapping[str, object], default: int = _SAMPLE_BUDGET
) -> int:
    raw = values.get("required_samples", default)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _samples_of_kind(
    samples: Sequence[EvidenceSample | UnavailableEvidence], kind: EvidenceSourceKind
) -> list[EvidenceSample | UnavailableEvidence]:
    return [item for item in samples if item.source_kind is kind]


def _first_sample(
    samples: Sequence[EvidenceSample | UnavailableEvidence], kind: EvidenceSourceKind
) -> EvidenceSample | UnavailableEvidence | None:
    matched = _samples_of_kind(samples, kind)
    return matched[0] if matched else None


def _require_sample(
    samples: Sequence[EvidenceSample | UnavailableEvidence],
    kind: EvidenceSourceKind,
    *,
    label: str,
) -> EvidenceSample:
    matched = _first_sample(samples, kind)
    if matched is None:
        raise MissingEvidenceError(f"required {label} evidence is missing")
    if isinstance(matched, UnavailableEvidence):
        raise MissingEvidenceError(f"required {label} evidence is unavailable")
    return matched


def _build_telemetry(ctx: FactBuildContext) -> TelemetryFacts:
    observed_at = _aware(ctx.observed_at)
    mode = ctx.control.telemetry_mode
    newest = observed_at
    if mode in {"interrupted", "unavailable"}:
        return TelemetryFacts(
            quality=0.0,
            newest_required_sample_at=newest,
            freshness_seconds=ctx.freshness_seconds,
            required_signals=frozenset({RequiredSignal.TELEMETRY_QUALITY}),
            clock_skew_seconds=0.0,
            required_sample_count=_SAMPLE_BUDGET,
            usable_sample_count=0,
            pipeline_available=False,
            comparison_valid=False,
            identity_conflict=False,
        )
    if mode == "incomplete":
        return TelemetryFacts(
            quality=0.70,
            newest_required_sample_at=newest,
            freshness_seconds=ctx.freshness_seconds,
            required_signals=frozenset({RequiredSignal.TELEMETRY_QUALITY}),
            clock_skew_seconds=0.0,
            required_sample_count=_SAMPLE_BUDGET,
            usable_sample_count=0,
            pipeline_available=True,
            comparison_valid=False,
            identity_conflict=False,
        )
    if mode == "stale":
        newest = observed_at - timedelta(seconds=2 * ctx.freshness_seconds + 10)
        sample = _require_sample(
            ctx.evidence_samples,
            EvidenceSourceKind.SIGNOZ_TELEMETRY,
            label="telemetry",
        )
        values = sample.values
        quality = float(values.get("quality", 1.0))
        usable = _usable_from_values(values)
        required = _expected_from_values(values)
        return TelemetryFacts(
            quality=quality,
            newest_required_sample_at=newest,
            freshness_seconds=ctx.freshness_seconds,
            required_signals=frozenset({RequiredSignal.TELEMETRY_QUALITY}),
            clock_skew_seconds=0.0,
            required_sample_count=required,
            usable_sample_count=usable,
            pipeline_available=bool(values.get("pipeline_available", True)),
            comparison_valid=bool(values.get("comparison_valid", True)),
            identity_conflict=False,
        )

    if "telemetry_quality" in ctx.required_signals or not ctx.control.telemetry_mode:
        sample = _require_sample(
            ctx.evidence_samples,
            EvidenceSourceKind.SIGNOZ_TELEMETRY,
            label="telemetry",
        )
        values = sample.values
        quality = float(values["quality"])
        usable = _usable_from_values(values)
        required = _expected_from_values(values)
        return TelemetryFacts(
            quality=quality,
            newest_required_sample_at=_aware(sample.observed_at),
            freshness_seconds=ctx.freshness_seconds,
            required_signals=frozenset({RequiredSignal.TELEMETRY_QUALITY}),
            clock_skew_seconds=0.0,
            required_sample_count=required,
            usable_sample_count=usable,
            pipeline_available=bool(values.get("pipeline_available", quality >= 0.80)),
            comparison_valid=bool(
                values.get("comparison_valid", quality >= 0.80 and usable > 0)
            ),
            identity_conflict=False,
        )
    raise MissingEvidenceError("required telemetry evidence is missing")


def _merged_sample_values(
    samples: Sequence[EvidenceSample | UnavailableEvidence],
) -> list[tuple[EvidenceSample, Mapping[str, object]]]:
    output: list[tuple[EvidenceSample, Mapping[str, object]]] = []
    for item in samples:
        if isinstance(item, EvidenceSample):
            output.append((item, item.values))
    return output


def _find_value(
    samples: Sequence[EvidenceSample | UnavailableEvidence], key: str
) -> tuple[EvidenceSample, object] | None:
    for sample, values in _merged_sample_values(samples):
        if key in values:
            return sample, values[key]
    return None


def _numeric_from_samples(
    ctx: FactBuildContext,
    identity: TargetIdentity,
    *,
    signal_name: str,
    value_key: str,
    group: str,
) -> NumericEvidence | None:
    found = _find_value(ctx.evidence_samples, value_key)
    if found is None:
        return None
    sample, raw = found
    baseline_key = _NUMERIC_BASELINE_KEYS.get(signal_name)
    baseline = None
    if baseline_key is not None:
        baseline_raw = sample.values.get(baseline_key)
        if baseline_raw is not None:
            baseline = float(baseline_raw)  # type: ignore[arg-type]
    usable = _usable_from_values(sample.values)
    expected = _expected_from_values(sample.values)
    common = _evidence_common(
        ctx,
        identity,
        subject_role=ctx.target_role,
        observed_at=_aware(sample.observed_at),
        source=_SIGNAL_SOURCE[signal_name],
        provenance_ref=sample.provenance_ref,
        independence_group=group,
        usable=usable,
        expected=expected,
    )
    return NumericEvidence(
        **common,
        value=float(raw),  # type: ignore[arg-type]
        baseline_value=baseline,
    )


def _boolean_from_samples(
    ctx: FactBuildContext,
    identity: TargetIdentity,
    *,
    signal_name: str,
    value_key: str,
    group: str,
    subject_role: str | None = None,
) -> BooleanEvidence | None:
    found = _find_value(ctx.evidence_samples, value_key)
    if found is None:
        return None
    sample, raw = found
    usable = _usable_from_values(sample.values)
    expected = _expected_from_values(sample.values)
    role = subject_role or ctx.target_role
    common = _evidence_common(
        ctx,
        identity,
        subject_role=role,
        observed_at=_aware(sample.observed_at),
        source=_SIGNAL_SOURCE[signal_name],
        provenance_ref=sample.provenance_ref,
        independence_group=group,
        usable=usable,
        expected=expected,
    )
    return BooleanEvidence(**common, value=bool(raw))


def _deployment_version_evidence(
    ctx: FactBuildContext, identity: TargetIdentity
) -> VersionEvidence | None:
    found_digest = _find_value(ctx.evidence_samples, "current_digest")
    if found_digest is None:
        return None
    sample, current_digest = found_digest
    previous_digest = sample.values.get("previous_digest")
    if previous_digest is None or current_digest is None:
        return None
    usable = _usable_from_values(sample.values)
    expected = _expected_from_values(sample.values)
    common = _evidence_common(
        ctx,
        identity,
        subject_role=ctx.target_role,
        observed_at=_aware(sample.observed_at),
        source=EvidenceSource.ADAPTER_OBSERVATION,
        provenance_ref=sample.provenance_ref,
        independence_group="deployment-version-transition",
        usable=usable,
        expected=expected,
    )
    previous_version = sample.values.get("previous_service_version")
    current_version = sample.values.get("current_service_version")
    return VersionEvidence(
        **common,
        previous_service_version=(
            str(previous_version) if previous_version is not None else None
        ),
        current_service_version=(
            str(current_version) if current_version is not None else None
        ),
        previous_digest=str(previous_digest),
        current_digest=str(current_digest),
    )


def _build_signals(ctx: FactBuildContext, identity: TargetIdentity) -> SignalFacts:
    kwargs: dict = {}
    builders = (
        ("request_rate", "request_rate", "load", "numeric"),
        ("cpu_utilization", "cpu_utilization", "util", "numeric"),
        ("memory_utilization", "memory_utilization", "util", "numeric"),
        ("error_rate", "error_rate", "errors", "numeric"),
        ("p95_latency_ms", "p95_latency_ms", "latency", "numeric"),
        ("restart_delta", "restart_delta", "pressure", "numeric"),
        ("topology_edge", "topology_edge", "topology", "boolean"),
        ("dependency_healthy", "dependency_healthy", "dependency", "dependency"),
        ("deployment_version", "deployment_version", "deployment", "version"),
    )
    for signal_name, value_key, group, kind in builders:
        evidence = None
        if kind == "numeric":
            evidence = _numeric_from_samples(
                ctx,
                identity,
                signal_name=signal_name,
                value_key=value_key,
                group=group,
            )
        elif kind == "boolean":
            evidence = _boolean_from_samples(
                ctx,
                identity,
                signal_name=signal_name,
                value_key=value_key,
                group=group,
            )
        elif kind == "dependency":
            evidence = _boolean_from_samples(
                ctx,
                identity,
                signal_name=signal_name,
                value_key=value_key,
                group=group,
                subject_role="dependency",
            )
        elif kind == "version":
            evidence = _deployment_version_evidence(ctx, identity)
        if evidence is not None:
            kwargs[signal_name] = evidence
        elif signal_name in ctx.required_signals:
            raise MissingEvidenceError(f"required {signal_name} evidence is missing")
    return SignalFacts(**kwargs)


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
        identity,
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
    """Normalize collector evidence into a strict incident submission.

    The returned facts never carry a scenario identifier, expected assertions,
    raw stimulus magnitudes, secrets, or demo-specific service names. Evidence
    originates only from independently sampled collector results and typed
    control fixtures. Healthy adapter state and successful load/fault results
    are never treated as symptom proof.
    """

    _require_tenant(ctx.tenant_id)
    _aware(ctx.observed_at)
    # Successful control results are deliberately unused as symptom evidence.
    _ = ctx.load_result
    _ = ctx.fault_result
    identity = _resolve_identity(ctx)
    telemetry = _build_telemetry(ctx)
    signals = _build_signals(ctx, identity)
    foreign = _foreign_evidence(ctx, identity)
    if foreign is not None:
        signals = signals.model_copy(update={"request_rate": foreign})
    scaler = _scaler_facts(ctx)
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
    evidence_samples: Sequence[EvidenceSample | UnavailableEvidence] = (),
) -> ObservationUpdate:
    """Normalize a fresh post-reset observation window into an update.

    Recovery may be evaluated only from a window that starts at or after the
    reset point and does not predate the observation timestamp. Telemetry
    quality must come from collector samples, never from ``healthy`` flags.
    """

    _require_tenant(ctx.tenant_id)
    observed_at = _aware(observed_at)
    window_started_at = _aware(window_started_at)
    if window_started_at > observed_at:
        raise FactNormalizationError(
            "observation window cannot start after the observation timestamp"
        )
    recovery_ctx = FactBuildContext(
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
        evidence_samples=tuple(evidence_samples),
        required_signals=frozenset({"telemetry_quality"}),
    )
    identity = _resolve_identity(recovery_ctx)
    telemetry = _build_telemetry(recovery_ctx)
    healthy = bool(observation_state.services) and telemetry.quality >= 0.80
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
