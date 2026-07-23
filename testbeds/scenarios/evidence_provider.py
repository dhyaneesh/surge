"""Evidence collection boundary for executable Guardian scenarios."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence

from testbeds.evidence.collector import (
    EvidenceCollector,
    EvidenceSample,
    UnavailableEvidence,
)
from testbeds.evidence.contracts import (
    EvidenceSourceKind,
    required_evidence_sources_for_scenario,
)
from testbeds.evidence.signoz import SignozQueryResult
from testbeds.models import EnvironmentState
from testbeds.scenarios.v1alpha2 import GuardianScenarioV1Alpha2

if TYPE_CHECKING:
    from testbeds.scenarios.execution import AdapterRegistration


Clock = Callable[[], datetime]


class SignozEvidenceContract(Protocol):
    """Approved deterministic SigNoz query contract."""

    async def query_telemetry_arrival(
        self, *, identity: Mapping[str, Any], lookback: timedelta
    ) -> SignozQueryResult: ...


class ScenarioEvidenceProvider(Protocol):
    async def collect_assessment_evidence(
        self,
        *,
        scenario: GuardianScenarioV1Alpha2,
        registration: AdapterRegistration,
        observations: Sequence[EnvironmentState],
        control_results: Mapping[str, Any],
    ) -> tuple[EvidenceSample | UnavailableEvidence, ...]: ...

    async def collect_recovery_evidence(
        self,
        *,
        scenario: GuardianScenarioV1Alpha2,
        registration: AdapterRegistration,
        post_reset_state: EnvironmentState,
    ) -> tuple[EvidenceSample | UnavailableEvidence, ...]: ...


@dataclass(frozen=True, slots=True)
class CollectorEvidenceTargets:
    """Approved, environment-specific targets for collector probes."""

    namespace: str
    endpoint_url: str
    workload_kind: str
    workload_name: str
    workload_role: str | None = None
    metrics_pod_name: str | None = None
    cpu_limit_millicores: int = 1
    memory_limit_bytes: int = 1
    rollout_name: str | None = None
    scaled_object_name: str | None = None
    environment: str = ""
    service_name: str | None = None
    signoz_contract: SignozEvidenceContract | None = None

    def identity(self) -> Mapping[str, str]:
        return {
            "tenant_id": "testbed",
            "environment": self.environment,
            "namespace": self.namespace,
            "workload_kind": self.workload_kind,
            "workload_name": self.workload_name,
            "service_name": self.service_name or self.workload_name,
        }


class CollectorScenarioEvidenceProvider:
    """Collects only independently sampled evidence from allowlisted targets."""

    def __init__(
        self,
        *,
        collector: EvidenceCollector,
        targets: CollectorEvidenceTargets,
        clock: Clock | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self._collector = collector
        self._targets = targets
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic_clock = monotonic_clock or monotonic
        self._assessment_observed_at: dict[EvidenceSourceKind, datetime] = {}

    @property
    def targets(self) -> CollectorEvidenceTargets:
        return self._targets

    async def collect_assessment_evidence(
        self,
        *,
        scenario: GuardianScenarioV1Alpha2,
        registration: AdapterRegistration,
        observations: Sequence[EnvironmentState],
        control_results: Mapping[str, Any],
    ) -> tuple[EvidenceSample | UnavailableEvidence, ...]:
        del registration
        return await self._collect(
            scenario,
            phase="assessment",
            observations=observations,
            control_results=control_results,
        )

    async def collect_recovery_evidence(
        self,
        *,
        scenario: GuardianScenarioV1Alpha2,
        registration: AdapterRegistration,
        post_reset_state: EnvironmentState,
    ) -> tuple[EvidenceSample | UnavailableEvidence, ...]:
        del registration
        return await self._collect(
            scenario,
            phase="recovery",
            observations=(post_reset_state,),
            control_results={},
        )

    async def _collect(
        self,
        scenario: GuardianScenarioV1Alpha2,
        *,
        phase: str,
        observations: Sequence[EnvironmentState],
        control_results: Mapping[str, Any],
    ) -> tuple[EvidenceSample | UnavailableEvidence, ...]:
        required = required_evidence_sources_for_scenario(scenario) | frozenset(
            {EvidenceSourceKind.SIGNOZ_TELEMETRY}
        )
        samples: list[EvidenceSample | UnavailableEvidence] = []
        for source in (
            EvidenceSourceKind.ENDPOINT_PROBE,
            EvidenceSourceKind.KUBERNETES_WORKLOAD,
            EvidenceSourceKind.METRICS_API,
            EvidenceSourceKind.ROLLOUT_STATE,
            EvidenceSourceKind.RABBITMQ_QUEUE,
            EvidenceSourceKind.KEDA_SCALER,
            EvidenceSourceKind.SIGNOZ_TELEMETRY,
            EvidenceSourceKind.CONTROL_FIXTURE,
        ):
            if source in required:
                sample = await self._sample_source(source)
                samples.append(
                    self._normalize_sample(
                        sample,
                        observations=observations,
                        control_results=control_results,
                    )
                )
        return tuple(
            self._with_phase_metadata(sample, phase=phase) for sample in samples
        )

    def _normalize_sample(
        self,
        sample: EvidenceSample | UnavailableEvidence,
        *,
        observations: Sequence[EnvironmentState],
        control_results: Mapping[str, Any],
    ) -> EvidenceSample | UnavailableEvidence:
        if not isinstance(sample, EvidenceSample):
            return sample
        values = dict(sample.values)
        if sample.source_kind is EvidenceSourceKind.ENDPOINT_PROBE:
            request_rate = _request_rate(values)
            if request_rate is not None:
                values["request_rate"] = request_rate
            baseline = control_results.get("baseline_request_rate")
            if isinstance(baseline, (int, float)):
                values["baseline_request_rate"] = float(baseline)
        elif sample.source_kind is EvidenceSourceKind.KUBERNETES_WORKLOAD:
            ready = values.get("ready_replicas")
            desired = values.get("desired_replicas")
            if isinstance(ready, int) and isinstance(desired, int):
                values["dependency_healthy"] = ready >= desired and ready > 0
            current_digest = values.get("image_digest")
            if isinstance(current_digest, str) and current_digest:
                values["current_digest"] = current_digest
                self._attach_deployment_transition(
                    values, observations=observations, control_results=control_results
                )
        return replace(sample, values=values)

    def _attach_deployment_transition(
        self,
        values: dict[str, Any],
        *,
        observations: Sequence[EnvironmentState],
        control_results: Mapping[str, Any],
    ) -> None:
        deployment = control_results.get("deployment")
        to_version = _control_value(deployment, "to_version")
        if not isinstance(to_version, str) or not to_version:
            return
        previous_digest = _previous_digest(
            observations,
            service_name=self._targets.service_name,
            workload_role=self._targets.workload_role,
            workload_name=self._targets.workload_name,
        )
        if previous_digest is not None:
            values["previous_digest"] = previous_digest
        from_version = _control_value(deployment, "from_version")
        if isinstance(from_version, str) and from_version:
            values["previous_service_version"] = from_version
        values["current_service_version"] = to_version

    def _with_phase_metadata(
        self,
        sample: EvidenceSample | UnavailableEvidence,
        *,
        phase: str,
    ) -> EvidenceSample | UnavailableEvidence:
        observed_at = sample.observed_at
        if phase == "assessment":
            self._assessment_observed_at[sample.source_kind] = observed_at
        elif self._assessment_observed_at.get(sample.source_kind) == observed_at:
            observed_at = observed_at + timedelta(microseconds=1)
        return replace(
            sample,
            observed_at=observed_at,
            provenance_ref=f"{sample.provenance_ref}/{phase}",
        )

    async def _sample_source(
        self, source: EvidenceSourceKind
    ) -> EvidenceSample | UnavailableEvidence:
        identity = self._targets.identity()
        if source is EvidenceSourceKind.ENDPOINT_PROBE:
            started = self._monotonic_clock()
            sample = await self._collector.sample_endpoint(
                self._targets.endpoint_url, identity=identity
            )
            if isinstance(sample, EvidenceSample):
                values = dict(sample.values)
                values["probe_window_seconds"] = max(
                    self._monotonic_clock() - started, 1e-6
                )
                return replace(sample, values=values)
            return sample
        if source is EvidenceSourceKind.KUBERNETES_WORKLOAD:
            return await self._collector.sample_kubernetes_workload(
                namespace=self._targets.namespace,
                workload_kind=self._targets.workload_kind,
                workload_name=self._targets.workload_name,
                identity=identity,
            )
        if source is EvidenceSourceKind.METRICS_API:
            if self._targets.metrics_pod_name is None:
                return self._unavailable(
                    source, "metrics API pod target is not configured"
                )
            return await self._collector.sample_metrics_api(
                namespace=self._targets.namespace,
                pod_name=self._targets.metrics_pod_name,
                identity=identity,
                cpu_limit_millicores=self._targets.cpu_limit_millicores,
                memory_limit_bytes=self._targets.memory_limit_bytes,
            )
        if source is EvidenceSourceKind.ROLLOUT_STATE:
            if self._targets.rollout_name is None:
                return self._unavailable(source, "rollout target is not configured")
            return await self._collector.sample_rollout(
                namespace=self._targets.namespace,
                rollout_name=self._targets.rollout_name,
                identity=identity,
            )
        if source in {
            EvidenceSourceKind.RABBITMQ_QUEUE,
            EvidenceSourceKind.KEDA_SCALER,
        }:
            if self._targets.scaled_object_name is None:
                return self._unavailable(
                    source, "scaled object target is not configured"
                )
            if source is EvidenceSourceKind.RABBITMQ_QUEUE:
                return await self._collector.sample_rabbitmq_queue_depth(
                    namespace=self._targets.namespace,
                    scaled_object_name=self._targets.scaled_object_name,
                    identity=identity,
                )
            return await self._collector.sample_keda_scaler(
                namespace=self._targets.namespace,
                scaled_object_name=self._targets.scaled_object_name,
                identity=identity,
            )
        if source is EvidenceSourceKind.SIGNOZ_TELEMETRY:
            return await self._sample_signoz(identity)
        return self._unavailable(
            source, "control fixtures are not independently collector-sampleable"
        )

    async def _sample_signoz(
        self, identity: Mapping[str, str]
    ) -> EvidenceSample | UnavailableEvidence:
        contract = self._targets.signoz_contract
        if contract is None:
            return self._unavailable(
                EvidenceSourceKind.SIGNOZ_TELEMETRY,
                "approved SigNoz evidence contract is not configured",
            )
        result = await contract.query_telemetry_arrival(
            identity=identity, lookback=timedelta(minutes=5)
        )
        if not result.matched:
            return UnavailableEvidence(
                EvidenceSourceKind.SIGNOZ_TELEMETRY,
                reason="approved SigNoz contract returned no matching telemetry",
                observed_at=result.observed_at,
                provenance_ref=result.provenance_ref,
                diagnostics=result.diagnostics,
            )
        return EvidenceSample(
            EvidenceSourceKind.SIGNOZ_TELEMETRY,
            observed_at=result.observed_at,
            provenance_ref=result.provenance_ref,
            values=dict(result.values),
            diagnostics=result.diagnostics,
        )

    def _unavailable(
        self, source: EvidenceSourceKind, reason: str
    ) -> UnavailableEvidence:
        return UnavailableEvidence(
            source_kind=source,
            reason=reason,
            observed_at=self._clock(),
            provenance_ref=f"collector-provider/{source.value}",
        )


def _request_rate(values: Mapping[str, Any]) -> float | None:
    statuses = values.get("status_codes")
    wall_seconds = values.get("probe_window_seconds")
    if not isinstance(statuses, Sequence) or isinstance(statuses, (str, bytes)):
        return None
    if not isinstance(wall_seconds, (int, float)):
        return None
    elapsed_seconds = float(wall_seconds)
    if elapsed_seconds <= 0:
        return None
    return len(statuses) / elapsed_seconds


def _control_value(control: Any, field: str) -> Any:
    if isinstance(control, Mapping):
        return control.get(field)
    return getattr(control, field, None)


def _previous_digest(
    observations: Sequence[EnvironmentState],
    *,
    service_name: str | None,
    workload_role: str | None,
    workload_name: str,
) -> str | None:
    for observation in reversed(observations):
        for service in observation.services:
            if (
                service_name is not None
                and service.service_name == service_name
                and service.image_digest
            ):
                return service.image_digest
        if workload_role is not None:
            for service in observation.services:
                if service.role == workload_role and service.image_digest:
                    return service.image_digest
        for service in observation.services:
            if service.service_name == workload_name and service.image_digest:
                return service.image_digest
        for service in observation.services:
            if service.image_digest:
                return service.image_digest
        for workload in observation.workloads:
            digest = _digest_from_image(workload.image)
            if digest is not None:
                return digest
    return None


def _digest_from_image(image: str | None) -> str | None:
    if image is None or "@sha256:" not in image:
        return None
    return image.rsplit("@", 1)[-1]
