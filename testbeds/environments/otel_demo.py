"""Pinned OpenTelemetry Demo configuration.

Source-derived checks come from the upstream chart/workloads and feature-flag
definitions. Inferred checks are Guardian testbed convergence criteria required
by ``testbeds/AGENTS.md`` but not promised by upstream as an install contract.
"""

from dataclasses import dataclass

from testbeds.models import (
    EnvironmentRelease,
    FaultSpecification,
    FaultType,
    LoadProfile,
    WorkloadSelector,
)


@dataclass(frozen=True, slots=True)
class OpenTelemetryDemoEnvironmentConfiguration:
    repository: str
    commit_sha: str
    chart: str
    chart_version: str
    chart_digest: str
    adapter_version: str
    source_derived_checks: tuple[str, ...]
    inferred_checks: tuple[str, ...]

    def release(self) -> EnvironmentRelease:
        return EnvironmentRelease(
            environment="otel-demo",
            adapter_version=self.adapter_version,
            repository=self.repository,
            commit_sha=self.commit_sha,
            package_version=self.chart_version,
            package_digest=self.chart_digest,
        )

    @property
    def smoke_load(self) -> LoadProfile:
        return LoadProfile(concurrent_users=10)

    @property
    def smoke_fault(self) -> FaultSpecification:
        return FaultSpecification(
            fault_type=FaultType.SERVICE_FAILURE,
            target=WorkloadSelector(role="transaction-processor"),
        )


OTEL_DEMO_ENVIRONMENT = OpenTelemetryDemoEnvironmentConfiguration(
    repository="https://github.com/open-telemetry/opentelemetry-demo.git",
    commit_sha="92df8a2783543d18e2446a924f2578c83d8a506c",
    chart="opentelemetry-helm/opentelemetry-demo",
    chart_version="0.40.10",
    chart_digest="sha256:56031427872101cbfce7974156de28c3b3223083d69abc7c4ead6902bc568717",
    adapter_version="1.0.0",
    source_derived_checks=(
        "all chart deployments report available replicas",
        "frontend and load-generator workloads are present",
        "loadGeneratorTraffic and loadGeneratorVUs flags are at baseline values",
    ),
    inferred_checks=(
        "deployment generations have converged",
        "observed service identities include version or image digest",
        "no adapter-managed fault is active",
    ),
)
