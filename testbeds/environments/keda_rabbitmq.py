"""Immutable KEDA RabbitMQ sample configuration for disposable clusters."""

from dataclasses import dataclass

from testbeds.models import EnvironmentRelease, FaultSpecification, FaultType, LoadProfile, WorkloadSelector


@dataclass(frozen=True, slots=True)
class KedaRabbitMqEnvironmentConfiguration:
    repository: str = "https://github.com/kedacore/sample-go-rabbitmq.git"
    commit_sha: str = "ce5dcd6a1a8050847bd0f136b15bb68a5aadcfd8"
    adapter_version: str = "1.0.0"
    helm_chart_version: str = "15.3.3"
    consumer_image: str = "ghcr.io/kedacore/rabbitmq-client:v1.0@sha256:8b7a1c965d26ea6617ab4c0a9f5bcd9f021bf9c6af086be1b5f46d1d5f40c9c1"
    rabbitmq_image: str = "docker.io/bitnami/rabbitmq:3.12.12-debian-12-r0@sha256:3e652677d5e50ec76065fe352ae9ee8549a88e4cf0db6c8cf3b4970e4d6e6a11"

    def release(self) -> EnvironmentRelease:
        return EnvironmentRelease("keda-rabbitmq", self.adapter_version, self.repository, self.commit_sha, self.helm_chart_version, self.rabbitmq_image.rsplit("@", 1)[1])

    @property
    def smoke_load(self) -> LoadProfile:
        return LoadProfile(5)

    @property
    def smoke_fault(self) -> FaultSpecification:
        return FaultSpecification(FaultType.DEPENDENCY_UNAVAILABLE, WorkloadSelector("rabbitmq"))


KEDA_RABBITMQ_ENVIRONMENT = KedaRabbitMqEnvironmentConfiguration()
