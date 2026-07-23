"""Unit contracts for testbed-to-production Guardian fact normalization.

TST-GRD-OPA-003-UNIT; TST-GRD-OPA-006-UNIT; TST-GRD-DRIFT-001-INTEGRATION; TST-GRD-DRIFT-003-INTEGRATION.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.guardian_api.models import (
    EvidenceSource,
    IncidentSubmission,
    ObservationUpdate,
    PolicyState,
    RequiredSignal,
)
from testbeds.evidence.collector import EvidenceSample, UnavailableEvidence
from testbeds.evidence.contracts import EvidenceSourceKind
from testbeds.models import (
    DeploymentEvent,
    EnvironmentRelease,
    EnvironmentState,
    FaultExecution,
    FaultSpecification,
    FaultType,
    LoadExecution,
    LoadProfile,
    ObservedServiceIdentity,
    RolloutState,
    ScalingState,
    WorkloadSelector,
    WorkloadState,
)
from testbeds.scenarios.facts import (
    ControlStimulus,
    FactBuildContext,
    FactNormalizationError,
    MissingEvidenceError,
    build_incident_submission,
    build_observation_update,
)

NOW = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def service(
    role: str = "transaction-processor",
    name: str | None = None,
    digest: str = DIGEST_A,
) -> ObservedServiceIdentity:
    return ObservedServiceIdentity(role, name or role, "1.2.3", digest)


def workload(
    role: str = "transaction-processor",
    name: str | None = None,
    digest: str = DIGEST_A,
) -> WorkloadState:
    bound = name or role
    return WorkloadState(role, bound, 3, 3, image=f"repo/{bound}@{digest}")


def healthy_state(
    *, services=(), workloads=(), scaling=None, rollouts=()
) -> EnvironmentState:
    return EnvironmentState(
        environment="otel-demo",
        namespace="guardian-test",
        release=EnvironmentRelease(environment="otel-demo"),
        workloads=tuple(workloads) or (workload(),),
        services=tuple(services) or (service(),),
        rollouts=tuple(rollouts),
        healthy=True,
        scaling=scaling,
    )


def telemetry_sample(
    *,
    quality: float = 1.0,
    usable: int = 10,
    pipeline_available: bool = True,
    observed_at: datetime = NOW,
) -> EvidenceSample:
    return EvidenceSample(
        EvidenceSourceKind.SIGNOZ_TELEMETRY,
        observed_at=observed_at,
        provenance_ref="signoz/telemetry-quality",
        values={
            "quality": quality,
            "usable_samples": usable,
            "required_samples": 10,
            "pipeline_available": pipeline_available,
            "comparison_valid": pipeline_available and quality >= 0.80,
        },
    )


def numeric_sample(
    kind: EvidenceSourceKind,
    *,
    values: dict,
    provenance: str,
    observed_at: datetime = NOW,
) -> EvidenceSample:
    return EvidenceSample(
        kind,
        observed_at=observed_at,
        provenance_ref=provenance,
        values=values,
    )


def context(
    *,
    observations=(),
    load_result=None,
    fault_result=None,
    deployment_result=None,
    control=ControlStimulus(),
    target_role="request-processor",
    bound="transaction-processor",
    service_name: str | None = None,
    tenant_id="tenant-a",
    evidence_samples=None,
    required_signals=frozenset({"telemetry_quality"}),
) -> FactBuildContext:
    resolved_name = service_name or bound
    default_observation = healthy_state(
        services=(service(role=bound, name=resolved_name),),
        workloads=(workload(role=bound, name=resolved_name),),
    )
    samples = (
        (telemetry_sample(),) if evidence_samples is None else tuple(evidence_samples)
    )
    return FactBuildContext(
        tenant_id=tenant_id,
        environment="otel-demo",
        target_role=target_role,
        role_bindings={target_role: bound},
        release=EnvironmentRelease(environment="otel-demo"),
        observed_at=NOW,
        observations=observations or (default_observation,),
        load_result=load_result,
        fault_result=fault_result,
        deployment_result=deployment_result,
        control=control,
        evidence_samples=samples,
        required_signals=frozenset(required_signals),
    )


def test_incident_submission_has_no_scenario_or_expected_or_stimulus_fields() -> None:
    submission = build_incident_submission(context())

    assert isinstance(submission, IncidentSubmission)
    assert submission.schema_version == "guardian.incident-facts/v1"
    for forbidden in ("scenario_id", "scenarioId", "expected", "stimulus", "assertion"):
        assert forbidden not in IncidentSubmission.model_fields
        assert forbidden not in submission.model_dump(mode="json")


def test_incident_submission_carries_authenticated_tenant_and_observed_identity() -> (
    None
):
    submission = build_incident_submission(context())

    assert submission.tenant_id == "tenant-a"
    identity = submission.identity
    assert identity is not None
    assert identity.target_role == "request-processor"
    assert identity.service_name == "transaction-processor"
    assert identity.image_digest == DIGEST_A
    assert identity.namespace == "guardian-test"
    assert identity.workload_kind
    assert identity.workload_name == "transaction-processor"


def test_evidence_identity_uses_bound_target_identity_not_role_string() -> None:
    submission = build_incident_submission(
        context(
            bound="transaction-processor",
            service_name="checkout",
            evidence_samples=(
                telemetry_sample(),
                numeric_sample(
                    EvidenceSourceKind.ENDPOINT_PROBE,
                    values={
                        "request_rate": 200.0,
                        "baseline_request_rate": 100.0,
                        "usable_samples": 10,
                        "required_samples": 10,
                    },
                    provenance="endpoint-probe/request-rate",
                ),
            ),
            required_signals=frozenset({"telemetry_quality", "request_rate"}),
        )
    )

    identity = submission.identity
    assert identity is not None
    assert identity.target_role == "request-processor"
    assert identity.workload_name == "checkout"
    assert identity.service_name == "checkout"
    rate = submission.signals.request_rate
    assert rate is not None
    assert rate.subject_role == "request-processor"
    assert rate.workload_name == "checkout"
    assert rate.service_name == "checkout"
    assert rate.environment == identity.environment
    assert rate.namespace == identity.namespace
    assert rate.workload_kind == identity.workload_kind


def test_environment_healthy_and_successful_controls_are_not_telemetry_proof() -> None:
    load = LoadExecution(LoadProfile(25), active=True)
    fault = FaultExecution(
        FaultSpecification(FaultType.HIGH_CPU, WorkloadSelector("checkout"), 1.0),
        active=True,
    )
    with pytest.raises(MissingEvidenceError, match="telemetry"):
        build_incident_submission(
            context(
                load_result=load,
                fault_result=fault,
                evidence_samples=(),
                required_signals=frozenset({"telemetry_quality"}),
            )
        )


def test_successful_load_injection_does_not_create_request_rate_evidence() -> None:
    submission = build_incident_submission(
        context(
            load_result=LoadExecution(LoadProfile(25), active=True),
            evidence_samples=(telemetry_sample(),),
            required_signals=frozenset({"telemetry_quality"}),
        )
    )

    assert submission.signals.request_rate is None
    assert submission.telemetry.quality == 1.0


def test_collector_samples_produce_required_signal_evidence() -> None:
    samples = (
        telemetry_sample(),
        numeric_sample(
            EvidenceSourceKind.ENDPOINT_PROBE,
            values={
                "request_rate": 220.0,
                "baseline_request_rate": 100.0,
                "error_rate": 0.02,
                "baseline_error_rate": 0.01,
                "p95_latency_ms": 120.0,
                "baseline_p95_latency_ms": 80.0,
                "usable_samples": 10,
                "required_samples": 10,
            },
            provenance="endpoint-probe/frontend",
        ),
        numeric_sample(
            EvidenceSourceKind.METRICS_API,
            values={
                "cpu_utilization": 0.91,
                "memory_utilization": 0.72,
                "usable_samples": 10,
                "required_samples": 10,
            },
            provenance="metrics-api/pod",
        ),
        numeric_sample(
            EvidenceSourceKind.KUBERNETES_WORKLOAD,
            values={
                "restart_delta": 0.0,
                "topology_edge": True,
                "usable_samples": 10,
                "required_samples": 10,
            },
            provenance="kubernetes-workload/checkout",
        ),
        numeric_sample(
            EvidenceSourceKind.ROLLOUT_STATE,
            values={
                "dependency_healthy": True,
                "usable_samples": 10,
                "required_samples": 10,
            },
            provenance="rollout-state/cache",
        ),
    )
    submission = build_incident_submission(
        context(
            evidence_samples=samples,
            required_signals=frozenset(
                {
                    "telemetry_quality",
                    "request_rate",
                    "cpu_utilization",
                    "memory_utilization",
                    "error_rate",
                    "p95_latency_ms",
                    "restart_delta",
                    "topology_edge",
                    "dependency_healthy",
                }
            ),
        )
    )

    assert submission.signals.request_rate is not None
    assert submission.signals.request_rate.value == 220.0
    assert submission.signals.cpu_utilization is not None
    assert submission.signals.memory_utilization is not None
    assert submission.signals.error_rate is not None
    assert submission.signals.p95_latency_ms is not None
    assert submission.signals.restart_delta is not None
    assert submission.signals.topology_edge is not None
    assert submission.signals.dependency_healthy is not None
    assert submission.signals.dependency_healthy.subject_role == "dependency"


def test_missing_required_collector_sample_fails_closed() -> None:
    with pytest.raises(MissingEvidenceError, match="request_rate"):
        build_incident_submission(
            context(
                evidence_samples=(telemetry_sample(),),
                required_signals=frozenset({"telemetry_quality", "request_rate"}),
            )
        )


def test_unavailable_required_sample_fails_closed() -> None:
    with pytest.raises(MissingEvidenceError, match="telemetry"):
        build_incident_submission(
            context(
                evidence_samples=(
                    UnavailableEvidence(
                        EvidenceSourceKind.SIGNOZ_TELEMETRY,
                        reason="signoz unavailable",
                        observed_at=NOW,
                        provenance_ref="signoz/telemetry-quality",
                    ),
                ),
                required_signals=frozenset({"telemetry_quality"}),
            )
        )


def test_deployment_result_becomes_version_evidence_not_a_stimulus() -> None:
    deployment = DeploymentEvent(
        target=WorkloadSelector("transaction-processor"),
        from_version="1.2.3",
        to_version="1.4.5",
        observed_at=NOW,
    )
    baseline = healthy_state(services=(service(digest=DIGEST_A),))
    after = healthy_state(services=(service(digest=DIGEST_B),))
    submission = build_incident_submission(
        context(
            observations=(baseline, after),
            deployment_result=deployment,
            evidence_samples=(
                telemetry_sample(),
                numeric_sample(
                    EvidenceSourceKind.KUBERNETES_WORKLOAD,
                    values={
                        "previous_service_version": "1.2.3",
                        "current_service_version": "1.4.5",
                        "previous_digest": DIGEST_A,
                        "current_digest": DIGEST_B,
                        "usable_samples": 10,
                        "required_samples": 10,
                    },
                    provenance="kubernetes-workload/version-transition",
                ),
            ),
            required_signals=frozenset({"telemetry_quality", "deployment_version"}),
        )
    )

    evidence = submission.signals.deployment_version
    assert evidence is not None
    assert evidence.source is EvidenceSource.ADAPTER_OBSERVATION
    assert evidence.previous_digest == DIGEST_A
    assert evidence.current_digest == DIGEST_B
    assert evidence.tenant_id == "tenant-a"
    assert evidence.subject_role == "request-processor"
    assert evidence.workload_name == "transaction-processor"


def test_dependency_health_evidence_comes_from_collector_not_rollout_alone() -> None:
    unhealthy = healthy_state(
        services=(service(), ObservedServiceIdentity("cache", "cache", None, None)),
        rollouts=(
            RolloutState(
                name="cache",
                phase="Degraded",
                paused=False,
                desired_replicas=3,
                ready_replicas=1,
                updated_replicas=3,
                unavailable_replicas=2,
                stable_hash=None,
                canary_hash=None,
                conditions=("ProgressDeadlineExceeded",),
                recovery_healthy=False,
            ),
        ),
    )
    with pytest.raises(MissingEvidenceError, match="dependency"):
        build_incident_submission(
            context(
                observations=(unhealthy,),
                evidence_samples=(telemetry_sample(),),
                required_signals=frozenset({"telemetry_quality", "dependency_healthy"}),
            )
        )


def test_scaler_facts_are_normalized_only_when_observed() -> None:
    scaling = ScalingState(current_replicas=2, desired_replicas=4, queue_depth=12)
    state = healthy_state(scaling=scaling)
    submission = build_incident_submission(context(observations=(state,)))

    assert submission.scaler is not None
    assert submission.scaler.tenant_id == "tenant-a"
    assert submission.scaler.requested_direction.value == "up"


def test_telemetry_quality_comes_from_collector_sample() -> None:
    submission = build_incident_submission(context())

    assert submission.telemetry.quality == 1.0
    assert submission.telemetry.pipeline_available is True
    assert RequiredSignal.TELEMETRY_QUALITY in submission.telemetry.required_signals


def test_missing_tenant_fails_closed_before_normalization() -> None:
    with pytest.raises(FactNormalizationError):
        build_incident_submission(context(tenant_id=""))


def test_unresolved_identity_fails_closed_as_missing_required_evidence() -> None:
    bare = EnvironmentState(
        environment="otel-demo", namespace="guardian-test", healthy=True
    )
    with pytest.raises(FactNormalizationError, match="identity"):
        build_incident_submission(context(observations=(bare,)))


def test_foreign_tenant_control_injects_foreign_evidence_for_api_rejection() -> None:
    submission = build_incident_submission(
        context(control=ControlStimulus(foreign_tenant=True))
    )

    evidence_tenants = {item.tenant_id for item in submission.signals.all_evidence()}
    assert "foreign-tenant" in evidence_tenants
    assert any(
        item.provenance_ref.startswith("test-control")
        for item in submission.signals.all_evidence()
    )


def test_policy_control_fixture_is_typed_with_provenance_not_a_stimulus() -> None:
    submission = build_incident_submission(
        context(control=ControlStimulus(policy_bundle_state="fail-closed"))
    )

    assert submission.policy.state is PolicyState.FAIL_CLOSED
    assert "scenario" not in submission.policy.model_dump(mode="json")


def test_drift_and_approval_controls_carry_explicit_control_provenance() -> None:
    submission = build_incident_submission(
        context(
            control=ControlStimulus(approval_after_expiry=True, operator_drift=True)
        )
    )

    assert submission.control.approval_expires_at is not None
    assert submission.control.protected_fingerprint == "protected-resource-v1"
    assert submission.control.current_fingerprint == "operator-changed-v2"


def test_telemetry_control_fixture_marks_unhealthy_without_fabricating_symptoms() -> (
    None
):
    submission = build_incident_submission(
        context(
            control=ControlStimulus(telemetry_mode="interrupted"),
            evidence_samples=(),
            required_signals=frozenset({"telemetry_quality"}),
        )
    )

    assert submission.telemetry.quality < 0.80
    assert submission.telemetry.pipeline_available is False
    assert submission.signals.request_rate is None
    assert submission.signals.error_rate is None


def test_observation_update_requires_fresh_window_after_reset() -> None:
    ctx = context()
    build_incident_submission(ctx)
    incident_id = "incident-1"
    fresh_at = NOW + timedelta(minutes=1)

    update = build_observation_update(
        ctx=ctx,
        incident_id=incident_id,
        observation_id="observation-1",
        sequence=1,
        window_key="post-reset-1",
        observed_at=fresh_at,
        window_started_at=fresh_at - timedelta(seconds=30),
        observation_state=healthy_state(),
        evidence_samples=(
            telemetry_sample(observed_at=fresh_at - timedelta(seconds=5)),
        ),
    )

    assert isinstance(update, ObservationUpdate)
    assert update.incident_id == incident_id
    assert update.tenant_id == "tenant-a"
    assert update.observed_at == fresh_at
    assert update.service_healthy is True
    assert update.window_started_at <= update.observed_at
    assert update.telemetry.quality == 1.0


def test_observation_update_rejects_healthy_flag_as_telemetry_proof() -> None:
    ctx = context()
    fresh_at = NOW + timedelta(minutes=1)

    with pytest.raises(MissingEvidenceError, match="telemetry"):
        build_observation_update(
            ctx=ctx,
            incident_id="incident-1",
            observation_id="observation-1",
            sequence=1,
            window_key="post-reset-1",
            observed_at=fresh_at,
            window_started_at=fresh_at - timedelta(seconds=30),
            observation_state=healthy_state(),
            evidence_samples=(),
        )


def test_observation_update_rejects_a_window_that_starts_in_the_future() -> None:
    ctx = context()
    fresh_at = NOW + timedelta(minutes=1)

    with pytest.raises(FactNormalizationError):
        build_observation_update(
            ctx=ctx,
            incident_id="incident-1",
            observation_id="observation-1",
            sequence=1,
            window_key="post-reset-1",
            observed_at=fresh_at,
            window_started_at=fresh_at + timedelta(seconds=1),
            observation_state=healthy_state(),
            evidence_samples=(telemetry_sample(observed_at=fresh_at),),
        )


def test_facts_never_embed_secrets_or_demo_service_name_literals() -> None:
    import inspect

    from testbeds.scenarios import facts as facts_module

    source = inspect.getsource(facts_module)
    for demo in (
        "checkoutservice",
        "transaction-processor",
        "rabbitmq",
        "redis",
        "frontend",
    ):
        assert demo not in source, f"demo service name leaked into facts.py: {demo}"
    for secret in ("Bearer", "GUARDIAN_SCENARIO_TOKEN", "password"):
        assert secret not in source
