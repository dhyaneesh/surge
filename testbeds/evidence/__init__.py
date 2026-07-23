"""Independent, provenance-backed evidence collection for disposable testbeds."""

from testbeds.evidence.collector import (
    EvidenceCollector,
    EvidenceSample,
    ProbeResult,
    UnavailableEvidence,
    control_result_is_not_symptom_evidence,
)
from testbeds.evidence.contracts import (
    EVIDENCE_TYPE_SOURCES,
    EvidenceSourceKind,
    missing_evidence_sources_for_scenario,
    required_evidence_sources_for_scenario,
    scenario_evidence_satisfied,
    substantiate_capabilities,
)
from testbeds.evidence.signoz import SignozEvidenceClient, SignozQueryResult

__all__ = [
    "EVIDENCE_TYPE_SOURCES",
    "EvidenceCollector",
    "EvidenceSample",
    "EvidenceSourceKind",
    "ProbeResult",
    "SignozEvidenceClient",
    "SignozQueryResult",
    "UnavailableEvidence",
    "control_result_is_not_symptom_evidence",
    "missing_evidence_sources_for_scenario",
    "required_evidence_sources_for_scenario",
    "scenario_evidence_satisfied",
    "substantiate_capabilities",
]
