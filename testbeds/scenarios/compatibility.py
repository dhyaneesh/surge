"""Pure capability-driven compatibility and preflight derivation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import model_validator

from testbeds.scenarios.models import StrictModel
from testbeds.scenarios.v1alpha2 import EnvironmentCapability, GuardianScenarioV1Alpha2


class CompatibilityStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


class AdapterOperation(StrEnum):
    INSTALL = "install"
    RESET = "reset"
    BASELINE = "wait-for-healthy-baseline"
    APPLY_LOAD = "apply-load"
    INJECT_FAULT = "inject-fault"
    DEPLOY_VERSION = "deploy-version"
    OBSERVE_STATE = "observe-state"
    CLEANUP = "cleanup"
    INTERRUPT_TELEMETRY = "interrupt-telemetry"
    CONTROL_POLICY = "control-policy-bundle"
    MUTATE_WORKLOAD = "mutate-workload"
    EMIT_INCIDENT = "emit-incident"
    CONTROL_APPROVAL = "control-approval"


class ObservationType(StrEnum):
    BASELINE = "baseline-health"
    TELEMETRY = "telemetry-quality"
    ASSESSMENT = "incident-assessment"
    WORKFLOW = "workflow-state"
    MUTATION = "mutation-count"
    AUDIT = "audit-event"
    WORKLOAD = "workload-state"
    DEPENDENCY = "dependency-topology"
    DEPLOYMENT = "deployment-version"
    RECOVERY = "recovery-evidence"
    SCALER = "scaler-result"
    POLICY = "policy-state"
    TENANT = "tenant-rejection"


CAPABILITY_CONTRACTS: dict[
    EnvironmentCapability,
    tuple[tuple[AdapterOperation, ...], tuple[ObservationType, ...]],
] = {
    EnvironmentCapability.HEALTHY_BASELINE: (
        (AdapterOperation.BASELINE,),
        (ObservationType.BASELINE,),
    ),
    EnvironmentCapability.LOAD_GENERATION: (
        (AdapterOperation.APPLY_LOAD,),
        (ObservationType.WORKLOAD,),
    ),
    EnvironmentCapability.FAULT_INJECTION: (
        (AdapterOperation.INJECT_FAULT,),
        (ObservationType.WORKLOAD,),
    ),
    EnvironmentCapability.DEPLOYMENT_TRANSITION: (
        (AdapterOperation.DEPLOY_VERSION,),
        (ObservationType.DEPLOYMENT,),
    ),
    EnvironmentCapability.PROGRESSIVE_DELIVERY: (
        (AdapterOperation.OBSERVE_STATE,),
        (ObservationType.DEPLOYMENT,),
    ),
    EnvironmentCapability.HORIZONTAL_SCALING: (
        (AdapterOperation.OBSERVE_STATE,),
        (ObservationType.WORKLOAD,),
    ),
    EnvironmentCapability.SCALE_TO_ZERO: (
        (AdapterOperation.OBSERVE_STATE,),
        (ObservationType.SCALER,),
    ),
    EnvironmentCapability.DEPENDENCY_OBSERVATION: (
        (AdapterOperation.OBSERVE_STATE,),
        (ObservationType.DEPENDENCY,),
    ),
    EnvironmentCapability.RESOURCE_PRESSURE: (
        (AdapterOperation.INJECT_FAULT,),
        (ObservationType.WORKLOAD,),
    ),
    EnvironmentCapability.TELEMETRY_INTERRUPTION: (
        (AdapterOperation.INTERRUPT_TELEMETRY,),
        (ObservationType.TELEMETRY,),
    ),
    EnvironmentCapability.POLICY_BUNDLE_CONTROL: (
        (AdapterOperation.CONTROL_POLICY,),
        (ObservationType.POLICY,),
    ),
    EnvironmentCapability.MANUAL_WORKLOAD_MUTATION: (
        (AdapterOperation.MUTATE_WORKLOAD,),
        (ObservationType.WORKLOAD,),
    ),
    EnvironmentCapability.MULTI_TENANT_FIXTURE: (
        (AdapterOperation.EMIT_INCIDENT,),
        (ObservationType.TENANT,),
    ),
    EnvironmentCapability.WORKFLOW_OBSERVATION: (
        (AdapterOperation.OBSERVE_STATE,),
        (ObservationType.WORKFLOW,),
    ),
    EnvironmentCapability.APPROVAL_CONTROL: (
        (AdapterOperation.CONTROL_APPROVAL,),
        (ObservationType.WORKFLOW,),
    ),
    EnvironmentCapability.RECOVERY_OBSERVATION: (
        (AdapterOperation.OBSERVE_STATE,),
        (ObservationType.RECOVERY,),
    ),
    EnvironmentCapability.MUTATION_OBSERVATION: (
        (AdapterOperation.OBSERVE_STATE,),
        (ObservationType.MUTATION, ObservationType.AUDIT),
    ),
    EnvironmentCapability.SCALER_OBSERVATION: (
        (AdapterOperation.OBSERVE_STATE,),
        (ObservationType.SCALER,),
    ),
    EnvironmentCapability.INCIDENT_INGRESS_CONTROL: (
        (AdapterOperation.EMIT_INCIDENT,),
        (ObservationType.WORKFLOW,),
    ),
    EnvironmentCapability.AMBIGUOUS_SYMPTOM: (
        (AdapterOperation.INJECT_FAULT,),
        (ObservationType.TELEMETRY,),
    ),
    EnvironmentCapability.ACTION_CONTROLLER_EXECUTION: (
        (),
        (ObservationType.MUTATION,),
    ),
}


class BlockingReason(StrEnum):
    ADAPTER_NOT_IMPLEMENTED = "adapter-not-implemented"
    OBSERVATION_NOT_IMPLEMENTED = "observation-not-implemented"
    SCENARIO_REQUIRES_UPGRADE = "scenario-requires-explicit-v1alpha2-upgrade"


class PlannedRequirementSupport(StrictModel):
    requirement: EnvironmentCapability
    reason: BlockingReason


class EnvironmentDeclaration(StrictModel):
    environment: str
    capabilities: frozenset[EnvironmentCapability]
    planned_support: tuple[PlannedRequirementSupport, ...] = ()

    @model_validator(mode="after")
    def planned_is_not_implemented(self):
        planned = [item.requirement for item in self.planned_support]
        if len(planned) != len(set(planned)):
            raise ValueError("planned requirements must be unique")
        if set(planned) & set(self.capabilities):
            raise ValueError("implemented capabilities cannot also be planned")
        return self


class ScenarioPreflightResult(StrictModel):
    scenario: str
    scenario_api_version: str
    environment: str
    status: CompatibilityStatus
    required_capabilities: tuple[EnvironmentCapability, ...]
    missing_capabilities: tuple[EnvironmentCapability, ...]
    required_adapter_operations: tuple[AdapterOperation, ...]
    required_observations: tuple[ObservationType, ...]
    missing_adapter_operations: tuple[AdapterOperation, ...]
    missing_observations: tuple[ObservationType, ...]
    blocking_reasons: tuple[BlockingReason, ...]


def derive_compatibility(
    scenario: GuardianScenarioV1Alpha2, environment: EnvironmentDeclaration
) -> ScenarioPreflightResult:
    required = set(scenario.spec.environment_requirements.capabilities)
    mutation_count = scenario.spec.expected.mutations.count
    if mutation_count.exact is not None and mutation_count.exact > 0:
        required.add(EnvironmentCapability.ACTION_CONTROLLER_EXECUTION)
    missing = required - environment.capabilities
    planned = {item.requirement: item.reason for item in environment.planned_support}
    unplanned = missing - planned.keys()
    if unplanned:
        status = CompatibilityStatus.UNSUPPORTED
        reasons: tuple[BlockingReason, ...] = ()
    elif missing:
        status = CompatibilityStatus.BLOCKED
        reasons = tuple(sorted({planned[item] for item in missing}, key=str))
    else:
        status = CompatibilityStatus.SUPPORTED
        reasons = ()
    required_operations = {
        item for capability in required for item in CAPABILITY_CONTRACTS[capability][0]
    }
    required_observations = {
        item for capability in required for item in CAPABILITY_CONTRACTS[capability][1]
    }
    available_operations = {
        item
        for capability in environment.capabilities
        for item in CAPABILITY_CONTRACTS[capability][0]
    }
    available_observations = {
        item
        for capability in environment.capabilities
        for item in CAPABILITY_CONTRACTS[capability][1]
    }
    return ScenarioPreflightResult(
        scenario=scenario.metadata.name,
        scenario_api_version=scenario.api_version,
        environment=environment.environment,
        status=status,
        required_capabilities=tuple(sorted(required, key=str)),
        missing_capabilities=tuple(sorted(missing, key=str)),
        required_adapter_operations=tuple(sorted(required_operations, key=str)),
        required_observations=tuple(sorted(required_observations, key=str)),
        missing_adapter_operations=tuple(
            sorted(required_operations - available_operations, key=str)
        ),
        missing_observations=tuple(
            sorted(required_observations - available_observations, key=str)
        ),
        blocking_reasons=reasons,
    )


def preflight_scenario(
    scenario: GuardianScenarioV1Alpha2, environment: EnvironmentDeclaration
) -> ScenarioPreflightResult:
    return derive_compatibility(scenario, environment)
