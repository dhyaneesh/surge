"""Non-authoritative diagnostic boundary contracts."""

from packages.diagnostics.boundary import (
    DiagnosticProviderAvailability,
    DiagnosticReport,
    DiagnosticRunResult,
    QueryContractProposal,
    ScenarioVerdict,
    diagnostic_report,
    draft_query_contract,
    emit_diagnostic_log,
    run_diagnostics_after_verdict,
)

__all__ = [
    "DiagnosticProviderAvailability",
    "DiagnosticReport",
    "DiagnosticRunResult",
    "QueryContractProposal",
    "ScenarioVerdict",
    "diagnostic_report",
    "draft_query_contract",
    "emit_diagnostic_log",
    "run_diagnostics_after_verdict",
]
