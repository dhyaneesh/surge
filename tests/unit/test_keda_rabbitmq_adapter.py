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
        return (
            self.responses.pop(0)
            if self.responses
            else CommandResult(tuple(argv), 0, "", "", 0.01)
        )


def payload():
    deployment = {
        "metadata": {"name": "rabbitmq-consumer", "generation": 1},
        "spec": {
            "replicas": 0,
            "template": {
                "spec": {
                    "containers": [{"image": KEDA_RABBITMQ_ENVIRONMENT.consumer_image}]
                }
            },
        },
        "status": {"availableReplicas": 0, "observedGeneration": 1},
    }
    rabbitmq = {
        "metadata": {"name": "rabbitmq", "generation": 1},
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [{"image": KEDA_RABBITMQ_ENVIRONMENT.rabbitmq_image}]
                }
            },
        },
        "status": {"readyReplicas": 1, "observedGeneration": 1},
    }
    scaled_object = {
        "metadata": {"name": "rabbitmq-consumer"},
        "spec": {"minReplicaCount": 0, "maxReplicaCount": 10},
        "status": {
            "hpaName": "keda-hpa-rabbitmq-consumer",
            "conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "Active", "status": "False"},
            ],
            "health": {"rabbitMQScaler": {"status": "Happy", "numberOfFailures": 0}},
        },
    }
    return [
        CommandResult((), 0, json.dumps({"items": [deployment]}), "", 0.01),
        CommandResult((), 0, json.dumps({"items": [rabbitmq]}), "", 0.01),
        CommandResult(
            (), 0, json.dumps({"items": [{"metadata": {"name": "rabbitmq"}}]}), "", 0.01
        ),
        CommandResult(
            (),
            0,
            json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "rabbitmq"},
                            "subsets": [{"addresses": [{"ip": "10.0.0.1"}]}],
                        }
                    ]
                }
            ),
            "",
            0.01,
        ),
        CommandResult((), 0, json.dumps({"items": []}), "", 0.01),
        CommandResult((), 0, json.dumps({"items": [scaled_object]}), "", 0.01),
        CommandResult(
            (),
            0,
            json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": "keda-hpa-rabbitmq-consumer"},
                            "status": {
                                "currentReplicas": 0,
                                "desiredReplicas": 0,
                                "currentMetrics": [
                                    {"external": {"current": {"averageValue": "0"}}}
                                ],
                            },
                        }
                    ]
                }
            ),
            "",
            0.01,
        ),
    ]


def test_release_is_immutable_and_adapter_isolated(tmp_path):
    assert len(KEDA_RABBITMQ_ENVIRONMENT.commit_sha) == 40
    int(KEDA_RABBITMQ_ENVIRONMENT.commit_sha, 16)
    assert "@sha256:" in KEDA_RABBITMQ_ENVIRONMENT.consumer_image
    assert (
        KedaRabbitMqAdapter(workspace=tmp_path, run_id="R_1").namespace
        == "guardian-keda-rabbitmq-r-1"
    )


def test_observation_baseline_load_fault_reset_and_cleanup_are_deterministic(tmp_path):
    runner = FakeRunner(payload() * 3)
    adapter = KedaRabbitMqAdapter(
        workspace=tmp_path, runner=runner, baseline_poll_seconds=0
    )
    state = asyncio.run(adapter.observe_state())
    assert state.healthy and {item.role for item in state.workloads} == {
        "consumer",
        "rabbitmq",
    }
    assert (
        next(item for item in state.workloads if item.role == "rabbitmq").ready_replicas
        == 1
    )
    assert state.scaling is not None
    assert state.scaling.scaled_object_ready
    assert state.scaling.current_replicas == 0
    assert state.scaling.desired_replicas == 0
    assert state.scaling.queue_depth == 0
    assert state.scaling.scale_to_zero
    assert not state.scaling.scaler_active
    assert not state.scaling.scaler_error
    assert asyncio.run(adapter.wait_for_healthy_baseline(timedelta(seconds=1))).healthy
    assert asyncio.run(adapter.apply_load(LoadProfile(5))).active
    assert asyncio.run(
        adapter.inject_fault(
            FaultSpecification(
                FaultType.DEPENDENCY_UNAVAILABLE, WorkloadSelector("rabbitmq")
            )
        )
    ).active
    asyncio.run(adapter.reset())
    asyncio.run(adapter.cleanup())
    asyncio.run(adapter.cleanup())
    assert any(
        call[0][:3] == ("kubectl", "delete", "namespace") for call in runner.calls
    )
    assert (
        sum(call[0][:3] == ("kubectl", "delete", "namespace") for call in runner.calls)
        == 1
    )


def test_install_pins_rabbitmq_digest_and_creates_scaled_object(tmp_path):
    runner = FakeRunner(
        [
            CommandResult((), 0, "", "", 0.01),
            CommandResult((), 0, KEDA_RABBITMQ_ENVIRONMENT.commit_sha, "", 0.01),
            CommandResult((), 0, "", "", 0.01),
            CommandResult((), 0, "", "", 0.01),
            CommandResult((), 0, "", "", 0.01),
            CommandResult((), 0, "", "", 0.01),
            CommandResult((), 0, "", "", 0.01),
            *payload(),
        ]
    )
    source = tmp_path / "source" / ".git"
    source.mkdir(parents=True)
    adapter = KedaRabbitMqAdapter(workspace=tmp_path, runner=runner)

    asyncio.run(adapter.install(KEDA_RABBITMQ_ENVIRONMENT.release()))

    helm = next(call for call in runner.calls if call[0][:2] == ("helm", "upgrade"))
    assert (
        "image.digest=sha256:3e652677d5e50ec76065fe352ae9ee8549a88e4cf0db6c8cf3b4970e4d6e6a11"
        in helm[0]
    )
    manifests = [
        json.loads(call[3])
        for call in runner.calls
        if call[0][:2] == ("kubectl", "apply") and call[3]
    ]
    scaled_object = next(
        manifest for manifest in manifests if manifest.get("kind") == "ScaledObject"
    )
    assert scaled_object["spec"]["minReplicaCount"] == 0
    assert scaled_object["spec"]["maxReplicaCount"] == 10
    assert scaled_object["spec"]["cooldownPeriod"] == 30
    assert scaled_object["spec"]["triggers"][0]["metadata"]["queueName"] == "hello"


def test_apply_load_uses_requested_message_count(tmp_path):
    runner = FakeRunner()
    adapter = KedaRabbitMqAdapter(workspace=tmp_path, runner=runner)

    asyncio.run(adapter.apply_load(LoadProfile(25)))

    apply_call = next(
        call for call in runner.calls if call[0][:2] == ("kubectl", "apply")
    )
    manifest = json.loads(apply_call[3])
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    assert container["args"][-1] == "25"
