"""Versioned deterministic Guardian evidence rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from apps.guardian_api.models import (
    ActionType,
    EvidenceFact,
    EvidenceFreshness,
    EvidenceSource,
    HypothesisName,
    HypothesisScore,
    SignalFacts,
)


class Polarity(StrEnum):
    SUPPORT = "support"
    CONTRADICTION = "contradiction"


@dataclass(frozen=True)
class Contribution:
    group: str
    polarity: Polarity
    weight: float
    confidence: float
    independence_group: str

    @property
    def normalized_weight(self) -> float:
        return self.weight * self.confidence


@dataclass(frozen=True)
class HypothesisRule:
    required_groups: tuple[frozenset[str], ...]
    action: ActionType
    discriminatory_groups: tuple[str, ...]


@dataclass(frozen=True)
class RuleDefinition:
    version: str
    hypotheses: dict[HypothesisName, HypothesisRule]


RULE_DEFINITION = RuleDefinition(
    version="guardian-rules/v1",
    hypotheses={
        HypothesisName.LOAD_SPIKE: HypothesisRule(
            required_groups=(frozenset({"load"}), frozenset({"utilization"})),
            action=ActionType.SCALE_UP,
            discriminatory_groups=("load", "utilization"),
        ),
        HypothesisName.DEPLOYMENT_REGRESSION: HypothesisRule(
            required_groups=(
                frozenset({"deployment"}),
                frozenset({"exceptions", "latency"}),
            ),
            action=ActionType.ROLLBACK,
            discriminatory_groups=("deployment", "exceptions|latency"),
        ),
        HypothesisName.RESOURCE_SATURATION: HypothesisRule(
            required_groups=(
                frozenset({"utilization"}),
                frozenset({"pressure"}),
            ),
            action=ActionType.SCALE_UP,
            discriminatory_groups=("pressure", "utilization"),
        ),
        HypothesisName.DEPENDENCY_FAILURE: HypothesisRule(
            required_groups=(frozenset({"topology"}), frozenset({"dependency"})),
            action=ActionType.PROTECT_DEPENDENCY,
            discriminatory_groups=("dependency", "topology"),
        ),
    },
)

INDEPENDENT_TELEMETRY_SOURCES = frozenset(
    {EvidenceSource.QUERY_CONTRACT, EvidenceSource.ADAPTER_OBSERVATION}
)
ALLOWED_SOURCES_BY_SIGNAL = {
    "request_rate": INDEPENDENT_TELEMETRY_SOURCES,
    "cpu_utilization": INDEPENDENT_TELEMETRY_SOURCES,
    "memory_utilization": INDEPENDENT_TELEMETRY_SOURCES,
    "throttling_ratio": INDEPENDENT_TELEMETRY_SOURCES,
    "error_rate": INDEPENDENT_TELEMETRY_SOURCES,
    "p95_latency_ms": INDEPENDENT_TELEMETRY_SOURCES,
    "oom_killed": INDEPENDENT_TELEMETRY_SOURCES | {EvidenceSource.CONTROL_PLANE},
    "restart_delta": INDEPENDENT_TELEMETRY_SOURCES | {EvidenceSource.CONTROL_PLANE},
    "deployment_version": frozenset(
        {
            EvidenceSource.ADAPTER_OBSERVATION,
            EvidenceSource.CONTROL_PLANE,
            EvidenceSource.DEPLOYMENT_EVENT,
        }
    ),
    "topology_edge": INDEPENDENT_TELEMETRY_SOURCES | {EvidenceSource.CONTROL_PLANE},
    "dependency_healthy": INDEPENDENT_TELEMETRY_SOURCES
    | {EvidenceSource.CONTROL_PLANE},
}


def _fresh(
    signal_name: str,
    evidence: EvidenceFact | None,
    *,
    now: datetime,
    freshness_seconds: int,
) -> bool:
    return (
        evidence is not None
        and evidence.freshness is EvidenceFreshness.FRESH
        and evidence.source in ALLOWED_SOURCES_BY_SIGNAL[signal_name]
        and evidence.observed_at <= now
        and now - evidence.observed_at <= timedelta(seconds=freshness_seconds)
    )


def _add(
    output: list[Contribution],
    evidence: EvidenceFact,
    group: str,
    polarity: Polarity,
    weight: float,
) -> None:
    output.append(
        Contribution(
            group=group,
            polarity=polarity,
            weight=weight,
            confidence=evidence.usable_confidence,
            independence_group=evidence.independence_group,
        )
    )


def evidence_contributions(
    signals: SignalFacts, *, now: datetime, freshness_seconds: int
) -> tuple[Contribution, ...]:
    """Convert normalized facts using the exact guardian-rules/v1 thresholds."""

    output: list[Contribution] = []
    rate = signals.request_rate
    if (
        _fresh("request_rate", rate, now=now, freshness_seconds=freshness_seconds)
        and rate is not None
        and rate.baseline_value is not None
    ):
        if rate.baseline_value > 0 and rate.value >= 2 * rate.baseline_value:
            _add(output, rate, "load", Polarity.SUPPORT, 0.40)
        elif rate.baseline_value > 0 and rate.value < 1.25 * rate.baseline_value:
            _add(output, rate, "load", Polarity.CONTRADICTION, 0.40)

    utilization = []
    if signals.cpu_utilization is not None and _fresh(
        "cpu_utilization",
        signals.cpu_utilization,
        now=now,
        freshness_seconds=freshness_seconds,
    ):
        utilization.append(signals.cpu_utilization)
    if signals.memory_utilization is not None and _fresh(
        "memory_utilization",
        signals.memory_utilization,
        now=now,
        freshness_seconds=freshness_seconds,
    ):
        utilization.append(signals.memory_utilization)
    for item in utilization:
        if item.value >= 0.85:
            _add(output, item, "utilization", Polarity.SUPPORT, 0.40)

    pressure: list[EvidenceFact] = []
    throttling = signals.throttling_ratio
    if (
        _fresh(
            "throttling_ratio",
            throttling,
            now=now,
            freshness_seconds=freshness_seconds,
        )
        and throttling is not None
        and throttling.value >= 0.20
    ):
        pressure.append(throttling)
    oom = signals.oom_killed
    if (
        _fresh("oom_killed", oom, now=now, freshness_seconds=freshness_seconds)
        and oom is not None
        and oom.value
    ):
        pressure.append(oom)
    restarts = signals.restart_delta
    if (
        _fresh("restart_delta", restarts, now=now, freshness_seconds=freshness_seconds)
        and restarts is not None
        and restarts.value >= 1
    ):
        pressure.append(restarts)
    for item in pressure:
        _add(output, item, "pressure", Polarity.SUPPORT, 0.35)

    if utilization and not pressure and max(item.value for item in utilization) < 0.70:
        least_confident = min(utilization, key=lambda item: item.usable_confidence)
        _add(
            output,
            least_confident,
            "utilization",
            Polarity.CONTRADICTION,
            0.40,
        )

    deployment = signals.deployment_version
    if (
        _fresh(
            "deployment_version",
            deployment,
            now=now,
            freshness_seconds=freshness_seconds,
        )
        and deployment is not None
    ):
        polarity = (
            Polarity.SUPPORT
            if deployment.previous_digest != deployment.current_digest
            else Polarity.CONTRADICTION
        )
        _add(output, deployment, "deployment", polarity, 0.40)

    errors = signals.error_rate
    if (
        _fresh("error_rate", errors, now=now, freshness_seconds=freshness_seconds)
        and errors is not None
        and errors.baseline_value is not None
    ):
        if errors.value >= max(0.05, 2 * errors.baseline_value):
            _add(output, errors, "exceptions", Polarity.SUPPORT, 0.45)

    latency = signals.p95_latency_ms
    if (
        _fresh(
            "p95_latency_ms",
            latency,
            now=now,
            freshness_seconds=freshness_seconds,
        )
        and latency is not None
        and latency.baseline_value is not None
    ):
        if (
            latency.value >= 1.5 * latency.baseline_value
            and latency.value - latency.baseline_value >= 100
        ):
            _add(output, latency, "latency", Polarity.SUPPORT, 0.45)

    topology = signals.topology_edge
    if (
        _fresh("topology_edge", topology, now=now, freshness_seconds=freshness_seconds)
        and topology is not None
        and topology.value
    ):
        _add(output, topology, "topology", Polarity.SUPPORT, 0.35)

    dependency = signals.dependency_healthy
    if (
        _fresh(
            "dependency_healthy",
            dependency,
            now=now,
            freshness_seconds=freshness_seconds,
        )
        and dependency is not None
    ):
        _add(
            output,
            dependency,
            "dependency",
            Polarity.CONTRADICTION if dependency.value else Polarity.SUPPORT,
            0.30 if dependency.value else 0.45,
        )
    return tuple(output)


def _deduplicate_contributions(
    contributions: list[Contribution],
) -> tuple[Contribution, ...]:
    selected: dict[str, Contribution] = {}
    for contribution in contributions:
        current = selected.get(contribution.independence_group)
        if (
            current is None
            or contribution.normalized_weight > current.normalized_weight
        ):
            selected[contribution.independence_group] = contribution
    return tuple(selected.values())


def score_hypotheses(
    signals: SignalFacts,
    *,
    telemetry_healthy: bool,
    identity_resolved: bool,
    now: datetime,
    freshness_seconds: int,
) -> tuple[HypothesisScore, ...]:
    """Score and gate every supported causal hypothesis deterministically."""

    contributions = evidence_contributions(
        signals, now=now, freshness_seconds=freshness_seconds
    )
    results: list[HypothesisScore] = []
    for name, definition in RULE_DEFINITION.hypotheses.items():
        required_sets = definition.required_groups
        relevant_groups = frozenset().union(*required_sets)
        relevant = list(
            _deduplicate_contributions(
                [item for item in contributions if item.group in relevant_groups]
            )
        )
        supporting = [item for item in relevant if item.polarity is Polarity.SUPPORT]
        contradicting = [
            item for item in relevant if item.polarity is Polarity.CONTRADICTION
        ]
        support = min(1.0, sum(item.normalized_weight for item in supporting))
        contradiction = min(1.0, sum(item.normalized_weight for item in contradicting))
        deterministic_score = max(0.0, support - contradiction)
        group_confidence: dict[str, float] = {}
        for alternatives in required_sets:
            label = "|".join(sorted(alternatives))
            group_confidence[label] = max(
                (item.confidence for item in supporting if item.group in alternatives),
                default=0.0,
            )
        evidence_confidence = min(group_confidence.values(), default=0.0)
        eligible = (
            deterministic_score >= 0.70
            and evidence_confidence >= 0.85
            and contradiction <= 0.25
            and telemetry_healthy
            and identity_resolved
        )
        results.append(
            HypothesisScore(
                name=name,
                support=support,
                contradiction=contradiction,
                deterministic_score=deterministic_score,
                evidence_confidence=evidence_confidence,
                required_group_confidence=group_confidence,
                eligible=eligible,
            )
        )
    return tuple(results)
