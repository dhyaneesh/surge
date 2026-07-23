import asyncio
import json

from testbeds.scenarios.environment_suite import SuiteSummary
from testbeds.scenarios.matrix import run_matrix
from testbeds.scenarios.registry import SUPPORTED_ENVIRONMENTS


def test_matrix_executes_every_environment_and_writes_combined_reports(
    tmp_path, monkeypatch
):
    calls = []

    async def fake_run(environment, **kwargs):
        calls.append(environment)
        return SuiteSummary(environment, 1, 1, 1, 0, 0, ())

    monkeypatch.setattr("testbeds.scenarios.matrix.run_environment", fake_run)

    summary = asyncio.run(
        run_matrix(
            guardian_url="http://guardian.test",
            artifact_root=tmp_path,
            scenario_directory=tmp_path,
        )
    )

    assert calls == list(SUPPORTED_ENVIRONMENTS)
    assert summary["executed"] == summary["passed"] == 5
    assert summary["failed"] == summary["skipped"] == 0
    assert summary["emptyEnvironments"] == []
    assert json.loads((tmp_path / "matrix-summary.json").read_text())["passed"] == 5
    assert (tmp_path / "matrix-summary.md").is_file()
    assert (tmp_path / "coverage.json").is_file()
    assert (tmp_path / "requirement-coverage.json").is_file()
    assert (tmp_path / "capability-coverage.json").is_file()
    assert (tmp_path / "safety-gate-coverage.json").is_file()


def test_matrix_records_an_environment_that_executes_no_scenarios(
    tmp_path, monkeypatch
):
    async def fake_run(environment, **kwargs):
        executed = 0 if environment == SUPPORTED_ENVIRONMENTS[0] else 1
        return SuiteSummary(environment, executed, executed, executed, 0, 0, ())

    monkeypatch.setattr("testbeds.scenarios.matrix.run_environment", fake_run)

    summary = asyncio.run(
        run_matrix(
            guardian_url="http://guardian.test",
            artifact_root=tmp_path,
            scenario_directory=tmp_path,
        )
    )

    assert summary["emptyEnvironments"] == [SUPPORTED_ENVIRONMENTS[0]]
