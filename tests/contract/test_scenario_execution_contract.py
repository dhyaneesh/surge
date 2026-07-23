from pathlib import Path

import pytest

from testbeds.scenarios.loader import load_guardian_scenario
from testbeds.scenarios.registry import (
    SUPPORTED_ENVIRONMENTS,
    build_adapter_registration,
)
from testbeds.scenarios.v1alpha2 import GuardianScenarioV1Alpha2


def scenario_paths():
    return sorted(
        path
        for path in Path("testbeds/scenarios").glob("*.yaml")
        if path.name not in {"compatibility.yaml", "index.yaml"}
    )


def test_all_v1alpha2_scenarios_have_unique_ids_and_registered_environments(tmp_path):
    scenarios = [load_guardian_scenario(path) for path in scenario_paths()]
    names = [scenario.metadata.name for scenario in scenarios]
    assert len(names) == len(set(names))
    for scenario in scenarios:
        if not isinstance(scenario, GuardianScenarioV1Alpha2):
            continue
        assert set(scenario.spec.candidate_environments) <= set(SUPPORTED_ENVIRONMENTS)
        for environment in scenario.spec.candidate_environments:
            registration = build_adapter_registration(
                environment, workspace=tmp_path / environment, run_id="contract"
            )
            assert registration.environment == environment
            assert registration.release.environment == environment


def test_unknown_environment_is_rejected_before_adapter_construction(tmp_path):
    with pytest.raises(ValueError, match="unknown environment"):
        build_adapter_registration("unknown", workspace=tmp_path)


def test_v1alpha1_remains_explicitly_non_executable():
    legacy = load_guardian_scenario(
        "tests/fixtures/scenarios/legitimate-demand-scale-up-v1alpha1.yaml"
    )
    assert not isinstance(legacy, GuardianScenarioV1Alpha2)
