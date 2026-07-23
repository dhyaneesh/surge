"""Environment-specific adapter construction isolated from the scenario engine."""

from __future__ import annotations

import os
from pathlib import Path

from testbeds.adapters.argo_rollouts import ArgoRolloutsDemoAdapter
from testbeds.adapters.aws_retail import AwsRetailAdapter
from testbeds.adapters.command_runner import CommandRunner
from testbeds.adapters.keda_rabbitmq import KedaRabbitMqAdapter
from testbeds.adapters.online_boutique import OnlineBoutiqueAdapter
from testbeds.adapters.otel_demo import OpenTelemetryDemoAdapter
from testbeds.evidence.collector import EvidenceCollector
from testbeds.evidence.signoz import SignozEvidenceClient, UrllibHttpProbeRunner
from testbeds.environments.argo_rollouts import ARGO_ROLLOUTS_ENVIRONMENT
from testbeds.environments.aws_retail import AWS_RETAIL_ENVIRONMENT
from testbeds.environments.capabilities import ENVIRONMENT_DECLARATIONS
from testbeds.environments.keda_rabbitmq import KEDA_RABBITMQ_ENVIRONMENT
from testbeds.environments.online_boutique import ONLINE_BOUTIQUE_ENVIRONMENT
from testbeds.environments.otel_demo import OTEL_DEMO_ENVIRONMENT
from testbeds.models import FaultType
from testbeds.scenarios.evidence_provider import (
    CollectorEvidenceTargets,
    CollectorScenarioEvidenceProvider,
)
from testbeds.scenarios.execution import AdapterRegistration


SUPPORTED_ENVIRONMENTS = (
    "otel-demo",
    "aws-retail",
    "online-boutique",
    "argo-rollouts",
    "keda-rabbitmq",
)


def _signoz_contract(
    http_runner: UrllibHttpProbeRunner,
) -> SignozEvidenceClient | None:
    """Build the approved fixed-query contract only when both endpoints exist."""
    otlp_endpoint = os.getenv("GUARDIAN_SIGNOZ_OTLP_ENDPOINT")
    query_endpoint = os.getenv("GUARDIAN_SIGNOZ_QUERY_ENDPOINT")
    if not otlp_endpoint or not query_endpoint:
        return None
    return SignozEvidenceClient(
        otlp_endpoint=otlp_endpoint,
        query_endpoint=query_endpoint,
        http_runner=http_runner,
    )


def _endpoint_url(*, endpoint_service: str, namespace: str) -> str:
    full_url = os.getenv("GUARDIAN_EVIDENCE_ENDPOINT_URL")
    if full_url:
        return full_url
    base_url = os.getenv("GUARDIAN_EVIDENCE_ENDPOINT_BASE")
    if base_url:
        return f"{base_url.rstrip('/')}/{endpoint_service}"
    return f"http://{endpoint_service}.{namespace}.svc.cluster.local"


def _evidence_provider(
    adapter,
    *,
    environment: str,
    workload_kind: str,
    workload_name: str,
    workload_role: str,
    endpoint_service: str,
    rollout_name: str | None = None,
    scaled_object_name: str | None = None,
) -> CollectorScenarioEvidenceProvider:
    """Bind probes to the disposable namespace created by an adapter."""
    namespace = adapter.namespace
    http_runner = UrllibHttpProbeRunner()
    return CollectorScenarioEvidenceProvider(
        collector=EvidenceCollector(
            command_runner=adapter._runner,
            http_runner=http_runner,
        ),
        targets=CollectorEvidenceTargets(
            namespace=namespace,
            endpoint_url=_endpoint_url(
                endpoint_service=endpoint_service, namespace=namespace
            ),
            workload_kind=workload_kind,
            workload_name=workload_name,
            workload_role=workload_role,
            # Resource Metrics API evidence requires GUARDIAN_EVIDENCE_METRICS_POD
            # (with optional configured resource limits); absent a pod target it
            # fails closed rather than fabricating utilization.
            metrics_pod_name=os.getenv("GUARDIAN_EVIDENCE_METRICS_POD"),
            rollout_name=rollout_name,
            scaled_object_name=scaled_object_name,
            environment=environment,
            service_name=endpoint_service,
            signoz_contract=_signoz_contract(http_runner),
        ),
    )


