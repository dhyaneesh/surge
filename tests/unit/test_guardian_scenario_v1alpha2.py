from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from testbeds.scenarios.v1alpha2 import GuardianScenarioV1Alpha2


def document() -> dict:
    return {
        "apiVersion": "tests.guardian.io/v1alpha2",
        "kind": "GuardianScenario",
        "metadata": {"name": "scale-pressure"},
        "spec": {
            "description": "Scale a pressured request processor.",
            "candidateEnvironments": ["otel-demo"],
            "environmentRequirements": {
                "capabilities": [
                    "healthy-baseline",
                    "horizontal-scaling",
                    "mutation-observation",
                    "recovery-observation",
                ]
            },
            "traceability": {"normativeRequirements": [], "acceptanceTests": []},
            "target": {
                "serviceSelector": {
                    "role": "request-processor",
                    "capabilities": ["horizontally-scalable"],
                }
            },
            "baseline": {"healthyFor": "5m"},
            "stimulus": {
                "load": {"pattern": "step", "multiplier": 4, "duration": "10m"}
            },
            "expected": {
                "incident": {
                    "incidentClass": "resource_saturation",
                    "actionable": True,
                },
                "evidence": {
                    "supporting": [
                        {
                            "evidenceType": "resource-utilization",
                            "subjectRole": "request-processor",
                            "freshness": "fresh",
                        }
                    ],
                    "contradicting": [
                        {
                            "evidenceType": "dependency-health",
                            "subjectRole": "dependency",
                            "freshness": "fresh",
                        }
                    ],
                    "requiredFresh": [
                        {
                            "evidenceType": "recovery-telemetry",
                            "subjectRole": "request-processor",
                            "freshness": "fresh",
                        }
                    ],
                },
                "actions": {
                    "eligible": [{"actionType": "scale", "scaleDirection": "up"}],
                    "forbidden": [{"actionType": "rollback"}],
                    "proposed": {"actionType": "scale", "scaleDirection": "up"},
                },
                "policy": {"decision": "allowed", "failClosed": True},
                "workflow": {
                    "requiredStates": ["active", "recovery-verification"],
                    "parentCount": {"exact": 1},
                    "proposalCount": {"atMost": 1},
                    "approvalCount": {"exact": 0},
                },
                "mutations": {
                    "count": {"atMost": 1},
                    "actions": [{"actionType": "scale", "scaleDirection": "up"}],
                },
                "audit": {
                    "events": [
                        {"eventType": "mutation-executed", "count": {"atMost": 1}}
                    ]
                },
                "safetyGates": ["post-action-evidence-for-recovery"],
                "recovery": {
                    "contractRef": "service-scale-recovery",
                    "contractVersion": 1,
                    "registryVersion": "registry-v1",
                    "requireFreshTelemetry": True,
                    "evidence": [
                        {
                            "evidenceType": "recovery-telemetry",
                            "subjectRole": "request-processor",
                            "freshness": "fresh",
                        }
                    ],
                    "conditions": ["capacity-converged", "service-healthy"],
                    "minimumPostActionWindows": 1,
                },
            },
        },
    }


def test_complete_v1alpha2_is_frozen_and_serializes_with_aliases() -> None:
    scenario = GuardianScenarioV1Alpha2.model_validate(document())
    proposed = scenario.spec.expected.actions.proposed
    assert proposed is not None
    assert proposed.scale_direction is not None
    assert proposed.scale_direction.value == "up"
    assert scenario.model_dump(mode="json", by_alias=True)["apiVersion"].endswith(
        "v1alpha2"
    )
    with pytest.raises(ValidationError):
        scenario.spec.description = "changed"


@pytest.mark.parametrize(
    "mutation", ["missing-recovery", "direction-mismatch", "action-overlap"]
)
def test_mutation_safety_invariants(mutation: str) -> None:
    value = copy.deepcopy(document())
    expected = value["spec"]["expected"]
    if mutation == "missing-recovery":
        expected.pop("recovery")
    elif mutation == "direction-mismatch":
        expected["mutations"]["actions"][0]["scaleDirection"] = "down"
    else:
        expected["actions"]["forbidden"] = [
            {"actionType": "scale", "scaleDirection": "any"}
        ]
    with pytest.raises(ValidationError):
        GuardianScenarioV1Alpha2.model_validate(value)


def test_unknown_with_unhealthy_telemetry_is_rejected() -> None:
    value = document()
    expected = value["spec"]["expected"]
    expected["incident"] = {
        "incidentClass": "unknown",
        "actionable": False,
        "telemetryQuality": "failed",
    }
    expected["actions"] = {
        "eligible": [],
        "forbidden": [{"actionType": "scale", "scaleDirection": "any"}],
    }
    expected["mutations"] = {"count": {"exact": 0}, "actions": []}
    expected.pop("recovery")
    with pytest.raises(ValidationError):
        GuardianScenarioV1Alpha2.model_validate(value)
