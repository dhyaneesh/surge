from pathlib import Path

import pytest
import yaml

from testbeds.scenarios import GuardianScenario, load_scenario
from testbeds.scenarios.loader import load_guardian_scenario
from testbeds.scenarios.upgrade import UpgradeError, upgrade_v1alpha1
from testbeds.scenarios.v1alpha2 import GuardianScenarioV1Alpha2, ScaleDirection


ROOT = Path(__file__).parents[2]
LEGACY = ROOT / "tests/fixtures/scenarios/legitimate-demand-scale-up-v1alpha1.yaml"


def test_loader_dispatches_without_implicitly_upgrading(tmp_path: Path) -> None:
    assert isinstance(load_scenario(LEGACY), GuardianScenario)
    assert isinstance(load_guardian_scenario(LEGACY), GuardianScenario)

    document = yaml.safe_load(LEGACY.read_text())
    document["apiVersion"] = "tests.guardian.io/v9"
    path = tmp_path / "unknown.yaml"
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(ValueError, match="unknown.yaml"):
        load_guardian_scenario(path)


def test_explicit_upgrade_is_deterministic_and_requires_scale_direction() -> None:
    legacy = load_scenario(LEGACY)
    with pytest.raises(UpgradeError):
        upgrade_v1alpha1(
            legacy,
            description="Legitimate demand scale-up.",
            capabilities={
                "healthy-baseline",
                "horizontal-scaling",
                "mutation-observation",
                "recovery-observation",
            },
            scale_direction=None,
            recovery_contract_ref="service-scale-recovery",
            recovery_contract_version=1,
            recovery_registry_version="registry-v1",
        )
    first = upgrade_v1alpha1(
        legacy,
        description="Legitimate demand scale-up.",
        capabilities={
            "healthy-baseline",
            "horizontal-scaling",
            "mutation-observation",
            "recovery-observation",
        },
        scale_direction=ScaleDirection.UP,
        recovery_contract_ref="service-scale-recovery",
        recovery_contract_version=1,
        recovery_registry_version="registry-v1",
    )
    second = upgrade_v1alpha1(
        legacy,
        description="Legitimate demand scale-up.",
        capabilities={
            "healthy-baseline",
            "horizontal-scaling",
            "mutation-observation",
            "recovery-observation",
        },
        scale_direction=ScaleDirection.UP,
        recovery_contract_ref="service-scale-recovery",
        recovery_contract_version=1,
        recovery_registry_version="registry-v1",
    )
    assert isinstance(first, GuardianScenarioV1Alpha2)
    assert first == second
    assert first.spec.candidate_environments == legacy.spec.applicable_environments
