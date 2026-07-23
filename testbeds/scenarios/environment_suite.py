"""Execute every supported scenario for one real test environment."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from testbeds.scenarios.compatibility import CompatibilityStatus, derive_compatibility
from testbeds.scenarios.execution import (
    EnvironmentInvalidatedError,
    ExecutionSettings,
    ExecutionStatus,
    ScenarioExecutor,
)
from testbeds.scenarios.guardian_client import HttpGuardianClient
from testbeds.scenarios.loader import load_guardian_scenario
from testbeds.scenarios.registry import (
    SUPPORTED_ENVIRONMENTS,
    build_adapter_registration,
)
from testbeds.scenarios.v1alpha2 import GuardianScenarioV1Alpha2


@dataclass(frozen=True, slots=True)
class SuiteSummary:
    environment: str
    selected: int
    executed: int
    passed: int
    failed: int
    skipped: int
    skip_reasons: tuple[str, ...]
    reset_completed: bool = True
    cleanup_completed: bool = True
    environment_invalidated: bool = False


def select_scenarios(
    environment: str, directory: Path
) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    registration = build_adapter_registration(
        environment, workspace=directory / ".selection", run_id="selection"
    )
    selected: list[Path] = []
    skipped: list[str] = []
    for path in sorted(directory.glob("*.yaml")):
        if path.name in {"compatibility.yaml", "index.yaml"}:
            continue
        scenario = load_guardian_scenario(path)
        if not isinstance(scenario, GuardianScenarioV1Alpha2):
            skipped.append(f"{path.name}: v1alpha1 requires explicit upgrade")
            continue
        if environment not in scenario.spec.candidate_environments:
            skipped.append(f"{scenario.metadata.name}: environment is not a candidate")
            continue
        compatibility = derive_compatibility(scenario, registration.declaration)
        if compatibility.status is not CompatibilityStatus.SUPPORTED:
            missing = ",".join(
                item.value for item in compatibility.missing_capabilities
            )
            skipped.append(f"{scenario.metadata.name}: missing capabilities: {missing}")
            continue
        selected.append(path)
    return tuple(selected), tuple(skipped)


async def run_environment(
    environment: str,
    *,
    guardian_url: str,
    artifact_root: Path,
    scenario_directory: Path,
) -> SuiteSummary:
    selected, skip_reasons = select_scenarios(environment, scenario_directory)
    artifact_root.mkdir(parents=True, exist_ok=True)
    if not selected:
        summary = SuiteSummary(environment, 0, 0, 0, 0, len(skip_reasons), skip_reasons)
        _write_summary(artifact_root, summary)
        return summary
    passed = failed = executed = 0
    reset_completed = True
    cleanup_completed = True
    environment_invalidated = False
    for index, path in enumerate(selected, start=1):
        scenario = load_guardian_scenario(path)
        registration = build_adapter_registration(
            environment,
            workspace=artifact_root / "workspaces" / scenario.metadata.name,
            run_id=f"{index}-{scenario.metadata.name}",
        )
        try:
            result = await ScenarioExecutor(HttpGuardianClient(guardian_url)).execute(
                scenario,
                registration,
                ExecutionSettings(artifact_root / "executions"),
            )
        except EnvironmentInvalidatedError as exc:
            result = exc.result
            environment_invalidated = True
            reset_completed = reset_completed and result.reset_completed
            cleanup_completed = cleanup_completed and result.cleanup_completed
            executed += 1
            failed += 1
            summary = SuiteSummary(
                environment,
                len(selected),
                executed,
                passed,
                failed,
                len(skip_reasons),
                skip_reasons,
                reset_completed=reset_completed,
                cleanup_completed=cleanup_completed,
                environment_invalidated=True,
            )
            _write_summary(artifact_root, summary)
            return summary
        executed += 1
        reset_completed = reset_completed and result.reset_completed
        cleanup_completed = cleanup_completed and result.cleanup_completed
        if result.status is ExecutionStatus.PASSED:
            passed += 1
        else:
            failed += 1
    summary = SuiteSummary(
        environment,
        len(selected),
        executed,
        passed,
        failed,
        len(skip_reasons),
        skip_reasons,
        reset_completed=reset_completed,
        cleanup_completed=cleanup_completed,
        environment_invalidated=environment_invalidated,
    )
    _write_summary(artifact_root, summary)
    return summary


def _write_summary(artifact_root: Path, summary: SuiteSummary) -> None:
    (artifact_root / "environment-summary.json").write_text(
        json.dumps(asdict(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", choices=SUPPORTED_ENVIRONMENTS, required=True)
    parser.add_argument("--guardian-url", required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args(argv)
    summary = asyncio.run(
        run_environment(
            args.environment,
            guardian_url=args.guardian_url,
            artifact_root=args.artifacts,
            scenario_directory=Path("testbeds/scenarios"),
        )
    )
    print(
        f"environment={summary.environment} selected={summary.selected} "
        f"executed={summary.executed} passed={summary.passed} "
        f"failed={summary.failed} skipped={summary.skipped}"
    )
    for reason in summary.skip_reasons:
        print(f"skip: {reason}")
    if summary.environment_invalidated:
        return 1
    if summary.executed == 0:
        return 1
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
