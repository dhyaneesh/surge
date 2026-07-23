"""Argo Rollouts demo validation adapter; never imported by Guardian services."""

import asyncio
import json
import re
import secrets
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from testbeds.adapters.command_runner import (
    AllowlistedCommandRunner,
    CommandResult,
    redact,
)
from testbeds.environments.argo_rollouts import ARGO_ROLLOUTS_ENVIRONMENT
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
    RolloutState,
    WorkloadState,
)

_TIMEOUT = timedelta(minutes=2)
_ROLLOUT = "canary-demo"
_PREVIEW_SERVICE = "canary-demo-preview"
_FAULT_IMAGES = {FaultType.SERVICE_FAILURE: "bad-orange"}


class ArgoRolloutsDemoAdapter:
    capabilities = EnvironmentCapabilities(
        fault_types=frozenset(_FAULT_IMAGES),
        adjustable_load=True,
        version_deployment=True,
    )

    def __init__(
        self,
        *,
        workspace: Path,
        runner=None,
        run_id: str | None = None,
        namespace: str | None = None,
        baseline_poll_seconds: float = 2,
    ):
        self._runner = runner or AllowlistedCommandRunner()
        self._workspace, self._source = Path(workspace), Path(workspace) / "source"
        suffix = self._safe_name(run_id or secrets.token_hex(6))
        self.namespace = namespace or f"guardian-argo-rollouts-{suffix}"
        if (
            not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", self.namespace)
            or len(self.namespace) > 63
        ):
            raise ValueError("namespace must be a valid DNS label")
        if baseline_poll_seconds < 0:
            raise ValueError("baseline_poll_seconds must not be negative")
        self._baseline_poll_seconds, self._release = (
            baseline_poll_seconds,
            ARGO_ROLLOUTS_ENVIRONMENT.release(),
        )
        self._original_image: str | None = None
        self._active_faults: set[FaultType] = set()
        self._created: set[tuple[str, str]] = set()
        self._changed: list[ChangedResource] = []
        self._diagnostics: list[DiagnosticArtifactReference] = []
        self._history: list[dict[str, object]] = []
        self._cleaned = False
        self.contaminated = False

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")
        cleaned = re.sub(r"-+", "-", cleaned)[:36].rstrip("-")
        if not cleaned:
            raise ValueError("run_id must contain a DNS-label character")
        return cleaned

    async def install(self, release: EnvironmentRelease) -> EnvironmentState:
        if release != self._release:
            raise ValueError("release does not match central pinned configuration")
        try:
            await self._prepare_source()
            head = await self._run(
                ["git", "rev-parse", "HEAD"], cwd=self._source, timeout=_TIMEOUT
            )
            if head.stdout.strip() != release.commit_sha:
                raise RuntimeError("checked-out HEAD does not match pinned commit")
            await self._run(
                ["kubectl", "apply", "-f", "-"],
                timeout=_TIMEOUT,
                input_text=json.dumps(
                    {
                        "apiVersion": "v1",
                        "kind": "Namespace",
                        "metadata": {
                            "name": self.namespace,
                            "labels": {"guardian.test/environment": "argo-rollouts"},
                        },
                    }
                ),
            )
            rendered = await self._run(
                ["kubectl", "kustomize", str(self._source / "examples" / "canary")],
                timeout=_TIMEOUT,
            )
            await self._apply_manifest(self._pin_images(rendered.stdout))
            self._changed.append(
                ChangedResource(
                    "Namespace", self.namespace, self.namespace, "installed"
                )
            )
            self._cleaned = False
            return await self.observe_state()
        except Exception as error:
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

    @staticmethod
    def _pin_images(manifest: str) -> str:
        pinned = manifest.replace(
            "argoproj/rollouts-demo:blue", ARGO_ROLLOUTS_ENVIRONMENT.stable_image
        )
        images = re.findall(r"^\s*image:\s*(\S+)\s*$", pinned, re.MULTILINE)
        if not images or any("@sha256:" not in image for image in images):
            raise RuntimeError(
                "rendered Argo Rollouts fixture contains an unpinned image"
            )
        return pinned

    async def observe_state(self) -> EnvironmentState:
        try:
            rollouts, replica_sets, pods, services, endpoints = await asyncio.gather(
                *(
                    self._json_get(resource)
                    for resource in (
                        "rollouts",
                        "replicasets",
                        "pods",
                        "services",
                        "endpoints",
                    )
                )
            )
            desired_rollouts = tuple(
                self._rollout(item)
                for item in rollouts.get("items", [])
                if item.get("metadata", {}).get("name") == _ROLLOUT
            )
            workloads = tuple(
                self._replica_set(
                    item, desired_rollouts[0] if desired_rollouts else None
                )
                for item in replica_sets.get("items", [])
            )
            normalized_rollouts = tuple(
                self._with_observed_versions(item, workloads)
                for item in desired_rollouts
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
            endpoint_names = {
                item.get("metadata", {}).get("name")
                for item in endpoints.get("items", [])
                if any(s.get("addresses") for s in item.get("subsets", []))
            }
            services_present = {
                item.get("metadata", {}).get("name")
                for item in services.get("items", [])
            }
            pods_ready = bool(pods.get("items")) and all(
                self._pod_ready(item) for item in pods["items"]
            )
            rollout_healthy = bool(normalized_rollouts) and all(
                item.recovery_healthy for item in normalized_rollouts
            )
            healthy = (
                rollout_healthy
                and bool(workloads)
                and all(
                    item.ready_replicas >= item.desired_replicas for item in workloads
                )
                and pods_ready
                and _PREVIEW_SERVICE in services_present
                and _PREVIEW_SERVICE in endpoint_names
                and not self._active_faults
                and not self.contaminated
            )
            return EnvironmentState(
                environment="argo-rollouts",
                namespace=self.namespace,
                release=self._release,
                workloads=workloads,
                services=identities,
                rollouts=normalized_rollouts,
                healthy=healthy,
                contaminated=self.contaminated,
                changed_resources=tuple(self._changed),
                diagnostics=tuple(self._diagnostics),
                available_endpoints=frozenset(endpoint_names),
                pods_ready=pods_ready,
            )
        except Exception as error:
            await self._capture_diagnostics("observe", error)
            raise

    async def _json_get(self, resource: str) -> dict:
        result = await self._run(
            ["kubectl", "get", resource, "-n", self.namespace, "-o", "json"],
            timeout=_TIMEOUT,
        )
        return json.loads(result.stdout or '{"items": []}')

    def _rollout(self, raw: dict) -> RolloutState:
        spec, status = raw.get("spec", {}), raw.get("status", {})
        image = self._first_image(spec)
        desired, ready = spec.get("replicas", 1), status.get("readyReplicas", 0)
        phase, paused = status.get("phase"), bool(status.get("paused", False))
        return RolloutState(
            _ROLLOUT,
            phase,
            paused,
            desired,
            ready,
            status.get("updatedReplicas", 0),
            status.get("unavailableReplicas", 0),
            status.get("stableRS"),
            status.get("currentPodHash"),
            tuple(
                f"{item.get('type')}={item.get('status')}"
                for item in status.get("conditions", [])
            ),
            image,
            None,
            False,
        )

    @staticmethod
    def _with_observed_versions(
        rollout: RolloutState, workloads: tuple[WorkloadState, ...]
    ) -> RolloutState:
        stable = next((item for item in workloads if item.role == "stable"), None)
        observed = stable.image if stable else None
        return replace(
            rollout,
            observed_image=observed,
            recovery_healthy=(
                rollout.phase == "Healthy"
                and not rollout.paused
                and rollout.ready_replicas >= rollout.desired_replicas
                and rollout.unavailable_replicas == 0
                and observed == ARGO_ROLLOUTS_ENVIRONMENT.stable_image
            ),
        )

    def _replica_set(self, raw: dict, rollout: RolloutState | None) -> WorkloadState:
        metadata, spec, status = (
            raw.get("metadata", {}),
            raw.get("spec", {}),
            raw.get("status", {}),
        )
        labels = metadata.get("labels", {})
        hash_value = labels.get("rollouts-pod-template-hash")
        role = "stable" if rollout and hash_value == rollout.stable_hash else "canary"
        return WorkloadState(
            role,
            metadata.get("name", ""),
            spec.get("replicas", 1),
            status.get("readyReplicas", 0),
            image=self._first_image(spec),
        )

    @staticmethod
    def _first_image(spec: dict) -> str | None:
        containers = spec.get("template", {}).get("spec", {}).get("containers", [])
        return next(
            (item.get("image") for item in containers if item.get("image")), None
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
        deadline, stable = (
            asyncio.get_running_loop().time() + timeout.total_seconds(),
            0,
        )
        while asyncio.get_running_loop().time() < deadline:
            state = await self.observe_state()
            stable = stable + 1 if state.healthy else 0
            checks = self._baseline_checks(state, stable)
            if all(item.passed for item in checks):
                return BaselineState(True, checks=checks, environment=state)
            await asyncio.sleep(
                min(
                    self._baseline_poll_seconds,
                    max(0, deadline - asyncio.get_running_loop().time()),
                )
            )
        error = TimeoutError(
            "healthy Argo Rollouts baseline did not converge before timeout"
        )
        await self._capture_diagnostics("baseline", error)
        raise error

    def _baseline_checks(
        self, state: EnvironmentState, stable: int
    ) -> tuple[BaselineCheck, ...]:
        rollout = state.rollouts[0] if state.rollouts else None
        return (
            BaselineCheck(
                "rollout_healthy",
                bool(rollout and rollout.recovery_healthy),
                "Argo Rollouts status",
            ),
            BaselineCheck(
                "stable_replicaset_ready",
                any(
                    item.role == "stable"
                    and item.ready_replicas >= item.desired_replicas
                    for item in state.workloads
                ),
                "ReplicaSet status",
            ),
            BaselineCheck(
                "service_available",
                _PREVIEW_SERVICE in state.available_endpoints,
                "Kubernetes endpoints",
            ),
            BaselineCheck(
                "expected_version",
                bool(
                    rollout
                    and rollout.observed_image == ARGO_ROLLOUTS_ENVIRONMENT.stable_image
                ),
                "Rollout pod template",
            ),
            BaselineCheck(
                "stable_observation", stable >= 2, "two consecutive observations"
            ),
            BaselineCheck("faults_clear", not self._active_faults, "adapter state"),
        )

    async def apply_load(self, profile: LoadProfile) -> LoadExecution:
        if profile.concurrent_users not in {5, 10, 25, 50}:
            raise ValueError("concurrent_users must be one of 5, 10, 25, or 50")
        name = "guardian-rollouts-load"
        manifest = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": self.namespace,
                "labels": {"guardian.test/load": "argo-rollouts"},
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": name}},
                "template": {
                    "metadata": {"labels": {"app": name}},
                    "spec": {
                        "containers": [
                            {
                                "name": "load",
                                "image": ARGO_ROLLOUTS_ENVIRONMENT.images["green"],
                                "command": [
                                    "sh",
                                    "-c",
                                    f"while true; do wget -q -O- http://{_PREVIEW_SERVICE}:8080 >/dev/null; sleep {max(1, 50 // profile.concurrent_users)}; done",
                                ],
                            }
                        ]
                    },
                },
            },
        }
        await self._apply_manifest(json.dumps(manifest))
        self._created.add(("deployment", name))
        change = ChangedResource("Deployment", name, self.namespace, "load-applied")
        self._changed.append(change)
        return LoadExecution(profile, True, (change,))

    async def inject_fault(self, fault: FaultSpecification) -> FaultExecution:
        if (
            fault.fault_type not in _FAULT_IMAGES
            or fault.target.role != "canary"
            or not 0 < fault.magnitude <= 1
        ):
            raise ValueError("unsupported Argo Rollouts fault or target")
        try:
            current = await self._get_rollout()
            self._original_image = self._original_image or self._first_image(
                current.get("spec", {})
            )
            image = ARGO_ROLLOUTS_ENVIRONMENT.images[_FAULT_IMAGES[fault.fault_type]]
            await self._set_rollout_image(image)
            self._active_faults.add(fault.fault_type)
            change = ChangedResource(
                "Rollout", _ROLLOUT, self.namespace, "fault-applied"
            )
            self._changed.append(change)
            return FaultExecution(fault, True, (change,))
        except Exception as error:
            await self._capture_diagnostics("inject-fault", error)
            raise

    async def deploy_version(
        self, deployment: DeploymentSpecification
    ) -> DeploymentEvent:
        if (
            deployment.target.role != "canary"
            or not deployment.image_digest
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", deployment.image_digest)
        ):
            raise ValueError("canary deployment requires an immutable image digest")
        if (
            "@sha256:" not in deployment.version
            or deployment.version.rsplit("@", 1)[1] != deployment.image_digest
        ):
            raise ValueError(
                "deployment version must include the supplied immutable digest"
            )
        current = await self._get_rollout()
        previous = self._first_image(current.get("spec", {}))
        self._original_image = self._original_image or previous
        try:
            await self._set_rollout_image(deployment.version)
        except Exception as error:
            await self._capture_diagnostics("deploy-version", error)
            raise
        change = ChangedResource("Rollout", _ROLLOUT, self.namespace, "canary-deployed")
        self._changed.append(change)
        return DeploymentEvent(
            deployment.target,
            self._image_version(previous),
            deployment.version,
            changed_resources=(change,),
        )

    async def _get_rollout(self) -> dict:
        result = await self._run(
            ["kubectl", "get", "rollout", _ROLLOUT, "-n", self.namespace, "-o", "json"],
            timeout=_TIMEOUT,
        )
        return json.loads(result.stdout or "{}")

    async def _set_rollout_image(self, image: str) -> None:
        await self._run(
            [
                "kubectl",
                "argo",
                "rollouts",
                "set",
                "image",
                _ROLLOUT,
                f"canary-demo={image}",
                "-n",
                self.namespace,
            ],
            timeout=_TIMEOUT,
        )

    async def reset(self) -> None:
        try:
            await self._run(
                [
                    "kubectl",
                    "argo",
                    "rollouts",
                    "abort",
                    _ROLLOUT,
                    "-n",
                    self.namespace,
                ],
                timeout=_TIMEOUT,
            )
            await self._run(
                [
                    "kubectl",
                    "delete",
                    "analysisruns",
                    "--all",
                    "-n",
                    self.namespace,
                    "--ignore-not-found=true",
                ],
                timeout=_TIMEOUT,
            )
            await self._run(
                [
                    "kubectl",
                    "delete",
                    "experiments",
                    "--all",
                    "-n",
                    self.namespace,
                    "--ignore-not-found=true",
                ],
                timeout=_TIMEOUT,
            )
            for kind, name in sorted(self._created):
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
            await self._set_rollout_image(ARGO_ROLLOUTS_ENVIRONMENT.stable_image)
            await self._run(
                [
                    "kubectl",
                    "argo",
                    "rollouts",
                    "promote",
                    _ROLLOUT,
                    "--full",
                    "-n",
                    self.namespace,
                ],
                timeout=_TIMEOUT,
            )
            self._created.clear()
            self._active_faults.clear()
            self._original_image = None
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
            self._created.clear()
            self._active_faults.clear()
            self._original_image = None
        except Exception as error:
            self.contaminated = True
            await self._capture_diagnostics("cleanup", error)
            raise

    async def _apply_manifest(self, manifest: str) -> None:
        await self._run(
            ["kubectl", "apply", "-f", "-", "-n", self.namespace],
            timeout=timedelta(minutes=5),
            input_text=manifest,
        )

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
                "resources",
                [
                    "kubectl",
                    "get",
                    "rollouts,replicasets,pods,services,endpoints",
                    "-n",
                    self.namespace,
                    "-o",
                    "wide",
                ],
            ),
            (
                "rollout",
                [
                    "kubectl",
                    "get",
                    "rollout",
                    _ROLLOUT,
                    "-n",
                    self.namespace,
                    "-o",
                    "yaml",
                ],
            ),
            (
                "replicasets",
                ["kubectl", "get", "replicasets", "-n", self.namespace, "-o", "yaml"],
            ),
            (
                "events",
                ["kubectl", "get", "events", "-n", self.namespace, "-o", "json"],
            ),
            (
                "logs",
                [
                    "kubectl",
                    "logs",
                    "-n",
                    self.namespace,
                    "-l",
                    "app=canary-demo",
                    "--all-containers=true",
                    "--tail=100",
                    "--limit-bytes=262144",
                ],
            ),
            (
                "analysis",
                [
                    "kubectl",
                    "get",
                    "analysisruns,experiments",
                    "-n",
                    self.namespace,
                    "-o",
                    "yaml",
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
        state = directory / "adapter-state.json"
        state.write_text(
            json.dumps(
                {
                    "operation": operation,
                    "error": redact(str(error)),
                    "namespace": self.namespace,
                    "release": self._release.__dict__
                    if hasattr(self._release, "__dict__")
                    else {
                        "repository": self._release.repository,
                        "commit_sha": self._release.commit_sha,
                        "adapter_version": self._release.adapter_version,
                    },
                    "active_faults": sorted(item.value for item in self._active_faults),
                    "history": self._history[-50:],
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self._diagnostics.append(
            DiagnosticArtifactReference("adapter-state", str(state))
        )

    @staticmethod
    def _image_version(image: str | None) -> str | None:
        if not image:
            return None
        tagged = image.split("@", 1)[0].rsplit("/", 1)[-1]
        return tagged.rsplit(":", 1)[1] if ":" in tagged else None

    @staticmethod
    def _image_digest(image: str | None) -> str | None:
        return image.rsplit("@", 1)[1] if image and "@" in image else None
