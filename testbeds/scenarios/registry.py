"""Environment-specific adapter construction isolated from the scenario engine."""

from __future__ import annotations

from pathlib import Path

from testbeds.adapters.argo_rollouts import ArgoRolloutsDemoAdapter
from testbeds.adapters.aws_retail import AwsRetailAdapter
from testbeds.adapters.command_runner import CommandRunner
from testbeds.adapters.keda_rabbitmq import KedaRabbitMqAdapter
from testbeds.adapters.online_boutique import OnlineBoutiqueAdapter
from testbeds.adapters.otel_demo import OpenTelemetryDemoAdapter
from testbeds.environments.argo_rollouts import ARGO_ROLLOUTS_ENVIRONMENT
from testbeds.environments.aws_retail import AWS_RETAIL_ENVIRONMENT
from testbeds.environments.capabilities import ENVIRONMENT_DECLARATIONS
from testbeds.environments.keda_rabbitmq import KEDA_RABBITMQ_ENVIRONMENT
from testbeds.environments.online_boutique import ONLINE_BOUTIQUE_ENVIRONMENT
from testbeds.environments.otel_demo import OTEL_DEMO_ENVIRONMENT
from testbeds.models import FaultType
from testbeds.scenarios.execution import AdapterRegistration


SUPPORTED_ENVIRONMENTS = (
    "otel-demo",
    "aws-retail",
    "online-boutique",
    "argo-rollouts",
    "keda-rabbitmq",
)


def build_adapter_registration(
    environment: str,
    *,
    workspace: Path,
    runner: CommandRunner | None = None,
    run_id: str | None = None,
) -> AdapterRegistration:
    common = {"workspace": workspace, "runner": runner, "run_id": run_id}
    if environment == "otel-demo":
        return AdapterRegistration(
            environment,
            OpenTelemetryDemoAdapter(**common),
            OTEL_DEMO_ENVIRONMENT.release(),
            ENVIRONMENT_DECLARATIONS[environment],
            {"request-processor": "transaction-processor"},
            {item: "transaction-processor" for item in FaultType},
            {},
        )
    if environment == "aws-retail":
        return AdapterRegistration(
            environment,
            AwsRetailAdapter(**common),
            AWS_RETAIL_ENVIRONMENT.release(),
            ENVIRONMENT_DECLARATIONS[environment],
            {"request-processor": "checkout", "data-store": "database"},
            {
                FaultType.DEPENDENCY_UNAVAILABLE: "cache",
                FaultType.HIGH_CPU: "database",
                FaultType.ARTIFICIAL_LATENCY: "checkout",
            },
            {},
        )
    if environment == "online-boutique":
        return AdapterRegistration(
            environment,
            OnlineBoutiqueAdapter(**common),
            ONLINE_BOUTIQUE_ENVIRONMENT.release(),
            ENVIRONMENT_DECLARATIONS[environment],
            {"request-processor": "checkout"},
            {
                FaultType.DEPENDENCY_UNAVAILABLE: "cache",
                FaultType.HIGH_CPU: "cart",
                FaultType.ARTIFICIAL_LATENCY: "checkout",
            },
            {},
        )
    if environment == "argo-rollouts":
        return AdapterRegistration(
            environment,
            ArgoRolloutsDemoAdapter(**common),
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
        )
    if environment == "keda-rabbitmq":
        return AdapterRegistration(
            environment,
            KedaRabbitMqAdapter(**common),
            KEDA_RABBITMQ_ENVIRONMENT.release(),
            ENVIRONMENT_DECLARATIONS[environment],
            {"request-processor": "consumer"},
            {
                FaultType.QUEUE_LAG: "consumer",
                FaultType.DEPENDENCY_UNAVAILABLE: "rabbitmq",
            },
            {},
        )
    raise ValueError(
        f"unknown environment {environment!r}; expected one of {', '.join(SUPPORTED_ENVIRONMENTS)}"
    )
