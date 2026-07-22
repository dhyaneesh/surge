import inspect
import unittest
from datetime import timedelta

from testbeds.adapters.base import EnvironmentAdapter
from testbeds.models import (
    BaselineState,
    DeploymentEvent,
    DeploymentSpecification,
    EnvironmentRelease,
    EnvironmentState,
    FaultExecution,
    FaultSpecification,
    LoadExecution,
    LoadProfile,
)


class EnvironmentAdapterContractTests(unittest.TestCase):
    def test_protocol_exposes_normative_async_operations(self) -> None:
        expected = {
            "install": (EnvironmentRelease, EnvironmentState),
            "reset": (None, None),
            "wait_for_healthy_baseline": (timedelta, BaselineState),
            "apply_load": (LoadProfile, LoadExecution),
            "inject_fault": (FaultSpecification, FaultExecution),
            "deploy_version": (DeploymentSpecification, DeploymentEvent),
            "observe_state": (None, EnvironmentState),
            "cleanup": (None, None),
        }

        for method_name, (parameter_type, return_type) in expected.items():
            method = inspect.getattr_static(EnvironmentAdapter, method_name)
            self.assertTrue(inspect.iscoroutinefunction(method), method_name)
            signature = inspect.signature(method, eval_str=True)
            parameters = list(signature.parameters.values())
            self.assertEqual("self", parameters[0].name)
            if parameter_type is None:
                self.assertEqual(1, len(parameters), method_name)
            else:
                self.assertEqual(2, len(parameters), method_name)
                self.assertIs(parameter_type, parameters[1].annotation)
            self.assertIs(return_type, signature.return_annotation)

    def test_protocol_is_structurally_runtime_checkable(self) -> None:
        class CompleteAdapter:
            async def install(self, release): ...
            async def reset(self): ...
            async def wait_for_healthy_baseline(self, timeout): ...
            async def apply_load(self, profile): ...
            async def inject_fault(self, fault): ...
            async def deploy_version(self, deployment): ...
            async def observe_state(self): ...
            async def cleanup(self): ...

        self.assertIsInstance(CompleteAdapter(), EnvironmentAdapter)

    def test_models_are_distinct_constructible_types(self) -> None:
        model_types = {
            EnvironmentRelease,
            EnvironmentState,
            BaselineState,
            LoadProfile,
            LoadExecution,
            FaultSpecification,
            FaultExecution,
            DeploymentSpecification,
            DeploymentEvent,
        }
        self.assertEqual(9, len(model_types))
        for model_type in model_types:
            self.assertIsInstance(model_type(), model_type)


if __name__ == "__main__":
    unittest.main()
