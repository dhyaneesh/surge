from __future__ import annotations

from datetime import UTC, datetime
import logging
from pathlib import Path

import pytest

from packages.diagnostics import (
    DiagnosticProviderAvailability,
    ScenarioVerdict,
    diagnostic_report,
    draft_query_contract,
    emit_diagnostic_log,
    run_diagnostics_after_verdict,
)
from testbeds.scenarios import GuardianScenario


def minimal_scenario() -> dict[str, object]:
    return {
        "apiVersion": "tests.guardian.io/v1alpha1",
        "kind": "GuardianScenario",
        "metadata": {"name": "mcp-independent"},
        "spec": {
            "applicableEnvironments": ["otel-demo"],
            "target": {"serviceSelector": {"role": "request-processor"}},
        },
    }


def frozen_failed_verdict() -> ScenarioVerdict:
    return ScenarioVerdict.freeze(
        outcome="failed",
        determined_at=datetime(2026, 7, 22, tzinfo=UTC),
        assertion_contract_ids=("approved-recovery-contract",),
    )


def test_scenario_schema_and_compatibility_do_not_depend_on_mcp_availability() -> None:
    scenario = GuardianScenario.model_validate(minimal_scenario())

    assert scenario.metadata.name == "mcp-independent"
    assert "diagnosticProviders" not in scenario.spec.model_dump(by_alias=True)


def test_deterministic_verdict_is_available_without_mcp() -> None:
    verdict = ScenarioVerdict.freeze(
        outcome="passed",
        determined_at=datetime(2026, 7, 22, tzinfo=UTC),
        assertion_contract_ids=("approved-scaling-contract",),
    )

    assert verdict.outcome == "passed"
    assert verdict.assertion_contract_ids == ("approved-scaling-contract",)


def test_mcp_diagnostics_run_only_after_a_verdict_is_frozen() -> None:
    events: list[str] = []

    result = run_diagnostics_after_verdict(
        frozen_failed_verdict(),
        DiagnosticProviderAvailability.available("signoz-mcp"),
        lambda: (
            events.append("diagnostics")
            or diagnostic_report("signoz-mcp", {"span": "failed request"})
        ),
    )

    assert events == ["diagnostics"]
    assert result.verdict.outcome == "failed"


def test_mcp_diagnostics_reject_an_unfrozen_verdict() -> None:
    with pytest.raises(ValueError, match="frozen"):
        run_diagnostics_after_verdict(
            ScenarioVerdict(outcome="failed"),
            DiagnosticProviderAvailability.available("signoz-mcp"),
            lambda: diagnostic_report("signoz-mcp", {}),
        )


def test_mcp_diagnostics_cannot_mutate_the_frozen_verdict() -> None:
    verdict = frozen_failed_verdict()

    result = run_diagnostics_after_verdict(
        verdict,
        DiagnosticProviderAvailability.available("signoz-mcp"),
        lambda: diagnostic_report("signoz-mcp", {"suggested_outcome": "passed"}),
    )

    assert result.verdict is verdict
    assert result.verdict.outcome == "failed"


def test_mcp_diagnostic_reports_are_explicitly_non_authoritative() -> None:
    report = diagnostic_report("signoz-mcp", {"candidate": "telemetry query"})

    assert report.authoritative is False


def test_diagnostic_serialization_and_logs_redact_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    report = diagnostic_report(
        "signoz-mcp",
        {
            "authorization": "Bearer super-secret-token",
            "endpoint": "https://user:password@example.test/query?token=abc123",
        },
    )

    serialized = report.serialize()
    emit_diagnostic_log(logging.getLogger("guardian.diagnostics"), report)

    assert "super-secret-token" not in serialized
    assert "password" not in serialized
    assert "abc123" not in serialized
    assert "[REDACTED]" in serialized
    assert "super-secret-token" not in caplog.text
    assert "password" not in caplog.text
    assert "abc123" not in caplog.text


def test_mcp_unavailability_is_a_warning_without_a_changed_verdict() -> None:
    verdict = frozen_failed_verdict()

    result = run_diagnostics_after_verdict(
        verdict,
        DiagnosticProviderAvailability.unavailable("signoz-mcp", "not configured"),
        lambda: diagnostic_report("signoz-mcp", {"unexpected": "call"}),
    )

    assert result.verdict is verdict
    assert result.report is None
    assert result.warnings == ("signoz-mcp diagnostics unavailable: not configured",)


def test_mcp_query_contract_drafts_require_explicit_review_before_activation() -> None:
    proposal = draft_query_contract("signoz-mcp", "SELECT count() FROM spans")

    assert proposal.review_status == "draft"
    assert proposal.activation_status == "not-active"


def test_mcp_task_targets_are_explicit_and_not_part_of_unit_tests() -> None:
    taskfile = (Path(__file__).parents[2] / "Taskfile.yml").read_text(encoding="utf-8")
    unit_task = taskfile.split("  test:unit:\n", maxsplit=1)[1].split(
        "\n  test:contract:", maxsplit=1
    )[0]

    assert "  mcp:signoz:check:" in taskfile
    assert "  mcp:signoz:smoke:" in taskfile
    assert "  diagnostics:signoz:" in taskfile
    assert "mcp:signoz" not in unit_task
