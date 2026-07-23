"""Collector-backed scenario evidence provider contracts."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence, cast

import pytest

from testbeds.adapters.command_runner import CommandResult
from testbeds.evidence.collector import (
    EvidenceCollector,
    EvidenceSample,
    ProbeResult,
    UnavailableEvidence,
)
from testbeds.evidence.contracts import EvidenceSourceKind
from testbeds.evidence.signoz import SignozQueryResult
from testbeds.models import (
    DeploymentEvent,
    EnvironmentState,
    ObservedServiceIdentity,
    WorkloadSelector,
)
from testbeds.scenarios.execution import AdapterRegistration
from testbeds.scenarios.v1alpha2 import GuardianScenarioV1Alpha2
from tests.unit.test_guardian_scenario_v1alpha2 import document


class FakeCommandRunner:
    def __init__(self, responses: Sequence[str]) -> None:
        self._responses = list(responses)

    async def run(self, argv, *, timeout, cwd=None, input_text=None) -> CommandResult:
        return CommandResult(tuple(argv), 0, self._responses.pop(0), "", 0.01)


class FakeHttpRunner:
    def __init__(self, latencies: Sequence[float] = (10.0,)) -> None:
        self._latencies = list(latencies)
        self._index = 0

    async def probe(self, url, *, timeout, headers=None) -> ProbeResult:
        latency = self._latencies[self._index % len(self._latencies)]
        self._index += 1
        return ProbeResult(status_code=200, latency_ms=latency)

    async def post_json(self, url, payload, *, timeout, headers=None) -> ProbeResult:
        return ProbeResult(status_code=200, latency_ms=10.0)


class FakeSignozContract:
    def __init__(self, observed_at: datetime) -> None:
        self.observed_at = observed_at
        self.calls = 0

    async def query_telemetry_arrival(self, *, identity, lookback) -> SignozQueryResult:
        self.calls += 1
        return SignozQueryResult(
            matched=True,
            observed_at=self.observed_at,
            provenance_ref="signoz/telemetry-arrival",
            values={
                "quality": 1.0,
                "usable_samples": 10,
                "required_samples": 10,
                "pipeline_available": True,
                "comparison_valid": True,
            },
        )


@pytest.fixture
def fake_collector_deps() -> tuple[EvidenceCollector, list[datetime]]:
    timestamps = [
        datetime(2026, 7, 23, 12, 0, tzinfo=UTC) + timedelta(seconds=index)
        for index in range(6)
    ]
    workload = json.dumps(
        {
            "spec": {
                "replicas": 1,
                "template": {"spec": {"containers": [{"image": "example:v1"}]}},
            },
            "status": {"readyReplicas": 1},
        }
    )
    metrics = json.dumps({"containers": [{"usage": {"cpu": "100m", "memory": "64Mi"}}]})
    collector = EvidenceCollector(
        command_runner=FakeCommandRunner([workload, metrics, workload, metrics]),
        http_runner=FakeHttpRunner(),
        clock=lambda: timestamps.pop(0),
    )
    return collector, timestamps


def _scenario() -> GuardianScenarioV1Alpha2:
    return GuardianScenarioV1Alpha2.model_validate(document())


def _load_scenario() -> GuardianScenarioV1Alpha2:
    payload = copy.deepcopy(document())
    payload["spec"]["expected"]["evidence"]["supporting"].append(
        {
            "evidenceType": "load",
            "subjectRole": "request-processor",
            "freshness": "fresh",
        }
    )
    return GuardianScenarioV1Alpha2.model_validate(payload)


def test_collector_provider_samples_after_assessment_request(
    fake_collector_deps,
) -> None:
    from testbeds.scenarios.evidence_provider import (
        CollectorEvidenceTargets,
        CollectorScenarioEvidenceProvider,
    )

    collector, _ = fake_collector_deps
    provider = CollectorScenarioEvidenceProvider(
        collector=collector,
        targets=CollectorEvidenceTargets(
            namespace="guardian-test",
            endpoint_url="http://frontend.guardian-test.svc.cluster.local",
            workload_kind="deployment",
            workload_name="frontend",
            metrics_pod_name="frontend-0",
            cpu_limit_millicores=500,
            memory_limit_bytes=128 * 1024 * 1024,
        ),
    )

    samples = asyncio.run(
        provider.collect_assessment_evidence(
            scenario=_load_scenario(),
            registration=cast(AdapterRegistration, object()),
            observations=(),
            control_results={},
        )
    )

    assert samples
    assert all(sample.provenance_ref for sample in samples)
    assert all(sample.observed_at.tzinfo is not None for sample in samples)
    endpoint = next(
        sample
        for sample in samples
        if sample.source_kind is EvidenceSourceKind.ENDPOINT_PROBE
    )
    assert isinstance(endpoint, EvidenceSample)
    assert endpoint.values["request_rate"] > 0


def test_request_rate_uses_probe_wall_window_not_probe_latencies() -> None:
    from testbeds.scenarios.evidence_provider import (
        CollectorEvidenceTargets,
        CollectorScenarioEvidenceProvider,
    )

    def endpoint_values(latencies: Sequence[float]) -> dict[str, Any]:
        collector = EvidenceCollector(
            command_runner=FakeCommandRunner([]),
            http_runner=FakeHttpRunner(latencies),
            clock=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        )
        provider = CollectorScenarioEvidenceProvider(
            collector=collector,
            targets=CollectorEvidenceTargets(
                namespace="guardian-test",
                endpoint_url="http://frontend.guardian-test.svc.cluster.local",
                workload_kind="deployment",
                workload_name="frontend",
            ),
            monotonic_clock=iter((10.0, 12.0)).__next__,
        )
        samples = asyncio.run(
            provider.collect_assessment_evidence(
                scenario=_load_scenario(),
                registration=cast(AdapterRegistration, object()),
                observations=(),
                control_results={},
            )
        )
        endpoint = next(
            sample
            for sample in samples
            if sample.source_kind is EvidenceSourceKind.ENDPOINT_PROBE
        )
        assert isinstance(endpoint, EvidenceSample)
        return dict(endpoint.values)

    fast = endpoint_values((1.0, 1.0, 1.0))
    slow = endpoint_values((100.0, 100.0, 100.0))

    assert fast["probe_window_seconds"] == pytest.approx(2.0)
    assert slow["probe_window_seconds"] == pytest.approx(2.0)
    assert fast["request_rate"] == pytest.approx(1.5)
    assert slow["request_rate"] == pytest.approx(1.5)


def test_collector_provider_normalizes_workload_signals_for_deployment() -> None:
    from testbeds.scenarios.evidence_provider import (
        CollectorEvidenceTargets,
        CollectorScenarioEvidenceProvider,
    )

    previous_digest = "sha256:" + "a" * 64
    current_digest = "sha256:" + "b" * 64
    workload = json.dumps(
        {
            "spec": {
                "replicas": 2,
                "template": {
                    "spec": {"containers": [{"image": f"example:v2@{current_digest}"}]}
                },
            },
            "status": {"readyReplicas": 2},
        }
    )
    metrics = json.dumps({"containers": [{"usage": {"cpu": "100m", "memory": "64Mi"}}]})
    observed_at = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    provider = CollectorScenarioEvidenceProvider(
        collector=EvidenceCollector(
            command_runner=FakeCommandRunner([workload, metrics]),
            http_runner=FakeHttpRunner(),
            clock=lambda: observed_at,
        ),
        targets=CollectorEvidenceTargets(
            namespace="guardian-test",
            endpoint_url="http://frontend.guardian-test.svc.cluster.local",
            workload_kind="rollout",
            workload_name="canary-demo",
            workload_role="canary",
            service_name="canary-demo-preview",
            metrics_pod_name="frontend-0",
            cpu_limit_millicores=500,
            memory_limit_bytes=128 * 1024 * 1024,
        ),
        clock=lambda: observed_at,
    )
    deployment = DeploymentEvent(
        target=WorkloadSelector("request-processor"),
        from_version="v1",
        to_version="v2",
    )

    samples = asyncio.run(
        provider.collect_assessment_evidence(
            scenario=_scenario(),
            registration=cast(AdapterRegistration, object()),
            observations=(
                EnvironmentState(
                    services=(
                        ObservedServiceIdentity(
                            "canary",
                            "canary-demo-7b9b956b5d",
                            "v1",
                            previous_digest,
                        ),
                    )
                ),
            ),
            control_results={"deployment": deployment},
        )
    )

    workload_sample = next(
        sample
        for sample in samples
        if sample.source_kind is EvidenceSourceKind.KUBERNETES_WORKLOAD
    )
    assert isinstance(workload_sample, EvidenceSample)
    assert workload_sample.values["dependency_healthy"] is True
    assert workload_sample.values["current_digest"] == current_digest
    assert workload_sample.values["previous_digest"] == previous_digest
    assert workload_sample.values["previous_service_version"] == "v1"
    assert workload_sample.values["current_service_version"] == "v2"


def test_collector_provider_collects_telemetry_quality_from_approved_contract(
    fake_collector_deps,
) -> None:
    from testbeds.scenarios.evidence_provider import (
        CollectorEvidenceTargets,
        CollectorScenarioEvidenceProvider,
    )

    collector, _ = fake_collector_deps
    signoz = FakeSignozContract(datetime(2026, 7, 23, 12, 0, tzinfo=UTC))
    provider = CollectorScenarioEvidenceProvider(
        collector=collector,
        targets=CollectorEvidenceTargets(
            namespace="guardian-test",
            endpoint_url="http://frontend.guardian-test.svc.cluster.local",
            workload_kind="deployment",
            workload_name="frontend",
            metrics_pod_name="frontend-0",
            cpu_limit_millicores=500,
            memory_limit_bytes=128 * 1024 * 1024,
            signoz_contract=signoz,
        ),
    )

    samples = asyncio.run(
        provider.collect_assessment_evidence(
            scenario=_scenario(),
            registration=cast(AdapterRegistration, object()),
            observations=(),
            control_results={},
        )
    )

    telemetry = next(
        sample
        for sample in samples
        if sample.source_kind is EvidenceSourceKind.SIGNOZ_TELEMETRY
    )
    assert isinstance(telemetry, EvidenceSample)
    assert telemetry.values["quality"] == 1.0
    assert signoz.calls == 1


def test_recovery_collection_uses_distinct_clock_and_provenance() -> None:
    from testbeds.scenarios.evidence_provider import (
        CollectorEvidenceTargets,
        CollectorScenarioEvidenceProvider,
    )

    constant_time = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    workload = json.dumps(
        {
            "spec": {
                "replicas": 1,
                "template": {"spec": {"containers": [{"image": "example:v1"}]}},
            },
            "status": {"readyReplicas": 1},
        }
    )
    metrics = json.dumps({"containers": [{"usage": {"cpu": "100m", "memory": "64Mi"}}]})
    collector = EvidenceCollector(
        command_runner=FakeCommandRunner([workload, metrics, workload, metrics]),
        http_runner=FakeHttpRunner(),
        clock=lambda: constant_time,
    )
    provider = CollectorScenarioEvidenceProvider(
        collector=collector,
        targets=CollectorEvidenceTargets(
            namespace="guardian-test",
            endpoint_url="http://frontend.guardian-test.svc.cluster.local",
            workload_kind="deployment",
            workload_name="frontend",
            metrics_pod_name="frontend-0",
            cpu_limit_millicores=500,
            memory_limit_bytes=128 * 1024 * 1024,
        ),
        clock=lambda: constant_time,
    )
    scenario = _scenario()
    assessment = asyncio.run(
        provider.collect_assessment_evidence(
            scenario=scenario,
            registration=cast(AdapterRegistration, object()),
            observations=(),
            control_results={},
        )
    )
    recovery = asyncio.run(
        provider.collect_recovery_evidence(
            scenario=scenario,
            registration=cast(AdapterRegistration, object()),
            post_reset_state=cast(Any, object()),
        )
    )

    assert all(
        assessment_sample is not recovery_sample
        for assessment_sample, recovery_sample in zip(assessment, recovery, strict=True)
    )
    assert {sample.provenance_ref for sample in assessment}.isdisjoint(
        sample.provenance_ref for sample in recovery
    )
    assert min(sample.observed_at for sample in recovery) > max(
        sample.observed_at for sample in assessment
    )


def test_collector_provider_preserves_unavailable_required_signal(
    fake_collector_deps,
) -> None:
    from testbeds.scenarios.evidence_provider import (
        CollectorEvidenceTargets,
        CollectorScenarioEvidenceProvider,
    )

    _, _ = fake_collector_deps
    collector = EvidenceCollector(
        command_runner=FakeCommandRunner(["", ""]),
        http_runner=FakeHttpRunner(),
        clock=lambda: datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )
    provider = CollectorScenarioEvidenceProvider(
        collector=collector,
        targets=CollectorEvidenceTargets(
            namespace="guardian-test",
            endpoint_url="http://frontend.guardian-test.svc.cluster.local",
            workload_kind="deployment",
            workload_name="frontend",
            metrics_pod_name="frontend-0",
            cpu_limit_millicores=500,
            memory_limit_bytes=128 * 1024 * 1024,
        ),
    )

    samples = asyncio.run(
        provider.collect_assessment_evidence(
            scenario=_scenario(),
            registration=cast(AdapterRegistration, object()),
            observations=(),
            control_results={},
        )
    )

    telemetry = next(
        sample
        for sample in samples
        if sample.source_kind is EvidenceSourceKind.SIGNOZ_TELEMETRY
    )
    assert isinstance(telemetry, UnavailableEvidence)
    assert "approved SigNoz evidence contract" in telemetry.reason
