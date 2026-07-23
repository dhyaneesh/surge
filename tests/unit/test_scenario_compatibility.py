import copy

from testbeds.evidence.contracts import EvidenceSourceKind
from testbeds.scenarios.compatibility import (
    BlockingReason,
    EnvironmentDeclaration,
    PlannedRequirementSupport,
    derive_compatibility,
    preflight_scenario,
)
from testbeds.scenarios.v1alpha2 import GuardianScenarioV1Alpha2
from tests.unit.test_guardian_scenario_v1alpha2 import document

_DOCUMENT_EVIDENCE = (
    EvidenceSourceKind.ENDPOINT_PROBE.value,
    EvidenceSourceKind.KUBERNETES_WORKLOAD.value,
    EvidenceSourceKind.METRICS_API.value,
)


def declaration(
    *capabilities: str, planned=(), evidence_sources=_DOCUMENT_EVIDENCE
) -> EnvironmentDeclaration:
    return EnvironmentDeclaration.model_validate(
        {
            "environment": "otel-demo",
            "capabilities": capabilities,
            "evidenceSources": evidence_sources,
            "plannedSupport": planned,
        }
    )


def test_compatibility_is_capability_driven_not_name_driven() -> None:
    scenario = GuardianScenarioV1Alpha2.model_validate(document())
    result = derive_compatibility(scenario, declaration("healthy-baseline"))
    assert result.status.value == "unsupported"
    assert "horizontal-scaling" in [item.value for item in result.missing_capabilities]


def test_planned_missing_requirements_are_blocked() -> None:
    scenario = GuardianScenarioV1Alpha2.model_validate(document())
    required = scenario.spec.environment_requirements.capabilities
    missing = next(item for item in required if item.value == "horizontal-scaling")
    environment = declaration(
        *(item.value for item in required if item != missing),
        planned=[
            PlannedRequirementSupport(
                requirement=missing, reason=BlockingReason.ADAPTER_NOT_IMPLEMENTED
            )
        ],
    )
    result = preflight_scenario(scenario, environment)
    assert result.status.value == "blocked"
    assert result.blocking_reasons == (BlockingReason.ADAPTER_NOT_IMPLEMENTED,)


def test_preflight_serialization_is_deterministic() -> None:
    scenario = GuardianScenarioV1Alpha2.model_validate(document())
    environment = declaration(
        *(item.value for item in scenario.spec.environment_requirements.capabilities)
    )
    first = preflight_scenario(scenario, environment).model_dump_json()
    assert first == preflight_scenario(scenario, environment).model_dump_json()


def test_exact_positive_mutation_requires_action_controller_execution_capability() -> (
    None
):
    value = copy.deepcopy(document())
    value["spec"]["expected"]["mutations"]["count"] = {"exact": 1}
    scenario = GuardianScenarioV1Alpha2.model_validate(value)
    authored = scenario.spec.environment_requirements.capabilities

    unsupported = preflight_scenario(
        scenario, declaration(*(item.value for item in authored))
    )
    assert unsupported.status.value == "unsupported"
    assert [item.value for item in unsupported.missing_capabilities] == [
        "action-controller-execution"
    ]

    supported = preflight_scenario(
        scenario,
        declaration(*(item.value for item in authored), "action-controller-execution"),
    )
    assert supported.status.value == "supported"


def test_at_least_positive_mutation_requires_action_controller_execution_capability() -> (
    None
):
    value = copy.deepcopy(document())
    value["spec"]["expected"]["mutations"]["count"] = {"atLeast": 1}
    scenario = GuardianScenarioV1Alpha2.model_validate(value)
    authored = scenario.spec.environment_requirements.capabilities

    result = preflight_scenario(
        scenario, declaration(*(item.value for item in authored))
    )

    assert result.status.value == "unsupported"
    assert [item.value for item in result.missing_capabilities] == [
        "action-controller-execution"
    ]
