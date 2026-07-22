"""Pinned Argo Rollouts demo configuration for disposable validation clusters."""

from dataclasses import dataclass

from testbeds.models import (
    EnvironmentRelease,
    FaultSpecification,
    FaultType,
    LoadProfile,
    WorkloadSelector,
)


@dataclass(frozen=True, slots=True)
class ArgoRolloutsEnvironmentConfiguration:
    repository: str
    commit_sha: str
    adapter_version: str
    images: dict[str, str]
    approval_gating_supported: bool = False

    def release(self) -> EnvironmentRelease:
        return EnvironmentRelease(
            environment="argo-rollouts",
            adapter_version=self.adapter_version,
            repository=self.repository,
            commit_sha=self.commit_sha,
            package_version="rollouts-demo-canary",
            package_digest=self.image_digests["blue"],
        )

    @property
    def image_digests(self) -> dict[str, str]:
        return {name: image.rsplit("@", 1)[1] for name, image in self.images.items()}

    @property
    def stable_image(self) -> str:
        return self.images["blue"]

    @property
    def canary_image(self) -> str:
        return self.images["yellow"]

    @property
    def smoke_load(self) -> LoadProfile:
        return LoadProfile(concurrent_users=5)

    @property
    def smoke_fault(self) -> FaultSpecification:
        return FaultSpecification(
            FaultType.SERVICE_FAILURE, WorkloadSelector("canary"), 1
        )


ARGO_ROLLOUTS_ENVIRONMENT = ArgoRolloutsEnvironmentConfiguration(
    repository="https://github.com/argoproj/rollouts-demo.git",
    commit_sha="f528fdd2189e877dfb8a2de21b6989853e8e8d26",
    adapter_version="1.0.0",
    images={
        "blue": "argoproj/rollouts-demo:blue@sha256:3225193a6415b14b3fcdd160c40248b2bfd62f8c77326480559b91a41ced6e20",
        "yellow": "argoproj/rollouts-demo:yellow@sha256:12a1c7e694f9dcbf5c44f1f642905dea45128227f450e695d900c241ff853c09",
        "bad-orange": "argoproj/rollouts-demo:bad-orange@sha256:df8075ab8459caf95e2067930747089a9d80fca309ca89819994a2f0522888fe",
        "green": "argoproj/rollouts-demo:green@sha256:e32df3d15f759d36c323b3dccb7003d38df1a4274d37217715151f085c24c58f",
    },
)
