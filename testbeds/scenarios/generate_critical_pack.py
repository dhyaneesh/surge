"""Generate the deterministic critical GuardianScenario v1alpha2 YAML pack."""

from __future__ import annotations

from pathlib import Path

import yaml


DIRECTORY = Path(__file__).parent
ALL_ENVIRONMENTS = [
    "otel-demo",
    "aws-retail",
    "online-boutique",
    "argo-rollouts",
    "keda-rabbitmq",
]


def action(action_type: str, direction: str | None = None) -> dict:
    value = {"actionType": action_type}
    if direction:
        value["scaleDirection"] = direction
    return value


def evidence(
    kind: str,
    role: str = "request-processor",
    freshness: str = "fresh",
    tenant: str = "same-tenant",
) -> dict:
    return {
        "evidenceType": kind,
        "subjectRole": role,
        "freshness": freshness,
        "tenantRelation": tenant,
    }


def scenario(
    name: str,
    description: str,
    capabilities: list[str],
    *,
    environments: list[str] | None = None,
    incident: str | None = None,
    telemetry: str | None = "healthy",
    stimulus: dict | None = None,
    supporting: list[dict] | None = None,
    contradicting: list[dict] | None = None,
    eligible: list[dict] | None = None,
    forbidden: list[dict] | None = None,
    proposed: dict | None = None,
    workflow_states: list[str] | None = None,
    terminal_reason: str | None = None,
    mutation_count: dict | None = None,
    mutation_actions: list[dict] | None = None,
    policy: dict | None = None,
    audit_events: list[dict] | None = None,
    safety_gates: list[str] | None = None,
    recovery_conditions: list[str] | None = None,
    extra_expected: dict | None = None,
    requirements: list[str] | None = None,
    acceptance_tests: list[str] | None = None,
) -> dict:
    eligible = eligible or []
    forbidden = forbidden or []
    mutation_actions = mutation_actions or []
    mutating = bool(mutation_actions)
    expected = {
        "incident": {
            "incidentClass": incident,
            "actionable": bool(proposed),
            "telemetryQuality": telemetry,
        },
        "evidence": {
            "supporting": supporting or [],
            "contradicting": contradicting or [],
            "requiredFresh": [],
        },
        "actions": {"eligible": eligible, "forbidden": forbidden, "proposed": proposed},
        "policy": policy
        or {
            "decision": "denied" if not proposed else "approval-required",
            "failClosed": True,
        },
        "workflow": {
            "requiredStates": workflow_states or ["active", "assessment", "closed"],
            "parentCount": {"exact": 1},
            "proposalCount": {"atMost": 1},
            "approvalCount": {"atMost": 1} if proposed else {"exact": 0},
            "terminalReason": terminal_reason,
        },
        "mutations": {
            "count": mutation_count or ({"atMost": 1} if mutating else {"exact": 0}),
            "actions": mutation_actions,
        },
        "audit": {
            "events": audit_events
            or [{"eventType": "observation-recorded", "count": {"atLeast": 1}}]
        },
        "safetyGates": safety_gates or [],
        "recovery": None,
    }
    if mutating:
        expected["safetyGates"] = sorted(
            set(expected["safetyGates"] + ["post-action-evidence-for-recovery"])
        )
        recovery_evidence = evidence("recovery-telemetry")
        expected["evidence"]["requiredFresh"] = [recovery_evidence]
        expected["recovery"] = {
            "contractRef": f"{name}-recovery",
            "contractVersion": 1,
            "registryVersion": "critical-pack-v1",
            "requireFreshTelemetry": True,
            "evidence": [recovery_evidence],
            "conditions": recovery_conditions or ["service-healthy"],
            "minimumPostActionWindows": 1,
        }
    if extra_expected:
        expected.update(extra_expected)
    return {
        "apiVersion": "tests.guardian.io/v1alpha2",
        "kind": "GuardianScenario",
        "metadata": {"name": name},
        "spec": {
            "description": description,
            "candidateEnvironments": environments or ALL_ENVIRONMENTS,
            "environmentRequirements": {"capabilities": capabilities},
            "traceability": {
                "normativeRequirements": requirements or [],
                "acceptanceTests": acceptance_tests or [],
            },
            "target": {"serviceSelector": {"role": "request-processor"}},
            "baseline": {"healthyFor": "5m"},
            "stimulus": stimulus or {},
            "expected": expected,
        },
    }


