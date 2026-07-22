from testbeds.scenarios.compatibility import (
    BlockingReason,
    EnvironmentDeclaration,
    PlannedRequirementSupport,
    derive_compatibility,
    preflight_scenario,
)
from testbeds.scenarios.v1alpha2 import GuardianScenarioV1Alpha2
from tests.unit.test_guardian_scenario_v1alpha2 import document


def declaration(*capabilities: str, planned=()) -> EnvironmentDeclaration:
    return EnvironmentDeclaration.model_validate(
        {
            "environment": "otel-demo",
            "capabilities": capabilities,
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
