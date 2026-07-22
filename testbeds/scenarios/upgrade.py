"""Explicit, pure v1alpha1 to v1alpha2 migration."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import ValidationError

from testbeds.scenarios.models import ActionType, GuardianScenario
from testbeds.scenarios.v1alpha2 import (
    EnvironmentCapability,
    GuardianScenarioV1Alpha2,
    ScaleDirection,
)


class UpgradeError(ValueError):
    """Legacy semantics cannot be represented safely without reviewed input."""


def upgrade_v1alpha1(
    scenario: GuardianScenario,
    *,
    description: str,
    capabilities: Iterable[EnvironmentCapability | str],
    scale_direction: ScaleDirection | None,
    recovery_contract_ref: str,
    recovery_contract_version: int,
    recovery_registry_version: str,
) -> GuardianScenarioV1Alpha2:
    expected = scenario.spec.expected
    has_scale = ActionType.SCALE in expected.allowed_actions or (
        expected.proposed_action is not None
        and expected.proposed_action.action_type is ActionType.SCALE
    )
    if has_scale and scale_direction is None:
        raise UpgradeError(
            "legacy scale assertions require an explicit reviewed direction"
        )

    def action(value: ActionType) -> dict[str, str]:
        result = {"actionType": value.value}
        if value is ActionType.SCALE:
            result["scaleDirection"] = scale_direction.value  # type: ignore[union-attr]
        return result

    eligible = [action(value) for value in expected.allowed_actions]
    forbidden = [
        action(value)
        if value is not ActionType.SCALE
        else {"actionType": "scale", "scaleDirection": "any"}
        for value in expected.forbidden_actions
    ]
    proposed = (
        action(expected.proposed_action.action_type)
        if expected.proposed_action
        else None
    )
    mutating = any(
        item in {ActionType.SCALE, ActionType.ROLLBACK}
        for item in expected.allowed_actions
    )
    evidence = [
        {"evidenceType": item.value, "freshness": "fresh"}
        for item in expected.evidence_types
    ]
    document = {
        "apiVersion": "tests.guardian.io/v1alpha2",
        "kind": scenario.kind,
        "metadata": scenario.metadata.model_dump(by_alias=True),
        "spec": {
            "description": description,
            "candidateEnvironments": list(scenario.spec.applicable_environments),
            "environmentRequirements": {
                "capabilities": [getattr(item, "value", item) for item in capabilities]
            },
            "traceability": {
                "normativeRequirements": [],
                "acceptanceTests": [],
            },
            "target": scenario.spec.target.model_dump(mode="json", by_alias=True),
            "baseline": scenario.spec.baseline.model_dump(mode="python", by_alias=True)
            if scenario.spec.baseline
            else {"healthyFor": "1s"},
            "stimulus": scenario.spec.stimulus.model_dump(
                mode="python", by_alias=True, exclude_none=True
            ),
            "expected": {
                "incident": {
                    "incidentClass": expected.incident_class.value
                    if expected.incident_class
                    else None,
                    "actionable": mutating,
                },
                "evidence": {
                    "supporting": evidence,
                    "contradicting": [],
                    "requiredFresh": [],
                },
                "actions": {
                    "eligible": eligible,
                    "forbidden": forbidden,
                    "proposed": proposed,
                },
                "policy": {
                    "decision": (expected.policy_decision or "denied"),
                    "failClosed": True,
                },
                "workflow": {
                    "requiredStates": [item.value for item in expected.workflow_states],
                    "parentCount": {"exact": 1},
                    "proposalCount": {"atMost": 1},
                    "approvalCount": {"atMost": 1},
                },
                "mutations": {
                    "count": {"atMost": 1} if mutating else {"exact": 0},
                    "actions": [proposed] if proposed and mutating else [],
                },
                "audit": {"events": []},
                "safetyGates": ["post-action-evidence-for-recovery"]
                if mutating
                else [],
                "recovery": (
                    {
                        "contractRef": recovery_contract_ref,
                        "contractVersion": recovery_contract_version,
                        "registryVersion": recovery_registry_version,
                        "requireFreshTelemetry": True,
                        "evidence": [
                            {"evidenceType": "recovery-telemetry", "freshness": "fresh"}
                        ],
                        "conditions": ["service-healthy"],
                        "minimumPostActionWindows": 1,
                    }
                    if mutating
                    else None
                ),
            },
        },
    }
    try:
        return GuardianScenarioV1Alpha2.model_validate(document)
    except ValidationError as error:
        raise UpgradeError(f"v1alpha1 upgrade failed: {error}") from error
