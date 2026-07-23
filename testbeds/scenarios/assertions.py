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
    if expected.policy.bundle_state is not None:
        add(
            "policy.bundle_state",
            expected.policy.bundle_state.value,
            snapshot.policy_bundle_state,
        )
    add(
        "policy.permitted_operations",
        [item.value for item in expected.policy.permitted_operations],
        list(snapshot.permitted_operations),
        all(
            item.value in snapshot.permitted_operations
            for item in expected.policy.permitted_operations
        ),
    )
    add(
        "policy.forbidden_operations",
        [item.value for item in expected.policy.forbidden_operations],
        list(snapshot.forbidden_operations),
        all(
            item.value in snapshot.forbidden_operations
            for item in expected.policy.forbidden_operations
        ),
    )
    for name, wanted, actual in (
        (
            "evidence.supporting",
            expected.evidence.supporting,
            snapshot.supporting_evidence,
        ),
        (
            "evidence.contradicting",
            expected.evidence.contradicting,
            snapshot.contradicting_evidence,
        ),
        (
            "evidence.required_fresh",
            expected.evidence.required_fresh,
            snapshot.required_fresh_evidence,
        ),
    ):
        rendered = [
            item.model_dump(mode="json", by_alias=True, exclude_none=True)
            for item in wanted
        ]
        add(name, rendered, list(actual), all(item in actual for item in rendered))
    eligible = [
        item.model_dump(mode="json", by_alias=True, exclude_none=True)
        for item in expected.actions.eligible
    ]
    forbidden = [
        item.model_dump(mode="json", by_alias=True, exclude_none=True)
        for item in expected.actions.forbidden
    ]
    proposed = (
        expected.actions.proposed.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        if expected.actions.proposed
        else None
    )
    add(
        "actions.eligible",
        eligible,
        list(snapshot.eligible_actions),
        all(item in snapshot.eligible_actions for item in eligible),
    )
    add(
        "actions.forbidden",
        forbidden,
        list(snapshot.forbidden_actions),
        all(item in snapshot.forbidden_actions for item in forbidden),
    )
    add("actions.proposed", proposed, snapshot.proposed_action)
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
        cardinality_passed = _cardinality(actual, cardinality)
        if name == "mutations.count":
            cardinality_passed = cardinality_passed and actual == len(
                snapshot.executed_mutations
            )
        add(
            name,
            cardinality.model_dump(mode="json", by_alias=True),
            actual,
            cardinality_passed,
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
    allowed_mutation_actions = [
        item.model_dump(mode="json", by_alias=True, exclude_none=True)
        for item in expected.mutations.allowed_actions
    ]
    executed_mutations = list(snapshot.executed_mutations)
    add(
        "mutations.actions",
        allowed_mutation_actions,
        executed_mutations,
        all(item in allowed_mutation_actions for item in executed_mutations),
    )
    add(
        "safety_gates",
        [item.value for item in expected.safety_gates],
        list(snapshot.safety_gates),
        all(item.value in snapshot.safety_gates for item in expected.safety_gates),
    )
    if expected.tenant_isolation is not None:
        wanted_tenant = expected.tenant_isolation.model_dump(mode="json", by_alias=True)
        add("tenant_isolation", wanted_tenant, snapshot.tenant_isolation)
    if expected.scaler is not None:
        add("scaler.result", expected.scaler.result.value, snapshot.scaler_result)
        if expected.scaler.fabricated_zero_forbidden:
            add(
                "scaler.fabricated_zero_forbidden",
                False,
                snapshot.scaler_fabricated_zero,
            )
        if expected.scaler.scale_down_forbidden:
            add(
                "scaler.scale_down_forbidden",
                False,
                snapshot.scaler_scale_down_permitted,
            )
    if expected.recovery is not None:
        add(
            "recovery.state",
            [condition.value for condition in expected.recovery.conditions],
            snapshot.recovery_state,
            snapshot.recovery_state in {"healthy", "recovered"},
        )
    return tuple(results)
