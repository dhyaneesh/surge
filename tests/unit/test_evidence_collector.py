"""Independent testbed evidence collector contracts and sampling."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import pytest

from testbeds.adapters.command_runner import CommandResult
from testbeds.evidence.collector import (
    EvidenceCollector,
    EvidenceSample,
    ProbeResult,
    UnavailableEvidence,
    control_result_is_not_symptom_evidence,
)
from testbeds.evidence.contracts import (
    EVIDENCE_TYPE_SOURCES,
    EvidenceSourceKind,
    required_evidence_sources_for_scenario,
    substantiate_capabilities,
)
from testbeds.evidence.signoz import SignozEvidenceClient
from testbeds.environments.capabilities import ENVIRONMENT_DECLARATIONS
from testbeds.models import (
    EnvironmentState,
    FaultExecution,
    FaultSpecification,
    FaultType,
    LoadExecution,
    LoadProfile,
    WorkloadSelector,
)
from testbeds.scenarios.compatibility import (
    BlockingReason,
    CompatibilityStatus,
    EnvironmentDeclaration,
    derive_compatibility,
)
from testbeds.scenarios.loader import load_guardian_scenario
from testbeds.scenarios.models import EvidenceType
from testbeds.scenarios.v1alpha2 import EnvironmentCapability, GuardianScenarioV1Alpha2
from tests.unit.test_guardian_scenario_v1alpha2 import document


NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


@dataclass
class FakeCommandRunner:
    responses: list[Any]
    calls: list[tuple[tuple[str, ...], timedelta]]

    def __init__(self, responses: Sequence[Any] = ()):
        self.responses = list(responses)
        self.calls = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: timedelta,
        cwd=None,
        input_text=None,
    ) -> CommandResult:
        command = tuple(argv)
        self.calls.append((command, timeout))
        if not self.responses:
            return CommandResult(command, 0, "", "", 0.01)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, CommandResult):
            return response
        return CommandResult(command, 0, str(response), "", 0.01)


@dataclass
class FakeHttpRunner:
    responses: list[Any]
    calls: list[tuple[str, timedelta, Mapping[str, str] | None]]
    posts: list[tuple[str, Mapping[str, Any], timedelta]]

    def __init__(self, responses: Sequence[Any] = ()):
        self.responses = list(responses)
        self.calls = []
        self.posts = []

    async def probe(
        self,
        url: str,
        *,
        timeout: timedelta,
        headers: Mapping[str, str] | None = None,
    ) -> ProbeResult:
        self.calls.append((url, timeout, headers))
        if not self.responses:
            return ProbeResult(status_code=200, latency_ms=12.5, body="")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: timedelta,
        headers: Mapping[str, str] | None = None,
    ) -> ProbeResult:
        self.posts.append((url, dict(payload), timeout))
        if not self.responses:
            return ProbeResult(status_code=200, latency_ms=3.0, body="{}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _identity(**overrides: Any) -> dict[str, Any]:
    base = {
        "tenant_id": "tenant-a",
        "environment": "otel-demo",
        "namespace": "otel-demo",
        "workload_kind": "Deployment",
        "workload_name": "frontend",
        "service_name": "frontend",
        "service_version": "1.2.3",
        "image_digest": "sha256:" + ("a" * 64),
    }
    base.update(overrides)
    return base


def test_evidence_type_contracts_map_to_deterministic_sources() -> None:
    assert EvidenceType.LOAD in EVIDENCE_TYPE_SOURCES
    assert EvidenceType.METRICS in EVIDENCE_TYPE_SOURCES
    assert EvidenceType.RESOURCE_UTILIZATION in EVIDENCE_TYPE_SOURCES
    assert EvidenceType.POLICY_DECISION in EVIDENCE_TYPE_SOURCES
    assert EvidenceType.ACTION_RESULT in EVIDENCE_TYPE_SOURCES
    assert EvidenceSourceKind.ENDPOINT_PROBE in EVIDENCE_TYPE_SOURCES[EvidenceType.LOAD]
    assert (
        EvidenceSourceKind.METRICS_API
        in EVIDENCE_TYPE_SOURCES[EvidenceType.RESOURCE_UTILIZATION]
    )
    assert (
        EvidenceSourceKind.SIGNOZ_TELEMETRY
        in EVIDENCE_TYPE_SOURCES[EvidenceType.TELEMETRY_QUALITY]
    )
    assert (
        EvidenceSourceKind.CONTROL_FIXTURE
        in EVIDENCE_TYPE_SOURCES[EvidenceType.POLICY_DECISION]
    )
    assert (
        EvidenceSourceKind.CONTROL_FIXTURE
        in EVIDENCE_TYPE_SOURCES[EvidenceType.ACTION_RESULT]
    )


def test_control_execution_alone_is_never_symptom_evidence() -> None:
    load = LoadExecution(profile=LoadProfile(concurrent_users=20), active=True)
    fault = FaultExecution(
        fault=FaultSpecification(
            fault_type=FaultType.HIGH_CPU,
            target=WorkloadSelector(role="request-processor"),
            magnitude=1.0,
        ),
        active=True,
    )
    healthy_state = EnvironmentState(environment="otel-demo", healthy=True)
    assert control_result_is_not_symptom_evidence(load) is True
    assert control_result_is_not_symptom_evidence(fault) is True
    assert control_result_is_not_symptom_evidence(healthy_state) is True
    collector = EvidenceCollector(
        command_runner=FakeCommandRunner(),
        http_runner=FakeHttpRunner(),
        clock=lambda: NOW,
    )
    assert collector.symptom_evidence_from_control(load) == ()
    assert collector.symptom_evidence_from_control(fault) == ()
    assert collector.symptom_evidence_from_control(healthy_state) == ()


def test_endpoint_probe_attaches_latency_status_timestamp_and_provenance() -> None:
    http = FakeHttpRunner(
        [
            ProbeResult(status_code=200, latency_ms=18.0, body="ok"),
            ProbeResult(status_code=500, latency_ms=40.0, body="err"),
            ProbeResult(status_code=200, latency_ms=22.0, body="ok"),
        ]
    )
    collector = EvidenceCollector(
        command_runner=FakeCommandRunner(),
        http_runner=http,
        clock=lambda: NOW,
    )
    sample = asyncio.run(
        collector.sample_endpoint(
            "http://frontend:8080/health",
            identity=_identity(),
            sample_count=3,
        )
    )
    assert isinstance(sample, EvidenceSample)
    assert sample.source_kind is EvidenceSourceKind.ENDPOINT_PROBE
    assert sample.observed_at == NOW
    assert "endpoint-probe" in sample.provenance_ref
    assert "http://frontend:8080/health" in sample.provenance_ref
    assert sample.values["status_codes"] == (200, 500, 200)
    assert sample.values["latency_ms"] == (18.0, 40.0, 22.0)
    assert sample.values["error_rate"] == pytest.approx(1 / 3)
    assert sample.values["p95_latency_ms"] >= 22.0


def test_kubernetes_workload_identity_and_replicas() -> None:
    payload = {
        "metadata": {"name": "frontend", "namespace": "otel-demo"},
        "spec": {
            "replicas": 3,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "frontend",
                            "image": "ghcr.io/demo/frontend:1.2.3@sha256:" + ("a" * 64),
                        }
                    ]
                }
            },
        },
        "status": {"readyReplicas": 2, "replicas": 3},
    }
    runner = FakeCommandRunner(
        [CommandResult(("kubectl",), 0, json.dumps(payload), "", 0.02)]
    )
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    sample = asyncio.run(
        collector.sample_kubernetes_workload(
            namespace="otel-demo",
            workload_kind="Deployment",
            workload_name="frontend",
            identity=_identity(),
        )
    )
    assert isinstance(sample, EvidenceSample)
    assert sample.source_kind is EvidenceSourceKind.KUBERNETES_WORKLOAD
    assert sample.values["desired_replicas"] == 3
    assert sample.values["ready_replicas"] == 2
    assert sample.values["service_version"] == "1.2.3"
    assert sample.values["image_digest"] == "sha256:" + ("a" * 64)
    assert runner.calls[0][0][:4] == ("kubectl", "get", "deployment", "frontend")
    assert "-o" in runner.calls[0][0]
    assert "json" in runner.calls[0][0]


def test_metrics_api_cpu_and_memory_samples() -> None:
    payload = {
        "metadata": {"name": "frontend-abc", "namespace": "otel-demo"},
        "containers": [
            {
                "name": "frontend",
                "usage": {"cpu": "250m", "memory": "256Mi"},
            }
        ],
    }
    runner = FakeCommandRunner(
        [CommandResult(("kubectl",), 0, json.dumps(payload), "", 0.02)]
    )
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    sample = asyncio.run(
        collector.sample_metrics_api(
            namespace="otel-demo",
            pod_name="frontend-abc",
            identity=_identity(),
            cpu_limit_millicores=1000,
            memory_limit_bytes=512 * 1024 * 1024,
        )
    )
    assert isinstance(sample, EvidenceSample)
    assert sample.source_kind is EvidenceSourceKind.METRICS_API
    assert sample.values["cpu_utilization"] == pytest.approx(0.25)
    assert sample.values["memory_utilization"] == pytest.approx(0.5)
    argv = runner.calls[0][0]
    assert argv[:3] == ("kubectl", "get", "--raw")
    assert "/apis/metrics.k8s.io/" in argv[3]


def test_rollout_state_sampling() -> None:
    payload = {
        "metadata": {"name": "rollouts-demo"},
        "status": {
            "phase": "Healthy",
            "replicas": 2,
            "readyReplicas": 2,
            "updatedReplicas": 2,
            "unavailableReplicas": 0,
            "stableRS": "abc",
            "currentPodHash": "def",
        },
    }
    runner = FakeCommandRunner(
        [CommandResult(("kubectl",), 0, json.dumps(payload), "", 0.02)]
    )
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    sample = asyncio.run(
        collector.sample_rollout(
            namespace="argo-rollouts",
            rollout_name="rollouts-demo",
            identity=_identity(environment="argo-rollouts", namespace="argo-rollouts"),
        )
    )
    assert isinstance(sample, EvidenceSample)
    assert sample.source_kind is EvidenceSourceKind.ROLLOUT_STATE
    assert sample.values["phase"] == "Healthy"
    assert sample.values["ready_replicas"] == 2
    assert "rollout" in " ".join(runner.calls[0][0])


def test_rabbitmq_queue_depth_and_keda_scaler_health() -> None:
    scaled = {
        "metadata": {"name": "rabbitmq-consumer"},
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True"},
                {"type": "Active", "status": "True"},
            ],
            "health": {"rabbitmq-consumer": {"status": "Happy", "numberOfFailures": 0}},
            "hpaName": "keda-hpa-rabbitmq-consumer",
        },
    }
    hpa = {
        "metadata": {"name": "keda-hpa-rabbitmq-consumer"},
        "status": {
            "currentReplicas": 2,
            "desiredReplicas": 3,
            "currentMetrics": [
                {
                    "type": "External",
                    "external": {
                        "current": {"averageValue": "17"},
                        "metric": {"name": "s0-rabbitmq-test"},
                    },
                }
            ],
        },
    }
    runner = FakeCommandRunner(
        [
            CommandResult(("kubectl",), 0, json.dumps(scaled), "", 0.02),
            CommandResult(("kubectl",), 0, json.dumps(hpa), "", 0.02),
            CommandResult(("kubectl",), 0, json.dumps(scaled), "", 0.02),
        ]
    )
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    queue = asyncio.run(
        collector.sample_rabbitmq_queue_depth(
            namespace="keda-rabbitmq",
            scaled_object_name="rabbitmq-consumer",
            identity=_identity(environment="keda-rabbitmq", namespace="keda-rabbitmq"),
        )
    )
    scaler = asyncio.run(
        collector.sample_keda_scaler(
            namespace="keda-rabbitmq",
            scaled_object_name="rabbitmq-consumer",
            identity=_identity(environment="keda-rabbitmq", namespace="keda-rabbitmq"),
        )
    )
    assert isinstance(queue, EvidenceSample)
    assert queue.source_kind is EvidenceSourceKind.RABBITMQ_QUEUE
    assert queue.values["queue_depth"] == 17
    assert isinstance(scaler, EvidenceSample)
    assert scaler.source_kind is EvidenceSourceKind.KEDA_SCALER
    assert scaler.values["scaler_error"] is False
    assert scaler.values["ready"] is True


def test_signoz_queries_require_matching_identity_attributes() -> None:
    http = FakeHttpRunner(
        [
            ProbeResult(status_code=200, latency_ms=5.0, body="ok"),
            ProbeResult(status_code=200, latency_ms=8.0, body='{"status":"ok"}'),
            ProbeResult(
                status_code=200,
                latency_ms=9.0,
                body=json.dumps(
                    {
                        "data": [
                            {
                                "tenant.id": "tenant-a",
                                "deployment.environment": "otel-demo",
                                "service.name": "frontend",
                                "k8s.deployment.name": "frontend",
                                "service.version": "1.2.3",
                            }
                        ]
                    }
                ),
            ),
        ]
    )
    client = SignozEvidenceClient(
        otlp_endpoint="http://signoz-otel:4318",
        query_endpoint="http://signoz:8080",
        http_runner=http,
        clock=lambda: NOW,
    )
    export = asyncio.run(
        client.export_blackbox_probe(
            probe_url="http://frontend:8080/health",
            identity=_identity(),
        )
    )
    assert export["latency_ms"] > 0
    assert export["available"] is True
    assert len(http.posts) == 1
    assert http.posts[0][0].endswith("/v1/metrics")
    assert "resourceMetrics" in http.posts[0][1]
    assert not any(
        headers and "X-Guardian-OTLP-Payload" in headers for _, _, headers in http.calls
    )
    arrival = asyncio.run(
        client.query_telemetry_arrival(
            identity=_identity(), lookback=timedelta(minutes=5)
        )
    )
    assert arrival.matched is True
    assert arrival.values["service.name"] == "frontend"
    assert arrival.values["service.version"] == "1.2.3"


def test_signoz_identity_version_mismatch_fails_closed() -> None:
    http = FakeHttpRunner(
        [
            ProbeResult(
                status_code=200,
                latency_ms=9.0,
                body=json.dumps(
                    {
                        "data": [
                            {
                                "tenant.id": "tenant-a",
                                "deployment.environment": "otel-demo",
                                "service.name": "frontend",
                                "k8s.deployment.name": "frontend",
                                "service.version": "9.9.9",
                            }
                        ]
                    }
                ),
            )
        ]
    )
    client = SignozEvidenceClient(
        otlp_endpoint="http://signoz-otel:4318",
        query_endpoint="http://signoz:8080",
        http_runner=http,
        clock=lambda: NOW,
    )
    arrival = asyncio.run(
        client.query_service_version(
            identity=_identity(), lookback=timedelta(minutes=5)
        )
    )
    assert arrival.matched is False
    assert arrival.values == {}
    assert "did not match" in arrival.diagnostics


def test_empty_kubernetes_payload_is_unavailable_not_zero_sample() -> None:
    runner = FakeCommandRunner([CommandResult(("kubectl",), 0, "", "", 0.02)])
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    sample = asyncio.run(
        collector.sample_kubernetes_workload(
            namespace="otel-demo",
            workload_kind="Deployment",
            workload_name="frontend",
            identity=_identity(),
        )
    )
    assert isinstance(sample, UnavailableEvidence)
    assert sample.source_kind is EvidenceSourceKind.KUBERNETES_WORKLOAD
    assert "desired_replicas" not in getattr(sample, "values", {})


def test_partial_kubernetes_payload_without_replicas_is_unavailable() -> None:
    payload = {
        "metadata": {"name": "frontend"},
        "spec": {
            "template": {"spec": {"containers": [{"name": "frontend", "image": "x:1"}]}}
        },
        "status": {},
    }
    runner = FakeCommandRunner(
        [CommandResult(("kubectl",), 0, json.dumps(payload), "", 0.02)]
    )
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    sample = asyncio.run(
        collector.sample_kubernetes_workload(
            namespace="otel-demo",
            workload_kind="Deployment",
            workload_name="frontend",
            identity=_identity(),
        )
    )
    assert isinstance(sample, UnavailableEvidence)


def test_omitted_ready_replicas_means_zero_when_status_present() -> None:
    payload = {
        "metadata": {"name": "frontend"},
        "spec": {
            "replicas": 3,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "frontend",
                            "image": "ghcr.io/demo/frontend:1.2.3@sha256:" + ("a" * 64),
                        }
                    ]
                }
            },
        },
        # Kubernetes omitempty drops readyReplicas when zero after a crash.
        "status": {"replicas": 3},
    }
    runner = FakeCommandRunner(
        [CommandResult(("kubectl",), 0, json.dumps(payload), "", 0.02)]
    )
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    sample = asyncio.run(
        collector.sample_kubernetes_workload(
            namespace="otel-demo",
            workload_kind="Deployment",
            workload_name="frontend",
            identity=_identity(),
        )
    )
    assert isinstance(sample, EvidenceSample)
    assert sample.values["desired_replicas"] == 3
    assert sample.values["ready_replicas"] == 0


def test_omitted_unavailable_replicas_means_zero_on_healthy_rollout() -> None:
    payload = {
        "metadata": {"name": "rollouts-demo"},
        "status": {
            "phase": "Healthy",
            "replicas": 2,
            "readyReplicas": 2,
            "updatedReplicas": 2,
            # unavailableReplicas omitted via omitempty when zero
        },
    }
    runner = FakeCommandRunner(
        [CommandResult(("kubectl",), 0, json.dumps(payload), "", 0.02)]
    )
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    sample = asyncio.run(
        collector.sample_rollout(
            namespace="argo-rollouts",
            rollout_name="rollouts-demo",
            identity=_identity(environment="argo-rollouts", namespace="argo-rollouts"),
        )
    )
    assert isinstance(sample, EvidenceSample)
    assert sample.values["unavailable_replicas"] == 0
    assert sample.values["ready_replicas"] == 2


def test_non_object_kubernetes_payload_is_unavailable() -> None:
    runner = FakeCommandRunner(
        [CommandResult(("kubectl",), 0, json.dumps([{"kind": "List"}]), "", 0.02)]
    )
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    sample = asyncio.run(
        collector.sample_kubernetes_workload(
            namespace="otel-demo",
            workload_kind="Deployment",
            workload_name="frontend",
            identity=_identity(),
        )
    )
    assert isinstance(sample, UnavailableEvidence)


def test_empty_rollout_payload_is_unavailable_not_zero_sample() -> None:
    runner = FakeCommandRunner([CommandResult(("kubectl",), 0, "{}", "", 0.02)])
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    sample = asyncio.run(
        collector.sample_rollout(
            namespace="argo-rollouts",
            rollout_name="rollouts-demo",
            identity=_identity(environment="argo-rollouts", namespace="argo-rollouts"),
        )
    )
    assert isinstance(sample, UnavailableEvidence)
    assert sample.source_kind is EvidenceSourceKind.ROLLOUT_STATE


def test_signoz_otlp_export_fails_closed_on_non_2xx() -> None:
    from testbeds.evidence.signoz import SignozExportError

    http = FakeHttpRunner(
        [
            ProbeResult(status_code=200, latency_ms=5.0, body="ok"),
            ProbeResult(status_code=503, latency_ms=2.0, body="unavailable"),
        ]
    )
    client = SignozEvidenceClient(
        otlp_endpoint="http://signoz-otel:4318",
        query_endpoint="http://signoz:8080",
        http_runner=http,
        clock=lambda: NOW,
    )
    with pytest.raises(SignozExportError, match="503"):
        asyncio.run(
            client.export_blackbox_probe(
                probe_url="http://frontend:8080/health",
                identity=_identity(),
            )
        )
    assert len(http.posts) == 1


def test_metrics_missing_usage_fields_are_unavailable_not_zero() -> None:
    payload = {
        "metadata": {"name": "frontend-abc"},
        "containers": [{"name": "frontend", "usage": {}}],
    }
    runner = FakeCommandRunner(
        [CommandResult(("kubectl",), 0, json.dumps(payload), "", 0.02)]
    )
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    sample = asyncio.run(
        collector.sample_metrics_api(
            namespace="otel-demo",
            pod_name="frontend-abc",
            identity=_identity(),
            cpu_limit_millicores=1000,
            memory_limit_bytes=512 * 1024 * 1024,
        )
    )
    assert isinstance(sample, UnavailableEvidence)
    assert "cpu_utilization" not in getattr(sample, "values", {})
    assert "memory_utilization" not in getattr(sample, "values", {})


def test_metrics_partial_usage_missing_memory_is_unavailable() -> None:
    payload = {
        "metadata": {"name": "frontend-abc"},
        "containers": [{"name": "frontend", "usage": {"cpu": "100m"}}],
    }
    runner = FakeCommandRunner(
        [CommandResult(("kubectl",), 0, json.dumps(payload), "", 0.02)]
    )
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    sample = asyncio.run(
        collector.sample_metrics_api(
            namespace="otel-demo",
            pod_name="frontend-abc",
            identity=_identity(),
            cpu_limit_millicores=1000,
            memory_limit_bytes=512 * 1024 * 1024,
        )
    )
    assert isinstance(sample, UnavailableEvidence)


def test_rabbitmq_and_keda_empty_payloads_are_unavailable() -> None:
    runner = FakeCommandRunner(
        [
            CommandResult(("kubectl",), 0, "", "", 0.02),
            CommandResult(("kubectl",), 0, "", "", 0.02),
        ]
    )
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    queue = asyncio.run(
        collector.sample_rabbitmq_queue_depth(
            namespace="keda-rabbitmq",
            scaled_object_name="rabbitmq-consumer",
            identity=_identity(environment="keda-rabbitmq", namespace="keda-rabbitmq"),
        )
    )
    scaler = asyncio.run(
        collector.sample_keda_scaler(
            namespace="keda-rabbitmq",
            scaled_object_name="rabbitmq-consumer",
            identity=_identity(environment="keda-rabbitmq", namespace="keda-rabbitmq"),
        )
    )
    assert isinstance(queue, UnavailableEvidence)
    assert queue.source_kind is EvidenceSourceKind.RABBITMQ_QUEUE
    assert isinstance(scaler, UnavailableEvidence)
    assert scaler.source_kind is EvidenceSourceKind.KEDA_SCALER


def test_policy_and_action_evidence_require_control_fixture_source() -> None:
    value = document()
    value["spec"]["expected"]["evidence"]["supporting"] = [
        {
            "evidenceType": "policy-decision",
            "subjectRole": "request-processor",
            "freshness": "fresh",
        },
        {
            "evidenceType": "action-result",
            "subjectRole": "request-processor",
            "freshness": "fresh",
        },
    ]
    scenario = GuardianScenarioV1Alpha2.model_validate(value)
    declaration = EnvironmentDeclaration.model_validate(
        {
            "environment": "otel-demo",
            "capabilities": [
                item.value
                for item in scenario.spec.environment_requirements.capabilities
            ],
            "evidenceSources": [
                EvidenceSourceKind.ENDPOINT_PROBE.value,
                EvidenceSourceKind.KUBERNETES_WORKLOAD.value,
                EvidenceSourceKind.METRICS_API.value,
            ],
        }
    )
    result = derive_compatibility(scenario, declaration)
    assert result.status is CompatibilityStatus.UNSUPPORTED
    assert EvidenceSourceKind.CONTROL_FIXTURE in result.missing_evidence_sources


def test_secret_values_are_redacted_from_diagnostics() -> None:
    http = FakeHttpRunner(
        [
            TimeoutError(
                "timed out contacting Authorization: Bearer super-secret token=abc"
            )
        ]
    )
    collector = EvidenceCollector(
        command_runner=FakeCommandRunner(),
        http_runner=http,
        clock=lambda: NOW,
    )
    sample = asyncio.run(
        collector.sample_endpoint(
            "http://frontend:8080/health",
            identity=_identity(),
            sample_count=1,
        )
    )
    assert isinstance(sample, UnavailableEvidence)
    assert "super-secret" not in sample.diagnostics
    assert "token=abc" not in sample.diagnostics or "[REDACTED]" in sample.diagnostics
    assert (
        "Bearer [REDACTED]" in sample.diagnostics or "[REDACTED]" in sample.diagnostics
    )


def test_timeout_and_unavailable_sources_return_typed_results_not_zeros() -> None:
    runner = FakeCommandRunner([TimeoutError("metrics.k8s.io unavailable")])
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    sample = asyncio.run(
        collector.sample_metrics_api(
            namespace="otel-demo",
            pod_name="frontend-abc",
            identity=_identity(),
            cpu_limit_millicores=1000,
            memory_limit_bytes=512 * 1024 * 1024,
        )
    )
    assert isinstance(sample, UnavailableEvidence)
    assert sample.source_kind is EvidenceSourceKind.METRICS_API
    assert "unavailable" in sample.reason.lower() or "timeout" in sample.reason.lower()
    assert "cpu_utilization" not in getattr(sample, "values", {})


def test_unsubstantiated_capabilities_are_removed() -> None:
    declared = frozenset(
        {
            EnvironmentCapability.HEALTHY_BASELINE,
            EnvironmentCapability.RESOURCE_PRESSURE,
            EnvironmentCapability.TELEMETRY_INTERRUPTION,
            EnvironmentCapability.SCALER_OBSERVATION,
        }
    )
    sources = frozenset(
        {
            EvidenceSourceKind.ENDPOINT_PROBE,
            EvidenceSourceKind.KUBERNETES_WORKLOAD,
        }
    )
    kept = substantiate_capabilities(declared, sources)
    assert EnvironmentCapability.HEALTHY_BASELINE in kept
    assert EnvironmentCapability.RESOURCE_PRESSURE not in kept
    assert EnvironmentCapability.TELEMETRY_INTERRUPTION not in kept
    assert EnvironmentCapability.SCALER_OBSERVATION not in kept


def test_compatibility_rejects_missing_deterministic_evidence_source() -> None:
    scenario = GuardianScenarioV1Alpha2.model_validate(document())
    required = scenario.spec.environment_requirements.capabilities
    declaration = EnvironmentDeclaration.model_validate(
        {
            "environment": "otel-demo",
            "capabilities": [item.value for item in required],
            "evidenceSources": [
                EvidenceSourceKind.ENDPOINT_PROBE.value,
                EvidenceSourceKind.KUBERNETES_WORKLOAD.value,
            ],
        }
    )
    result = derive_compatibility(scenario, declaration)
    assert EvidenceSourceKind.METRICS_API in required_evidence_sources_for_scenario(
        scenario
    )
    assert result.status is CompatibilityStatus.UNSUPPORTED
    assert EvidenceSourceKind.METRICS_API in result.missing_evidence_sources
    assert BlockingReason.EVIDENCE_SOURCE_UNAVAILABLE in result.blocking_reasons


def test_environment_declarations_omit_unsubstantiated_pressure_and_telemetry() -> None:
    for declaration in ENVIRONMENT_DECLARATIONS.values():
        assert EnvironmentCapability.RESOURCE_PRESSURE not in declaration.capabilities
        assert (
            EnvironmentCapability.TELEMETRY_INTERRUPTION not in declaration.capabilities
        )
        assert declaration.evidence_sources
        kept = substantiate_capabilities(
            declaration.capabilities, declaration.evidence_sources
        )
        assert kept == declaration.capabilities


def test_every_environment_retains_independently_observable_scenario() -> None:
    from pathlib import Path

    from testbeds.evidence.contracts import scenario_evidence_satisfied
    from testbeds.scenarios.environment_suite import select_scenarios

    for environment in ENVIRONMENT_DECLARATIONS:
        selected, _ = select_scenarios(environment, Path("testbeds/scenarios"))
        assert selected, f"{environment} has no independently observable scenario"
        available = ENVIRONMENT_DECLARATIONS[environment].evidence_sources
        for path in selected:
            scenario = load_guardian_scenario(path)
            assert isinstance(scenario, GuardianScenarioV1Alpha2)
            assert scenario_evidence_satisfied(scenario, available)


def test_no_shell_execution_in_collector_argv_construction() -> None:
    runner = FakeCommandRunner()
    collector = EvidenceCollector(
        command_runner=runner, http_runner=FakeHttpRunner(), clock=lambda: NOW
    )
    sample = asyncio.run(
        collector.sample_kubernetes_workload(
            namespace="otel-demo",
            workload_kind="Deployment",
            workload_name="frontend; rm -rf /",
            identity=_identity(),
        )
    )
    assert isinstance(sample, UnavailableEvidence)
    assert runner.calls == []
