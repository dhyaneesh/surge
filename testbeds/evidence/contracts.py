"""Deterministic evidence-source contracts for testbed scenarios.

These contracts live exclusively under ``testbeds`` and must never be imported by
production Guardian services. They describe which independently sampleable
sources can substantiate scenario evidence types and environment capabilities.
"""

from __future__ import annotations

from enum import StrEnum

from testbeds.scenarios.models import EvidenceType
from testbeds.scenarios.v1alpha2 import EnvironmentCapability, GuardianScenarioV1Alpha2


class EvidenceSourceKind(StrEnum):
    ENDPOINT_PROBE = "endpoint-probe"
    KUBERNETES_WORKLOAD = "kubernetes-workload"
    METRICS_API = "metrics-api"
    ROLLOUT_STATE = "rollout-state"
    RABBITMQ_QUEUE = "rabbitmq-queue"
    KEDA_SCALER = "keda-scaler"
    SIGNOZ_TELEMETRY = "signoz-telemetry"


# Collector contracts: each evidence type is satisfied when the declaration
# provides at least one listed source.
EVIDENCE_TYPE_SOURCES: dict[EvidenceType, frozenset[EvidenceSourceKind]] = {
    EvidenceType.METRICS: frozenset({EvidenceSourceKind.ENDPOINT_PROBE}),
    EvidenceType.LOAD: frozenset({EvidenceSourceKind.ENDPOINT_PROBE}),
    EvidenceType.EXCEPTIONS: frozenset({EvidenceSourceKind.ENDPOINT_PROBE}),
    EvidenceType.RECOVERY_TELEMETRY: frozenset({EvidenceSourceKind.ENDPOINT_PROBE}),
    EvidenceType.RESOURCE_UTILIZATION: frozenset({EvidenceSourceKind.METRICS_API}),
    EvidenceType.DEPENDENCY_HEALTH: frozenset({EvidenceSourceKind.KUBERNETES_WORKLOAD}),
    EvidenceType.TOPOLOGY: frozenset({EvidenceSourceKind.KUBERNETES_WORKLOAD}),
    EvidenceType.WORKLOAD_STATE: frozenset({EvidenceSourceKind.KUBERNETES_WORKLOAD}),
    EvidenceType.SERVICE_IDENTITY: frozenset({EvidenceSourceKind.KUBERNETES_WORKLOAD}),
    EvidenceType.IDENTITY_CONFLICT: frozenset({EvidenceSourceKind.KUBERNETES_WORKLOAD}),
    EvidenceType.DEPLOYMENT_EVENT: frozenset(
        {
            EvidenceSourceKind.KUBERNETES_WORKLOAD,
            EvidenceSourceKind.ROLLOUT_STATE,
        }
    ),
    EvidenceType.TELEMETRY_QUALITY: frozenset({EvidenceSourceKind.SIGNOZ_TELEMETRY}),
    EvidenceType.TRACES: frozenset({EvidenceSourceKind.SIGNOZ_TELEMETRY}),
    EvidenceType.LOGS: frozenset({EvidenceSourceKind.SIGNOZ_TELEMETRY}),
}

# Capabilities that claim observational power must be backed by these sources.
CAPABILITY_EVIDENCE_REQUIREMENTS: dict[
    EnvironmentCapability, frozenset[EvidenceSourceKind]
] = {
    EnvironmentCapability.HEALTHY_BASELINE: frozenset(
        {
            EvidenceSourceKind.ENDPOINT_PROBE,
            EvidenceSourceKind.KUBERNETES_WORKLOAD,
        }
    ),
    EnvironmentCapability.RESOURCE_PRESSURE: frozenset(
        {EvidenceSourceKind.METRICS_API}
    ),
    EnvironmentCapability.TELEMETRY_INTERRUPTION: frozenset(
        {EvidenceSourceKind.SIGNOZ_TELEMETRY}
    ),
    EnvironmentCapability.SCALER_OBSERVATION: frozenset(
        {EvidenceSourceKind.KEDA_SCALER}
    ),
    EnvironmentCapability.PROGRESSIVE_DELIVERY: frozenset(
        {EvidenceSourceKind.ROLLOUT_STATE}
    ),
    EnvironmentCapability.RECOVERY_OBSERVATION: frozenset(
        {EvidenceSourceKind.ENDPOINT_PROBE}
    ),
    EnvironmentCapability.DEPENDENCY_OBSERVATION: frozenset(
        {EvidenceSourceKind.KUBERNETES_WORKLOAD}
    ),
    EnvironmentCapability.HORIZONTAL_SCALING: frozenset(
        {EvidenceSourceKind.KUBERNETES_WORKLOAD}
    ),
    EnvironmentCapability.SCALE_TO_ZERO: frozenset({EvidenceSourceKind.KEDA_SCALER}),
}


def scenario_evidence_types(
    scenario: GuardianScenarioV1Alpha2,
) -> frozenset[EvidenceType]:
    expected = scenario.spec.expected
    types: set[EvidenceType] = set()
    for group in (
        expected.evidence.supporting,
        expected.evidence.contradicting,
        expected.evidence.required_fresh,
    ):
        for item in group:
            types.add(item.evidence_type)
    if expected.recovery is not None:
        for item in expected.recovery.evidence:
            types.add(item.evidence_type)
    return frozenset(types)


def required_evidence_sources_for_scenario(
    scenario: GuardianScenarioV1Alpha2,
) -> frozenset[EvidenceSourceKind]:
    required: set[EvidenceSourceKind] = set()
    for evidence_type in scenario_evidence_types(scenario):
        sources = EVIDENCE_TYPE_SOURCES.get(evidence_type)
        if sources is not None:
            required |= set(sources)
    return frozenset(required)


def missing_evidence_sources_for_scenario(
    scenario: GuardianScenarioV1Alpha2,
    available: frozenset[EvidenceSourceKind],
) -> frozenset[EvidenceSourceKind]:
    missing: set[EvidenceSourceKind] = set()
    for evidence_type in scenario_evidence_types(scenario):
        allowed = EVIDENCE_TYPE_SOURCES.get(evidence_type)
        if allowed is None:
            continue
        if not (allowed & available):
            missing |= set(allowed)
    return frozenset(missing)


def scenario_evidence_satisfied(
    scenario: GuardianScenarioV1Alpha2,
    available: frozenset[EvidenceSourceKind],
) -> bool:
    return not missing_evidence_sources_for_scenario(scenario, available)


def substantiate_capabilities(
    capabilities: frozenset[EnvironmentCapability],
    evidence_sources: frozenset[EvidenceSourceKind],
) -> frozenset[EnvironmentCapability]:
    kept: set[EnvironmentCapability] = set()
    for capability in capabilities:
        required = CAPABILITY_EVIDENCE_REQUIREMENTS.get(capability)
        if required is None or required <= evidence_sources:
            kept.add(capability)
    return frozenset(kept)
