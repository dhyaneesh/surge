"""Fail-closed lifecycle coordinator for executable Guardian scenarios."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from testbeds.adapters.base import EnvironmentAdapter
from testbeds.evidence.collector import EvidenceSample, UnavailableEvidence
from testbeds.models import (
    DeploymentSpecification,
    EnvironmentRelease,
    EnvironmentState,
    FaultSpecification,
    FaultType,
    LoadProfile,
    WorkloadSelector,
)
from testbeds.scenarios.artifacts import ArtifactWriter
from testbeds.scenarios.assertions import AssertionResult, evaluate_assertions
from testbeds.scenarios.compatibility import (
    CompatibilityStatus,
    EnvironmentDeclaration,
    derive_compatibility,
)
from testbeds.scenarios.evidence_provider import ScenarioEvidenceProvider
from testbeds.scenarios.facts import (
    ControlStimulus,
    FactBuildContext,
    build_incident_submission,
    build_observation_update,
)
from testbeds.scenarios.guardian_client import GuardianClient
from testbeds.scenarios.models import GuardianScenario
from testbeds.scenarios.v1alpha2 import GuardianScenarioV1Alpha2

RECOVERY_ASSERTION = "recovery.state"


class UnsupportedScenarioError(ValueError):
    pass


class ExecutionStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionState(StrEnum):
    PENDING = "pending"
    VALIDATING = "validating"
    INSTALLING = "installing"
    AWAITING_BASELINE = "awaiting-baseline"
    APPLYING_LOAD = "applying-load"
    INJECTING_FAULT = "injecting-fault"
    DEPLOYING_VERSION = "deploying-version"
    SUBMITTING_INCIDENT = "submitting-incident"
    AWAITING_GUARDIAN = "awaiting-guardian"
    AWAITING_APPROVAL = "awaiting-approval"
    EXECUTING_ACTION = "executing-action"
    VERIFYING_RECOVERY = "verifying-recovery"
    RESETTING = "resetting"
    CLEANING_UP = "cleaning-up"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AdapterRegistration:
    environment: str
    adapter: EnvironmentAdapter
    release: EnvironmentRelease
    declaration: EnvironmentDeclaration
    role_bindings: Mapping[str, str]
    fault_role_bindings: Mapping[FaultType, str]
    deployment_bindings: Mapping[str, tuple[str, str]]
    evidence_provider: ScenarioEvidenceProvider


@dataclass(frozen=True, slots=True)
class ExecutionSettings:
    artifact_root: Path
    tenant_id: str = "tenant-a"
    baseline_timeout: timedelta = timedelta(minutes=15)
    operation_timeout: timedelta = timedelta(minutes=20)
    install_environment: bool = True
    cleanup_environment: bool = True
    verify_cleanup_idempotency: bool = True


@dataclass(frozen=True, slots=True)
class StateTransition:
    state: ExecutionState
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    execution_id: str
    status: ExecutionStatus
    artifact_directory: Path
    assertions: tuple[AssertionResult, ...]
    transitions: tuple[StateTransition, ...]
    reset_completed: bool = False
    cleanup_completed: bool = False
    environment_invalidated: bool = False


class EnvironmentInvalidatedError(RuntimeError):
    """Reset or cleanup failed; the disposable environment is no longer usable."""

    def __init__(self, message: str, *, result: ExecutionResult) -> None:
        super().__init__(message)
        self.result = result


def _control_stimulus(
    scenario: GuardianScenarioV1Alpha2, *, observed_at: datetime
) -> ControlStimulus:
    stimulus = scenario.spec.stimulus
    # Assessment-handoff timestamp for recovery window gating (not a K8s mutation
    # completion). Recommendation-only scenarios stamp this when recovery is
    # expected so an independent post-reset observation can verify recovery.
    action_completed_at = (
        observed_at if scenario.spec.expected.recovery is not None else None
    )
    return ControlStimulus(
        telemetry_mode=stimulus.telemetry.mode.value if stimulus.telemetry else None,
        policy_bundle_state=(
            stimulus.policy_bundle.state.value if stimulus.policy_bundle else None
        ),
        approval_after_expiry=stimulus.approval is not None,
        operator_drift=stimulus.operator_drift is not None,
        foreign_tenant=stimulus.tenant_injection is not None,
        action_completed_at=action_completed_at,
    )


_EVIDENCE_TYPE_SIGNALS: dict[str, frozenset[str]] = {
    "load": frozenset({"request_rate"}),
    "resource-utilization": frozenset({"cpu_utilization"}),
    "dependency-health": frozenset({"dependency_healthy"}),
    "exceptions": frozenset({"error_rate"}),
    "deployment-event": frozenset({"deployment_version"}),
    "topology": frozenset({"topology_edge"}),
    "telemetry-quality": frozenset({"telemetry_quality"}),
}


def _required_signals(scenario: GuardianScenarioV1Alpha2) -> frozenset[str]:
    required: set[str] = {"telemetry_quality"}
    expected = scenario.spec.expected
    for group in (
        expected.evidence.supporting,
        expected.evidence.contradicting,
    ):
        for item in group:
            mapped = _EVIDENCE_TYPE_SIGNALS.get(item.evidence_type.value)
            if mapped:
                required |= set(mapped)
    return frozenset(required)


DEFERRED_RECOVERY_ASSERTIONS = frozenset(
    {
        "recovery.state",
        "evidence.required_fresh",
        "safety_gates",
        "workflow.required_states",
    }
)


def _fact_context(
    scenario: GuardianScenarioV1Alpha2,
    registration: AdapterRegistration,
    observations: list[EnvironmentState],
    *,
    load_result,
    fault_result,
    deployment_result,
    observed_at: datetime,
    control: ControlStimulus,
    tenant_id: str,
    evidence_samples: Sequence[EvidenceSample | UnavailableEvidence],
) -> FactBuildContext:
    return FactBuildContext(
        tenant_id=tenant_id,
        environment=registration.environment,
        target_role=scenario.spec.target.service_selector.role.value,
        role_bindings=registration.role_bindings,
        release=registration.release,
        observed_at=observed_at,
        observations=tuple(observations),
        load_result=load_result,
        fault_result=fault_result,
        deployment_result=deployment_result,
        control=control,
        evidence_samples=tuple(evidence_samples),
        required_signals=_required_signals(scenario),
    )


class ScenarioExecutor:
    def __init__(self, guardian: GuardianClient) -> None:
        self.guardian = guardian

    async def execute(
        self,
        scenario: GuardianScenario | GuardianScenarioV1Alpha2,
        registration: AdapterRegistration,
        settings: ExecutionSettings,
    ) -> ExecutionResult:
        if not isinstance(scenario, GuardianScenarioV1Alpha2):
            raise UnsupportedScenarioError(
                "v1alpha1 requires explicit upgrade before execution"
            )
        if registration.environment not in scenario.spec.candidate_environments:
            raise UnsupportedScenarioError(
                f"scenario {scenario.metadata.name} does not target {registration.environment}"
            )
        compatibility = derive_compatibility(scenario, registration.declaration)
        if compatibility.status is not CompatibilityStatus.SUPPORTED:
            raise UnsupportedScenarioError(
                f"scenario {scenario.metadata.name} is {compatibility.status.value} for {registration.environment}: "
                f"{', '.join(item.value for item in compatibility.missing_capabilities)}"
            )

        execution_id = uuid.uuid4().hex
        writer = ArtifactWriter(settings.artifact_root / execution_id)
        transitions: list[StateTransition] = []
        assertions: list[AssertionResult] = []
        installed = False
        operational_error: Exception | None = None
        cancelled = False
        reset_completed = False
        cleanup_completed = False
        environment_invalidated = False

        async def bounded(operation):
            return await asyncio.wait_for(
                operation, timeout=settings.operation_timeout.total_seconds()
            )

        def transition(state: ExecutionState) -> None:
            transitions.append(StateTransition(state))

        transition(ExecutionState.PENDING)
        transition(ExecutionState.VALIDATING)
        writer.write(
            "execution-metadata.json",
            {
                "schemaVersion": "guardian.scenario-execution/v1",
                "executionId": execution_id,
                "scenarioId": scenario.metadata.name,
            },
        )
        writer.write("scenario.json", scenario)
        writer.write("capabilities.json", compatibility)
        writer.write("environment-release.json", registration.release)
        writer.write(
            "environment-identity.json",
            {
                "environment": registration.environment,
                "release": registration.release,
                "roles": registration.role_bindings,
            },
        )
        observations: list[EnvironmentState] = []
        load_artifacts = []
        fault_artifacts = []
        deployment_artifacts = []
        guardian_artifacts = []
        submissions = []
        incident_payloads = []
        observation_payloads = []
        recovery_expected = scenario.spec.expected.recovery is not None
        fresh_snapshot = None
        ctx: FactBuildContext | None = None
        try:
            if settings.install_environment:
                transition(ExecutionState.INSTALLING)
                observations.append(
                    await bounded(registration.adapter.install(registration.release))
                )
                installed = True
            transition(ExecutionState.AWAITING_BASELINE)
            baseline = await bounded(
                registration.adapter.wait_for_healthy_baseline(
                    settings.baseline_timeout
                )
            )
            writer.write("baseline.json", baseline)
            observations.append(await bounded(registration.adapter.observe_state()))

            stimulus = scenario.spec.stimulus
            if stimulus.load is not None:
                transition(ExecutionState.APPLYING_LOAD)
                users = {2: 10, 4: 25}.get(round(stimulus.load.multiplier), 10)
                load_artifacts.append(
                    await bounded(registration.adapter.apply_load(LoadProfile(users)))
                )
                observations.append(await bounded(registration.adapter.observe_state()))
            if stimulus.fault is not None:
                transition(ExecutionState.INJECTING_FAULT)
                role = registration.fault_role_bindings.get(
                    stimulus.fault.fault_type,
                    self._bound_role(scenario, registration),
                )
                fault_artifacts.append(
                    await bounded(
                        registration.adapter.inject_fault(
                            FaultSpecification(
                                stimulus.fault.fault_type,
                                WorkloadSelector(role),
                                stimulus.fault.magnitude,
                            )
                        )
                    )
                )
                observations.append(await bounded(registration.adapter.observe_state()))
            if stimulus.deployment is not None:
                transition(ExecutionState.DEPLOYING_VERSION)
                try:
                    version, digest = registration.deployment_bindings[
                        stimulus.deployment.to_version
                    ]
                except KeyError as exc:
                    raise UnsupportedScenarioError(
                        f"no immutable deployment binding for {stimulus.deployment.to_version!r}"
                    ) from exc
                role = self._bound_role(scenario, registration)
                deployment_artifacts.append(
                    await bounded(
                        registration.adapter.deploy_version(
                            DeploymentSpecification(
                                WorkloadSelector(role), version, digest
                            )
                        )
                    )
                )
                observations.append(await bounded(registration.adapter.observe_state()))

            observed_at = datetime.now(UTC)
            control = _control_stimulus(scenario, observed_at=observed_at)
            control_results: Mapping[str, Any] = {
                "load": load_artifacts[-1] if load_artifacts else None,
                "fault": fault_artifacts[-1] if fault_artifacts else None,
                "deployment": deployment_artifacts[-1]
                if deployment_artifacts
                else None,
            }
            evidence_samples = (
                await registration.evidence_provider.collect_assessment_evidence(
                    scenario=scenario,
                    registration=registration,
                    observations=observations,
                    control_results=control_results,
                )
            )
            writer.write("assessment-evidence.json", evidence_samples)
            ctx = _fact_context(
                scenario,
                registration,
                observations,
                load_result=control_results["load"],
                fault_result=control_results["fault"],
                deployment_result=control_results["deployment"],
                observed_at=observed_at,
                control=control,
                tenant_id=settings.tenant_id,
                evidence_samples=evidence_samples,
            )
            submission = build_incident_submission(ctx)
            transition(ExecutionState.SUBMITTING_INCIDENT)
            incident_payloads.append(submission)
            delivery_count = (
                stimulus.incident_delivery.count if stimulus.incident_delivery else 1
            )
            for _ in range(delivery_count):
                submissions.append(
                    await bounded(
                        self.guardian.submit_incident(
                            submission, idempotency_key=execution_id
                        )
                    )
                )
            guardian_artifacts.extend(submissions)
            transition(ExecutionState.AWAITING_GUARDIAN)
            initial_snapshot = await bounded(
                self.guardian.observe(submissions[0].incident_id)
            )
            guardian_artifacts.append(initial_snapshot)
            writer.write("initial-snapshot.json", initial_snapshot)
            initial_results = evaluate_assertions(scenario, initial_snapshot)
            deferred = (
                DEFERRED_RECOVERY_ASSERTIONS
                if recovery_expected
                else frozenset({RECOVERY_ASSERTION})
            )
            assertions.extend(
                result for result in initial_results if result.name not in deferred
            )
        except asyncio.CancelledError:
            cancelled = True
            assertions.append(
                AssertionResult(
                    name="execution.cancelled",
                    passed=False,
                    expected=False,
                    actual=True,
                )
            )
        except Exception as exc:
            operational_error = exc
            assertions.append(
                AssertionResult(
                    name="execution.error",
                    passed=False,
                    expected="successful lifecycle",
                    actual=f"{type(exc).__name__}: {exc}",
                )
            )

        if installed:
            try:
                transition(ExecutionState.RESETTING)
                await bounded(registration.adapter.reset())
                await bounded(
                    registration.adapter.wait_for_healthy_baseline(
                        settings.baseline_timeout
                    )
                )
                post_reset_state = await bounded(registration.adapter.observe_state())
                recovery_samples = (
                    await registration.evidence_provider.collect_recovery_evidence(
                        scenario=scenario,
                        registration=registration,
                        post_reset_state=post_reset_state,
                    )
                )
                writer.write("recovery-evidence.json", recovery_samples)
                reset_completed = True
                transition(ExecutionState.VERIFYING_RECOVERY)
                writer.write("reset.json", {"completed": True})
                if submissions:
                    await self._submit_fresh_observation(
                        scenario,
                        registration,
                        settings,
                        ctx,
                        submissions[0].incident_id,
                        bounded,
                        post_reset_state,
                        recovery_samples,
                        observation_payloads,
                        guardian_artifacts,
                    )
                    fresh_snapshot = await bounded(
                        self.guardian.observe(submissions[0].incident_id)
                    )
                    guardian_artifacts.append(fresh_snapshot)
                    writer.write("recovery-snapshot.json", fresh_snapshot)
            except Exception as exc:
                operational_error = operational_error or exc
                environment_invalidated = True
                assertions.append(
                    AssertionResult(
                        name="reset.restored_baseline",
                        passed=False,
                        expected=True,
                        actual=f"{type(exc).__name__}: {exc}",
                    )
                )

            if recovery_expected:
                if fresh_snapshot is not None:
                    fresh_results = evaluate_assertions(scenario, fresh_snapshot)
                    assertions.extend(
                        result
                        for result in fresh_results
                        if result.name in DEFERRED_RECOVERY_ASSERTIONS
                    )
                else:
                    assertions.append(
                        AssertionResult(
                            name=RECOVERY_ASSERTION,
                            passed=False,
                            expected="fresh post-reset observation window",
                            actual="no fresh observation window",
                        )
                    )

        if settings.cleanup_environment:
            try:
                transition(ExecutionState.CLEANING_UP)
                await bounded(registration.adapter.cleanup())
                if settings.verify_cleanup_idempotency:
                    await bounded(registration.adapter.cleanup())
                cleanup_completed = True
                writer.write("cleanup.json", {"completed": True})
            except Exception as exc:
                operational_error = operational_error or exc
                environment_invalidated = True
                assertions.append(
                    AssertionResult(
                        name="cleanup.completed",
                        passed=False,
                        expected=True,
                        actual=f"{type(exc).__name__}: {exc}",
                    )
                )

        writer.write("observations.json", observations)
        writer.write("load-results.json", load_artifacts)
        writer.write("fault-results.json", fault_artifacts)
        writer.write("deployment-results.json", deployment_artifacts)
        writer.write("incident-payloads.json", incident_payloads)
        writer.write("observation-payloads.json", observation_payloads)
        writer.write("guardian.json", guardian_artifacts)
        writer.write(
            "diagnostics.json",
            {
                "observations": observations,
                "operationalError": (
                    f"{type(operational_error).__name__}: {operational_error}"
                    if operational_error
                    else None
                ),
                "environmentInvalidated": environment_invalidated,
                "resetCompleted": reset_completed,
                "cleanupCompleted": cleanup_completed,
            },
        )

        passed = (
            not cancelled
            and operational_error is None
            and all(item.passed for item in assertions)
            and not environment_invalidated
        )
        status = (
            ExecutionStatus.CANCELLED
            if cancelled
            else ExecutionStatus.PASSED
            if passed
            else ExecutionStatus.FAILED
        )
        transition(
            ExecutionState.CANCELLED
            if cancelled
            else ExecutionState.PASSED
            if passed
            else ExecutionState.FAILED
        )
        writer.write("assertions.json", assertions)
        writer.write("timeline.json", transitions)
        writer.write(
            "summary.json",
            {
                "schemaVersion": "guardian.scenario-execution-summary/v1",
                "executionId": execution_id,
                "scenarioId": scenario.metadata.name,
                "environment": registration.environment,
                "status": status,
                "assertionsPassed": sum(item.passed for item in assertions),
                "assertionsFailed": sum(not item.passed for item in assertions),
                "resetCompleted": reset_completed,
                "cleanupCompleted": cleanup_completed,
                "environmentInvalidated": environment_invalidated,
            },
        )
        result = ExecutionResult(
            execution_id,
            status,
            writer.directory,
            tuple(assertions),
            tuple(transitions),
            reset_completed=reset_completed,
            cleanup_completed=cleanup_completed,
            environment_invalidated=environment_invalidated,
        )
        if environment_invalidated:
            raise EnvironmentInvalidatedError(
                f"environment invalidated during scenario {scenario.metadata.name}",
                result=result,
            )
        return result

    async def _submit_fresh_observation(
        self,
        scenario: GuardianScenarioV1Alpha2,
        registration: AdapterRegistration,
        settings: ExecutionSettings,
        ctx: FactBuildContext | None,
        incident_id: str,
        bounded,
        post_reset_state: EnvironmentState,
        recovery_samples: Sequence[EvidenceSample | UnavailableEvidence],
        observation_payloads: list,
        guardian_artifacts: list,
    ) -> None:
        if ctx is None:
            return
        observed_at = datetime.now(UTC)
        handoff = ctx.control.action_completed_at
        window_started_at = observed_at - timedelta(seconds=30)
        if handoff is not None and window_started_at <= handoff:
            window_started_at = handoff + timedelta(milliseconds=1)
        if window_started_at > observed_at:
            observed_at = window_started_at + timedelta(seconds=1)
        observation = build_observation_update(
            ctx,
            incident_id=incident_id,
            observation_id=f"{incident_id}-recovery-1",
            sequence=1,
            window_key="post-reset-1",
            observed_at=observed_at,
            window_started_at=window_started_at,
            observation_state=post_reset_state,
            evidence_samples=recovery_samples,
        )
        observation_payloads.append(observation)
        guardian_artifacts.append(
            await bounded(
                self.guardian.submit_observation(
                    observation, idempotency_key=f"{incident_id}-recovery"
                )
            )
        )

    @staticmethod
    def _bound_role(
        scenario: GuardianScenarioV1Alpha2, registration: AdapterRegistration
    ) -> str:
        normalized = scenario.spec.target.service_selector.role.value
        try:
            return registration.role_bindings[normalized]
        except KeyError as exc:
            raise UnsupportedScenarioError(
                f"no adapter role binding for {normalized!r}"
            ) from exc
