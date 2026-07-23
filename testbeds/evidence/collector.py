"""Allowlisted independent evidence sampling for disposable testbeds."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from testbeds.adapters.command_runner import CommandRunner, redact
from testbeds.evidence.contracts import EvidenceSourceKind

_UNSAFE_NAME = re.compile(r"[;|&`$<>\n\x00]|&&|\|\|")
_DEFAULT_TIMEOUT = timedelta(seconds=10)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status_code: int
    latency_ms: float
    body: str = ""


@dataclass(frozen=True, slots=True)
class EvidenceSample:
    source_kind: EvidenceSourceKind
    observed_at: datetime
    provenance_ref: str
    values: Mapping[str, Any]
    diagnostics: str = ""


@dataclass(frozen=True, slots=True)
class UnavailableEvidence:
    source_kind: EvidenceSourceKind
    reason: str
    observed_at: datetime
    provenance_ref: str
    diagnostics: str = ""


class HttpProbeRunner(Protocol):
    async def probe(
        self,
        url: str,
        *,
        timeout: timedelta,
        headers: Mapping[str, str] | None = None,
    ) -> ProbeResult: ...


Clock = Callable[[], datetime]


def control_result_is_not_symptom_evidence(control: Any) -> bool:
    """Controls (load/fault/deploy) never prove symptoms by themselves."""
    return True


class EvidenceCollector:
    """Samples observable effects through fixed argv and HTTP probe contracts."""

    def __init__(
        self,
        *,
        command_runner: CommandRunner,
        http_runner: HttpProbeRunner,
        clock: Clock | None = None,
        timeout: timedelta = _DEFAULT_TIMEOUT,
    ) -> None:
        self._commands = command_runner
        self._http = http_runner
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._timeout = timeout

    def symptom_evidence_from_control(self, control: Any) -> tuple[EvidenceSample, ...]:
        return ()

    async def sample_endpoint(
        self,
        url: str,
        *,
        identity: Mapping[str, Any],
        sample_count: int = 3,
    ) -> EvidenceSample | UnavailableEvidence:
        observed_at = self._clock()
        provenance = f"endpoint-probe/{url}"
        statuses: list[int] = []
        latencies: list[float] = []
        try:
            for _ in range(max(1, sample_count)):
                result = await self._http.probe(url, timeout=self._timeout)
                statuses.append(int(result.status_code))
                latencies.append(float(result.latency_ms))
        except Exception as error:
            return UnavailableEvidence(
                EvidenceSourceKind.ENDPOINT_PROBE,
                reason="endpoint probe timeout or unavailable",
                observed_at=observed_at,
                provenance_ref=provenance,
                diagnostics=redact(str(error)),
            )
        error_rate = sum(1 for code in statuses if code >= 400) / len(statuses)
        return EvidenceSample(
            EvidenceSourceKind.ENDPOINT_PROBE,
            observed_at=observed_at,
            provenance_ref=provenance,
            values={
                "status_codes": tuple(statuses),
                "latency_ms": tuple(latencies),
                "error_rate": error_rate,
                "p95_latency_ms": _percentile(latencies, 95),
                "identity": dict(identity),
            },
        )

    async def sample_kubernetes_workload(
        self,
        *,
        namespace: str,
        workload_kind: str,
        workload_name: str,
        identity: Mapping[str, Any],
    ) -> EvidenceSample | UnavailableEvidence:
        observed_at = self._clock()
        provenance = f"kubernetes-workload/{namespace}/{workload_kind}/{workload_name}"
        unsafe = _validate_k8s_token(namespace, "namespace") or _validate_k8s_token(
            workload_name, "workload_name"
        )
        if unsafe is not None:
            return UnavailableEvidence(
                EvidenceSourceKind.KUBERNETES_WORKLOAD,
                reason=unsafe,
                observed_at=observed_at,
                provenance_ref=provenance,
                diagnostics=unsafe,
            )
        kind = workload_kind.lower()
        argv = (
            "kubectl",
            "get",
            kind,
            workload_name,
            "-n",
            namespace,
            "-o",
            "json",
        )
        try:
            result = await self._commands.run(argv, timeout=self._timeout)
            payload = json.loads(result.stdout or "{}")
        except Exception as error:
            return UnavailableEvidence(
                EvidenceSourceKind.KUBERNETES_WORKLOAD,
                reason="kubernetes workload unavailable",
                observed_at=observed_at,
                provenance_ref=provenance,
                diagnostics=redact(str(error)),
            )
        spec = payload.get("spec", {})
        status = payload.get("status", {})
        image = _first_image(payload)
        version, digest = _split_image(image)
        return EvidenceSample(
            EvidenceSourceKind.KUBERNETES_WORKLOAD,
            observed_at=observed_at,
            provenance_ref=provenance,
            values={
                "desired_replicas": int(
                    spec.get("replicas") or status.get("replicas") or 0
                ),
                "ready_replicas": int(status.get("readyReplicas") or 0),
                "service_version": version,
                "image_digest": digest,
                "identity": dict(identity),
            },
        )

    async def sample_metrics_api(
        self,
        *,
        namespace: str,
        pod_name: str,
        identity: Mapping[str, Any],
        cpu_limit_millicores: int,
        memory_limit_bytes: int,
    ) -> EvidenceSample | UnavailableEvidence:
        observed_at = self._clock()
        path = f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods/{pod_name}"
        provenance = f"metrics-api{path}"
        unsafe = _validate_k8s_token(namespace, "namespace") or _validate_k8s_token(
            pod_name, "pod_name"
        )
        if unsafe is not None:
            return UnavailableEvidence(
                EvidenceSourceKind.METRICS_API,
                reason=unsafe,
                observed_at=observed_at,
                provenance_ref=provenance,
                diagnostics=unsafe,
            )
        argv = ("kubectl", "get", "--raw", path)
        try:
            result = await self._commands.run(argv, timeout=self._timeout)
            payload = json.loads(result.stdout or "{}")
        except Exception as error:
            return UnavailableEvidence(
                EvidenceSourceKind.METRICS_API,
                reason="metrics api unavailable",
                observed_at=observed_at,
                provenance_ref=provenance,
                diagnostics=redact(str(error)),
            )
        containers = payload.get("containers") or []
        if not containers:
            return UnavailableEvidence(
                EvidenceSourceKind.METRICS_API,
                reason="metrics api returned no container usage",
                observed_at=observed_at,
                provenance_ref=provenance,
            )
        usage = containers[0].get("usage", {})
        cpu = _parse_cpu_millicores(usage.get("cpu", "0"))
        memory = _parse_memory_bytes(usage.get("memory", "0"))
        return EvidenceSample(
            EvidenceSourceKind.METRICS_API,
            observed_at=observed_at,
            provenance_ref=provenance,
            values={
                "cpu_utilization": cpu / max(cpu_limit_millicores, 1),
                "memory_utilization": memory / max(memory_limit_bytes, 1),
                "identity": dict(identity),
            },
        )

    async def sample_rollout(
        self,
        *,
        namespace: str,
        rollout_name: str,
        identity: Mapping[str, Any],
    ) -> EvidenceSample | UnavailableEvidence:
        observed_at = self._clock()
        provenance = f"rollout-state/{namespace}/{rollout_name}"
        unsafe = _validate_k8s_token(namespace, "namespace") or _validate_k8s_token(
            rollout_name, "rollout_name"
        )
        if unsafe is not None:
            return UnavailableEvidence(
                EvidenceSourceKind.ROLLOUT_STATE,
                reason=unsafe,
                observed_at=observed_at,
                provenance_ref=provenance,
                diagnostics=unsafe,
            )
        argv = (
            "kubectl",
            "get",
            "rollout",
            rollout_name,
            "-n",
            namespace,
            "-o",
            "json",
        )
        try:
            result = await self._commands.run(argv, timeout=self._timeout)
            payload = json.loads(result.stdout or "{}")
        except Exception as error:
            return UnavailableEvidence(
                EvidenceSourceKind.ROLLOUT_STATE,
                reason="rollout state unavailable",
                observed_at=observed_at,
                provenance_ref=provenance,
                diagnostics=redact(str(error)),
            )
        status = payload.get("status", {})
        return EvidenceSample(
            EvidenceSourceKind.ROLLOUT_STATE,
            observed_at=observed_at,
            provenance_ref=provenance,
            values={
                "phase": status.get("phase"),
                "ready_replicas": int(status.get("readyReplicas") or 0),
                "desired_replicas": int(status.get("replicas") or 0),
                "updated_replicas": int(status.get("updatedReplicas") or 0),
                "unavailable_replicas": int(status.get("unavailableReplicas") or 0),
                "stable_hash": status.get("stableRS"),
                "canary_hash": status.get("currentPodHash"),
                "identity": dict(identity),
            },
        )

    async def sample_rabbitmq_queue_depth(
        self,
        *,
        namespace: str,
        scaled_object_name: str,
        identity: Mapping[str, Any],
    ) -> EvidenceSample | UnavailableEvidence:
        observed_at = self._clock()
        provenance = f"rabbitmq-queue/{namespace}/{scaled_object_name}"
        scaled = await self._get_json(
            namespace,
            "scaledobject",
            scaled_object_name,
            EvidenceSourceKind.RABBITMQ_QUEUE,
            provenance,
            observed_at,
        )
        if isinstance(scaled, UnavailableEvidence):
            return scaled
        hpa_name = scaled.get("status", {}).get("hpaName")
        if not hpa_name:
            return UnavailableEvidence(
                EvidenceSourceKind.RABBITMQ_QUEUE,
                reason="scaledobject missing hpaName",
                observed_at=observed_at,
                provenance_ref=provenance,
            )
        hpa = await self._get_json(
            namespace,
            "horizontalpodautoscaler",
            hpa_name,
            EvidenceSourceKind.RABBITMQ_QUEUE,
            provenance,
            observed_at,
        )
        if isinstance(hpa, UnavailableEvidence):
            return hpa
        depth = _queue_depth(hpa.get("status", {}).get("currentMetrics", []))
        if depth is None:
            return UnavailableEvidence(
                EvidenceSourceKind.RABBITMQ_QUEUE,
                reason="queue depth metric unavailable",
                observed_at=observed_at,
                provenance_ref=provenance,
            )
        return EvidenceSample(
            EvidenceSourceKind.RABBITMQ_QUEUE,
            observed_at=observed_at,
            provenance_ref=provenance,
            values={"queue_depth": depth, "identity": dict(identity)},
        )

    async def sample_keda_scaler(
        self,
        *,
        namespace: str,
        scaled_object_name: str,
        identity: Mapping[str, Any],
    ) -> EvidenceSample | UnavailableEvidence:
        observed_at = self._clock()
        provenance = f"keda-scaler/{namespace}/{scaled_object_name}"
        scaled = await self._get_json(
            namespace,
            "scaledobject",
            scaled_object_name,
            EvidenceSourceKind.KEDA_SCALER,
            provenance,
            observed_at,
        )
        if isinstance(scaled, UnavailableEvidence):
            return scaled
        status = scaled.get("status", {})
        conditions = status.get("conditions", [])
        health = status.get("health", {})
        ready = any(
            item.get("type") == "Ready" and item.get("status") == "True"
            for item in conditions
        )
        scaler_error = any(
            value.get("status") != "Happy" or value.get("numberOfFailures", 0) > 0
            for value in health.values()
            if isinstance(value, Mapping)
        )
        return EvidenceSample(
            EvidenceSourceKind.KEDA_SCALER,
            observed_at=observed_at,
            provenance_ref=provenance,
            values={
                "ready": ready,
                "scaler_error": scaler_error,
                "active": any(
                    item.get("type") == "Active" and item.get("status") == "True"
                    for item in conditions
                ),
                "identity": dict(identity),
            },
        )

    async def _get_json(
        self,
        namespace: str,
        kind: str,
        name: str,
        source: EvidenceSourceKind,
        provenance: str,
        observed_at: datetime,
    ) -> dict[str, Any] | UnavailableEvidence:
        unsafe = _validate_k8s_token(namespace, "namespace") or _validate_k8s_token(
            name, "name"
        )
        if unsafe is not None:
            return UnavailableEvidence(
                source,
                reason=unsafe,
                observed_at=observed_at,
                provenance_ref=provenance,
                diagnostics=unsafe,
            )
        argv = ("kubectl", "get", kind, name, "-n", namespace, "-o", "json")
        try:
            result = await self._commands.run(argv, timeout=self._timeout)
            return json.loads(result.stdout or "{}")
        except Exception as error:
            return UnavailableEvidence(
                source,
                reason=f"{source.value} unavailable",
                observed_at=observed_at,
                provenance_ref=provenance,
                diagnostics=redact(str(error)),
            )


def _validate_k8s_token(value: str, field: str) -> str | None:
    if not value or _UNSAFE_NAME.search(value) or "/" in value:
        return f"unsafe {field} rejected"
    return None


def _first_image(payload: Mapping[str, Any]) -> str | None:
    containers = (
        payload.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    for item in containers:
        image = item.get("image")
        if image:
            return str(image)
    return None


def _split_image(image: str | None) -> tuple[str | None, str | None]:
    if not image:
        return None, None
    digest = None
    remainder = image
    if "@sha256:" in image:
        remainder, digest = image.split("@", 1)
    version = None
    if ":" in remainder.rsplit("/", 1)[-1]:
        version = remainder.rsplit(":", 1)[-1]
    return version, digest


def _parse_cpu_millicores(value: str) -> float:
    text = str(value)
    if text.endswith("n"):
        return float(text[:-1]) / 1_000_000.0
    if text.endswith("u"):
        return float(text[:-1]) / 1_000.0
    if text.endswith("m"):
        return float(text[:-1])
    return float(text) * 1000.0


def _parse_memory_bytes(value: str) -> float:
    text = str(value)
    units = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
    }
    for suffix, multiplier in units.items():
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * multiplier
    return float(text)


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (percentile / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _queue_depth(metrics: Sequence[Mapping[str, Any]]) -> int | None:
    for metric in metrics:
        external = metric.get("external") or {}
        current = external.get("current") or {}
        for key in ("averageValue", "value"):
            raw = current.get(key)
            if raw is None:
                continue
            try:
                return int(str(raw))
            except ValueError:
                continue
    return None
