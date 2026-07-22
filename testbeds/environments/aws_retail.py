"""Immutable AWS Containers Retail Sample testbed configuration."""

from dataclasses import dataclass

from testbeds.models import (
    EnvironmentRelease,
    FaultSpecification,
    FaultType,
    LoadProfile,
    WorkloadSelector,
)


@dataclass(frozen=True, slots=True)
class AwsRetailEnvironmentConfiguration:
    repository: str
    commit_sha: str
    package_version: str
    package_digest: str
    adapter_version: str
    required_workloads: tuple[str, ...]
    image_digests: dict[str, str]

    def release(self) -> EnvironmentRelease:
        return EnvironmentRelease(
            environment="aws-retail",
            adapter_version=self.adapter_version,
            repository=self.repository,
            commit_sha=self.commit_sha,
            package_version=self.package_version,
            package_digest=self.package_digest,
        )

    @property
    def smoke_load(self) -> LoadProfile:
        return LoadProfile(concurrent_users=5)

    @property
    def smoke_fault(self) -> FaultSpecification:
        return FaultSpecification(
            fault_type=FaultType.DEPENDENCY_UNAVAILABLE,
            target=WorkloadSelector(role="cache"),
            magnitude=1.0,
        )


AWS_RETAIL_ENVIRONMENT = AwsRetailEnvironmentConfiguration(
    repository="https://github.com/aws-containers/retail-store-sample-app.git",
    commit_sha="d7fa670befd8e6dc16893b689df3a18984290914",
    package_version="1.6.1",
    package_digest="sha256:0645e10c7ee287d6e106258f18a829414cabd7648f770ac0800b8d58f9998f61",
    adapter_version="1.0.0",
    required_workloads=("frontend", "catalog", "cart", "orders", "checkout"),
    image_digests={
        "ui": "sha256:0ba8d67825bfca421bb83644f7b3aa7f5430fde71342dfa0dbf970c1b3f06fb8",
        "catalog": "sha256:60ffeb5729e42752403e85b73a5b64b4497cbb1afd5cfdf09edf9e4107f26a84",
        "cart": "sha256:72782485a0f4f8e06bf45230e426a5639904f89224af72573db609cadb9537af",
        "orders": "sha256:3a2b3dadf8e10d1e3334108ce0b2df2adf4315e4a6ec381b8fc73ad7f768475b",
        "checkout": "sha256:a176713d27fd62f27330e9c52b9f0508e9a50239b42b6730b2653d0659cc1f30",
        "mysql": "sha256:7dcddc01f13bab2f15cde676d44d01f61fc9f99fe7785e86196dfc07d358ae2b",
        "dynamodb": "sha256:58e980787da883cdc5b6d9c75f4b5f8946fcae9c73046a43f807b0f804f6fe4d",
        "postgresql": "sha256:09f23e02d76670d3b346a3c00aa33a27cf57aab8341eedfcdaed41459d14f5c4",
        "redis": "sha256:2b35fc7d2908e25aa6aa197f97882c8a67829d3b106ad5ea5c8028f816f26aa8",
        "load-generator": "sha256:5bc77c492c71853cbdba03bab6318a19be8bf696e50953f39f9323778704f0cd",
    },
)