SCALE_UP = action("scale", "up")
SCALE_ANY = action("scale", "any")
ROLLBACK = action("rollback")

SCENARIOS = [
    scenario(
        "healthy-load-no-action",
        "Healthy elevated load remains observation-only.",
        [
            "healthy-baseline",
            "load-generation",
            "workflow-observation",
            "mutation-observation",
        ],
        stimulus={"load": {"pattern": "step", "multiplier": 2, "duration": "10m"}},
        supporting=[evidence("metrics")],
        forbidden=[SCALE_ANY, ROLLBACK],
        mutation_count={"exact": 0},
    ),
    scenario(
        "legitimate-demand-scale-up",
        "Sustained legitimate demand permits one scale-up.",
        [
            "healthy-baseline",
            "load-generation",
            "horizontal-scaling",
            "dependency-observation",
            "workflow-observation",
            "mutation-observation",
            "recovery-observation",
        ],
        environments=["otel-demo", "online-boutique", "keda-rabbitmq"],
        incident="load_spike",
        stimulus={"load": {"pattern": "sustained", "multiplier": 4, "duration": "10m"}},
        supporting=[evidence("load"), evidence("resource-utilization")],
        contradicting=[evidence("dependency-health", "dependency")],
        eligible=[SCALE_UP],
        forbidden=[ROLLBACK],
        proposed=SCALE_UP,
        mutation_actions=[SCALE_UP],
        recovery_conditions=["capacity-converged", "service-healthy"],
    ),
    scenario(
        "deployment-error-regression",
        "A version-linked error regression permits rollback.",
        [
            "healthy-baseline",
            "deployment-transition",
            "progressive-delivery",
            "workflow-observation",
            "mutation-observation",
            "recovery-observation",
        ],
        environments=["argo-rollouts"],
        incident="deployment_regression",
        stimulus={
            "deployment": {"fromVersion": "stable", "toVersion": "error-regression"}
        },
        supporting=[evidence("deployment-event"), evidence("exceptions")],
        eligible=[ROLLBACK],
        forbidden=[SCALE_ANY],
        proposed=ROLLBACK,
        mutation_actions=[ROLLBACK],
        safety_gates=["identity-before-version-action"],
        recovery_conditions=["error-improved", "desired-version-restored"],
    ),
    scenario(
        "deployment-latency-regression",
        "A version-linked latency regression permits rollback.",
        [
            "healthy-baseline",
            "deployment-transition",
            "progressive-delivery",
            "dependency-observation",
            "workflow-observation",
            "mutation-observation",
            "recovery-observation",
        ],
        environments=["argo-rollouts"],
        incident="deployment_regression",
        stimulus={
            "deployment": {"fromVersion": "stable", "toVersion": "latency-regression"}
        },
        supporting=[evidence("deployment-event"), evidence("metrics")],
        contradicting=[evidence("dependency-health", "dependency")],
        eligible=[ROLLBACK],
        forbidden=[SCALE_ANY],
        proposed=ROLLBACK,
        mutation_actions=[ROLLBACK],
        safety_gates=["identity-before-version-action"],
        recovery_conditions=["latency-improved", "desired-version-restored"],
    ),
    scenario(
        "dependency-failure-do-not-scale-caller",
        "A failing dependency does not authorize caller scaling.",
        [
            "healthy-baseline",
            "fault-injection",
            "dependency-observation",
            "workflow-observation",
            "mutation-observation",
        ],
        environments=["otel-demo", "aws-retail", "online-boutique"],
        incident="dependency_failure",
        stimulus={"fault": {"type": "dependency_unavailable", "magnitude": 1}},
        supporting=[evidence("dependency-health", "dependency"), evidence("topology")],
        contradicting=[evidence("resource-utilization")],
        forbidden=[SCALE_ANY],
        mutation_count={"exact": 0},
        audit_events=[{"eventType": "action-rejected", "count": {"atLeast": 1}}],
    ),
    scenario(
        "resource-saturation",
        "Local resource pressure permits scale-up without rollback.",
        [
            "healthy-baseline",
            "resource-pressure",
            "dependency-observation",
            "horizontal-scaling",
            "workflow-observation",
            "mutation-observation",
            "recovery-observation",
        ],
        incident="resource_saturation",
        stimulus={"fault": {"type": "high_cpu", "magnitude": 1}},
        supporting=[evidence("resource-utilization")],
        contradicting=[evidence("dependency-health", "dependency")],
        eligible=[SCALE_UP],
        forbidden=[ROLLBACK],
        proposed=SCALE_UP,
        mutation_actions=[SCALE_UP],
        recovery_conditions=["resource-pressure-cleared", "service-healthy"],
    ),
    scenario(
        "telemetry-failure",
        "Unusable telemetry fails closed and requires fresh restoration.",
        [
            "healthy-baseline",
            "telemetry-interruption",
            "workflow-observation",
            "mutation-observation",
            "recovery-observation",
        ],
        incident="telemetry_failure",
        telemetry="failed",
        stimulus={"telemetry": {"mode": "interrupted"}},
        supporting=[evidence("telemetry-quality", freshness="missing")],
        forbidden=[SCALE_ANY, ROLLBACK],
        workflow_states=["active", "telemetry-failure", "closed"],
        terminal_reason="telemetry-unusable",
        mutation_count={"exact": 0},
        safety_gates=["fresh-evidence-before-eligibility"],
        requirements=["GRD-CLS-001", "GRD-CLS-003", "GRD-CLS-005"],
        acceptance_tests=["AT-CLS-001"],
    ),
    scenario(
        "healthy-telemetry-unknown-cause",
        "Fresh complete telemetry with no eligible cause remains unknown.",
        [
            "healthy-baseline",
            "ambiguous-symptom",
            "workflow-observation",
            "mutation-observation",
        ],
        environments=["otel-demo"],
        incident="unknown",
        telemetry="healthy",
        stimulus={"ambiguousSymptom": {"preserveHealthyTelemetry": True}},
        supporting=[evidence("telemetry-quality")],
        forbidden=[SCALE_ANY, ROLLBACK],
        workflow_states=["active", "unknown", "closed"],
        mutation_count={"exact": 0},
        requirements=["GRD-CLS-001", "GRD-CLS-004"],
        acceptance_tests=["AT-CLS-002"],
    ),
    scenario(
        "scale-versus-rollback-conflict",
        "Eligible scale and rollback candidates enter unresolved conflict.",
        [
            "healthy-baseline",
            "load-generation",
            "deployment-transition",
            "horizontal-scaling",
            "progressive-delivery",
            "workflow-observation",
            "mutation-observation",
            "recovery-observation",
        ],
        environments=["argo-rollouts"],
        incident="deployment_regression",
        stimulus={
            "load": {"pattern": "sustained", "multiplier": 4, "duration": "10m"},
            "deployment": {"fromVersion": "stable", "toVersion": "regressed"},
        },
        supporting=[evidence("load"), evidence("deployment-event")],
        eligible=[SCALE_UP, ROLLBACK],
        workflow_states=["active", "conflict-resolution", "closed"],
        terminal_reason="conflict-unresolved",
        mutation_count={"exact": 0},
        requirements=["GRD-ACT-001", "GRD-ACT-002", "GRD-ACT-005"],
        acceptance_tests=["AT-ACT-001"],
    ),
    scenario(
        "duplicate-alert-single-workflow",
        "Duplicate incident deliveries converge on one tenant-scoped parent.",
        ["incident-ingress-control", "workflow-observation", "mutation-observation"],
        stimulus={"incidentDelivery": {"count": 3, "mode": "duplicate"}},
        mutation_count={"exact": 0},
        audit_events=[
            {"eventType": "duplicate-event-received", "count": {"atLeast": 2}}
        ],
        extra_expected={
            "tenantIsolation": {
                "rejectForeignEvidenceBeforeScoring": False,
                "rejectMismatchBeforeExternalIo": False,
                "tenantScopedWorkflowIdentity": True,
                "tenantScopedDeduplication": True,
                "tenantScopedCacheAndTopology": True,
                "tenantScopedApproval": True,
            }
        },
        requirements=["GRD-WF-001"],
        acceptance_tests=["AT-WF-001"],
    ),
    scenario(
        "expired-approval-no-mutation",
        "Expired approval and replay attempts cannot mutate.",
        ["approval-control", "workflow-observation", "mutation-observation"],
        stimulus={"approval": {"attemptAfterExpiry": True}},
        eligible=[ROLLBACK],
        terminal_reason="approval-expired",
        mutation_count={"exact": 0},
        safety_gates=["expiry-before-mutation"],
        audit_events=[{"eventType": "action-rejected", "count": {"atLeast": 1}}],
        requirements=["GRD-TTL-003", "GRD-TTL-005"],
        acceptance_tests=["AT-TTL-001"],
    ),
    scenario(
        "operator-drift-supersedes-action",
        "External protected-field drift supersedes an outdated proposal.",
        ["manual-workload-mutation", "workflow-observation", "mutation-observation"],
        environments=["argo-rollouts", "keda-rabbitmq"],
        stimulus={"operatorDrift": {"protectedFieldChangeBeforeMutation": True}},
        eligible=[ROLLBACK],
        terminal_reason="operator-drift",
        mutation_count={"exact": 0},
        safety_gates=["drift-before-each-mutation"],
        audit_events=[{"eventType": "action-rejected", "count": {"atLeast": 1}}],
        requirements=["GRD-DRIFT-001", "GRD-DRIFT-003"],
        acceptance_tests=["AT-DRIFT-001"],
    ),
    scenario(
        "stale-signoz-no-scale-down",
        "Stale SigNoz input returns safe hold and forbids scale-down.",
        [
            "telemetry-interruption",
            "scale-to-zero",
            "scaler-observation",
            "mutation-observation",
        ],
        environments=["keda-rabbitmq"],
        incident="telemetry_failure",
        telemetry="stale",
        stimulus={"telemetry": {"mode": "stale"}},
        supporting=[evidence("telemetry-quality", freshness="stale")],
        forbidden=[action("scale", "down")],
        mutation_count={"exact": 0},
        extra_expected={
            "scaler": {
                "result": "safe-hold",
                "fabricatedZeroForbidden": True,
                "scaleDownForbidden": True,
                "gatewayConvergenceRequired": True,
            }
        },
        requirements=["GRD-SCL-006", "GRD-OPA-006"],
        acceptance_tests=["AT-SCL-002"],
    ),
    scenario(
        "stale-opa-fail-closed",
        "An unusable OPA bundle denies every new write.",
        ["policy-bundle-control", "workflow-observation", "mutation-observation"],
        stimulus={"policyBundle": {"state": "fail-closed"}},
        eligible=[ROLLBACK],
        terminal_reason="policy-unusable",
        mutation_count={"exact": 0},
        policy={
            "decision": "denied",
            "bundleState": "fail-closed",
            "failClosed": True,
            "permittedOperations": ["read-only-investigation"],
            "forbiddenOperations": [
                "rollback",
                "scale-up",
                "scale-down",
                "scaler-pause",
                "policy-activation",
                "approval-issuance",
            ],
        },
        safety_gates=["policy-before-mutation"],
        requirements=["GRD-OPA-003", "GRD-OPA-006"],
        acceptance_tests=["AT-OPA-001"],
    ),
    scenario(
        "cross-tenant-evidence-rejected",
        "Foreign tenant evidence is rejected before scoring and I/O.",
        ["multi-tenant-fixture", "workflow-observation", "mutation-observation"],
        stimulus={"tenantInjection": {"evidenceTenantRelation": "foreign-tenant"}},
        supporting=[evidence("metrics")],
        contradicting=[evidence("metrics", tenant="foreign-tenant")],
        mutation_count={"exact": 0},
        terminal_reason="tenant-mismatch",
        safety_gates=["tenant-before-scoring", "tenant-before-external-io"],
        audit_events=[
            {"eventType": "tenant-reference-rejected", "count": {"atLeast": 1}}
        ],
        extra_expected={
            "tenantIsolation": {
                "rejectForeignEvidenceBeforeScoring": True,
                "rejectMismatchBeforeExternalIo": True,
                "tenantScopedWorkflowIdentity": True,
                "tenantScopedDeduplication": True,
                "tenantScopedCacheAndTopology": True,
                "tenantScopedApproval": True,
            }
        },
        requirements=["GRD-TEN-001", "GRD-TEN-006"],
        acceptance_tests=["AT-TEN-001"],
    ),
]


def main() -> None:
    for document in SCENARIOS:
        path = DIRECTORY / f"{document['metadata']['name']}.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    main()
