"""Catalog file boundary; semantic derivation remains pure."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from testbeds.scenarios.compatibility import (
    BlockingReason,
    EnvironmentDeclaration,
    derive_compatibility,
)
from testbeds.scenarios.loader import load_guardian_scenario
from testbeds.scenarios.models import GuardianScenario


def derive_catalog(
    directory: Path, declarations: dict[str, EnvironmentDeclaration]
) -> tuple[dict[str, Any], dict[str, Any]]:
    matrix: dict[str, Any] = {"scenarios": {}}
    entries: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.yaml")):
        if path.name in {"compatibility.yaml", "index.yaml"}:
            continue
        scenario = load_guardian_scenario(path)
        rows: dict[str, Any] = {}
        if isinstance(scenario, GuardianScenario):
            for environment in sorted(declarations):
                rows[environment] = {
                    "status": "blocked",
                    "blockingReasons": [BlockingReason.SCENARIO_REQUIRES_UPGRADE.value],
                }
            api_version = scenario.api_version
            required: list[str] = []
            incident_class = None
            allowed_actions: list[dict[str, Any]] = []
            forbidden_actions: list[dict[str, Any]] = []
            mutation_expected = False
            approval_required = False
            fresh_recovery = False
            normative: list[str] = []
            acceptance: list[str] = []
        else:
            for environment, declaration in sorted(declarations.items()):
                result = derive_compatibility(scenario, declaration)
                rows[environment] = result.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude={
                        "scenario",
                        "scenario_api_version",
                        "environment",
                        "required_capabilities",
                    },
                )
            api_version = scenario.api_version
            required = sorted(
                item.value
                for item in scenario.spec.environment_requirements.capabilities
            )
            expected = scenario.spec.expected
            incident_class = (
                expected.incident.incident_class.value
                if expected.incident.incident_class
                else None
            )
            allowed_actions = [
                item.model_dump(mode="json", by_alias=True)
                for item in expected.actions.eligible
            ]
            forbidden_actions = [
                item.model_dump(mode="json", by_alias=True)
                for item in expected.actions.forbidden
            ]
            mutation_expected = expected.mutations.count.exact != 0
            approval_required = expected.policy.decision.value == "approval-required"
            fresh_recovery = expected.recovery is not None
            normative = list(scenario.spec.traceability.normative_requirements)
            acceptance = list(scenario.spec.traceability.acceptance_tests)
        matrix["scenarios"][scenario.metadata.name] = {"environments": rows}
        entries.append(
            {
                "name": scenario.metadata.name,
                "apiVersion": api_version,
                "file": str(path.relative_to(directory.parent.parent)),
                "requiredCapabilities": required,
                "candidateEnvironments": list(scenario.spec.candidate_environments)
                if not isinstance(scenario, GuardianScenario)
                else list(scenario.spec.applicable_environments),
                "description": scenario.spec.description
                if not isinstance(scenario, GuardianScenario)
                else "Legacy v1alpha1 scenario",
                "expectedIncidentClass": incident_class
                if not isinstance(scenario, GuardianScenario)
                else None,
                "allowedActions": allowed_actions
                if not isinstance(scenario, GuardianScenario)
                else [],
                "forbiddenActions": forbidden_actions
                if not isinstance(scenario, GuardianScenario)
                else [],
                "mutationExpected": mutation_expected
                if not isinstance(scenario, GuardianScenario)
                else False,
                "approvalRequired": approval_required
                if not isinstance(scenario, GuardianScenario)
                else False,
                "freshRecoveryEvidenceRequired": fresh_recovery
                if not isinstance(scenario, GuardianScenario)
                else False,
                "normativeRequirements": normative
                if not isinstance(scenario, GuardianScenario)
                else [],
                "acceptanceTests": acceptance
                if not isinstance(scenario, GuardianScenario)
                else [],
                "implementationStatus": "implemented",
                "compatibilityResolved": not isinstance(scenario, GuardianScenario),
            }
        )
    return matrix, {"scenarios": entries}


def validate_catalog_files(
    directory: Path, declarations: dict[str, EnvironmentDeclaration]
) -> None:
    expected_matrix, expected_index = derive_catalog(directory, declarations)
    actual_matrix = yaml.safe_load(
        (directory / "compatibility.yaml").read_text(encoding="utf-8")
    )
    actual_index = yaml.safe_load(
        (directory / "index.yaml").read_text(encoding="utf-8")
    )
    if actual_matrix != expected_matrix:
        raise ValueError("compatibility.yaml differs from capability derivation")
    if actual_index != expected_index:
        raise ValueError("index.yaml differs from scenario catalog derivation")
