"""KEDA RabbitMQ sample adapter; test-only and never imported by Guardian services."""

import asyncio
import json
import re
import secrets
from datetime import timedelta
from pathlib import Path

from testbeds.adapters.command_runner import AllowlistedCommandRunner, CommandResult, redact
from testbeds.environments.keda_rabbitmq import KEDA_RABBITMQ_ENVIRONMENT
from testbeds.models import BaselineCheck, BaselineState, ChangedResource, DeploymentEvent, DeploymentSpecification, DiagnosticArtifactReference, EnvironmentCapabilities, EnvironmentRelease, EnvironmentState, FaultExecution, FaultSpecification, FaultType, LoadExecution, LoadProfile, ObservedServiceIdentity, WorkloadState

_TIMEOUT = timedelta(minutes=2)
_CONSUMER, _RABBITMQ = "rabbitmq-consumer", "rabbitmq"


class KedaRabbitMqAdapter:
    capabilities = EnvironmentCapabilities(frozenset({FaultType.QUEUE_LAG, FaultType.DEPENDENCY_UNAVAILABLE}), True, True)

    def __init__(self, *, workspace: Path, runner=None, run_id: str | None = None, namespace: str | None = None, baseline_poll_seconds: float = 2):
        suffix = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", (run_id or secrets.token_hex(6)).lower())).strip("-")[:36]
        self.namespace = namespace or f"guardian-keda-rabbitmq-{suffix}"
        if not suffix or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", self.namespace) or len(self.namespace) > 63: raise ValueError("namespace must be a valid DNS label")
        self._workspace, self._source, self._runner = Path(workspace), Path(workspace) / "source", runner or AllowlistedCommandRunner()
        self._release, self._poll, self._faults, self._changed, self._diagnostics = KEDA_RABBITMQ_ENVIRONMENT.release(), baseline_poll_seconds, set(), [], []
        self._created_load = False; self._cleaned = False; self.contaminated = False

    async def install(self, release: EnvironmentRelease) -> EnvironmentState:
        if release != self._release: raise ValueError("release does not match central pinned configuration")
        try:
            await self._prepare_source()
            if (await self._run(["git", "rev-parse", "HEAD"], cwd=self._source, timeout=_TIMEOUT)).stdout.strip() != release.commit_sha: raise RuntimeError("checked-out HEAD does not match pinned commit")
            await self._apply(json.dumps({"apiVersion":"v1","kind":"Namespace","metadata":{"name":self.namespace,"labels":{"guardian.test/environment":"keda-rabbitmq"}}}), namespace=False)
            await self._run(["helm","upgrade","--install",_RABBITMQ,"oci://registry-1.docker.io/bitnamicharts/rabbitmq","--version",KEDA_RABBITMQ_ENVIRONMENT.helm_chart_version,"-n",self.namespace,"--set",f"image.repository={KEDA_RABBITMQ_ENVIRONMENT.rabbitmq_image.split('@')[0].rsplit(':',1)[0]}","--wait"], timeout=timedelta(minutes=5))
            await self._apply(self._consumer_manifest())
            self._changed.append(ChangedResource("Namespace", self.namespace, self.namespace, "installed")); self._cleaned = False
            return await self.observe_state()
        except Exception as error:
            await self._diagnose("install", error); await self.cleanup(); raise

    async def _prepare_source(self):
        if (self._source / ".git").exists(): await self._run(["git","checkout","--detach",self._release.commit_sha], cwd=self._source, timeout=_TIMEOUT); return
        self._source.parent.mkdir(parents=True, exist_ok=True)
        await self._run(["git","clone","--no-checkout",self._release.repository,str(self._source)], timeout=timedelta(minutes=5)); await self._run(["git","checkout","--detach",self._release.commit_sha], cwd=self._source, timeout=_TIMEOUT)

    def _consumer_manifest(self):
        return json.dumps({"apiVersion":"apps/v1","kind":"Deployment","metadata":{"name":_CONSUMER},"spec":{"replicas":0,"selector":{"matchLabels":{"app":_CONSUMER}},"template":{"metadata":{"labels":{"app":_CONSUMER}},"spec":{"containers":[{"name":"consumer","image":KEDA_RABBITMQ_ENVIRONMENT.consumer_image,"command":["receive"]}]}}}})

    async def observe_state(self) -> EnvironmentState:
        try:
            deployments, services, endpoints, pods, scaled = await asyncio.gather(*(self._json(resource) for resource in ("deployments","services","endpoints","pods","scaledobjects")))
            workloads = tuple(self._workload(item) for item in deployments.get("items", []) if item.get("metadata",{}).get("name") in {_CONSUMER,_RABBITMQ})
            services_present = {x.get("metadata",{}).get("name") for x in services.get("items",[])}; endpoints_present = {x.get("metadata",{}).get("name") for x in endpoints.get("items",[]) if any(s.get("addresses") for s in x.get("subsets",[]))}
            scaled_ready = any(x.get("metadata",{}).get("name")==_CONSUMER and any(c.get("type")=="Ready" and c.get("status")=="True" for c in x.get("status",{}).get("conditions",[])) for x in scaled.get("items",[]))
            rabbit = next((x for x in workloads if x.role == "rabbitmq"), None)
            healthy = bool(rabbit and rabbit.ready_replicas >= rabbit.desired_replicas and _RABBITMQ in services_present and _RABBITMQ in endpoints_present and scaled_ready and not self._faults and not self.contaminated)
            return EnvironmentState("keda-rabbitmq", self.namespace, self._release, workloads, tuple(ObservedServiceIdentity(x.role,x.name,self._version(x.image),self._digest(x.image)) for x in workloads), healthy=healthy, contaminated=self.contaminated, changed_resources=tuple(self._changed), diagnostics=tuple(self._diagnostics))
        except Exception as error: await self._diagnose("observe", error); raise

    def _workload(self, raw):
        spec,status,meta=raw.get("spec",{}),raw.get("status",{}),raw.get("metadata",{}); image=next((x.get("image") for x in spec.get("template",{}).get("spec",{}).get("containers",[]) if x.get("image")),None); name=meta.get("name","")
        return WorkloadState("consumer" if name==_CONSUMER else "rabbitmq",name,spec.get("replicas",1),status.get("availableReplicas",0),status.get("observedGeneration"),meta.get("generation"),image)

    async def wait_for_healthy_baseline(self, timeout: timedelta) -> BaselineState:
        if timeout.total_seconds() <= 0: raise ValueError("timeout must be positive")
        deadline=asyncio.get_running_loop().time()+timeout.total_seconds(); stable=0
        while asyncio.get_running_loop().time() < deadline:
            state=await self.observe_state(); stable=stable+1 if state.healthy else 0
            checks=(BaselineCheck("rabbitmq_ready",state.healthy,"deployment/service/endpoints"),BaselineCheck("scaledobject_ready",state.healthy,"KEDA ScaledObject"),BaselineCheck("stable_observation",stable>=2,"two consecutive observations"),BaselineCheck("faults_clear",not self._faults,"adapter state"))
            if all(x.passed for x in checks): return BaselineState(True,checks=checks,environment=state)
            await asyncio.sleep(min(self._poll,max(0,deadline-asyncio.get_running_loop().time())))
        error=TimeoutError("healthy KEDA RabbitMQ baseline did not converge"); await self._diagnose("baseline",error); raise error

    async def apply_load(self, profile: LoadProfile) -> LoadExecution:
        if profile.concurrent_users not in {5,10,25,50}: raise ValueError("concurrent_users must be one of 5, 10, 25, or 50")
        await self._run(["kubectl","apply","-f", "-","-n",self.namespace],timeout=_TIMEOUT,input_text=json.dumps({"apiVersion":"batch/v1","kind":"Job","metadata":{"name":"guardian-rabbitmq-publish"},"spec":{"template":{"spec":{"restartPolicy":"Never","containers":[{"name":"publisher","image":KEDA_RABBITMQ_ENVIRONMENT.consumer_image}]}}}})); self._created_load=True; change=ChangedResource("Job","guardian-rabbitmq-publish",self.namespace,"load-applied");self._changed.append(change);return LoadExecution(profile,True,(change,))

    async def inject_fault(self, fault: FaultSpecification) -> FaultExecution:
        if fault.fault_type == FaultType.QUEUE_LAG and fault.target.role == "consumer": await self._run(["kubectl","scale",f"deployment/{_CONSUMER}","--replicas=0","-n",self.namespace],timeout=_TIMEOUT)
        elif fault.fault_type == FaultType.DEPENDENCY_UNAVAILABLE and fault.target.role == "rabbitmq": await self._run(["kubectl","scale",f"statefulset/{_RABBITMQ}","--replicas=0","-n",self.namespace],timeout=_TIMEOUT)
        else: raise ValueError("unsupported KEDA RabbitMQ fault or target")
        self._faults.add(fault.fault_type); change=ChangedResource("Workload",fault.target.role,self.namespace,"fault-applied");self._changed.append(change);return FaultExecution(fault,True,(change,))

    async def deploy_version(self, deployment: DeploymentSpecification) -> DeploymentEvent:
        if deployment.target.role != "consumer" or not deployment.image_digest or not re.fullmatch(r"sha256:[a-f0-9]{64}",deployment.image_digest) or "@" not in deployment.version: raise ValueError("consumer deployment requires an immutable image digest")
        await self._run(["kubectl","set","image",f"deployment/{_CONSUMER}",f"consumer={deployment.version}","-n",self.namespace],timeout=_TIMEOUT); change=ChangedResource("Deployment",_CONSUMER,self.namespace,"version-deployed");self._changed.append(change);return DeploymentEvent(deployment.target,None,deployment.version,changed_resources=(change,))

    async def reset(self):
        try:
            if self._created_load: await self._run(["kubectl","delete","job","guardian-rabbitmq-publish","-n",self.namespace,"--ignore-not-found=true"],timeout=_TIMEOUT)
            await self._run(["kubectl","scale",f"statefulset/{_RABBITMQ}","--replicas=1","-n",self.namespace],timeout=_TIMEOUT)
            await self._run(["kubectl","set","image",f"deployment/{_CONSUMER}",f"consumer={KEDA_RABBITMQ_ENVIRONMENT.consumer_image}","-n",self.namespace],timeout=_TIMEOUT)
            self._faults.clear();self._created_load=False;self.contaminated=False
        except Exception as error: self.contaminated=True;await self._diagnose("reset",error);raise

    async def cleanup(self):
        if self._cleaned:return
        try: await self._run(["kubectl","delete","namespace",self.namespace,"--ignore-not-found=true","--wait=true","--timeout=10m"],timeout=timedelta(minutes=11));self._cleaned=True
        except Exception as error:self.contaminated=True;await self._diagnose("cleanup",error);raise

    async def _json(self, resource): return json.loads((await self._run(["kubectl","get",resource,"-n",self.namespace,"-o","json"],timeout=_TIMEOUT)).stdout or '{"items": []}')
    async def _apply(self, manifest, namespace=True): await self._run(["kubectl","apply","-f","-"] + (["-n",self.namespace] if namespace else []),timeout=timedelta(minutes=5),input_text=manifest)
    async def _run(self, argv, *, timeout, cwd=None, input_text=None) -> CommandResult: return await self._runner.run(argv,timeout=timeout,cwd=cwd,input_text=input_text)
    async def _diagnose(self, operation, error):
        directory=self._workspace/"diagnostics"/f"{operation}-{len(self._diagnostics)+1}";directory.mkdir(parents=True,exist_ok=True)
        for category,argv in (("resources",["kubectl","get","deployments,statefulsets,scaledobjects,pods,services,endpoints","-n",self.namespace,"-o","wide"]),("events",["kubectl","get","events","-n",self.namespace,"-o","json"])):
            try: content=(await self._runner.run(argv,timeout=_TIMEOUT)).stdout
            except Exception as diagnostic_error: content=f"diagnostic collection failed: {diagnostic_error}"
            path=directory/f"{category}.txt";path.write_text(redact(content),encoding="utf-8");self._diagnostics.append(DiagnosticArtifactReference(category,str(path)))
    @staticmethod
    def _version(image): return image.split("@",1)[0].rsplit(":",1)[-1] if image and ":" in image else None
    @staticmethod
    def _digest(image): return image.rsplit("@",1)[1] if image and "@" in image else None
