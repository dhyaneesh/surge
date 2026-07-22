"""Safe YAML loading boundary for Guardian scenarios."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from testbeds.scenarios.models import GuardianScenario


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
