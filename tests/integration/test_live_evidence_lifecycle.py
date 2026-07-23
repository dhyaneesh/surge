"""Prove assessment and recovery evidence are independently collected."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import Any, Iterator, Sequence

import pytest

from apps.guardian_api.http import create_server
from apps.guardian_api.service import GuardianService
from testbeds.adapters.otel_demo import OpenTelemetryDemoAdapter
from testbeds.adapters.command_runner import CommandResult
from testbeds.evidence.collector import EvidenceCollector, ProbeResult
from testbeds.evidence.signoz import SignozQueryResult
from testbeds.environments.capabilities import ENVIRONMENT_DECLARATIONS
from testbeds.environments.otel_demo import OTEL_DEMO_ENVIRONMENT
from testbeds.models import (
    BaselineState,
    EnvironmentRelease,
    EnvironmentState,
    FaultExecution,
    FaultSpecification,
    LoadExecution,
    LoadProfile,
    ObservedServiceIdentity,
    WorkloadState,
)
from testbeds.scenarios.evidence_provider import (
    CollectorEvidenceTargets,
    CollectorScenarioEvidenceProvider,
)
from testbeds.scenarios.execution import (
    AdapterRegistration,
    ExecutionSettings,
    ExecutionStatus,
    ScenarioExecutor,
)
from testbeds.scenarios.guardian_client import HttpGuardianClient
from testbeds.scenarios.loader import load_guardian_scenario
from testbeds.scenarios.v1alpha2 import EnvironmentCapability, GuardianScenarioV1Alpha2

TOKEN = "guardian_local_token_A_0123456789"
DIGEST = "sha256:" + "a" * 64


def _state() -> EnvironmentState:
    return EnvironmentState(
        environment="otel-demo",
        namespace="guardian-live-evidence",
        release=OTEL_DEMO_ENVIRONMENT.release(),
        services=(
            ObservedServiceIdentity("request-processor", "checkout", "1.2.3", DIGEST),
            ObservedServiceIdentity("dependency", "redis", "1.0.0", DIGEST),
        ),
        workloads=(
            WorkloadState("request-processor", "checkout", 3, 3),
            WorkloadState("dependency", "redis", 1, 1),
        ),
        healthy=True,
    )


class ControlledOtelDemoAdapter(OpenTelemetryDemoAdapter):
    """Loopback-safe adapter implementing the scenario lifecycle."""

    def __init__(self, *, workspace: Path) -> None:
        self._workspace = workspace
        self.namespace = "guardian-live-evidence"
        self._release = OTEL_DEMO_ENVIRONMENT.release()
        self._active_load = False
        self._active_faults: set = set()
        self._created_resources: set = set()
        self._changed: list = []
        self._diagnostics: list = []
        self._command_history: list = []
        self._installed = False
        self._cleaned = False
        self.contaminated = False

    async def install(self, release: EnvironmentRelease) -> EnvironmentState:
        self._installed = True
        return _state()

    async def reset(self) -> None:
        self._active_load = False

    async def wait_for_healthy_baseline(self, timeout: timedelta) -> BaselineState:
        return BaselineState(True, environment=_state())

    async def apply_load(self, profile: LoadProfile) -> LoadExecution:
        self._active_load = True
        return LoadExecution(profile, active=True)

    async def inject_fault(self, fault: FaultSpecification) -> FaultExecution:
        self._active_faults.add(fault.fault_type)
        return FaultExecution(fault, active=True)

    async def observe_state(self) -> EnvironmentState:
        return _state()

    async def cleanup(self) -> None:
        self._cleaned = True


@dataclass
class ControlledCommandRunner:
    responses: list[str]
    calls: list[tuple[str, ...]]

    def __init__(self, responses: Sequence[str]) -> None:
        self.responses = list(responses)
        self.calls = []

    async def run(self, argv, *, timeout, cwd=None, input_text=None) -> CommandResult:
        command = tuple(argv)
        self.calls.append(command)
        return CommandResult(command, 0, self.responses.pop(0), "", 0.01)


@dataclass
class ControlledHttpRunner:
    probe_calls: int = 0

    async def probe(self, url, *, timeout, headers=None) -> ProbeResult:
        self.probe_calls += 1
        return ProbeResult(status_code=200, latency_ms=10.0)

    async def post_json(self, url, payload, *, timeout, headers=None) -> ProbeResult:
        return ProbeResult(status_code=200, latency_ms=10.0)


@dataclass
class ControlledSignozContract:
    calls: int = 0

    async def query_telemetry_arrival(self, *, identity, lookback) -> SignozQueryResult:
        self.calls += 1
        return SignozQueryResult(
            matched=True,
            observed_at=datetime.now(UTC),
            provenance_ref="signoz/approved-telemetry-arrival",
            values={
                "quality": 1.0,
                "usable_samples": 10,
                "required_samples": 10,
                "pipeline_available": True,
                "comparison_valid": True,
            },
        )


@pytest.fixture()
def server() -> Iterator[Any]:
    srv = create_server(
        token_tenants={TOKEN: "tenant-a"},
        service=GuardianService(),
        port=0,
    )
    thread = Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.drain_and_close()
        thread.join(timeout=2)


def test_lifecycle_persists_distinct_assessment_and_recovery_evidence(
    server, tmp_path: Path
) -> None:
    scenario_payload = load_guardian_scenario(
        "testbeds/scenarios/resource-saturation.yaml"
    ).model_dump(mode="python", by_alias=True)
    expected = scenario_payload["spec"]["expected"]
    expected["incident"].update({"incidentClass": None, "actionable": False})
    expected["actions"] = {"eligible": [], "forbidden": [], "proposed": None}
    expected["mutations"]["count"] = {"exact": 0}
    expected["mutations"]["actions"] = []
    expected["policy"]["decision"] = "denied"
    scenario = GuardianScenarioV1Alpha2.model_validate(scenario_payload)
    workload = json.dumps(
        {
            "spec": {
                "replicas": 3,
                "template": {"spec": {"containers": [{"image": "checkout:1.2.3"}]}},
            },
            "status": {"readyReplicas": 3},
        }
    )
    metrics = json.dumps({"containers": [{"usage": {"cpu": "90m", "memory": "120Mi"}}]})
    command_runner = ControlledCommandRunner((workload, metrics, workload, metrics))
    http_runner = ControlledHttpRunner()
    signoz_contract = ControlledSignozContract()
    provider = CollectorScenarioEvidenceProvider(
        collector=EvidenceCollector(
            command_runner=command_runner,
            http_runner=http_runner,
            clock=lambda: datetime.now(UTC),
        ),
        targets=CollectorEvidenceTargets(
            namespace="guardian-live-evidence",
            endpoint_url="http://checkout.guardian-live-evidence.svc.cluster.local",
            workload_kind="deployment",
            workload_name="checkout",
            workload_role="request-processor",
            service_name="checkout",
            metrics_pod_name="checkout-0",
            cpu_limit_millicores=100,
            memory_limit_bytes=128 * 1024 * 1024,
            signoz_contract=signoz_contract,
        ),
        monotonic_clock=iter((10.0, 10.01, 20.0, 20.01)).__next__,
    )
    registration = AdapterRegistration(
        environment="otel-demo",
        adapter=ControlledOtelDemoAdapter(workspace=tmp_path / "otel"),
        release=OTEL_DEMO_ENVIRONMENT.release(),
        declaration=ENVIRONMENT_DECLARATIONS["otel-demo"].model_copy(
            update={
                "capabilities": ENVIRONMENT_DECLARATIONS["otel-demo"].capabilities
                | {EnvironmentCapability.RESOURCE_PRESSURE}
            }
        ),
        role_bindings={"request-processor": "checkout"},
        fault_role_bindings={},
        deployment_bindings={},
        evidence_provider=provider,
    )
    client = HttpGuardianClient(
        f"http://{server.listening_address[0]}:{server.listening_address[1]}",
        token=TOKEN,
    )

    result = asyncio.run(
        ScenarioExecutor(client).execute(
            scenario,
            registration,
            ExecutionSettings(
                tmp_path / "artifacts",
                baseline_timeout=timedelta(seconds=1),
                operation_timeout=timedelta(seconds=5),
            ),
        )
    )

    artifact = result.artifact_directory
    assessment_path = artifact / "assessment-evidence.json"
    recovery_path = artifact / "recovery-evidence.json"
    assert result.status is ExecutionStatus.PASSED
    assert assessment_path.exists()
    assert recovery_path.exists()

    assessment = json.loads(assessment_path.read_text())
    recovery = json.loads(recovery_path.read_text())
    assert assessment[0]["observed_at"] != recovery[0]["observed_at"]
    assert assessment[0]["provenance_ref"] != recovery[0]["provenance_ref"]
    assert len(command_runner.calls) == 4
    assert http_runner.probe_calls == 6
    assert signoz_contract.calls == 2
