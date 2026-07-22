"""OpenTelemetry Demo validation adapter; never imported by Guardian services."""

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
from testbeds.environments.otel_demo import OTEL_DEMO_ENVIRONMENT
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
_RELEASE_NAME = "otel-demo"
_ISOLATED_HELM_VALUES = (
    "grafana.enabled=false",
    "jaeger.enabled=false",
    "prometheus.enabled=false",
    "opensearch.enabled=false",
)
_ROLE_BY_COMPONENT = {
    "checkout": "transaction-processor",
    "load-generator": "load-generator",
    "frontend": "frontend",
    "otel-collector": "telemetry-collector",
    "kafka": "message-broker",
}
_FAULT_BINDINGS = {
    FaultType.SERVICE_FAILURE: ("paymentFailure", "100%", "off"),
    FaultType.PARTIAL_FAILURE: ("cartFailure", "50%", "off"),
    FaultType.HIGH_CPU: ("adHighCpu", "on", "off"),
    FaultType.MANUAL_GC: ("adManualGc", "on", "off"),
    FaultType.MEMORY_LEAK: ("emailMemoryLeak", "100x", "off"),
    FaultType.READINESS_FAILURE: ("failedReadinessProbe", "on", "off"),
    FaultType.ARTIFICIAL_LATENCY: ("intlShippingSlowdown", "5sec", "off"),
    FaultType.QUEUE_LAG: ("kafkaQueueProblems", "on", "off"),
    FaultType.DEPENDENCY_UNAVAILABLE: ("paymentUnreachable", "on", "off"),
}


