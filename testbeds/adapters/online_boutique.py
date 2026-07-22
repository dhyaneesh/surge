"""Online Boutique validation adapter; never imported by Guardian services."""

import asyncio
import json
import re
import secrets
from datetime import timedelta
from pathlib import Path

from testbeds.adapters.command_runner import (
    AllowlistedCommandRunner,
    CommandResult,
    redact,
)
from testbeds.environments.online_boutique import ONLINE_BOUTIQUE_ENVIRONMENT
from testbeds.models import (
    BaselineCheck,
    BaselineState,
    ChangedResource,
    DeploymentEvent,
    DeploymentSpecification,
    DiagnosticArtifactReference,
    EnvironmentCapabilities,
    EnvironmentRelease,
    EnvironmentState,
    FaultExecution,
    FaultSpecification,
    FaultType,
    LoadExecution,
    LoadProfile,
    ObservedServiceIdentity,
    WorkloadState,
)

_TIMEOUT = timedelta(minutes=2)
_DNS_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_IMAGE = re.compile(r"[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+@sha256:[a-f0-9]{64}")
_ROLE_BY_NAME = {
    "emailservice": "email",
    "checkoutservice": "checkout",
    "recommendationservice": "recommendation",
    "frontend": "frontend",
    "paymentservice": "payment",
    "productcatalogservice": "catalog",
    "cartservice": "cart",
    "loadgenerator": "load-generator",
    "currencyservice": "currency",
    "shippingservice": "shipping",
    "redis-cart": "cache",
    "adservice": "advertising",
}
_NAME_BY_ROLE = {role: name for name, role in _ROLE_BY_NAME.items()}


