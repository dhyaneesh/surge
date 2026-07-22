"""Normalized, environment-neutral models for test environment adapters."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class FaultType(StrEnum):
    SERVICE_FAILURE = "service_failure"
    PARTIAL_FAILURE = "partial_failure"
    HIGH_CPU = "high_cpu"
    MANUAL_GC = "manual_gc"
    MEMORY_LEAK = "memory_leak"
    READINESS_FAILURE = "readiness_failure"
    ARTIFICIAL_LATENCY = "artificial_latency"
    QUEUE_LAG = "queue_lag"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    NETWORK_PARTITION = "network_partition"


@dataclass(frozen=True, slots=True)
class EnvironmentRelease:
    environment: str = ""
    adapter_version: str = ""
    repository: str = ""
    commit_sha: str = ""
    package_version: str = ""
    package_digest: str = ""


@dataclass(frozen=True, slots=True)
class ChangedResource:
    kind: str
    name: str
    namespace: str
    operation: str


@dataclass(frozen=True, slots=True)
class DiagnosticArtifactReference:
    category: str
    path: str
    sanitized: bool = True


@dataclass(frozen=True, slots=True)
class WorkloadState:
    role: str
    name: str
    desired_replicas: int
    ready_replicas: int
    observed_generation: int | None = None
    desired_generation: int | None = None
    image: str | None = None


@dataclass(frozen=True, slots=True)
class ObservedServiceIdentity:
    role: str
    service_name: str
    version: str | None = None
    image_digest: str | None = None


@dataclass(frozen=True, slots=True)
class RolloutState:
    """Normalized Argo Rollout controller state for test-only environments."""

    name: str
    phase: str | None
    paused: bool
    desired_replicas: int
    ready_replicas: int
    updated_replicas: int
    unavailable_replicas: int
    stable_hash: str | None
    canary_hash: str | None
    conditions: tuple[str, ...] = ()
    desired_image: str | None = None
    observed_image: str | None = None
    recovery_healthy: bool = False


@dataclass(frozen=True, slots=True)
class EnvironmentState:
    environment: str = ""
    namespace: str = ""
    release: EnvironmentRelease = field(default_factory=EnvironmentRelease)
    workloads: tuple[WorkloadState, ...] = ()
    services: tuple[ObservedServiceIdentity, ...] = ()
    rollouts: tuple[RolloutState, ...] = ()
    healthy: bool = False
    contaminated: bool = False
    changed_resources: tuple[ChangedResource, ...] = ()
    diagnostics: tuple[DiagnosticArtifactReference, ...] = ()


@dataclass(frozen=True, slots=True)
class BaselineCheck:
    name: str
    passed: bool
    provenance: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class BaselineState:
    healthy: bool = False
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    checks: tuple[BaselineCheck, ...] = ()
    environment: EnvironmentState | None = None


@dataclass(frozen=True, slots=True)
class LoadProfile:
    concurrent_users: int = 5


@dataclass(frozen=True, slots=True)
class LoadExecution:
    profile: LoadProfile = field(default_factory=LoadProfile)
    active: bool = False
    changed_resources: tuple[ChangedResource, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkloadSelector:
    role: str = ""


@dataclass(frozen=True, slots=True)
class FaultSpecification:
    fault_type: FaultType = FaultType.SERVICE_FAILURE
    target: WorkloadSelector = field(default_factory=WorkloadSelector)
    magnitude: float = 1.0


@dataclass(frozen=True, slots=True)
class FaultExecution:
    fault: FaultSpecification = field(default_factory=FaultSpecification)
    active: bool = False
    changed_resources: tuple[ChangedResource, ...] = ()


@dataclass(frozen=True, slots=True)
class DeploymentSpecification:
    target: WorkloadSelector = field(default_factory=WorkloadSelector)
    version: str = ""
    image_digest: str | None = None


@dataclass(frozen=True, slots=True)
class DeploymentEvent:
    target: WorkloadSelector = field(default_factory=WorkloadSelector)
    from_version: str | None = None
    to_version: str = ""
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    changed_resources: tuple[ChangedResource, ...] = ()


@dataclass(frozen=True, slots=True)
class EnvironmentCapabilities:
    fault_types: frozenset[FaultType] = frozenset()
    adjustable_load: bool = False
    version_deployment: bool = False
