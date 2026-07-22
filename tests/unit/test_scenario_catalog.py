from pathlib import Path

from testbeds.environments.capabilities import ENVIRONMENT_DECLARATIONS
from testbeds.scenarios.catalog import derive_catalog, validate_catalog_files


ROOT = Path(__file__).parents[2]


def test_catalog_derives_every_scenario_environment_pair() -> None:
    compatibility, index = derive_catalog(
        ROOT / "testbeds/scenarios", ENVIRONMENT_DECLARATIONS
    )
    assert len(compatibility["scenarios"]) == 15
    rows = compatibility["scenarios"]["legitimate-demand-scale-up"]["environments"]
    assert set(rows) == set(ENVIRONMENT_DECLARATIONS)
    assert all(
        entry["apiVersion"] == "tests.guardian.io/v1alpha2"
        for entry in index["scenarios"]
    )


def test_checked_in_catalog_is_exactly_derived() -> None:
    validate_catalog_files(ROOT / "testbeds/scenarios", ENVIRONMENT_DECLARATIONS)
