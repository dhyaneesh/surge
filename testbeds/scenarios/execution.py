"""Fail-closed lifecycle coordinator for executable Guardian scenarios."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Mapping

from testbeds.adapters.base import EnvironmentAdapter
from testbeds.models import (
    DeploymentSpecification,
    EnvironmentRelease,
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
from testbeds.scenarios.guardian_client import GuardianClient
from testbeds.scenarios.models import GuardianScenario
from testbeds.scenarios.v1alpha2 import GuardianScenarioV1Alpha2


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
    baseline_timeout: timedelta = timedelta(minutes=15)
    operation_timeout: timedelta = timedelta(minutes=20)
    install_environment: bool = True
    cleanup_environment: bool = True
    verify_cleanup_idempotency: bool = True


@dataclass(frozen=True, slots=True)
class StateTransition:
    state: ExecutionState
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    execution_id: str
    status: ExecutionStatus
    artifact_directory: Path
    assertions: tuple[AssertionResult, ...]
    transitions: tuple[StateTransition, ...]


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
        assertions: tuple[AssertionResult, ...] = ()
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
        observations = []
        load_artifacts = []
        fault_artifacts = []
        deployment_artifacts = []
        guardian_artifacts = []
        incident_payloads = []
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

            incident_payload = {
                "schemaVersion": "guardian.scenario-incident/v1",
                "executionId": execution_id,
                "scenarioId": scenario.metadata.name,
                "environment": registration.environment,
                "target": scenario.spec.target.model_dump(mode="json", by_alias=True),
                "stimulus": stimulus.model_dump(mode="json", by_alias=True),
            }
            transition(ExecutionState.SUBMITTING_INCIDENT)
            incident_payloads.append(incident_payload)
            delivery_count = (
                stimulus.incident_delivery.count if stimulus.incident_delivery else 1
            )
            submissions = []
            for _ in range(delivery_count):
                submissions.append(
                    await bounded(
                        self.guardian.submit_incident(
                            incident_payload, idempotency_key=execution_id
                        )
                    )
                )
            guardian_artifacts.extend(submissions)
            transition(ExecutionState.AWAITING_GUARDIAN)
            snapshot = await bounded(self.guardian.observe(submissions[0].incident_id))
            guardian_artifacts.append(snapshot)
            assertions = evaluate_assertions(scenario, snapshot)
        except asyncio.CancelledError:
            cancelled = True
            assertions = (
                AssertionResult(
                    name="execution.cancelled",
                    passed=False,
                    expected=False,
                    actual=True,
                ),
            )
        except Exception as exc:
            operational_error = exc
            assertions = (
                AssertionResult(
                    name="execution.error",
                    passed=False,
                    expected="successful lifecycle",
                    actual=f"{type(exc).__name__}: {exc}",
                ),
            )
        finally:
            writer.write("observations.json", observations)
            writer.write("load-results.json", load_artifacts)
            writer.write("fault-results.json", fault_artifacts)
            writer.write("deployment-results.json", deployment_artifacts)
            writer.write("incident-payloads.json", incident_payloads)
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
            if installed:
                try:
                    transition(ExecutionState.RESETTING)
                    await bounded(registration.adapter.reset())
                    restored = await bounded(
                        registration.adapter.wait_for_healthy_baseline(
                            settings.baseline_timeout
                        )
                    )
                    transition(ExecutionState.VERIFYING_RECOVERY)
                    writer.write("reset.json", {"restored": restored})
                except Exception as exc:
                    operational_error = operational_error or exc
                    assertions += (
                        AssertionResult(
                            name="reset.restored_baseline",
                            passed=False,
                            expected=True,
                            actual=f"{type(exc).__name__}: {exc}",
                        ),
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
                    assertions += (
                        AssertionResult(
                            name="cleanup.completed",
                            passed=False,
                            expected=True,
                            actual=f"{type(exc).__name__}: {exc}",
                        ),
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
            assertions,
            tuple(transitions),
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
