"""Conservative operational capability declarations for test fixtures."""

from testbeds.scenarios.compatibility import EnvironmentDeclaration


def _declaration(environment: str, *capabilities: str) -> EnvironmentDeclaration:
    return EnvironmentDeclaration.model_validate(
        {"environment": environment, "capabilities": capabilities}
    )


ENVIRONMENT_DECLARATIONS = {
    "otel-demo": _declaration(
        "otel-demo",
        "healthy-baseline",
        "load-generation",
        "fault-injection",
        "dependency-observation",
        "workflow-observation",
        "mutation-observation",
    ),
    "aws-retail": _declaration(
        "aws-retail",
        "healthy-baseline",
        "load-generation",
        "fault-injection",
        "dependency-observation",
        "workflow-observation",
        "mutation-observation",
    ),
    "online-boutique": _declaration(
        "online-boutique",
        "healthy-baseline",
        "load-generation",
        "fault-injection",
        "dependency-observation",
        "workflow-observation",
        "mutation-observation",
    ),
    "argo-rollouts": _declaration(
        "argo-rollouts",
        "healthy-baseline",
        "deployment-transition",
        "progressive-delivery",
        "workflow-observation",
        "mutation-observation",
        "recovery-observation",
    ),
    "keda-rabbitmq": _declaration(
        "keda-rabbitmq",
        "healthy-baseline",
        "load-generation",
        "horizontal-scaling",
        "scale-to-zero",
        "scaler-observation",
        "mutation-observation",
    ),
}
