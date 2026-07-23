"""Deterministic semantic assertion evaluation for v1alpha2 scenarios."""

from __future__ import annotations

from typing import Any

from testbeds.scenarios.guardian_client import GuardianSnapshot
from testbeds.scenarios.models import StrictModel
from testbeds.scenarios.v1alpha2 import (
    CardinalityExpectation,
    GuardianScenarioV1Alpha2,
)


class AssertionResult(StrictModel):
    name: str
    passed: bool
    expected: Any
    actual: Any


def _cardinality(value: int, expected: CardinalityExpectation) -> bool:
    if expected.exact is not None:
        return value == expected.exact
    if expected.at_most is not None:
        return value <= expected.at_most
    assert expected.at_least is not None
    return value >= expected.at_least


def evaluate_assertions(
    scenario: GuardianScenarioV1Alpha2, snapshot: GuardianSnapshot
) -> tuple[AssertionResult, ...]:
    expected = scenario.spec.expected
    results: list[AssertionResult] = []

    def add(name: str, wanted: Any, actual: Any, passed: bool | None = None) -> None:
        results.append(
            AssertionResult(
                name=name,
                expected=wanted,
                actual=actual,
                passed=(wanted == actual if passed is None else passed),
            )
        )

    add(
        "incident.class",
        expected.incident.incident_class.value
        if expected.incident.incident_class
        else None,
        snapshot.incident_class,
    )
    add("incident.actionable", expected.incident.actionable, snapshot.actionable)
    if expected.incident.telemetry_quality is not None:
        add(
            "incident.telemetry_quality",
            expected.incident.telemetry_quality.value,
            snapshot.telemetry_quality,
        )
    add("policy.decision", expected.policy.decision.value, snapshot.policy_decision)
    add("policy.fail_closed", expected.policy.fail_closed, snapshot.policy_fail_closed)
    add(
        "workflow.required_states",
        [item.value for item in expected.workflow.required_states],
        list(snapshot.workflow_states),
        all(
            item.value in snapshot.workflow_states
            for item in expected.workflow.required_states
        ),
    )
    for name, actual, cardinality in (
        (
            "workflow.parent_count",
            snapshot.parent_count,
            expected.workflow.parent_count,
        ),
        (
            "workflow.proposal_count",
            snapshot.proposal_count,
            expected.workflow.proposal_count,
        ),
        (
            "workflow.approval_count",
            snapshot.approval_count,
            expected.workflow.approval_count,
        ),
        ("mutations.count", snapshot.mutation_count, expected.mutations.count),
    ):
        add(
            name,
            cardinality.model_dump(mode="json", by_alias=True),
            actual,
            _cardinality(actual, cardinality),
        )
    add(
        "workflow.terminal_reason",
        expected.workflow.terminal_reason.value
        if expected.workflow.terminal_reason
        else None,
        snapshot.terminal_reason,
    )
    for event in expected.audit.events:
        actual = snapshot.audit_event_counts.get(event.event_type.value, 0)
        add(
            f"audit.{event.event_type.value}",
            event.count.model_dump(mode="json", by_alias=True),
            actual,
            _cardinality(actual, event.count),
        )
    if expected.scaler is not None:
        add("scaler.result", expected.scaler.result.value, snapshot.scaler_result)
    if expected.recovery is not None:
        add(
            "recovery.state",
            [condition.value for condition in expected.recovery.conditions],
            snapshot.recovery_state,
            snapshot.recovery_state in {"healthy", "recovered"},
        )
    return tuple(results)
