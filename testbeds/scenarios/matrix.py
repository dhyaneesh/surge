"""Sequential five-environment executable scenario matrix."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from testbeds.scenarios.environment_suite import run_environment, select_scenarios
from testbeds.scenarios.loader import load_guardian_scenario
from testbeds.scenarios.registry import SUPPORTED_ENVIRONMENTS
from testbeds.scenarios.v1alpha2 import GuardianScenarioV1Alpha2


async def run_matrix(
    *, guardian_url: str, artifact_root: Path, scenario_directory: Path
) -> dict[str, Any]:
    artifact_root.mkdir(parents=True, exist_ok=True)
    environment_summaries = []
    selected_paths: set[Path] = set()
    for environment in SUPPORTED_ENVIRONMENTS:
        selected, _ = select_scenarios(environment, scenario_directory)
        selected_paths.update(selected)
        environment_summaries.append(
            await run_environment(
                environment,
                guardian_url=guardian_url,
                artifact_root=artifact_root / environment,
                scenario_directory=scenario_directory,
            )
        )
    summary: dict[str, Any] = {
        "schemaVersion": "guardian.scenario-matrix/v1",
        "environments": [item.environment for item in environment_summaries],
        "selected": sum(item.selected for item in environment_summaries),
        "executed": sum(item.executed for item in environment_summaries),
        "passed": sum(item.passed for item in environment_summaries),
        "failed": sum(item.failed for item in environment_summaries),
        "skipped": sum(item.skipped for item in environment_summaries),
        "emptyEnvironments": [
            item.environment for item in environment_summaries if item.executed == 0
        ],
        "skipReasons": [
            {"environment": item.environment, "reason": reason}
            for item in environment_summaries
            for reason in item.skip_reasons
        ],
    }
    (artifact_root / "matrix-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    rows = [
        "# Guardian Scenario Matrix",
        "",
        "| Environment | Selected | Executed | Passed | Failed | Skipped |",
        "|---|---:|---:|---:|---:|---:|",
        *[
            f"| {item.environment} | {item.selected} | {item.executed} | {item.passed} | {item.failed} | {item.skipped} |"
            for item in environment_summaries
        ],
        "",
    ]
    (artifact_root / "matrix-summary.md").write_text("\n".join(rows), encoding="utf-8")
    scenarios = []
    requirements: set[str] = set()
    capabilities: set[str] = set()
    safety_gates: set[str] = set()
    incident_types: set[str] = set()
    mutation_types: set[str] = set()
    tenant_safety = approval = replay = recovery = False
    for path in sorted(selected_paths):
        scenario = load_guardian_scenario(path)
        if not isinstance(scenario, GuardianScenarioV1Alpha2):
            continue
        scenario_requirements = list(scenario.spec.traceability.normative_requirements)
        scenario_capabilities = sorted(
            item.value for item in scenario.spec.environment_requirements.capabilities
        )
        scenario_safety_gates = [
            item.value for item in scenario.spec.expected.safety_gates
        ]
        scenario_incident_type = (
            scenario.spec.expected.incident.incident_class.value
            if scenario.spec.expected.incident.incident_class
            else None
        )
        scenario_mutation_types = [
            item.action_type.value
            for item in scenario.spec.expected.mutations.allowed_actions
        ]
        requirements.update(scenario_requirements)
        capabilities.update(scenario_capabilities)
        safety_gates.update(scenario_safety_gates)
        if scenario_incident_type:
            incident_types.add(scenario_incident_type)
        mutation_types.update(scenario_mutation_types)
        tenant_safety |= scenario.spec.expected.tenant_isolation is not None
        approval |= scenario.spec.expected.workflow.approval_count is not None
        replay |= bool(
            scenario.spec.stimulus.incident_delivery
            and scenario.spec.stimulus.incident_delivery.count > 1
        )
        recovery |= scenario.spec.expected.recovery is not None
        scenarios.append(
            {
                "scenario": scenario.metadata.name,
                "environments": list(scenario.spec.candidate_environments),
                "capabilities": scenario_capabilities,
                "requirements": scenario_requirements,
                "incidentType": scenario_incident_type,
                "mutationTypes": scenario_mutation_types,
                "safetyGates": scenario_safety_gates,
            }
        )
    summary["coverage"] = {
        "incidentTypes": sorted(incident_types),
        "mutationTypes": sorted(mutation_types),
        "tenantSafety": tenant_safety,
        "approval": approval,
        "replay": replay,
        "recovery": recovery,
    }
    (artifact_root / "matrix-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (artifact_root / "coverage.json").write_text(
        json.dumps(
            {"schemaVersion": "guardian.scenario-coverage/v1", "scenarios": scenarios},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    for filename, key, values in (
        ("requirement-coverage.json", "requirements", requirements),
        ("capability-coverage.json", "capabilities", capabilities),
        ("safety-gate-coverage.json", "safetyGates", safety_gates),
    ):
        (artifact_root / filename).write_text(
            json.dumps(
                {"schemaVersion": "guardian.scenario-coverage/v1", key: sorted(values)},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--guardian-url", required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args(argv)
    summary = asyncio.run(
        run_matrix(
            guardian_url=args.guardian_url,
            artifact_root=args.artifacts,
            scenario_directory=Path("testbeds/scenarios"),
        )
    )
    print(json.dumps(summary, sort_keys=True))
    if summary["emptyEnvironments"] or summary["failed"] or summary["passed"] == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
