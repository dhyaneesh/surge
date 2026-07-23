"""Conservative operational capability declarations for test fixtures."""

from testbeds.evidence.contracts import EvidenceSourceKind, substantiate_capabilities
from testbeds.scenarios.compatibility import EnvironmentDeclaration
from testbeds.scenarios.v1alpha2 import EnvironmentCapability


def _declaration(
    environment: str,
    capabilities: tuple[str, ...],
    evidence_sources: tuple[str, ...],
) -> EnvironmentDeclaration:
    declared_capabilities = frozenset(
        EnvironmentCapability(item) for item in capabilities
    )
    declared_sources = frozenset(EvidenceSourceKind(item) for item in evidence_sources)
    substantiated = substantiate_capabilities(declared_capabilities, declared_sources)
    return EnvironmentDeclaration.model_validate(
        {
            "environment": environment,
            "capabilities": sorted(item.value for item in substantiated),
            "evidenceSources": sorted(item.value for item in declared_sources),
        }
    )


_COMMON_SOURCES = (
    EvidenceSourceKind.ENDPOINT_PROBE.value,
    EvidenceSourceKind.KUBERNETES_WORKLOAD.value,
    EvidenceSourceKind.METRICS_API.value,
)

ENVIRONMENT_DECLARATIONS = {
    "otel-demo": _declaration(
        "otel-demo",
        (
            "healthy-baseline",
            "load-generation",
            "fault-injection",
            "dependency-observation",
            "horizontal-scaling",
            "workflow-observation",
            "mutation-observation",
            "recovery-observation",
        ),
        _COMMON_SOURCES,
    ),
    "aws-retail": _declaration(
        "aws-retail",
        (
            "healthy-baseline",
            "load-generation",
            "fault-injection",
            "dependency-observation",
            "workflow-observation",
            "mutation-observation",
        ),
        _COMMON_SOURCES,
    ),
    "online-boutique": _declaration(
        "online-boutique",
        (
            "healthy-baseline",
            "load-generation",
            "fault-injection",
            "dependency-observation",
            "horizontal-scaling",
            "workflow-observation",
            "mutation-observation",
            "recovery-observation",
        ),
        _COMMON_SOURCES,
    ),
    "argo-rollouts": _declaration(
        "argo-rollouts",
        (
            "healthy-baseline",
            "deployment-transition",
            "progressive-delivery",
            "workflow-observation",
            "mutation-observation",
            "recovery-observation",
        ),
        (
            EvidenceSourceKind.ENDPOINT_PROBE.value,
            EvidenceSourceKind.KUBERNETES_WORKLOAD.value,
            EvidenceSourceKind.ROLLOUT_STATE.value,
        ),
    ),
    "keda-rabbitmq": _declaration(
        "keda-rabbitmq",
        (
            "healthy-baseline",
            "load-generation",
            "horizontal-scaling",
            "scale-to-zero",
            "scaler-observation",
            "workflow-observation",
            "mutation-observation",
        ),
        (
            EvidenceSourceKind.ENDPOINT_PROBE.value,
            EvidenceSourceKind.KUBERNETES_WORKLOAD.value,
            EvidenceSourceKind.RABBITMQ_QUEUE.value,
            EvidenceSourceKind.KEDA_SCALER.value,
        ),
    ),
}
