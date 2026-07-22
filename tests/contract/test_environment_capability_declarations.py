from testbeds.environments.capabilities import ENVIRONMENT_DECLARATIONS


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
