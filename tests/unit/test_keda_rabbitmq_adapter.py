import asyncio
import json
from datetime import timedelta

from testbeds.adapters.command_runner import CommandResult
from testbeds.adapters.keda_rabbitmq import KedaRabbitMqAdapter
from testbeds.environments.keda_rabbitmq import KEDA_RABBITMQ_ENVIRONMENT
from testbeds.models import FaultSpecification, FaultType, LoadProfile, WorkloadSelector


class FakeRunner:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    async def run(self, argv, *, timeout, cwd=None, input_text=None):
        self.calls.append((tuple(argv), timeout, cwd, input_text))
        return self.responses.pop(0) if self.responses else CommandResult(tuple(argv), 0, "", "", 0.01)


def payload():
    deployment = {"metadata": {"name": "rabbitmq-consumer", "generation": 1}, "spec": {"replicas": 0, "template": {"spec": {"containers": [{"image": KEDA_RABBITMQ_ENVIRONMENT.consumer_image}]}}}, "status": {"availableReplicas": 0, "observedGeneration": 1}}
    rabbitmq = {"metadata": {"name": "rabbitmq", "generation": 1}, "spec": {"replicas": 1, "template": {"spec": {"containers": [{"image": KEDA_RABBITMQ_ENVIRONMENT.rabbitmq_image}]}}}, "status": {"availableReplicas": 1, "observedGeneration": 1}}
    return [CommandResult((), 0, json.dumps({"items": [deployment, rabbitmq]}), "", 0.01), CommandResult((), 0, json.dumps({"items": [{"metadata": {"name": "rabbitmq"}}]}), "", 0.01), CommandResult((), 0, json.dumps({"items": [{"metadata": {"name": "rabbitmq"}, "subsets": [{"addresses": [{"ip": "10.0.0.1"}]}]}]}), "", 0.01), CommandResult((), 0, json.dumps({"items": [{"metadata": {"name": "consumer"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}), "", 0.01), CommandResult((), 0, json.dumps({"items": [{"metadata": {"name": "rabbitmq-consumer"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}), "", 0.01)]


def test_release_is_immutable_and_adapter_isolated(tmp_path):
    assert len(KEDA_RABBITMQ_ENVIRONMENT.commit_sha) == 40
    int(KEDA_RABBITMQ_ENVIRONMENT.commit_sha, 16)
    assert "@sha256:" in KEDA_RABBITMQ_ENVIRONMENT.consumer_image
    assert KedaRabbitMqAdapter(workspace=tmp_path, run_id="R_1").namespace == "guardian-keda-rabbitmq-r-1"


def test_observation_baseline_load_fault_reset_and_cleanup_are_deterministic(tmp_path):
    runner = FakeRunner(payload() * 3)
    adapter = KedaRabbitMqAdapter(workspace=tmp_path, runner=runner, baseline_poll_seconds=0)
    state = asyncio.run(adapter.observe_state())
    assert state.healthy and {item.role for item in state.workloads} == {"consumer", "rabbitmq"}
    assert asyncio.run(adapter.wait_for_healthy_baseline(timedelta(seconds=1))).healthy
    assert asyncio.run(adapter.apply_load(LoadProfile(5))).active
    assert asyncio.run(adapter.inject_fault(FaultSpecification(FaultType.DEPENDENCY_UNAVAILABLE, WorkloadSelector("rabbitmq")))).active
    asyncio.run(adapter.reset())
    asyncio.run(adapter.cleanup())
    assert any(call[0][:3] == ("kubectl", "delete", "namespace") for call in runner.calls)
