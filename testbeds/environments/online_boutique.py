"""Immutable Online Boutique testbed configuration."""

from dataclasses import dataclass

from testbeds.models import (
    EnvironmentRelease,
    FaultSpecification,
    FaultType,
    LoadProfile,
    WorkloadSelector,
)


@dataclass(frozen=True, slots=True)
class OnlineBoutiqueEnvironmentConfiguration:
    repository: str
    commit_sha: str
    package_version: str
    package_digest: str
    adapter_version: str
    workload_names: tuple[str, ...]
    image_digests: dict[str, str]

    def release(self) -> EnvironmentRelease:
        return EnvironmentRelease(
            environment="online-boutique",
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
            FaultType.DEPENDENCY_UNAVAILABLE,
            WorkloadSelector("cache"),
            1.0,
        )


ONLINE_BOUTIQUE_ENVIRONMENT = OnlineBoutiqueEnvironmentConfiguration(
    repository="https://github.com/GoogleCloudPlatform/microservices-demo.git",
    commit_sha="d138db567079e2ef982d46b2993f8349cb18e2b2",
    package_version="v0.9.0",
    package_digest="sha256:1a23e5e76be455ebbea50d20aaf3e7b05e3a0b96f7d62af6bf646af4ee958b74",
    adapter_version="1.0.0",
    workload_names=(
        "emailservice",
        "checkoutservice",
        "recommendationservice",
        "frontend",
        "paymentservice",
        "productcatalogservice",
        "cartservice",
        "loadgenerator",
        "currencyservice",
        "shippingservice",
        "redis-cart",
        "adservice",
    ),
    image_digests={
        "emailservice": "sha256:93e25b258931d3148b1de07dc147a557af08153b75c2f1184bcc505cbf20060d",
        "checkoutservice": "sha256:b660e1a48bfd95854fb769113dd453811bb8470b7b58b420efead6fad5b5df61",
        "recommendationservice": "sha256:200c10a2f26d77c98e6c6aa32ada517b91de8e75188e66cafc13f7421dd4c408",
        "frontend": "sha256:68f72fd525591c4879fb28fa9f81740ccfb7b1303e965f6dbc0ea43dbf2023fc",
        "paymentservice": "sha256:ddbec6dd0efd0d34c8d8a6de3c52aad299ec6848fdd43d46cc6d6032ef1ff7c7",
        "productcatalogservice": "sha256:1d51f9e5aa923ff9c338b1b155b39da737306c0ebc230e62ec6271f2b0cb7bef",
        "cartservice": "sha256:b0cf3662e60750e9a449fe14f5bc29f9150db0c553e52795569c477ec783974b",
        "loadgenerator": "sha256:b8542fe077d6e21dc03ca9d37a863e29140eb1af00ebb5d645c3ec05a093e5a8",
        "currencyservice": "sha256:16d16968f90aff24d4170ca7c4c0ff97303612b98c509052d5c3618f6e75937a",
        "shippingservice": "sha256:c906f4bdba49226494bdf3461ab790f878e53329ba3f9e2edde95a14581268b1",
        "redis-cart": "sha256:c8bb255c3559b3e458766db810aa7b3c7af1235b204cfdb304e79ff388fe1a5a",
        "adservice": "sha256:7a62fd95ef19b9379fbb4f8e3523ca2aa417a4039d336ef3246dca91624c60f9",
        "busybox": "sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662",
    },
)
