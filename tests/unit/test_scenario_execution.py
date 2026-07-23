import asyncio
import copy
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from testbeds.environments.capabilities import ENVIRONMENT_DECLARATIONS
from testbeds.evidence.collector import EvidenceSample, UnavailableEvidence
from testbeds.evidence.contracts import EvidenceSourceKind
from testbeds.models import (
    BaselineState,
    EnvironmentCapabilities,
    EnvironmentRelease,
    EnvironmentState,
    LoadExecution,
    ObservedServiceIdentity,
    WorkloadState,
)
from testbeds.scenarios.assertions import evaluate_assertions
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
from tests.unit.test_guardian_scenario_v1alpha2 import document


DIGEST = "sha256:" + "a" * 64


@dataclass
class FakeEvidenceProvider:
    assessment: tuple[EvidenceSample | UnavailableEvidence, ...]
    recovery: tuple[EvidenceSample | UnavailableEvidence, ...]
    assessment_calls: int = 0
    recovery_calls: int = 0
    events: list[str] = field(default_factory=list)
    assessment_after_stimulus: bool = False

    async def collect_assessment_evidence(self, **kwargs):
        self.assessment_calls += 1
        self.events.append("assessment-evidence")
        self.assessment_after_stimulus = (
            "load:10" in kwargs["registration"].adapter.calls
        )
        return self.assessment

    async def collect_recovery_evidence(self, **kwargs):
        self.recovery_calls += 1
        self.events.append("recovery-evidence")
        return self.recovery


class TracingGuardianClient(ScriptedGuardianClient):
    def __init__(self, snapshot, events):
        super().__init__(snapshot)
        self.events = events

    async def submit_incident(self, submission, *, idempotency_key):
        self.events.append("incident-submit")
        return await super().submit_incident(
            submission, idempotency_key=idempotency_key
        )


def _telemetry_samples(
    observed_at: datetime | None = None,
) -> tuple[EvidenceSample, ...]:
    at = observed_at or datetime.now(UTC)
    return (
        EvidenceSample(
            EvidenceSourceKind.SIGNOZ_TELEMETRY,
            observed_at=at,
            provenance_ref="signoz/telemetry-quality",
            values={
                "quality": 1.0,
                "usable_samples": 10,
                "required_samples": 10,
                "pipeline_available": True,
                "comparison_valid": True,
            },
        ),
    )


class RecordingAdapter:
    capabilities = EnvironmentCapabilities(adjustable_load=True)

    def __init__(self, lifecycle_events: list[str] | None = None):
        self.calls = []
        self.lifecycle_events = lifecycle_events
        self.namespace = "guardian-test"

    async def install(self, release):
        self.calls.append("install")
        return EnvironmentState(
            "otel-demo",
            self.namespace,
            release,
            healthy=True,
            services=(
                ObservedServiceIdentity(
                    "transaction-processor", "transaction-processor", "1.2.3", DIGEST
                ),
            ),
            workloads=(
                WorkloadState("transaction-processor", "transaction-processor", 3, 3),
            ),
        )

    async def reset(self):
        self.calls.append("reset")
        if self.lifecycle_events is not None:
            self.lifecycle_events.append("reset")

    async def wait_for_healthy_baseline(self, timeout) -> BaselineState:
        self.calls.append("baseline")
        if self.lifecycle_events is not None:
            event = "post-reset-baseline" if "reset" in self.calls else "baseline"
            self.lifecycle_events.append(event)
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
        return EnvironmentState(
            healthy=True,
            services=(
                ObservedServiceIdentity(
                    "transaction-processor", "transaction-processor", "1.2.3", DIGEST
                ),
            ),
            workloads=(
                WorkloadState("transaction-processor", "transaction-processor", 3, 3),
            ),
        )

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