class OnlineBoutiqueAdapter:
    capabilities = EnvironmentCapabilities(
        fault_types=frozenset(
            {
                FaultType.HIGH_CPU,
                FaultType.ARTIFICIAL_LATENCY,
                FaultType.DEPENDENCY_UNAVAILABLE,
            }
        ),
        adjustable_load=True,
        version_deployment=True,
    )

    def __init__(
        self,
        *,
        workspace: Path,
        runner: AllowlistedCommandRunner | None = None,
        run_id: str | None = None,
        namespace: str | None = None,
        baseline_poll_seconds: float = 2,
    ):
        self._runner = runner or AllowlistedCommandRunner()
        self._workspace = Path(workspace)
        self._source = self._workspace / "source"
        suffix = self._safe_name(run_id or secrets.token_hex(6))
        self.namespace = namespace or f"guardian-online-boutique-{suffix}"
        self._validate_name(self.namespace, "namespace")
        if baseline_poll_seconds < 0:
            raise ValueError("baseline_poll_seconds must not be negative")
        self._baseline_poll_seconds = baseline_poll_seconds
        self._release = ONLINE_BOUTIQUE_ENVIRONMENT.release()
        self._created_resources: set[tuple[str, str]] = set()
        self._original_replicas: dict[str, int] = {}
        self._original_images: dict[str, str] = {}
        self._active_faults: set[FaultType] = set()
        self._changed: list[ChangedResource] = []
        self._diagnostics: list[DiagnosticArtifactReference] = []
        self._history: list[dict[str, object]] = []
        self._load_active = False
        self._cluster_mutated = False
        self._cleaned = False
        self.contaminated = False

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")
        cleaned = re.sub(r"-+", "-", cleaned)[:36].rstrip("-")
        if not cleaned:
            raise ValueError("run_id must contain a DNS-label character")
        return cleaned

    @staticmethod
    def _validate_name(value: str, field: str) -> None:
        if len(value) > 63 or not _DNS_LABEL.fullmatch(value):
            raise ValueError(f"{field} must be a valid DNS label")

    async def install(self, release: EnvironmentRelease) -> EnvironmentState:
        if release != ONLINE_BOUTIQUE_ENVIRONMENT.release():
            raise ValueError("release does not match central pinned configuration")
        try:
            await self._prepare_source()
            head = await self._run(
                ["git", "rev-parse", "HEAD"], cwd=self._source, timeout=_TIMEOUT
            )
            if head.stdout.strip() != release.commit_sha:
                raise RuntimeError("checked-out HEAD does not match pinned commit")
            manifest = self._pinned_manifest(
                (self._source / "release" / "kubernetes-manifests.yaml").read_text(
                    encoding="utf-8"
                )
            )
            self._cluster_mutated = True
            await self._run(
                ["kubectl", "apply", "-f", "-"],
                timeout=_TIMEOUT,
                input_text=json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "Namespace",
                        "metadata": {
                            "name": self.namespace,
                            "labels": {"guardian.test/environment": "online-boutique"},
                        },
                    }
                ),
            )
            await self._run(
                ["kubectl", "apply", "-f", "-", "-n", self.namespace],
                timeout=timedelta(minutes=5),
                input_text=manifest,
            )
            self._changed.append(
                ChangedResource("Namespace", self.namespace, self.namespace, "installed")
            )
            self._cleaned = False
            return await self.observe_state()
        except Exception as error:
            if self._cluster_mutated:
                await self._capture_diagnostics("install", error)
                try:
                    await self.cleanup()
                except Exception as cleanup_error:
                    error.add_note(f"cleanup also failed: {redact(str(cleanup_error))}")
            raise

    async def _prepare_source(self) -> None:
        if (self._source / ".git").exists():
            await self._run(
                ["git", "checkout", "--detach", self._release.commit_sha],
                cwd=self._source,
                timeout=_TIMEOUT,
            )
            return
        self._source.parent.mkdir(parents=True, exist_ok=True)
        await self._run(
            ["git", "clone", "--no-checkout", self._release.repository, str(self._source)],
            timeout=timedelta(minutes=5),
        )
        await self._run(
            ["git", "checkout", "--detach", self._release.commit_sha],
            cwd=self._source,
            timeout=_TIMEOUT,
        )

    def _pinned_manifest(self, manifest: str) -> str:
        version = self._release.package_version
        for name in ONLINE_BOUTIQUE_ENVIRONMENT.workload_names:
            if name == "redis-cart":
                continue
            source = f"gcr.io/google-samples/microservices-demo/{name}:{version}"
            manifest = manifest.replace(
                source,
                f"{source}@{ONLINE_BOUTIQUE_ENVIRONMENT.image_digests[name]}",
            )
        manifest = manifest.replace(
            "redis:alpine",
            "redis:7.2.4-alpine@"
            + ONLINE_BOUTIQUE_ENVIRONMENT.image_digests["redis-cart"],
        )
        manifest = manifest.replace(
            "busybox:latest",
            "busybox:1.36.1@" + ONLINE_BOUTIQUE_ENVIRONMENT.image_digests["busybox"],
        )
        images = re.findall(r"^\s*image:\s*(\S+)\s*$", manifest, re.MULTILINE)
        if not images or any("@sha256:" not in image for image in images):
            raise RuntimeError("upstream manifest contains an unpinned image")
        return manifest

    async def observe_state(self) -> EnvironmentState:
        try:
            deployments = await self._json_get("deployments")
            services = await self._json_get("services")
            pods = await self._json_get("pods")
            endpoints = await self._json_get("endpoints")
            workloads = tuple(
                self._workload(item) for item in deployments.get("items", [])
            )
            identities = tuple(
                ObservedServiceIdentity(
                    item.role,
                    item.name,
                    self._image_version(item.image),
                    self._image_digest(item.image),
                )
                for item in workloads
            )
            expected = set(ONLINE_BOUTIQUE_ENVIRONMENT.workload_names)
            observed = {item.name for item in workloads}
            required_services = expected - {"loadgenerator"}
            service_names = {
                item.get("metadata", {}).get("name")
                for item in services.get("items", [])
            }
            endpoint_names = {
                item.get("metadata", {}).get("name")
                for item in endpoints.get("items", [])
                if any(subset.get("addresses") for subset in item.get("subsets", []))
            }
            workloads_ready = bool(workloads) and all(
                item.ready_replicas >= item.desired_replicas
                and item.observed_generation == item.desired_generation
                for item in workloads
            )
            pods_ready = bool(pods.get("items")) and all(
                self._pod_ready(item) for item in pods.get("items", [])
            )
            healthy = (
                expected <= observed
                and required_services <= service_names
                and required_services <= endpoint_names
                and workloads_ready
                and pods_ready
                and not self._active_faults
                and not self.contaminated
            )
            return EnvironmentState(
                environment="online-boutique",
                namespace=self.namespace,
                release=self._release,
                workloads=workloads,
                services=identities,
                healthy=healthy,
                contaminated=self.contaminated,
                changed_resources=tuple(self._changed),
                diagnostics=tuple(self._diagnostics),
            )
        except Exception as error:
            await self._capture_diagnostics("observe", error)
            raise

    async def _json_get(self, resource: str) -> dict:
        self._validate_name(resource, "resource")
        result = await self._run(
            ["kubectl", "get", resource, "-n", self.namespace, "-o", "json"],
            timeout=_TIMEOUT,
        )
        return json.loads(result.stdout or '{"items": []}')

    def _workload(self, raw: dict) -> WorkloadState:
        metadata = raw.get("metadata", {})
        spec = raw.get("spec", {})
        status = raw.get("status", {})
        name = metadata.get("name", "")
        self._validate_name(name, "workload name")
        containers = spec.get("template", {}).get("spec", {}).get("containers", [])
        image = next((item.get("image") for item in containers if item.get("image")), None)
        return WorkloadState(
            role=_ROLE_BY_NAME.get(name, "application-service"),
            name=name,
            desired_replicas=spec.get("replicas", 1),
            ready_replicas=status.get("availableReplicas", 0),
            observed_generation=status.get("observedGeneration"),
            desired_generation=metadata.get("generation"),
            image=image,
        )

    @staticmethod
    def _pod_ready(pod: dict) -> bool:
        return any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in pod.get("status", {}).get("conditions", [])
        )

    async def wait_for_healthy_baseline(self, timeout: timedelta) -> BaselineState:
        if timeout.total_seconds() <= 0:
            raise ValueError("timeout must be positive")
        deadline = asyncio.get_running_loop().time() + timeout.total_seconds()
        stable = 0
        last_state = None
        checks: tuple[BaselineCheck, ...] = ()
        while asyncio.get_running_loop().time() < deadline:
            last_state = await self.observe_state()
            stable = stable + 1 if last_state.healthy else 0
            checks = self._baseline_checks(last_state, stable)
            if all(check.passed for check in checks):
                return BaselineState(True, checks=checks, environment=last_state)
            await asyncio.sleep(
                min(
                    self._baseline_poll_seconds,
                    max(0, deadline - asyncio.get_running_loop().time()),
                )
            )
        error = TimeoutError("healthy baseline did not remain stable before timeout")
        await self._capture_diagnostics("baseline", error)
        raise error

    def _baseline_checks(
        self, state: EnvironmentState, stable: int
    ) -> tuple[BaselineCheck, ...]:
        expected = set(ONLINE_BOUTIQUE_ENVIRONMENT.workload_names)
        observed = {item.name for item in state.workloads}
        workloads_ready = bool(state.workloads) and all(
            item.ready_replicas >= item.desired_replicas
            and item.observed_generation == item.desired_generation
            for item in state.workloads
        )
        return (
            BaselineCheck("workloads_ready", expected <= observed and workloads_ready, "kubernetes deployments"),
            BaselineCheck("required_endpoints", state.healthy, "kubernetes services and endpoints"),
            BaselineCheck("pods_ready", state.healthy, "kubernetes pod conditions"),
            BaselineCheck("stable_readiness", stable >= 2, "two consecutive observations"),
            BaselineCheck("faults_clear", not self._active_faults, "adapter state"),
            BaselineCheck("environment_clean", not self.contaminated, "adapter state"),
        )

    async def apply_load(self, profile: LoadProfile) -> LoadExecution:
        replicas = {5: 1, 10: 2, 25: 5, 50: 10}.get(profile.concurrent_users)
        if replicas is None:
            raise ValueError("concurrent_users must be one of 5, 10, 25, or 50")
        original = await self._get_named("deployment", "loadgenerator")
        self._original_replicas.setdefault(
            "loadgenerator", original.get("spec", {}).get("replicas", 1)
        )
        await self._run(
            [
                "kubectl",
                "scale",
                "deployment/loadgenerator",
                f"--replicas={replicas}",
                "-n",
                self.namespace,
            ],
            timeout=_TIMEOUT,
        )
        self._load_active = True
        change = ChangedResource("Deployment", "loadgenerator", self.namespace, "scaled")
        self._changed.append(change)
        return LoadExecution(profile, True, (change,))

    async def inject_fault(self, fault: FaultSpecification) -> FaultExecution:
        if fault.fault_type not in self.capabilities.fault_types:
            raise ValueError(f"unsupported fault: {fault.fault_type}")
        if not 0 < fault.magnitude <= 1:
            raise ValueError("fault magnitude must be greater than zero and at most one")
        try:
            if fault.fault_type is FaultType.DEPENDENCY_UNAVAILABLE:
                if fault.target.role != "cache":
                    raise ValueError("Redis failure requires the cache role")
                kind, name = "Deployment", "redis-cart"
                original = await self._get_named("deployment", name)
                self._original_replicas.setdefault(
                    name, original.get("spec", {}).get("replicas", 1)
                )
                await self._run(
                    ["kubectl", "scale", f"deployment/{name}", "--replicas=0", "-n", self.namespace],
                    timeout=_TIMEOUT,
                )
            else:
                workload = self._fault_target(fault.target.role)
                if fault.fault_type is FaultType.HIGH_CPU:
                    kind, name = "StressChaos", "guardian-service-cpu"
                    manifest = self._stress_chaos(name, workload, fault.magnitude)
                    resource_kind = "stresschaos"
                else:
                    kind, name = "NetworkChaos", "guardian-grpc-latency"
                    manifest = self._network_chaos(name, workload, fault.magnitude)
                    resource_kind = "networkchaos"
                self._created_resources.add((resource_kind, name))
                await self._apply_manifest(manifest)
            self._active_faults.add(fault.fault_type)
            change = ChangedResource(kind, name, self.namespace, "fault-applied")
            self._changed.append(change)
            return FaultExecution(fault, True, (change,))
        except Exception as error:
            await self._capture_diagnostics("inject-fault", error)
            raise

    def _fault_target(self, role: str) -> str:
        if role in {"cache", "load-generator"} or role not in _NAME_BY_ROLE:
            raise ValueError("fault target must be an Online Boutique service role")
        return _NAME_BY_ROLE[role]

    def _stress_chaos(self, name: str, workload: str, magnitude: float) -> dict:
        return {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "StressChaos",
            "metadata": {"name": name, "namespace": self.namespace},
            "spec": {
                "mode": "all",
                "selector": {"namespaces": [self.namespace], "labelSelectors": {"app": workload}},
                "stressors": {"cpu": {"workers": 1, "load": max(1, round(magnitude * 100))}},
                "duration": "10m",
            },
        }

    def _network_chaos(self, name: str, workload: str, magnitude: float) -> dict:
        return {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "NetworkChaos",
            "metadata": {"name": name, "namespace": self.namespace},
            "spec": {
                "action": "delay",
                "mode": "all",
                "selector": {"namespaces": [self.namespace], "labelSelectors": {"app": workload}},
                "delay": {"latency": f"{max(1, round(magnitude * 1000))}ms"},
                "duration": "10m",
            },
        }

    async def _apply_manifest(self, manifest: dict) -> None:
        metadata = manifest.get("metadata", {})
        self._validate_name(metadata.get("name", ""), "resource name")
        if metadata.get("namespace") != self.namespace:
            raise ValueError("manifest namespace must match adapter namespace")
        await self._run(
            ["kubectl", "apply", "-f", "-", "-n", self.namespace],
            timeout=_TIMEOUT,
            input_text=json.dumps(manifest),
        )

    async def _get_named(self, kind: str, name: str) -> dict:
        self._validate_name(kind, "resource kind")
        self._validate_name(name, "resource name")
        result = await self._run(
            ["kubectl", "get", kind, name, "-n", self.namespace, "-o", "json"],
            timeout=_TIMEOUT,
        )
        return json.loads(result.stdout or "{}")

    async def deploy_version(
        self, deployment: DeploymentSpecification
    ) -> DeploymentEvent:
        if not deployment.image_digest:
            raise ValueError("deployment image digest is required")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", deployment.image_digest):
            raise ValueError("image_digest must be an immutable sha256 digest")
        image = f"{deployment.version}@{deployment.image_digest}"
        if not _IMAGE.fullmatch(image) or ":latest" in image:
            raise ValueError("image reference must be validated and immutable")
        state = await self.observe_state()
        target = next(
            (item for item in state.workloads if item.role == deployment.target.role),
            None,
        )
        if target is None:
            raise ValueError("deployment target role was not observed")
        if target.image:
            self._original_images.setdefault(target.name, target.image)
        try:
            await self._run(
                ["kubectl", "set", "image", f"deployment/{target.name}", f"*={image}", "-n", self.namespace],
                timeout=_TIMEOUT,
            )
            await self._run(
                ["kubectl", "rollout", "status", f"deployment/{target.name}", "-n", self.namespace, "--timeout=2m"],
                timeout=_TIMEOUT,
            )
        except Exception as error:
            await self._capture_diagnostics("deploy-version", error)
            raise
        change = ChangedResource("Deployment", target.name, self.namespace, "updated")
        self._changed.append(change)
        return DeploymentEvent(
            deployment.target,
            self._image_version(target.image),
            deployment.version,
            changed_resources=(change,),
        )

    async def reset(self) -> None:
        try:
            for kind, name in sorted(self._created_resources):
                await self._run(
                    ["kubectl", "delete", kind, name, "-n", self.namespace, "--ignore-not-found=true"],
                    timeout=_TIMEOUT,
                )
            for name, replicas in sorted(self._original_replicas.items()):
                await self._run(
                    ["kubectl", "scale", f"deployment/{name}", f"--replicas={replicas}", "-n", self.namespace],
                    timeout=_TIMEOUT,
                )
            for name, image in sorted(self._original_images.items()):
                await self._run(
                    ["kubectl", "set", "image", f"deployment/{name}", f"*={image}", "-n", self.namespace],
                    timeout=_TIMEOUT,
                )
            self._created_resources.clear()
            self._original_replicas.clear()
            self._original_images.clear()
            self._active_faults.clear()
            self._load_active = False
            self.contaminated = False
        except Exception as error:
            self.contaminated = True
            await self._capture_diagnostics("reset", error)
            raise

    async def cleanup(self) -> None:
        if self._cleaned:
            return
        try:
            await self._run(
                ["kubectl", "delete", "namespace", self.namespace, "--ignore-not-found=true", "--wait=true", "--timeout=10m"],
                timeout=timedelta(minutes=11),
            )
            self._cleaned = True
            self._cluster_mutated = False
            self._created_resources.clear()
            self._original_replicas.clear()
            self._original_images.clear()
            self._active_faults.clear()
            self._load_active = False
        except Exception as error:
            self.contaminated = True
            await self._capture_diagnostics("cleanup", error)
            raise

    async def _run(self, argv, *, timeout, cwd=None, input_text=None) -> CommandResult:
        safe = [redact(str(item)) for item in argv]
        try:
            result = await self._runner.run(
                argv, timeout=timeout, cwd=cwd, input_text=input_text
            )
        except Exception:
            self._history.append(
                {"argv": safe, "outcome": "failed", "timeout_seconds": timeout.total_seconds()}
            )
            raise
        self._history.append(
            {"argv": safe, "outcome": "succeeded", "returncode": result.returncode, "duration_seconds": result.duration_seconds}
        )
        return result

    async def _capture_diagnostics(self, operation: str, error: Exception) -> None:
        directory = self._workspace / "diagnostics" / f"{operation}-{len(self._diagnostics) + 1}"
        directory.mkdir(parents=True, exist_ok=True)
        commands = (
            ("resources", ["kubectl", "get", "deployments,services,pods,endpoints", "-n", self.namespace, "-o", "wide"]),
            ("events", ["kubectl", "get", "events", "-n", self.namespace, "-o", "json"]),
            ("rollouts", ["kubectl", "get", "deployments", "-n", self.namespace, "-o", "json"]),
            ("logs", ["kubectl", "logs", "-n", self.namespace, "-l", "app", "--all-containers=true", "--tail=100", "--limit-bytes=262144"]),
        )
        for category, argv in commands:
            try:
                result = await self._runner.run(argv, timeout=_TIMEOUT)
                content = result.stdout[-262144:] + (("\nSTDERR:\n" + result.stderr[-32768:]) if result.stderr else "")
            except Exception as diagnostic_error:
                content = f"diagnostic collection failed: {diagnostic_error}"
            path = directory / f"{category}.txt"
            path.write_text(redact(content), encoding="utf-8")
            self._diagnostics.append(DiagnosticArtifactReference(category, str(path)))
        state_path = directory / "adapter-state.json"
        state_path.write_text(
            json.dumps(
                {
                    "operation": operation,
                    "error": redact(str(error)),
                    "namespace": self.namespace,
                    "release": {
                        "repository": self._release.repository,
                        "commit_sha": self._release.commit_sha,
                        "package_version": self._release.package_version,
                        "package_digest": self._release.package_digest,
                        "adapter_version": self._release.adapter_version,
                    },
                    "active_faults": sorted(item.value for item in self._active_faults),
                    "load_active": self._load_active,
                    "contaminated": self.contaminated,
                    "history": self._history[-50:],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self._diagnostics.append(DiagnosticArtifactReference("adapter-state", str(state_path)))

    @staticmethod
    def _image_version(image: str | None) -> str | None:
        if not image:
            return None
        tagged = image.split("@", 1)[0]
        final = tagged.rsplit("/", 1)[-1]
        return final.rsplit(":", 1)[1] if ":" in final else None

    @staticmethod
    def _image_digest(image: str | None) -> str | None:
        if image and "@" in image:
            return image.rsplit("@", 1)[1]
        return None
