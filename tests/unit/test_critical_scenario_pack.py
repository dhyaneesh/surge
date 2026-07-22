from pathlib import Path

import pytest

from testbeds.scenarios.loader import load_guardian_scenario
from testbeds.scenarios.v1alpha2 import (
    ActionType,
    GuardianScenarioV1Alpha2,
    PolicyBundleState,
    ScaleDirection,
    TelemetryQuality,
    WorkflowReason,
)


ROOT = Path(__file__).parents[2]
SCENARIO_DIR = ROOT / "testbeds/scenarios"
NAMES = (
    "healthy-load-no-action",
    "legitimate-demand-scale-up",
    "deployment-error-regression",
    "deployment-latency-regression",
    "dependency-failure-do-not-scale-caller",
    "resource-saturation",
    "telemetry-failure",
    "healthy-telemetry-unknown-cause",
    "scale-versus-rollback-conflict",
    "duplicate-alert-single-workflow",
    "expired-approval-no-mutation",
    "operator-drift-supersedes-action",
    "stale-signoz-no-scale-down",
    "stale-opa-fail-closed",
    "cross-tenant-evidence-rejected",
)


def scenarios() -> dict[str, GuardianScenarioV1Alpha2]:
    result = {}
    for name in NAMES:
        scenario = load_guardian_scenario(SCENARIO_DIR / f"{name}.yaml")
        assert isinstance(scenario, GuardianScenarioV1Alpha2)
        result[name] = scenario
    return result


@pytest.mark.parametrize("name", NAMES)
def test_critical_scenario_loads_and_has_explicit_compatibility(name: str) -> None:
    scenario = load_guardian_scenario(SCENARIO_DIR / f"{name}.yaml")
    assert isinstance(scenario, GuardianScenarioV1Alpha2)
    assert scenario.metadata.name == name
    assert scenario.spec.candidate_environments
    assert scenario.spec.environment_requirements.capabilities


def test_pack_safety_invariants() -> None:
    pack = scenarios()
    healthy = pack["healthy-load-no-action"].spec.expected
    assert healthy.mutations.count.exact == 0 and not healthy.actions.eligible

    demand = pack["legitimate-demand-scale-up"].spec.expected
    assert any(
        a.action_type is ActionType.SCALE and a.scale_direction is ScaleDirection.UP
        for a in demand.actions.eligible
    )
    assert any(a.action_type is ActionType.ROLLBACK for a in demand.actions.forbidden)

    dependency = pack["dependency-failure-do-not-scale-caller"].spec.expected
    assert any(a.action_type is ActionType.SCALE for a in dependency.actions.forbidden)

    telemetry = pack["telemetry-failure"].spec.expected
    unknown = pack["healthy-telemetry-unknown-cause"].spec.expected
    assert telemetry.incident.telemetry_quality is not TelemetryQuality.HEALTHY
    assert unknown.incident.telemetry_quality is TelemetryQuality.HEALTHY

    conflict = pack["scale-versus-rollback-conflict"].spec.expected
    assert conflict.workflow.terminal_reason is WorkflowReason.CONFLICT_UNRESOLVED
    assert conflict.mutations.count.exact == 0

    duplicate = pack["duplicate-alert-single-workflow"].spec.expected
    assert duplicate.workflow.parent_count.exact == 1

    assert (
        pack["expired-approval-no-mutation"].spec.expected.workflow.terminal_reason
        is WorkflowReason.APPROVAL_EXPIRED
    )
    assert (
        pack["operator-drift-supersedes-action"].spec.expected.workflow.terminal_reason
        is WorkflowReason.OPERATOR_DRIFT
    )

    stale = pack["stale-signoz-no-scale-down"].spec.expected
    assert stale.scaler is not None and stale.scaler.scale_down_forbidden

    opa = pack["stale-opa-fail-closed"].spec.expected
    assert opa.policy.bundle_state is PolicyBundleState.FAIL_CLOSED
    assert opa.mutations.count.exact == 0

    tenant = pack["cross-tenant-evidence-rejected"].spec.expected
    assert tenant.tenant_isolation is not None
    assert tenant.tenant_isolation.reject_foreign_evidence_before_scoring
