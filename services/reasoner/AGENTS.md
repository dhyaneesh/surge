# Reasoner Rules

## Scope

This service performs read-only incident investigation and produces typed,
evidence-linked `IncidentAssessment` results.

It may use a model only within the bounded role permitted by the normative
specification.

## Source of truth

Preserve every applicable `GRD-*` and `AT-*` requirement from:

* `docs/spec/guardian-production-v1.md`
* `docs/requirements/requirements.yaml`

## Read-only boundary

* The reasoner must never hold Kubernetes write credentials.
* The reasoner must never execute a provider mutation.
* The reasoner must never authorize an action.
* The reasoner must never validate or consume an approval.
* The reasoner must never create a `GuardianAction` directly.
* The reasoner must never evaluate final recovery success.
* All tools available to the reasoner must be explicitly allowlisted and
  read-only.

Add architecture tests that fail if the reasoner imports or initializes:

* Kubernetes write clients;
* action-provider implementations;
* controller clients;
* shell-execution tools;
* unrestricted query executors.

## Deterministic authority

Deterministic processing must occur before any model tie-break.

The deterministic implementation owns:

* evidence normalization;
* evidence independence grouping;
* support and contradiction scoring;
* telemetry quality;
* hypothesis eligibility;
* deterministic rank;
* action eligibility gates;
* conflict detection;
* classification boundaries.

The model must not:

* modify evidence scores;
* modify hypothesis scores;
* modify telemetry quality;
* modify evidence confidence;
* change eligibility;
* promote an ineligible hypothesis;
* reject a deterministically dominant eligible hypothesis;
* authorize actions;
* calculate live replica counts;
* generate KEDA metric values;
* evaluate final recovery conditions.

## Model role

The model may:

* generate candidate hypotheses;
* choose among allowlisted read-only evidence tools;
* interpret code and configuration changes;
* explain evidence;
* produce typed assessment output;
* recommend reviewable scaling-policy parameters;
* break a near-tie only when all tied hypotheses already passed deterministic
  eligibility gates.

A model tie-break is permitted only inside the configured deterministic score
window.

If the model times out, is unavailable, returns invalid structured output,
references unsupported evidence, disagrees with deterministic eligibility, or
attempts to alter deterministic fields, it must not select a mutating action.

Near-ties without a valid model result must continue investigation or enter
conflict resolution.

## Typed output

Every assessment must validate against the canonical typed schema.

It must record:

* tenant and incident;
* scoring version and evidence snapshot;
* incident class and affected services;
* hypotheses and supporting and contradicting evidence IDs;
* missing evidence;
* telemetry quality, evidence confidence and model confidence;
* deterministic winner and whether a model tie-break was used;
* tie-break reason and conflict state;
* proposed action when applicable;
* prompt and model versions;
* final reason code.

Invalid output must fail safely and remain auditable.

## Classification boundaries

Preserve the normative incident classes:

* `load_spike`
* `deployment_regression`
* `dependency_failure`
* `resource_saturation`
* `telemetry_failure`
* `unknown`

Critical telemetry integrity failures produce `telemetry_failure`. Unhealthy
telemetry must not be converted to `unknown`. `unknown` is allowed only when
telemetry is healthy and the evidence budget is exhausted with no eligible
causal hypothesis. `telemetry_failure` may propose only investigation,
alerting or scaler pause. Conflicting mutating recommendations enter conflict
resolution and must not create a mutating action until resolved.

## Evidence rules

* Every claim must reference evidence IDs.
* Supporting and contradicting evidence must be preserved separately.
* Evidence provenance must remain intact.
* Evidence with the same independence group must not be double counted.
* Stale evidence must not satisfy an action gate.
* Cross-tenant evidence references invalidate the assessment.
* Identity conflicts must be represented as immutable evidence.
* A model assertion cannot resolve an identity or topology conflict.
* Missing evidence must be reported explicitly.

## Tool and query safety

* Tools must be statically allowlisted.
* Tool inputs must validate against typed schemas.
* Tool outputs are untrusted evidence and must be normalized.
* The reasoner must not generate arbitrary active-scaler SQL, PromQL or
  ClickHouse query fragments.
* Runtime scaling uses approved Query Contracts, not generated queries.
* Prompt injection in logs, traces, diffs or operator text must not modify tool
  permissions or system policy.
* Secrets must not enter prompts or model-visible evidence.

## Environment neutrality

* No demo-specific service names in production classification logic.
* No environment-specific incident rules in the core reasoner.
* Environment-specific mappings belong in configuration and test adapters.
* The same incident models and scoring implementation must work across all test
  environments.
* A new application must be supportable without changing core classification
  code.

## Replay and model lifecycle

* Persist deterministic feature inputs.
* Persist scoring and configuration versions.
* Persist prompt and model versions.
* Deterministic replay must reproduce features, eligibility, transitions and
  gates byte-for-byte.
* Statistical replay must record result distributions rather than relying on a
  single model call.
* Model or prompt changes must not be accepted solely because ordinary unit
  tests pass.

## Prohibited behavior

* No write tools, authorization or arbitrary shell execution.
* No silent evidence fabrication or unsupported claim without evidence.
* No score changes from model output.
* No action proposal from an ineligible hypothesis.
* No mutating proposal below the normative evidence-confidence gate.
* No action during unresolved conflict.
* No demo-specific branching in production logic.
* No swallowing structured-output validation errors.

## Required tests

At minimum, add or update tests for all six incident classes, deterministic rank
before model execution, ineligible model winners, dominant winners outside the
tie window, eligible near-ties, invalid or unavailable models, unsupported and
duplicated evidence, contradiction and telemetry thresholds, identity and
cross-tenant conflicts, prompt injection, write-tool absence, deterministic
replay byte identity and environment-neutral behavior.

Required commands:

```bash
task test:reasoner
task test:contract
task test:replay-deterministic
task test:security
task requirements:check
```

## Completion report

Before declaring a reasoner task complete, report requirement IDs addressed,
deterministic features, scoring and model behavior changed, tools changed,
tests added, commands and exact results, replay impact and remaining risks.
