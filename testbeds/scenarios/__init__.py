"""Public API for generalized Guardian test scenarios."""

from testbeds.scenarios.loader import load_scenario
from testbeds.scenarios.models import GuardianScenario

__all__ = ["GuardianScenario", "load_scenario"]
