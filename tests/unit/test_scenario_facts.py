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
from testbeds.models import (
    DeploymentEvent,
    EnvironmentRelease,
    EnvironmentState,
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
    build_incident_submission,
    build_observation_update,
)

NOW = datetime(2026, 7, 23, 10, 0, tzinfo=UTC)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def service(
    role: str = "transaction-processor", digest: str = DIGEST_A
) -> ObservedServiceIdentity:
    return ObservedServiceIdentity(role, role, "1.2.3", digest)


def workload(
    role: str = "transaction-processor", digest: str = DIGEST_A
) -> WorkloadState:
    return WorkloadState(role, role, 3, 3, image=f"repo/{role}@{digest}")


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


def context(
    *,
    observations=(),
    load_result=None,
    fault_result=None,
    deployment_result=None,
    control=ControlStimulus(),
    target_role="request-processor",
    bound="transaction-processor",
    tenant_id="tenant-a",
) -> FactBuildContext:
    return FactBuildContext(
        tenant_id=tenant_id,
        environment="otel-demo",
        target_role=target_role,
        role_bindings={target_role: bound},
        release=EnvironmentRelease(environment="otel-demo"),
        observed_at=NOW,
        observations=observations or (healthy_state(),),
        load_result=load_result,
        fault_result=fault_result,
        deployment_result=deployment_result,
        control=control,
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
        context(observations=(baseline, after), deployment_result=deployment)
    )

    evidence = submission.signals.deployment_version
    assert evidence is not None
    assert evidence.source is EvidenceSource.ADAPTER_OBSERVATION
    assert evidence.previous_digest == DIGEST_A
    assert evidence.current_digest == DIGEST_B
    assert evidence.tenant_id == "tenant-a"


def test_dependency_health_evidence_comes_from_observed_rollout_state() -> None:
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
    submission = build_incident_submission(context(observations=(unhealthy,)))

    dependency = submission.signals.dependency_healthy
    assert dependency is not None
    assert dependency.subject_role == "dependency"
    assert dependency.value is False


def test_scaler_facts_are_normalized_only_when_observed() -> None:
    scaling = ScalingState(current_replicas=2, desired_replicas=4, queue_depth=12)
    state = healthy_state(scaling=scaling)
    submission = build_incident_submission(context(observations=(state,)))

    assert submission.scaler is not None
    assert submission.scaler.tenant_id == "tenant-a"
    assert submission.scaler.requested_direction.value == "up"


def test_telemetry_is_healthy_from_a_fresh_healthy_observation() -> None:
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


def test_telemetry_control_fixture_marks_unhealthy_without_fabricating_symptoms() -> (
    None
):
    submission = build_incident_submission(
        context(control=ControlStimulus(telemetry_mode="interrupted"))
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
    )

    assert isinstance(update, ObservationUpdate)
    assert update.incident_id == incident_id
    assert update.tenant_id == "tenant-a"
    assert update.observed_at == fresh_at
    assert update.service_healthy is True
    assert update.window_started_at <= update.observed_at


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