def build_adapter_registration(
    environment: str,
    *,
    workspace: Path,
    runner: CommandRunner | None = None,
    run_id: str | None = None,
) -> AdapterRegistration:
    if environment not in SUPPORTED_ENVIRONMENTS:
        raise ValueError(
            f"unknown environment {environment!r}; expected one of {', '.join(SUPPORTED_ENVIRONMENTS)}"
        )
    common = {"workspace": workspace, "runner": runner, "run_id": run_id}
    if environment == "otel-demo":
        adapter = OpenTelemetryDemoAdapter(**common)
        return AdapterRegistration(
            environment,
            adapter,
            OTEL_DEMO_ENVIRONMENT.release(),
            ENVIRONMENT_DECLARATIONS[environment],
            {"request-processor": "transaction-processor"},
            {item: "transaction-processor" for item in FaultType},
            {},
            _evidence_provider(
                adapter,
                environment=environment,
                workload_kind="deployment",
                workload_name="checkout",
                workload_role="request-processor",
                endpoint_service="frontend",
            ),
        )
    if environment == "aws-retail":
        adapter = AwsRetailAdapter(**common)
        return AdapterRegistration(
            environment,
            adapter,
            AWS_RETAIL_ENVIRONMENT.release(),
            ENVIRONMENT_DECLARATIONS[environment],
            {"request-processor": "checkout", "data-store": "database"},
            {
                FaultType.DEPENDENCY_UNAVAILABLE: "cache",
                FaultType.HIGH_CPU: "database",
                FaultType.ARTIFICIAL_LATENCY: "checkout",
            },
            {},
            _evidence_provider(
                adapter,
                environment=environment,
                workload_kind="deployment",
                workload_name="checkout",
                workload_role="request-processor",
                endpoint_service="ui",
            ),
        )
    if environment == "online-boutique":
        adapter = OnlineBoutiqueAdapter(**common)
        return AdapterRegistration(
            environment,
            adapter,
            ONLINE_BOUTIQUE_ENVIRONMENT.release(),
            ENVIRONMENT_DECLARATIONS[environment],
            {"request-processor": "checkout"},
            {
                FaultType.DEPENDENCY_UNAVAILABLE: "cache",
                FaultType.HIGH_CPU: "cart",
                FaultType.ARTIFICIAL_LATENCY: "checkout",
            },
            {},
            _evidence_provider(
                adapter,
                environment=environment,
                workload_kind="deployment",
                workload_name="checkoutservice",
                workload_role="request-processor",
                endpoint_service="frontend",
            ),
        )
    if environment == "argo-rollouts":
        adapter = ArgoRolloutsDemoAdapter(**common)
        return AdapterRegistration(
            environment,
            adapter,
            ARGO_ROLLOUTS_ENVIRONMENT.release(),
            ENVIRONMENT_DECLARATIONS[environment],
            {"request-processor": "canary"},
            {FaultType.SERVICE_FAILURE: "canary"},
            {
                "error-regression": (
                    ARGO_ROLLOUTS_ENVIRONMENT.images["bad-orange"],
                    ARGO_ROLLOUTS_ENVIRONMENT.image_digests["bad-orange"],
                ),
                "latency-regression": (
                    ARGO_ROLLOUTS_ENVIRONMENT.images["yellow"],
                    ARGO_ROLLOUTS_ENVIRONMENT.image_digests["yellow"],
                ),
            },
            _evidence_provider(
                adapter,
                environment=environment,
                workload_kind="rollout",
                workload_name="canary-demo",
                workload_role="canary",
                endpoint_service="canary-demo-preview",
                rollout_name="canary-demo",
            ),
        )
    if environment == "keda-rabbitmq":
        adapter = KedaRabbitMqAdapter(**common)
        return AdapterRegistration(
            environment,
            adapter,
            KEDA_RABBITMQ_ENVIRONMENT.release(),
            ENVIRONMENT_DECLARATIONS[environment],
            {"request-processor": "consumer"},
            {
                FaultType.QUEUE_LAG: "consumer",
                FaultType.DEPENDENCY_UNAVAILABLE: "rabbitmq",
            },
            {},
            _evidence_provider(
                adapter,
                environment=environment,
                workload_kind="deployment",
                workload_name="rabbitmq-consumer",
                workload_role="consumer",
                endpoint_service="rabbitmq",
                scaled_object_name="rabbitmq-consumer",
            ),
        )
    raise ValueError(
        f"unknown environment {environment!r}; expected one of {', '.join(SUPPORTED_ENVIRONMENTS)}"
    )
