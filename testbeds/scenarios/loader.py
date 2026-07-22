"""Safe YAML loading boundary for Guardian scenarios."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from testbeds.scenarios.models import GuardianScenario
from testbeds.scenarios.v1alpha2 import GuardianScenarioV1Alpha2


class ScenarioLoadError(ValueError):
    """A scenario file could not be read as a YAML mapping."""


def load_scenario(path: str | Path) -> GuardianScenario:
    scenario_path = Path(path)
    try:
        content = scenario_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ScenarioLoadError(
            f"could not read GuardianScenario YAML {scenario_path}: {error}"
        ) from error
    try:
        document: Any = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise ScenarioLoadError(
            f"malformed GuardianScenario YAML {scenario_path}: {error}"
        ) from error
    if not isinstance(document, Mapping):
        raise ScenarioLoadError(
            f"GuardianScenario YAML {scenario_path} must contain a mapping document"
        )
    return GuardianScenario.model_validate(dict(document))


def load_guardian_scenario(
    path: str | Path,
) -> GuardianScenario | GuardianScenarioV1Alpha2:
    """Load the explicitly declared version without implicit migration."""

    scenario_path = Path(path)
    try:
        document: Any = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ScenarioLoadError(
            f"could not load GuardianScenario YAML {scenario_path}: {error}"
        ) from error
    if not isinstance(document, Mapping):
        raise ScenarioLoadError(
            f"GuardianScenario YAML {scenario_path} must contain a mapping document"
        )
    version = document.get("apiVersion")
    if version == "tests.guardian.io/v1alpha1":
        return GuardianScenario.model_validate(dict(document))
    if version == "tests.guardian.io/v1alpha2":
        return GuardianScenarioV1Alpha2.model_validate(dict(document))
    raise ScenarioLoadError(
        f"GuardianScenario YAML {scenario_path} has unsupported apiVersion {version!r}"
    )
