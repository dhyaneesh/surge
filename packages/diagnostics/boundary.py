"""Typed contracts for optional, non-authoritative diagnostics.

This module intentionally has no diagnostic-provider client, credential loading,
or runtime query execution. Deterministic assertions remain the responsibility
of approved Query Contracts executed through the Guardian SigNoz gateway.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


DiagnosticAvailability = Literal["available", "unavailable"]
ScenarioOutcome = Literal["passed", "failed"]

_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)(\S+)"),
    re.compile(r"(?i)(bearer\s+)(\S+)"),
    re.compile(r"(?i)(https?://[^/\s:@]+:)([^@\s]+)(@)"),
    re.compile(r"(?i)((?:token|api_key|secret|password)\s*[:=]\s*)(\S+)"),
)


def _redact(value: object) -> object:
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:

            def replace(match: re.Match[str]) -> str:
                suffix = (
                    match.group(3) if match.lastindex and match.lastindex >= 3 else ""
                )
                return f"{match.group(1)}[REDACTED]{suffix}"

            value = pattern.sub(replace, value)
        return value
    if isinstance(value, Mapping):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_redact(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class DiagnosticProviderAvailability:
    """The optional availability state of an agent-facing provider."""

    provider: str
    status: DiagnosticAvailability
    reason: str | None = None

    @classmethod
    def available(cls, provider: str) -> DiagnosticProviderAvailability:
        return cls(provider=provider, status="available")

    @classmethod
    def unavailable(cls, provider: str, reason: str) -> DiagnosticProviderAvailability:
        return cls(provider=provider, status="unavailable", reason=reason)


@dataclass(frozen=True, slots=True)
class ScenarioVerdict:
    """A deterministic scenario verdict that diagnostics can observe only."""

    outcome: ScenarioOutcome
    determined_at: datetime | None = None
    assertion_contract_ids: tuple[str, ...] = ()
    _frozen: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def freeze(
        cls,
        *,
        outcome: ScenarioOutcome,
        determined_at: datetime,
        assertion_contract_ids: tuple[str, ...],
    ) -> ScenarioVerdict:
        return cls(
            outcome=outcome,
            determined_at=determined_at,
            assertion_contract_ids=assertion_contract_ids,
            _frozen=True,
        )

    @property
    def is_frozen(self) -> bool:
        return self._frozen


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    provider: str
    payload: Mapping[str, object]
    authoritative: Literal[False] = False

    def serialize(self) -> str:
        """Return redacted output safe for diagnostic artifacts and logs."""
        return json.dumps(
            {
                "provider": self.provider,
                "authoritative": self.authoritative,
                "payload": _redact(self.payload),
            },
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class DiagnosticRunResult:
    verdict: ScenarioVerdict
    report: DiagnosticReport | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QueryContractProposal:
    """A candidate query that cannot become an approved contract by itself."""

    provider: str
    query: str
    review_status: Literal["draft"] = "draft"
    activation_status: Literal["not-active"] = "not-active"


def diagnostic_report(provider: str, payload: Mapping[str, object]) -> DiagnosticReport:
    return DiagnosticReport(provider=provider, payload=payload)


def run_diagnostics_after_verdict(
    verdict: ScenarioVerdict,
    availability: DiagnosticProviderAvailability,
    collect: Callable[[], DiagnosticReport],
) -> DiagnosticRunResult:
    """Run optional diagnostics after, and without authority over, a verdict."""
    if not verdict.is_frozen:
        raise ValueError("diagnostics require a frozen deterministic verdict")
    if availability.status == "unavailable":
        reason = availability.reason or "unavailable"
        return DiagnosticRunResult(
            verdict=verdict,
            report=None,
            warnings=(f"{availability.provider} diagnostics unavailable: {reason}",),
        )
    report = collect()
    if report.provider != availability.provider:
        raise ValueError("diagnostic report provider does not match availability")
    return DiagnosticRunResult(verdict=verdict, report=report)


def draft_query_contract(provider: str, query: str) -> QueryContractProposal:
    return QueryContractProposal(provider=provider, query=query)


def emit_diagnostic_log(logger: logging.Logger, report: DiagnosticReport) -> None:
    logger.info("non-authoritative diagnostic report: %s", report.serialize())
