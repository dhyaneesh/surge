import asyncio
import json
from datetime import timedelta

import pytest

from testbeds.environments.capabilities import ENVIRONMENT_DECLARATIONS
from testbeds.models import (
    BaselineState,
    EnvironmentCapabilities,
    EnvironmentRelease,
    EnvironmentState,
    LoadExecution,
)
from testbeds.scenarios.execution import (
    AdapterRegistration,
    ExecutionSettings,
    ExecutionStatus,
    ScenarioExecutor,
    UnsupportedScenarioError,
)
from testbeds.scenarios.guardian_client import GuardianSnapshot, ScriptedGuardianClient
from testbeds.scenarios.loader import load_guardian_scenario
from testbeds.scenarios.v1alpha2 import EnvironmentCapability, GuardianScenarioV1Alpha2


class RecordingAdapter:
    capabilities = EnvironmentCapabilities(adjustable_load=True)

    def __init__(self):
        self.calls = []
        self.namespace = "guardian-test"

    async def install(self, release):
        self.calls.append("install")
        return EnvironmentState("otel-demo", self.namespace, release, healthy=True)

    async def reset(self):
        self.calls.append("reset")

    async def wait_for_healthy_baseline(self, timeout) -> BaselineState:
        self.calls.append("baseline")
        return BaselineState(True, environment=EnvironmentState(healthy=True))

    async def apply_load(self, profile):
        self.calls.append(f"load:{profile.concurrent_users}")
        return LoadExecution(profile, True)

    async def inject_fault(self, fault):
        self.calls.append("fault")
        raise AssertionError("unexpected fault")

    async def deploy_version(self, deployment):
        self.calls.append("deploy")
        raise AssertionError("unexpected deployment")

    async def observe_state(self):
        self.calls.append("observe")
        return EnvironmentState(healthy=True)

    async def cleanup(self):
        self.calls.append("cleanup")


class BlockingAdapter(RecordingAdapter):
    def __init__(self):
        super().__init__()
        self.baseline_started = asyncio.Event()

    async def wait_for_healthy_baseline(self, timeout):
        self.calls.append("baseline")
        if self.calls.count("baseline") > 1:
            return BaselineState(True, environment=EnvironmentState(healthy=True))
        self.baseline_started.set()
        await asyncio.Event().wait()
        raise RuntimeError("unreachable")


def registration(adapter, declaration=None):
    return AdapterRegistration(
        environment="otel-demo",
        adapter=adapter,
        release=EnvironmentRelease(environment="otel-demo"),
        declaration=declaration or ENVIRONMENT_DECLARATIONS["otel-demo"],
        role_bindings={"request-processor": "transaction-processor"},
        fault_role_bindings={},
        deployment_bindings={},
    )


def passing_snapshot():
    return GuardianSnapshot(
        incident_class=None,
        actionable=False,
        telemetry_quality="healthy",
        supporting_evidence=(
            {
                "evidenceType": "metrics",
                "subjectRole": "request-processor",
                "tenantRelation": "same-tenant",
                "freshness": "fresh",
            },
        ),
        forbidden_actions=(
            {"actionType": "scale", "scaleDirection": "any"},
            {"actionType": "rollback"},
        ),
        policy_decision="denied",
        policy_fail_closed=True,
        workflow_states=("active", "assessment", "closed"),
        parent_count=1,
        proposal_count=0,
        approval_count=0,
        mutation_count=0,
        audit_event_counts={"observation-recorded": 1},
    )


def test_executor_rejects_capability_mismatch_before_install(tmp_path):
    scenario = load_guardian_scenario(
        "testbeds/scenarios/deployment-error-regression.yaml"
    )
    adapter = RecordingAdapter()
    executor = ScenarioExecutor(ScriptedGuardianClient(passing_snapshot()))

    with pytest.raises(UnsupportedScenarioError):
        asyncio.run(
            executor.execute(
                scenario,
                registration(adapter),
                ExecutionSettings(tmp_path, baseline_timeout=timedelta(seconds=1)),
            )
        )

    assert adapter.calls == []