def registration(adapter, declaration=None, evidence_provider=None):
    return AdapterRegistration(
        environment="otel-demo",
        adapter=adapter,
        release=EnvironmentRelease(environment="otel-demo"),
        declaration=declaration or ENVIRONMENT_DECLARATIONS["otel-demo"],
        role_bindings={"request-processor": "transaction-processor"},
        fault_role_bindings={},
        deployment_bindings={},
        evidence_provider=evidence_provider
        or FakeEvidenceProvider(
            assessment=_telemetry_samples(),
            recovery=_telemetry_samples(),
        ),
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


def mutation_assertions(*, count, executed_mutations, exact=None, allowed_actions=None):
    value = copy.deepcopy(document())
    if exact is not None:
        value["spec"]["expected"]["mutations"]["count"] = {"exact": exact}
    if allowed_actions is not None:
        value["spec"]["expected"]["actions"]["eligible"] = allowed_actions
        value["spec"]["expected"]["mutations"]["actions"] = allowed_actions
    scenario = GuardianScenarioV1Alpha2.model_validate(value)
    snapshot = passing_snapshot().model_copy(
        update={"mutation_count": count, "executed_mutations": executed_mutations}
    )
    return {
        result.name: result
        for result in evaluate_assertions(scenario, snapshot)
        if result.name.startswith("mutations.")
    }


def test_at_most_one_mutation_accepts_zero_executed_actions():
    results = mutation_assertions(count=0, executed_mutations=())

    assert results["mutations.count"].passed
    assert results["mutations.actions"].passed


def test_at_most_one_mutation_rejects_an_unexpected_executed_action():
    results = mutation_assertions(
        count=1,
        executed_mutations=({"actionType": "rollback"},),
    )

    assert results["mutations.count"].passed
    assert not results["mutations.actions"].passed


def test_exact_positive_mutation_rejects_a_missing_execution():
    results = mutation_assertions(count=1, executed_mutations=(), exact=1)

    assert not results["mutations.count"].passed


def test_mutation_count_must_match_executed_mutations():
    results = mutation_assertions(
        count=0,
        executed_mutations=({"actionType": "scale", "scaleDirection": "up"},),
    )

    assert not results["mutations.count"].passed
    assert results["mutations.actions"].passed


def test_one_execution_may_match_one_of_multiple_allowed_actions():
    scale_up = {"actionType": "scale", "scaleDirection": "up"}
    scale_down = {"actionType": "scale", "scaleDirection": "down"}
    results = mutation_assertions(
        count=1,
        executed_mutations=(scale_up,),
        exact=1,
        allowed_actions=[scale_up, scale_down],
    )

    assert results["mutations.count"].passed
    assert results["mutations.actions"].passed


def test_guardian_snapshot_preserves_mutations_as_wire_alias():
    snapshot = GuardianSnapshot.model_validate(
        {
            **passing_snapshot().model_dump(mode="json", by_alias=True),
            "mutationCount": 1,
            "mutations": [{"actionType": "scale", "scaleDirection": "up"}],
        }
    )

    assert snapshot.executed_mutations == (
        {"actionType": "scale", "scaleDirection": "up"},
    )
    assert snapshot.model_dump(mode="json", by_alias=True)["mutations"] == [
        {"actionType": "scale", "scaleDirection": "up"}
    ]


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
        "observe",
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
        "assessment-evidence.json",
        "recovery-evidence.json",
    } <= {path.name for path in result.artifact_directory.glob("*.json")}


def test_executor_collects_assessment_evidence_after_stimulus_before_submission(
    tmp_path,
):
    scenario = load_guardian_scenario("testbeds/scenarios/healthy-load-no-action.yaml")
    adapter = RecordingAdapter()
    events: list[str] = []
    provider = FakeEvidenceProvider(
        assessment=_telemetry_samples(),
        recovery=_telemetry_samples(),
        events=events,
    )

    result = asyncio.run(
        ScenarioExecutor(TracingGuardianClient(passing_snapshot(), events)).execute(
            scenario,
            registration(adapter, evidence_provider=provider),
            ExecutionSettings(tmp_path, baseline_timeout=timedelta(seconds=1)),
        )
    )

    assert result.status is ExecutionStatus.PASSED
    assert provider.assessment_calls == 1
    assert provider.assessment_after_stimulus is True
    assert events.index("assessment-evidence") < events.index("incident-submit")


def test_executor_collects_recovery_evidence_after_reset(tmp_path):
    scenario = load_guardian_scenario("testbeds/scenarios/healthy-load-no-action.yaml")
    events: list[str] = []
    adapter = RecordingAdapter(lifecycle_events=events)
    provider = FakeEvidenceProvider(
        assessment=_telemetry_samples(),
        recovery=_telemetry_samples(),
        events=events,
    )

    result = asyncio.run(
        ScenarioExecutor(ScriptedGuardianClient(passing_snapshot())).execute(
            scenario,
            registration(adapter, evidence_provider=provider),
            ExecutionSettings(tmp_path, baseline_timeout=timedelta(seconds=1)),
        )
    )

    assert result.status is ExecutionStatus.PASSED
    assert provider.recovery_calls == 1
    assert events.index("reset") < events.index("post-reset-baseline")
    assert events.index("post-reset-baseline") < events.index("recovery-evidence")


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
    assert adapter.calls[-5:] == ["reset", "baseline", "observe", "cleanup", "cleanup"]


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


