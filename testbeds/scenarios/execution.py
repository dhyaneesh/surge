"""Fail-closed lifecycle coordinator for executable Guardian scenarios."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from testbeds.adapters.base import EnvironmentAdapter
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


def _control_stimulus(scenario: GuardianScenarioV1Alpha2) -> ControlStimulus:
    stimulus = scenario.spec.stimulus
    return ControlStimulus(
        telemetry_mode=stimulus.telemetry.mode.value if stimulus.telemetry else None,
        policy_bundle_state=(
            stimulus.policy_bundle.state.value if stimulus.policy_bundle else None
        ),
        approval_after_expiry=stimulus.approval is not None,
        operator_drift=stimulus.operator_drift is not None,
        foreign_tenant=stimulus.tenant_injection is not None,
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
            control = _control_stimulus(scenario)
            ctx = _fact_context(
                scenario,
                registration,
                observations,
                load_result=load_artifacts[-1] if load_artifacts else None,
                fault_result=fault_artifacts[-1] if fault_artifacts else None,
                deployment_result=deployment_artifacts[-1]
                if deployment_artifacts
                else None,
                observed_at=observed_at,
                control=control,
                tenant_id=settings.tenant_id,
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
            initial_results = evaluate_assertions(scenario, initial_snapshot)
            assertions.extend(
                result
                for result in initial_results
                if result.name != RECOVERY_ASSERTION
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
                        observation_payloads,
                        guardian_artifacts,
                    )
                    fresh_snapshot = await bounded(
                        self.guardian.observe(submissions[0].incident_id)
                    )
                    guardian_artifacts.append(fresh_snapshot)
            except Exception as exc:
                operational_error = operational_error or exc
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
                        if result.name == RECOVERY_ASSERTION
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
                writer.write("cleanup.json", {"completed": True})
            except Exception as exc:
                operational_error = operational_error or exc
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
            },
        )

        passed = (
            not cancelled
            and operational_error is None
            and all(item.passed for item in assertions)
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
            },
        )
        return ExecutionResult(
            execution_id,
            status,
            writer.directory,
            tuple(assertions),
            tuple(transitions),
        )

    async def _submit_fresh_observation(
        self,
        scenario: GuardianScenarioV1Alpha2,
        registration: AdapterRegistration,
        settings: ExecutionSettings,
        ctx: FactBuildContext | None,
        incident_id: str,
        bounded,
        observation_payloads: list,
        guardian_artifacts: list,
    ) -> None:
        if ctx is None:
            return
        fresh_state = await bounded(registration.adapter.observe_state())
        observed_at = datetime.now(UTC)
        observation = build_observation_update(
            ctx,
            incident_id=incident_id,
            observation_id=f"{incident_id}-recovery-1",
            sequence=1,
            window_key="post-reset-1",
            observed_at=observed_at,
            window_started_at=observed_at - timedelta(seconds=30),
            observation_state=fresh_state,
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