class OpenTelemetryDemoAdapter:
    capabilities = EnvironmentCapabilities(
        fault_types=frozenset(_FAULT_BINDINGS),
        adjustable_load=True,
        version_deployment=True,
    )

    def __init__(
        self,
        *,
        runner=None,
        workspace: Path,
        run_id: str | None = None,
        namespace: str | None = None,
    ):
        self._runner = runner or AllowlistedCommandRunner()
        self._workspace = Path(workspace)
        self._source = self._workspace / "source"
        suffix = self._safe_name(run_id or secrets.token_hex(6))
        self.namespace = namespace or f"guardian-otel-demo-{suffix}"
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", self.namespace):
            raise ValueError("namespace must be a valid DNS label")
        self._release = OTEL_DEMO_ENVIRONMENT.release()
        self._active_load = False
        self._active_faults: set[FaultType] = set()
        self._created_resources: set[tuple[str, str]] = set()
        self._changed: list[ChangedResource] = []
        self._diagnostics: list[DiagnosticArtifactReference] = []
        self._command_history: list[dict[str, object]] = []
        self._installed = False
        self._cleaned = False
        self.contaminated = False

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9-]", "-", value.lower()).strip("-")
        if not cleaned:
            raise ValueError("run_id must contain a DNS-label character")
        return cleaned[:40].rstrip("-")

    async def install(self, release: EnvironmentRelease) -> EnvironmentState:
        try:
            if release != OTEL_DEMO_ENVIRONMENT.release():
                raise ValueError("release does not match central pinned configuration")
            await self._prepare_source()
            head = await self._run(
                ["git", "rev-parse", "HEAD"], cwd=self._source, timeout=_TIMEOUT
            )
            if head.stdout.strip() != release.commit_sha:
                raise RuntimeError("checked-out HEAD does not match pinned commit")
            await self._run(
                [
                    "helm",
                    "upgrade",
                    "--install",
                    _RELEASE_NAME,
                    OTEL_DEMO_ENVIRONMENT.chart,
                    "--version",
                    OTEL_DEMO_ENVIRONMENT.chart_version,
                    "--namespace",
                    self.namespace,
                    "--create-namespace",
                    *(
                        item
                        for value in _ISOLATED_HELM_VALUES
                        for item in ("--set", value)
                    ),
                    "--atomic",
                    "--wait",
                    "--timeout",
                    "15m",
                ],
                timeout=timedelta(minutes=16),
            )
            self._installed = True
            self._cleaned = False
            self._changed.append(
                ChangedResource("Namespace", self.namespace, self.namespace, "installed")
            )
            return await self.observe_state()
        except Exception as error:
            try:
                await self._capture_diagnostics("install", error)
            except Exception as diagnostics_error:
                error.add_note(
                    f"install diagnostics also failed: {redact(str(diagnostics_error))}"
                )
            try:
                await self.cleanup()
            except Exception as cleanup_error:
                error.add_note(
                    f"install cleanup also failed: {redact(str(cleanup_error))}"
                )
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

    async def reset(self) -> None:
        try:
            await self._run(
                ["kubectl", "scale", "deployment/load-generator", "--replicas=0", "-n", self.namespace],
                timeout=_TIMEOUT,
            )
            self._active_load = False
            for fault_type in tuple(self._active_faults):
                await self._set_fault(fault_type, active=False)
            for kind, name in tuple(self._created_resources):
                await self._run(
                    ["kubectl", "delete", kind, name, "-n", self.namespace, "--ignore-not-found=true"],
                    timeout=_TIMEOUT,
                )
            self._created_resources.clear()
            await self._run(
                [
                    "helm", "upgrade", _RELEASE_NAME, OTEL_DEMO_ENVIRONMENT.chart,
                    "--version", OTEL_DEMO_ENVIRONMENT.chart_version,
                    "--namespace", self.namespace, "--reset-values",
                    *(item for value in _ISOLATED_HELM_VALUES for item in ("--set", value)),
                    "--wait", "--timeout", "15m",
                ],
                timeout=timedelta(minutes=16),
            )
            try:
                await self.wait_for_healthy_baseline(timedelta(minutes=10))
            except Exception as first_error:
                self.contaminated = True
                await self._capture_diagnostics("reset-contaminated", first_error)
                await self._reinstall()
                await self.wait_for_healthy_baseline(timedelta(minutes=15))
            self.contaminated = False
        except Exception as error:
            self.contaminated = True
            await self._capture_diagnostics("reset", error)
            raise

    async def _reinstall(self) -> None:
        await self._run(
            ["helm", "uninstall", _RELEASE_NAME, "--namespace", self.namespace, "--ignore-not-found"],
            timeout=timedelta(minutes=5),
        )
        self._installed = False
        await self.install(self._release)

    async def wait_for_healthy_baseline(self, timeout: timedelta) -> BaselineState:
        if timeout.total_seconds() <= 0:
            raise ValueError("timeout must be positive")
        deadline = asyncio.get_running_loop().time() + timeout.total_seconds()
        last_state = None
        while asyncio.get_running_loop().time() < deadline:
            last_state = await self.observe_state()
            checks = self._baseline_checks(last_state)
            if all(check.passed for check in checks):
                return BaselineState(True, checks=checks, environment=last_state)
            await asyncio.sleep(min(2, max(0, deadline - asyncio.get_running_loop().time())))
        error = TimeoutError("healthy baseline did not converge before timeout")
        await self._capture_diagnostics("baseline", error)
        raise error

    def _baseline_checks(self, state: EnvironmentState) -> tuple[BaselineCheck, ...]:
        ready = bool(state.workloads) and all(
            item.ready_replicas >= item.desired_replicas
            and item.observed_generation == item.desired_generation
            for item in state.workloads
        )
        roles = {item.role for item in state.workloads}
        identities = bool(state.services) and all(
            item.version or item.image_digest for item in state.services
        )
        return (
            BaselineCheck("workloads_ready", ready, "source-derived"),
            BaselineCheck("required_endpoints", {"frontend", "load-generator"} <= roles, "source-derived"),
            BaselineCheck("identity_and_version", identities, "implementation-inference"),
            BaselineCheck("faults_clear", not self._active_faults, "implementation-inference"),
            BaselineCheck("environment_clean", not self.contaminated, "implementation-inference"),
        )

    def _healthy_baseline_for_test(self) -> BaselineState:
        return BaselineState(True)

    async def apply_load(self, profile: LoadProfile) -> LoadExecution:
        if profile.concurrent_users not in {5, 10, 25, 50}:
            raise ValueError("concurrent_users must be one of 5, 10, 25, or 50")
        try:
            await self._run(
                [
                    "kubectl",
                    "set",
                    "env",
                    "deployment/load-generator",
                    f"LOCUST_USERS={profile.concurrent_users}",
                    "-n",
                    self.namespace,
                ],
                timeout=_TIMEOUT,
            )
            await self._run(
                ["kubectl", "rollout", "status", "deployment/load-generator", "-n", self.namespace, "--timeout=2m"],
                timeout=_TIMEOUT,
            )
            self._active_load = True
            change = ChangedResource("ConfigMap", "load-control", self.namespace, "reconciled")
            self._changed.append(change)
            return LoadExecution(profile, True, (change,))
        except Exception as error:
            await self._capture_diagnostics("apply-load", error)
            raise

    async def inject_fault(self, fault: FaultSpecification) -> FaultExecution:
        if fault.fault_type not in self.capabilities.fault_types:
            raise ValueError(f"unsupported fault: {fault.fault_type}")
        try:
            await self._set_fault(fault.fault_type, active=True)
            change = ChangedResource("ConfigMap", "fault-control", self.namespace, "reconciled")
            self._changed.append(change)
            return FaultExecution(fault, True, (change,))
        except Exception as error:
            await self._capture_diagnostics("inject-fault", error)
            raise

    async def _set_fault(self, fault_type: FaultType, *, active: bool) -> None:
        flag, enabled, disabled = _FAULT_BINDINGS[fault_type]
        await self._set_flag(flag, enabled if active else disabled)
        if active:
            self._active_faults.add(fault_type)
        else:
            self._active_faults.discard(fault_type)

    async def _set_flag(self, flag: str, variant: str) -> None:
        current = await self._run(
            ["kubectl", "get", "configmap", "flagd-config", "-n", self.namespace, "-o", "json"],
            timeout=_TIMEOUT,
        )
        try:
            raw_document = json.loads(current.stdout)["data"]["demo.flagd.json"]
            document = json.loads(raw_document)
        except (KeyError, TypeError, json.JSONDecodeError):
            document = {
                "$schema": "https://flagd.dev/schema/v0/flags.json",
                "flags": {
                    name: {"state": "ENABLED", "defaultVariant": disabled, "variants": {disabled: disabled, enabled: enabled}}
                    for name, enabled, disabled in _FAULT_BINDINGS.values()
                }
                | {
                    "loadGeneratorTraffic": {"state": "ENABLED", "defaultVariant": "on", "variants": {"off": 0, "on": 1}},
                    "loadGeneratorVUs": {"state": "ENABLED", "defaultVariant": "5", "variants": {key: int(key) for key in ("5", "10", "25", "50")}},
                },
            }
        if flag not in document.get("flags", {}):
            raise RuntimeError(f"upstream feature flag is missing: {flag}")
        if variant not in document["flags"][flag].get("variants", {}):
            raise ValueError(f"unsupported variant {variant!r} for normalized control")
        document["flags"][flag]["defaultVariant"] = variant
        patch = json.dumps(
            {"data": {"demo.flagd.json": json.dumps(document, separators=(",", ":"))}},
            separators=(",", ":"),
        )
        await self._run(
            ["kubectl", "patch", "configmap", "flagd-config", "-n", self.namespace, "--type", "merge", "-p", patch],
            timeout=_TIMEOUT,
        )
        # The chart copies this ConfigMap into flagd's writable volume at pod
        # start, so restart and rollout convergence are part of reconciliation.
        await self._run(
            ["kubectl", "rollout", "restart", "deployment/flagd", "-n", self.namespace],
            timeout=_TIMEOUT,
        )
        await self._run(
            ["kubectl", "rollout", "status", "deployment/flagd", "-n", self.namespace, "--timeout=2m"],
            timeout=_TIMEOUT,
        )

    async def deploy_version(self, deployment: DeploymentSpecification) -> DeploymentEvent:
        state = await self.observe_state()
        target = next((item for item in state.workloads if item.role == deployment.target.role), None)
        if target is None:
            raise ValueError("deployment target role was not observed")
        image = deployment.version
        if deployment.image_digest:
            image = f"{image}@{deployment.image_digest}"
        await self._run(
            ["kubectl", "set", "image", f"deployment/{target.name}", f"*={image}", "-n", self.namespace],
            timeout=_TIMEOUT,
        )
        change = ChangedResource("Deployment", target.name, self.namespace, "updated")
        return DeploymentEvent(deployment.target, self._image_version(target.image), deployment.version, changed_resources=(change,))

    async def observe_state(self) -> EnvironmentState:
        try:
            response = await self._run(
                ["kubectl", "get", "deployments", "-n", self.namespace, "-o", "json"],
                timeout=_TIMEOUT,
            )
            payload = json.loads(response.stdout or '{"items": []}')
            workloads = tuple(self._workload(item) for item in payload.get("items", []))
            services = tuple(
                ObservedServiceIdentity(
                    item.role,
                    item.name,
                    self._image_version(item.image),
                    self._image_digest(item.image),
                )
                for item in workloads
            )
            healthy = bool(workloads) and all(
                item.ready_replicas >= item.desired_replicas for item in workloads
            )
            return EnvironmentState(
                "otel-demo", self.namespace, self._release, workloads, services,
                healthy, self.contaminated, tuple(self._changed), tuple(self._diagnostics)
            )
        except Exception as error:
            await self._capture_diagnostics("observe", error)
            raise

    def _workload(self, raw: dict) -> WorkloadState:
        metadata = raw.get("metadata", {})
        spec = raw.get("spec", {})
        status = raw.get("status", {})
        template = spec.get("template", {})
        labels = template.get("metadata", {}).get("labels", {})
        component = labels.get("app.kubernetes.io/component") or metadata.get("name", "")
        image = next((item.get("image") for item in template.get("spec", {}).get("containers", []) if item.get("image")), None)
        return WorkloadState(
            _ROLE_BY_COMPONENT.get(component, "application-service"),
            metadata.get("name", ""),
            spec.get("replicas", 1),
            status.get("availableReplicas", 0),
            status.get("observedGeneration"),
            metadata.get("generation"),
            image,
        )

    @staticmethod
    def _image_version(image: str | None) -> str | None:
        without_digest = image.split("@", 1)[0] if image else ""
        if ":" not in without_digest:
            return None
        return without_digest.rsplit(":", 1)[1]

    @staticmethod
    def _image_digest(image: str | None) -> str | None:
        return image.split("@", 1)[1] if image and "@" in image else None

    async def cleanup(self) -> None:
        if self._cleaned:
            return
        try:
            await self._run(
                ["helm", "uninstall", _RELEASE_NAME, "--namespace", self.namespace, "--ignore-not-found", "--wait", "--timeout", "10m"],
                timeout=timedelta(minutes=11),
            )
            await self._run(
                ["kubectl", "delete", "namespace", self.namespace, "--ignore-not-found=true", "--wait=true", "--timeout=10m"],
                timeout=timedelta(minutes=11),
            )
            self._cleaned = True
            self._installed = False
            self._active_load = False
            self._active_faults.clear()
            self._created_resources.clear()
        except Exception as error:
            self.contaminated = True
            await self._capture_diagnostics("cleanup", error)
            raise

    async def _run(self, argv, *, timeout, cwd=None) -> CommandResult:
        safe_argv = [redact(str(item)) for item in argv]
        try:
            result = await self._runner.run(argv, timeout=timeout, cwd=cwd)
        except Exception:
            self._command_history.append(
                {"argv": safe_argv, "timeout_seconds": timeout.total_seconds(), "outcome": "failed"}
            )
            raise
        self._command_history.append(
            {
                "argv": safe_argv,
                "timeout_seconds": timeout.total_seconds(),
                "outcome": "succeeded",
                "returncode": result.returncode,
                "duration_seconds": result.duration_seconds,
            }
        )
        return result

    async def _capture_diagnostics(self, operation: str, error: Exception) -> None:
        directory = self._workspace / "diagnostics" / f"{operation}-{len(self._diagnostics) + 1}"
        directory.mkdir(parents=True, exist_ok=True)
        commands = (
            ("resources", ["kubectl", "get", "all,configmaps", "-n", self.namespace, "-o", "yaml"]),
            ("workloads-pods", ["kubectl", "get", "deployments,pods", "-n", self.namespace, "-o", "wide"]),
            ("pod-descriptions", ["kubectl", "describe", "pods", "-n", self.namespace]),
            ("events", ["kubectl", "get", "events", "-n", self.namespace, "-o", "yaml"]),
            ("logs", ["kubectl", "logs", "-n", self.namespace, "-l", "app.kubernetes.io/part-of=opentelemetry-demo", "--all-containers=true", "--tail=200"]),
            ("helm-values", ["helm", "get", "values", _RELEASE_NAME, "-n", self.namespace, "--all"]),
            ("helm-manifest", ["helm", "get", "manifest", _RELEASE_NAME, "-n", self.namespace]),
        )
        command_metadata = []
        for category, argv in commands:
            try:
                result = await self._runner.run(argv, timeout=_TIMEOUT)
                content = result.stdout + ("\nSTDERR:\n" + result.stderr if result.stderr else "")
                command_metadata.append({"argv": list(result.argv), "returncode": result.returncode, "duration_seconds": result.duration_seconds})
            except Exception as diagnostic_error:
                content = f"diagnostic collection failed: {diagnostic_error}"
                command_metadata.append({"argv": list(argv), "error": str(diagnostic_error)})
            path = directory / f"{category}.txt"
            path.write_text(redact(content), encoding="utf-8")
            self._diagnostics.append(DiagnosticArtifactReference(category, str(path)))
        state_path = directory / "adapter-state.json"
        state_path.write_text(
            json.dumps({
                "operation": operation,
                "error": redact(str(error)),
                "namespace": self.namespace,
                "release": self._release.commit_sha,
                "active_load": self._active_load,
                "active_faults": sorted(item.value for item in self._active_faults),
                "contaminated": self.contaminated,
                "executed_commands": self._command_history,
                "commands": command_metadata,
            }, indent=2),
            encoding="utf-8",
        )
        self._diagnostics.append(DiagnosticArtifactReference("adapter-state", str(state_path)))