def test_executor_orders_lifecycle_and_persists_redacted_artifacts(tmp_path):
    scenario = load_guardian_scenario("testbeds/scenarios/healthy-load-no-action.yaml")
    adapter = RecordingAdapter()
    client = ScriptedGuardianClient(
        passing_snapshot(), response_metadata={"authorization": "Bearer secret-value"}
    )
    result = asyncio.run(
        ScenarioExecutor(client).execute(
            scenario,
            registration(adapter),
            ExecutionSettings(tmp_path, baseline_timeout=timedelta(seconds=1)),
        )
    )

    assert result.status is ExecutionStatus.PASSED
    assert adapter.calls == [
        "install",
        "baseline",
        "observe",
        "load:10",
        "observe",
        "reset",
        "baseline",
        "cleanup",
        "cleanup",
    ]
    summary = json.loads((result.artifact_directory / "summary.json").read_text())
    assert summary["status"] == "passed"
    artifacts = "\n".join(
        path.read_text() for path in result.artifact_directory.glob("*.json")
    )
    assert "secret-value" not in artifacts
    assert "[REDACTED]" in artifacts
    assert {
        "execution-metadata.json",
        "environment-identity.json",
        "load-results.json",
        "fault-results.json",
        "deployment-results.json",
        "incident-payloads.json",
        "diagnostics.json",
    } <= {path.name for path in result.artifact_directory.glob("*.json")}


def test_failed_assertion_returns_failed_status_and_still_resets_and_cleans(tmp_path):
    scenario = load_guardian_scenario("testbeds/scenarios/healthy-load-no-action.yaml")
    adapter = RecordingAdapter()
    snapshot = passing_snapshot().model_copy(update={"mutation_count": 1})

    result = asyncio.run(
        ScenarioExecutor(ScriptedGuardianClient(snapshot)).execute(
            scenario,
            registration(adapter),
            ExecutionSettings(tmp_path, baseline_timeout=timedelta(seconds=1)),
        )
    )

    assert result.status is ExecutionStatus.FAILED
    assert any(not assertion.passed for assertion in result.assertions)
    assert adapter.calls[-4:] == ["reset", "baseline", "cleanup", "cleanup"]


def test_duplicate_delivery_uses_one_idempotency_key(tmp_path):
    scenario = load_guardian_scenario(
        "testbeds/scenarios/duplicate-alert-single-workflow.yaml"
    )
    assert isinstance(scenario, GuardianScenarioV1Alpha2)
    adapter = RecordingAdapter()
    client = ScriptedGuardianClient(passing_snapshot())
    declaration = ENVIRONMENT_DECLARATIONS["otel-demo"].model_copy(
        update={
            "capabilities": ENVIRONMENT_DECLARATIONS["otel-demo"].capabilities
            | {EnvironmentCapability.INCIDENT_INGRESS_CONTROL}
        }
    )

    result = asyncio.run(
        ScenarioExecutor(client).execute(
            scenario,
            registration(adapter, declaration),
            ExecutionSettings(tmp_path, baseline_timeout=timedelta(seconds=1)),
        )
    )

    assert scenario.spec.stimulus.incident_delivery is not None
    assert len(client.submissions) == scenario.spec.stimulus.incident_delivery.count
    assert len({item[1] for item in client.submissions}) == 1
    assert result.execution_id == client.submissions[0][1]


def test_timeout_returns_failed_result_and_cleans_environment(tmp_path):
    scenario = load_guardian_scenario("testbeds/scenarios/healthy-load-no-action.yaml")
    adapter = BlockingAdapter()

    result = asyncio.run(
        ScenarioExecutor(ScriptedGuardianClient(passing_snapshot())).execute(
            scenario,
            registration(adapter),
            ExecutionSettings(
                tmp_path,
                baseline_timeout=timedelta(milliseconds=10),
                operation_timeout=timedelta(milliseconds=10),
            ),
        )
    )

    assert result.status is ExecutionStatus.FAILED
    assert adapter.calls[-2:] == ["cleanup", "cleanup"]


def test_cancellation_is_persisted_and_cleanup_still_runs(tmp_path):
    async def run():
        scenario = load_guardian_scenario(
            "testbeds/scenarios/healthy-load-no-action.yaml"
        )
        adapter = BlockingAdapter()
        task = asyncio.create_task(
            ScenarioExecutor(ScriptedGuardianClient(passing_snapshot())).execute(
                scenario,
                registration(adapter),
                ExecutionSettings(tmp_path, baseline_timeout=timedelta(seconds=10)),
            )
        )
        await adapter.baseline_started.wait()
        task.cancel()
        return await task, adapter

    result, adapter = asyncio.run(run())

    assert result.status is ExecutionStatus.CANCELLED
    assert adapter.calls[-2:] == ["cleanup", "cleanup"]
    summary = json.loads((result.artifact_directory / "summary.json").read_text())
    assert summary["status"] == "cancelled"
