"""AWS Retail validation adapter; never imported by Guardian services."""

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
from testbeds.environments.aws_retail import AWS_RETAIL_ENVIRONMENT
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
_IMAGE = re.compile(r"[A-Za-z0-9._/-]+(?::[A-Za-z0-9._-]+)?(?:@sha256:[a-f0-9]{64})?")
_RELEASES = ("catalog", "carts", "orders", "checkout", "ui")
_ROLE_BY_NAME = {
    "ui": "frontend",
    "catalog": "catalog",
    "carts": "cart",
    "cart": "cart",
    "orders": "orders",
    "checkout": "checkout",
    "catalog-mysql": "database",
    "orders-postgresql": "database",
    "checkout-redis": "cache",
}


class AwsRetailAdapter:
    capabilities = EnvironmentCapabilities(
        fault_types=frozenset(
            {
                FaultType.HIGH_CPU,
                FaultType.DEPENDENCY_UNAVAILABLE,
                FaultType.ARTIFICIAL_LATENCY,
            }
        ),
        adjustable_load=True,
        version_deployment=True,
    )

    def __init__(
        self,
        *,
        runner: AllowlistedCommandRunner | None = None,
        workspace: Path,
        run_id: str | None = None,
        namespace: str | None = None,
        baseline_poll_seconds: float = 2,
    ):
        self._runner = runner or AllowlistedCommandRunner()
        self._workspace = Path(workspace)
        self._source = self._workspace / "source"
        suffix = self._safe_name(run_id or secrets.token_hex(6))
        self.namespace = namespace or f"guardian-aws-retail-{suffix}"
        self._validate_name(self.namespace, "namespace")
        if baseline_poll_seconds < 0:
            raise ValueError("baseline_poll_seconds must not be negative")
        self._baseline_poll_seconds = baseline_poll_seconds
        self._release = AWS_RETAIL_ENVIRONMENT.release()
        self._created_resources: set[tuple[str, str]] = set()
        self._original_replicas: dict[str, int] = {}
        self._original_images: dict[str, str] = {}
        self._changed: list[ChangedResource] = []
        self._diagnostics: list[DiagnosticArtifactReference] = []
        self._history: list[dict[str, object]] = []
        self._last_state: EnvironmentState | None = None
        self._load_active = False
        self._active_faults: set[FaultType] = set()
        self._cluster_mutated = False
        self._cleaned = False
        self.contaminated = False

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")
        cleaned = re.sub(r"-+", "-", cleaned)[:40].rstrip("-")
        if not cleaned:
            raise ValueError("run_id must contain a DNS-label character")
        return cleaned

    @staticmethod
    def _validate_name(value: str, field: str) -> None:
        if len(value) > 63 or not _DNS_LABEL.fullmatch(value):
            raise ValueError(f"{field} must be a valid DNS label")

    async def install(self, release: EnvironmentRelease) -> EnvironmentState:
        if release != AWS_RETAIL_ENVIRONMENT.release():
            raise ValueError("release does not match central pinned configuration")
        try:
            await self._prepare_source()
            head = await self._run(
                ["git", "rev-parse", "HEAD"], cwd=self._source, timeout=_TIMEOUT
            )
            if head.stdout.strip() != release.commit_sha:
                raise RuntimeError("checked-out HEAD does not match pinned commit")
            for name in _RELEASES:
                # Helm may create resources before a transport-level failure is
                # reported, so cleanup must be armed before the first mutation.
                self._cluster_mutated = True
                await self._install_chart(name)
            self._cleaned = False
            self._changed.append(
                ChangedResource(
                    "Namespace", self.namespace, self.namespace, "installed"
                )
            )
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
            [
                "git",
                "clone",
                "--no-checkout",
                self._release.repository,
                str(self._source),
            ],
            timeout=timedelta(minutes=5),
        )
        await self._run(
            ["git", "checkout", "--detach", self._release.commit_sha],
            cwd=self._source,
            timeout=_TIMEOUT,
        )

    async def _install_chart(self, name: str) -> None:
        chart_name = "cart" if name == "carts" else name
        chart = self._source / "src" / chart_name / "chart"
        digest = AWS_RETAIL_ENVIRONMENT.image_digests[chart_name]
        values = ["--set", f"image.tag={self._release.package_version}@{digest}"]
        if name == "catalog":
            values += [
                "--set",
                "app.persistence.provider=mysql",
                "--set",
                "mysql.create=true",
                "--set",
                f"mysql.image.tag=8.0@{AWS_RETAIL_ENVIRONMENT.image_digests['mysql']}",
            ]
        elif name == "carts":
            values += [
                "--set",
                "app.persistence.provider=dynamodb",
                "--set",
                "dynamodb.create=true",
                "--set",
                f"dynamodb.image.tag=1.25.1@{AWS_RETAIL_ENVIRONMENT.image_digests['dynamodb']}",
            ]
        elif name == "orders":
            values += [
                "--set",
                "app.persistence.provider=postgres",
                "--set",
                "postgresql.create=true",
                "--set",
                f"postgresql.image.tag=16.1@{AWS_RETAIL_ENVIRONMENT.image_digests['postgresql']}",
            ]
        elif name == "checkout":
            values += [
                "--set",
                "app.persistence.provider=redis",
                "--set",
                "redis.create=true",
                "--set",
                f"redis.image.tag=6.0-alpine@{AWS_RETAIL_ENVIRONMENT.image_digests['redis']}",
                "--set",
                "app.endpoints.orders=http://orders:80",
            ]
        elif name == "ui":
            for service in ("catalog", "carts", "orders", "checkout"):
                values += ["--set", f"app.endpoints.{service}=http://{service}:80"]
        await self._run(
            [
                "helm",
                "upgrade",
                "--install",
                name,
                str(chart),
                "--namespace",
                self.namespace,
                "--create-namespace",
                *values,
                "--wait",
                "--timeout",
                "10m",
            ],
            timeout=timedelta(minutes=11),
        )

    async def observe_state(self) -> EnvironmentState:
        try:
            deployments = await self._json_get("deployments")
            services_raw = await self._json_get("services")
            pods = await self._json_get("pods")
            endpoints_raw = await self._json_get("endpoints")
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
            required = set(AWS_RETAIL_ENVIRONMENT.required_workloads)
            roles = {item.role for item in workloads}
            service_names = {
                item.get("metadata", {}).get("name")
                for item in services_raw.get("items", [])
            }
            endpoint_names = {
                item.get("metadata", {}).get("name")
                for item in endpoints_raw.get("items", [])
                if any(subset.get("addresses") for subset in item.get("subsets", []))
            }
            pods_ready = bool(pods.get("items")) and all(
                self._pod_ready(item) for item in pods.get("items", [])
            )
            workloads_ready = bool(workloads) and all(
                item.ready_replicas >= item.desired_replicas
                and item.observed_generation == item.desired_generation
                for item in workloads
            )
            state = EnvironmentState(
                environment="aws-retail",
                namespace=self.namespace,
                release=self._release,
                workloads=workloads,
                services=identities,
                healthy=(
                    required <= roles
                    and {"ui", "catalog", "carts", "orders", "checkout"}
                    <= service_names
                    and {"ui", "catalog", "carts", "orders", "checkout"}
                    <= endpoint_names
                    and workloads_ready
                    and pods_ready
                    and not self._active_faults
                ),
                contaminated=self.contaminated,
                changed_resources=tuple(self._changed),
                diagnostics=tuple(self._diagnostics),
            )
            self._last_state = state
            return state
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
        image = next(
            (item.get("image") for item in containers if item.get("image")), None
        )
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
            item.get("type") == "Ready" and item.get("status") == "True"
            for item in pod.get("status", {}).get("conditions", [])
        )

    async def wait_for_healthy_baseline(self, timeout: timedelta) -> BaselineState:
        if timeout.total_seconds() <= 0:
            raise ValueError("timeout must be positive")
        deadline = asyncio.get_running_loop().time() + timeout.total_seconds()
        stable = 0
        last_state = None
        last_checks: tuple[BaselineCheck, ...] = ()
        while asyncio.get_running_loop().time() < deadline:
            last_state = await self.observe_state()
            stable = stable + 1 if last_state.healthy else 0
            last_checks = self._baseline_checks(last_state, stable)
            if all(item.passed for item in last_checks):
                return BaselineState(True, checks=last_checks, environment=last_state)
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
        required = set(AWS_RETAIL_ENVIRONMENT.required_workloads)
        roles = {item.role for item in state.workloads}
        workloads_ready = bool(state.workloads) and all(
            item.ready_replicas >= item.desired_replicas
            and item.observed_generation == item.desired_generation
            for item in state.workloads
        )
        return (
            BaselineCheck("workloads_ready", workloads_ready, "kubernetes deployments"),
            BaselineCheck(
                "required_endpoints", required <= roles, "kubernetes services"
            ),
            BaselineCheck("pods_ready", state.healthy, "kubernetes pod conditions"),
            BaselineCheck(
                "stable_readiness", stable >= 2, "two consecutive observations"
            ),
            BaselineCheck("faults_clear", not self._active_faults, "adapter state"),
            BaselineCheck("environment_clean", not self.contaminated, "adapter state"),
        )

    async def apply_load(self, profile: LoadProfile) -> LoadExecution:
        if profile.concurrent_users not in {5, 10, 25, 50}:
            raise ValueError("concurrent_users must be one of 5, 10, 25, or 50")
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "guardian-load-generator",
                "namespace": self.namespace,
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "guardian-load-generator"}},
                "template": {
                    "metadata": {
                        "labels": {
                            "app": "guardian-load-generator",
                            "guardian.test/created": "true",
                        }
                    },
                    "spec": {
                        "containers": [
                            {
                                "name": "load",
                                "image": f"fortio/fortio:1.69.2@{AWS_RETAIL_ENVIRONMENT.image_digests['load-generator']}",
                                "args": [
                                    "load",
                                    "-c",
                                    str(profile.concurrent_users),
                                    "-qps",
                                    str(profile.concurrent_users),
                                    "http://ui",
                                ],
                            }
                        ]
                    },
                },
            },
        }
        self._created_resources.add(("deployment", "guardian-load-generator"))
        await self._apply_manifest(manifest)
        self._load_active = True
        change = ChangedResource(
            "Deployment", "guardian-load-generator", self.namespace, "applied"
        )
        self._changed.append(change)
        return LoadExecution(profile, True, (change,))

    async def inject_fault(self, fault: FaultSpecification) -> FaultExecution:
        if fault.fault_type not in self.capabilities.fault_types:
            raise ValueError(f"unsupported fault: {fault.fault_type}")
        if not 0 < fault.magnitude <= 1:
            raise ValueError(
                "fault magnitude must be greater than zero and at most one"
            )
        try:
            if fault.fault_type == FaultType.HIGH_CPU:
                if fault.target.role != "database":
                    raise ValueError("database saturation requires the database role")
                kind, name = "StressChaos", "guardian-database-saturation"
                self._created_resources.add(("stresschaos", name))
                await self._apply_manifest(self._stress_chaos(name, fault.magnitude))
            elif fault.fault_type == FaultType.ARTIFICIAL_LATENCY:
                if fault.target.role not in AWS_RETAIL_ENVIRONMENT.required_workloads:
                    raise ValueError("latency target must be a required workload role")
                kind, name = "NetworkChaos", "guardian-service-latency"
                self._created_resources.add(("networkchaos", name))
                await self._apply_manifest(
                    self._network_chaos(name, fault.target.role, fault.magnitude)
                )
            else:
                if fault.target.role != "cache":
                    raise ValueError("cache failure requires the cache role")
                kind, name = "Deployment", "checkout-redis"
                original = await self._get_named("deployment", name)
                self._original_replicas.setdefault(
                    name, original.get("spec", {}).get("replicas", 1)
                )
                await self._run(
                    [
                        "kubectl",
                        "scale",
                        f"deployment/{name}",
                        "--replicas=0",
                        "-n",
                        self.namespace,
                    ],
                    timeout=_TIMEOUT,
                )
            self._active_faults.add(fault.fault_type)
            change = ChangedResource(kind, name, self.namespace, "fault-applied")
            self._changed.append(change)
            return FaultExecution(fault, True, (change,))
        except Exception as error:
            await self._capture_diagnostics("inject-fault", error)
            raise

    async def _get_named(self, kind: str, name: str) -> dict:
        self._validate_name(kind, "resource kind")
        self._validate_name(name, "resource name")
        result = await self._run(
            ["kubectl", "get", kind, name, "-n", self.namespace, "-o", "json"],
            timeout=_TIMEOUT,
        )
        return json.loads(result.stdout or "{}")

    def _stress_chaos(self, name: str, magnitude: float) -> dict:
        return {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "StressChaos",
            "metadata": {"name": name, "namespace": self.namespace},
            "spec": {
                "mode": "one",
                "selector": {
                    "namespaces": [self.namespace],
                    "labelSelectors": {
                        "app.kubernetes.io/name": "catalog",
                        "app.kubernetes.io/instance": "catalog",
                        "app.kubernetes.io/component": "mysql",
                    },
                },
                "stressors": {
                    "cpu": {"workers": 1, "load": max(1, round(magnitude * 100))}
                },
                "duration": "10m",
            },
        }

    def _network_chaos(self, name: str, role: str, magnitude: float) -> dict:
        workload = "ui" if role == "frontend" else ("carts" if role == "cart" else role)
        return {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "NetworkChaos",
            "metadata": {"name": name, "namespace": self.namespace},
            "spec": {
                "action": "delay",
                "mode": "all",
                "selector": {
                    "namespaces": [self.namespace],
                    "labelSelectors": {
                        "app.kubernetes.io/name": workload,
                        "app.kubernetes.io/instance": workload,
                        "app.kubernetes.io/component": "service",
                    },
                },
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

    async def deploy_version(
        self, deployment: DeploymentSpecification
    ) -> DeploymentEvent:
        if not deployment.image_digest:
            raise ValueError("deployment image digest is required")
        state = await self.observe_state()
        target = next(
            (item for item in state.workloads if item.role == deployment.target.role),
            None,
        )
        if target is None:
            raise ValueError("deployment target role was not observed")
        image = deployment.version
        if deployment.image_digest:
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", deployment.image_digest):
                raise ValueError("image_digest must be an immutable sha256 digest")
            image = f"{image}@{deployment.image_digest}"
        if not _IMAGE.fullmatch(image) or ":latest" in image:
            raise ValueError("image reference must be validated and immutable")
        if target.image:
            self._original_images.setdefault(target.name, target.image)
        try:
            await self._run(
                [
                    "kubectl",
                    "set",
                    "image",
                    f"deployment/{target.name}",
                    f"*={image}",
                    "-n",
                    self.namespace,
                ],
                timeout=_TIMEOUT,
            )
            await self._run(
                [
                    "kubectl",
                    "rollout",
                    "status",
                    f"deployment/{target.name}",
                    "-n",
                    self.namespace,
                    "--timeout=2m",
                ],
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
                    [
                        "kubectl",
                        "delete",
                        kind,
                        name,
                        "-n",
                        self.namespace,
                        "--ignore-not-found=true",
                    ],
                    timeout=_TIMEOUT,
                )
            for name, replicas in sorted(self._original_replicas.items()):
                await self._run(
                    [
                        "kubectl",
                        "scale",
                        f"deployment/{name}",
                        f"--replicas={replicas}",
                        "-n",
                        self.namespace,
                    ],
                    timeout=_TIMEOUT,
                )
            for name, image in sorted(self._original_images.items()):
                await self._run(
                    [
                        "kubectl",
                        "set",
                        "image",
                        f"deployment/{name}",
                        f"*={image}",
                        "-n",
                        self.namespace,
                    ],
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
                [
                    "kubectl",
                    "delete",
                    "namespace",
                    self.namespace,
                    "--ignore-not-found=true",
                    "--wait=true",
                    "--timeout=10m",
                ],
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
                {
                    "argv": safe,
                    "outcome": "failed",
                    "timeout_seconds": timeout.total_seconds(),
                }
            )
            raise
        self._history.append(
            {
                "argv": safe,
                "outcome": "succeeded",
                "returncode": result.returncode,
                "duration_seconds": result.duration_seconds,
            }
        )
        return result

    async def _capture_diagnostics(self, operation: str, error: Exception) -> None:
        directory = (
            self._workspace
            / "diagnostics"
            / f"{operation}-{len(self._diagnostics) + 1}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        commands = (
            (
                "workloads-pods",
                [
                    "kubectl",
                    "get",
                    "deployments,statefulsets,pods",
                    "-n",
                    self.namespace,
                    "-o",
                    "wide",
                ],
            ),
            (
                "events",
                ["kubectl", "get", "events", "-n", self.namespace, "-o", "json"],
            ),
            (
                "rollouts",
                [
                    "kubectl",
                    "get",
                    "deployments,statefulsets",
                    "-n",
                    self.namespace,
                    "-o",
                    "json",
                ],
            ),
            (
                "logs",
                [
                    "kubectl",
                    "logs",
                    "-n",
                    self.namespace,
                    "-l",
                    "app.kubernetes.io/part-of=retail-store-sample",
                    "--all-containers=true",
                    "--tail=100",
                    "--limit-bytes=262144",
                ],
            ),
        )
        for category, argv in commands:
            try:
                result = await self._runner.run(argv, timeout=_TIMEOUT)
                content = result.stdout[-262144:] + (
                    ("\nSTDERR:\n" + result.stderr[-32768:]) if result.stderr else ""
                )
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
                    "release": self._release.commit_sha,
                    "active_load": self._load_active,
                    "active_faults": sorted(item.value for item in self._active_faults),
                    "contaminated": self.contaminated,
                    "operation_history": self._history[-200:],
                    "normalized_state": self._state_summary(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self._diagnostics.append(
            DiagnosticArtifactReference("adapter-state", str(state_path))
        )

    def _state_summary(self) -> dict | None:
        if self._last_state is None:
            return None
        return {
            "environment": self._last_state.environment,
            "namespace": self._last_state.namespace,
            "healthy": self._last_state.healthy,
            "workloads": [
                {
                    "role": item.role,
                    "name": item.name,
                    "desired_replicas": item.desired_replicas,
                    "ready_replicas": item.ready_replicas,
                    "image": item.image,
                }
                for item in self._last_state.workloads
            ],
        }

    @staticmethod
    def _image_version(image: str | None) -> str | None:
        without_digest = image.split("@", 1)[0] if image else ""
        return without_digest.rsplit(":", 1)[1] if ":" in without_digest else None

    @staticmethod
    def _image_digest(image: str | None) -> str | None:
        return image.split("@", 1)[1] if image and "@" in image else None