class FailingResetAdapter(RecordingAdapter):
    async def reset(self):
        self.calls.append("reset")
        raise RuntimeError("reset failed")


def test_reset_failure_invalidates_environment_and_raises(tmp_path):
    from testbeds.scenarios.execution import EnvironmentInvalidatedError

    scenario = load_guardian_scenario("testbeds/scenarios/healthy-load-no-action.yaml")
    adapter = FailingResetAdapter()

    with pytest.raises(EnvironmentInvalidatedError) as raised:
        asyncio.run(
            ScenarioExecutor(ScriptedGuardianClient(passing_snapshot())).execute(
                scenario,
                registration(adapter),
                ExecutionSettings(tmp_path, baseline_timeout=timedelta(seconds=1)),
            )
        )

    result = raised.value.result
    assert result.environment_invalidated is True
    assert result.reset_completed is False
    assert result.cleanup_completed is True
    assert result.status is ExecutionStatus.FAILED
    diagnostics = json.loads(
        (result.artifact_directory / "diagnostics.json").read_text()
    )
    assert diagnostics["environmentInvalidated"] is True


def test_executor_fails_closed_when_assessment_evidence_is_empty(tmp_path):
    """An empty provider result must not fabricate healthy telemetry samples."""

    scenario = load_guardian_scenario("testbeds/scenarios/healthy-load-no-action.yaml")
    adapter = RecordingAdapter()
    provider = FakeEvidenceProvider(assessment=(), recovery=_telemetry_samples())
    result = asyncio.run(
        ScenarioExecutor(ScriptedGuardianClient(passing_snapshot())).execute(
            scenario,
            registration(adapter, evidence_provider=provider),
            ExecutionSettings(tmp_path, baseline_timeout=timedelta(seconds=1)),
        )
    )

    assert result.status is ExecutionStatus.FAILED
    error = next(item for item in result.assertions if item.name == "execution.error")
    assert error.passed is False
    assert (
        "MissingEvidenceError" in str(error.actual)
        or "evidence" in str(error.actual).lower()
    )
    payloads = json.loads(
        (result.artifact_directory / "incident-payloads.json").read_text()
    )
    assert payloads == []


def test_executor_fails_closed_when_assessment_evidence_omits_required_signals(
    tmp_path,
):
    scenario = load_guardian_scenario(
        "testbeds/scenarios/legitimate-demand-scale-up.yaml"
    )
    adapter = RecordingAdapter()
    provider = FakeEvidenceProvider(
        assessment=_telemetry_samples(),
        recovery=_telemetry_samples(),
    )

    result = asyncio.run(
        ScenarioExecutor(ScriptedGuardianClient(passing_snapshot())).execute(
            scenario,
            registration(adapter, evidence_provider=provider),
            ExecutionSettings(tmp_path, baseline_timeout=timedelta(seconds=1)),
        )
    )

    assert result.status is ExecutionStatus.FAILED
    assert provider.assessment_calls == 1
    error = next(item for item in result.assertions if item.name == "execution.error")
    assert "MissingEvidenceError" in str(error.actual)
    payloads = json.loads(
        (result.artifact_directory / "incident-payloads.json").read_text()
    )
    assert payloads == []


def test_adapter_registration_requires_evidence_provider():
    with pytest.raises(TypeError, match="evidence_provider"):
        AdapterRegistration(
            environment="otel-demo",
            adapter=RecordingAdapter(),
            release=EnvironmentRelease(environment="otel-demo"),
            declaration=ENVIRONMENT_DECLARATIONS["otel-demo"],
            role_bindings={"request-processor": "transaction-processor"},
            fault_role_bindings={},
            deployment_bindings={},
        )  # type: ignore[call-arg]


def test_required_signals_include_scenario_supporting_evidence_groups() -> None:
    from testbeds.scenarios.execution import _required_signals

    scenario = load_guardian_scenario(
        "testbeds/scenarios/legitimate-demand-scale-up.yaml"
    )
    assert isinstance(scenario, GuardianScenarioV1Alpha2)
    required = _required_signals(scenario)
    assert "telemetry_quality" in required
    assert "request_rate" in required
    assert "cpu_utilization" in required or "memory_utilization" in required
    assert "dependency_healthy" in required
