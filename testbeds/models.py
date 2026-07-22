"""Opaque typed values used by the test-only environment adapter contract.

Section 21 names these values but does not yet define their fields. Keeping the
records empty avoids inventing a data contract before the normative specification
defines one.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EnvironmentRelease:
    pass


@dataclass(frozen=True, slots=True)
class EnvironmentState:
    pass


@dataclass(frozen=True, slots=True)
class BaselineState:
    pass


@dataclass(frozen=True, slots=True)
class LoadProfile:
    pass


@dataclass(frozen=True, slots=True)
class LoadExecution:
    pass


@dataclass(frozen=True, slots=True)
class FaultSpecification:
    pass


@dataclass(frozen=True, slots=True)
class FaultExecution:
    pass


@dataclass(frozen=True, slots=True)
class DeploymentSpecification:
    pass


@dataclass(frozen=True, slots=True)
class DeploymentEvent:
    pass
