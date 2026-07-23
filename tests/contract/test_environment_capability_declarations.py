from testbeds.environments.capabilities import ENVIRONMENT_DECLARATIONS
from testbeds.scenarios.v1alpha2 import EnvironmentCapability


def test_all_five_environment_declarations_are_frozen_and_unique() -> None:
    assert set(ENVIRONMENT_DECLARATIONS) == {
        "otel-demo",
        "aws-retail",
        "online-boutique",
        "argo-rollouts",
        "keda-rabbitmq",
    }
    for identifier, declaration in ENVIRONMENT_DECLARATIONS.items():
        assert declaration.environment == identifier
        assert declaration.capabilities


def test_minimal_runtime_does_not_declare_action_controller_execution() -> None:
    for declaration in ENVIRONMENT_DECLARATIONS.values():
        assert (
            EnvironmentCapability.ACTION_CONTROLLER_EXECUTION
            not in declaration.capabilities
        )
