# Testbed Rules

## Scope

Everything under this directory is test-only. Testbeds validate Guardian
against unrelated Kubernetes applications; they are not production dependencies
and must not define production behavior.

## Source of truth

Preserve test-environment and generalized-scenario requirements from:

* `docs/spec/guardian-production-v1.md`
* `docs/requirements/requirements.yaml`

## Production separation

* Testbed adapters must never be imported by production services.
* Production packages must not depend on testbed models, fixtures or helpers.
* Maintain an architecture test scanning `apps/`, `services/` and `packages/`
  for forbidden `testbeds` imports.
* Demo names may exist in adapters, manifests and scenario bindings, but not in
  production reasoning or policy logic.
* Testbed credentials and fixtures must never be reused in production.
* Namespaces, certificates, secrets and storage must be isolated.

## Environment pinning

* Pin upstream repositories to full immutable commit SHAs.
* Pin final-acceptance images to immutable digests.
* Do not use moving branches or mutable `latest` tags.
* Record repository commit, image digest, Helm version and adapter version in
  every result.
* Upstream version changes require review and compatible scenario reruns.

## EnvironmentAdapter contract

Every adapter implements `install`, `reset`, `wait_for_healthy_baseline`,
`apply_load`, `inject_fault`, `deploy_version`, `observe_state` and `cleanup`.
Each operation uses typed I/O, enforces a timeout, emits diagnostics on failure,
is retry-safe where practical, preserves environment/tenant identity and records
resources changed.

## Baseline requirements

A scenario begins only after a healthy baseline proves required workloads are
ready, endpoints and load generators are reachable, telemetry arrives in
SigNoz, service/workload identity and version attributes exist, no fault is
active, no previous Guardian incident or action remains, and expected
dependencies are healthy. An unverified baseline makes the run invalid, not
passed or failed.

## Reset and cleanup

* Reset removes all scenario-created state; cleanup removes all environment
  state unless diagnostics are explicitly retained.
* Failed reset invalidates subsequent scenarios; failed cleanup invalidates the
  run and preserves diagnostics.
* No later scenario may inherit load, fault, replicas, deployment version,
  incidents, approvals, locks, policy fixtures, certificate fixtures or
  test-created data.

## Scenario design

Express scenarios with normalized service/workload/dependency roles,
environment references, load profiles, fault types, deployment transitions,
expected classes, allowed/forbidden actions, policy outcomes and recovery
conditions. Production code must not know demo-specific names; adapters bind
normalized roles to actual services.

## Assertions

Validate observable incident creation, exactly one parent workflow, typed
assessment, deterministic score/eligibility, evidence and contradictions,
telemetry quality, policy, approval, idempotency, provider and operator-drift
results, recovery from fresh telemetry, audit completeness, tenant isolation and
final state. Log text alone is not proof.

## Diagnostics

Every failed scenario collects, where available, the scenario and environment
manifests, baseline/final states, Kubernetes resources and Events, Guardian logs
and traces, SigNoz provenance, assessment, evidence index, workflow history,
policy decisions, approvals, action state, audit events and cleanup result.
Diagnostics must not contain secrets.

## Safety

* Never weaken or bypass production authorization, OPA or approval boundaries.
* Never add unrestricted Kubernetes credentials or model-controlled shell
  execution.
* Fault injection is limited to disposable test environments.
* Destructive tests verify cluster and namespace before execution.
* Cross-tenant tests use synthetic tenants and isolated credentials.

## Environment coverage

Primary adapters are `OpenTelemetryDemoAdapter`, `AwsRetailAdapter`,
`OnlineBoutiqueAdapter`, `ArgoRolloutsDemoAdapter` and
`KedaRabbitMqAdapter`. Every scenario declares supported and unsupported
environments with reasons, required capabilities/faults/telemetry/providers.
Do not force scenarios onto incompatible environments.

## Completion rules

An adapter or scenario is complete only when contract tests pass; installation
is reproducible; baseline, fault/load, reset and cleanup are verified;
diagnostics exist; versions are immutable; production imports remain clean; and
requirement traceability is updated.

## Required tests

```bash
task test:testbeds-unit
task test:testbeds-contract
task test:integration
task test:architecture
task requirements:check
```

For environment-specific changes:

```bash
task test:env ENV=<environment>
```

## Completion report

Report environment/scenario, pinned versions, capabilities, baseline checks,
fault/load controls, reset/cleanup, diagnostics, tests and exact results,
unsupported scenarios and remaining flakiness/environmental risks.
