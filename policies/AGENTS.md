# Policy Rules

## Scope

This directory contains versioned policy source, tests, schemas, bundle
metadata and fixtures. Policies are normative production artifacts and may
affect whether Guardian permits or denies production actions.

## Source of truth

Preserve every applicable `GRD-*` and `AT-*` requirement from:

- `docs/spec/guardian-production-v1.md`
- `docs/requirements/requirements.yaml`

Do not weaken a fail-closed invariant to make a test pass.

## Required architecture boundaries

* The reasoner proposes actions; OPA decides whether they are permitted.
* Policy must not delegate authorization to a model.
* Policy must not modify deterministic scores, hypothesis eligibility,
  telemetry quality or evidence confidence.
* Decisions use typed, versioned inputs.
* Unknown fields, malformed inputs and unsupported actions are denied.
* Missing tenant identity is denied before provider or external I/O.
* Identical input and bundle versions produce deterministic decisions.
* Record applied bundle version and decision reason.

## Required policy inputs

Where applicable, explicitly evaluate tenant, environment, severity, action
type, evidence confidence, telemetry quality, target, parameters, replica
bounds, action-domain state, approval identity and validity, proposal and policy
expiry, rollback availability, Recovery Contract binding, change freeze,
separation of duties, operator drift and concurrent actions. Never infer missing
values.

## OPA staleness behavior

Preserve `FRESH`, `RESTRICTED` and `FAIL_CLOSED` tiers. `FRESH` permits normal
evaluation. `RESTRICTED` permits read-only investigation and alert-only
behavior but denies new destructive or mutating actions. `FAIL_CLOSED` denies
all new writes, including scale-up. Read-only investigation remains available
in every tier. Invalid signatures, unknown desired versions, integrity failures
and unsafe clock uncertainty fail closed. Running actions must not begin another
mutation after entering `FAIL_CLOSED`.

## Approval rules

* Production rollback requires approval unless the normative specification
  explicitly permits otherwise.
* Approval identity is authenticated and tenant scoped; nonces are single-use.
* Expired proposals, approvals, nonces and policy decisions are not executable.
* Delayed approval cannot revive an expired proposal.
* Reevaluate OPA immediately before execution within the normative bound.
* Enforce configured separation of duties.

## Query Contract and policy activation

* Approved versions are immutable and resolved by version and content hash.
* Activation and supersession are atomic.
* Partial promotion leaves the previous approved version active.
* Signed updates preserve tenant scope.
* Desired/applied versions, signature state and bundle age are observable.

## Prohibited behavior

* No secrets in policy source, fixtures or logs.
* No demo-specific names in general production policies.
* No arbitrary shell, SQL, PromQL, ClickHouse fragments or backend escapes.
* No default allow for incomplete inputs or typed-validation bypasses.
* No model confidence substituted for evidence confidence.
* No authorization of deterministically ineligible hypotheses.

## Required tests

At minimum, test allow/deny for every action; missing tenant; cross-tenant and
malformed inputs; stale evidence; telemetry/evidence thresholds; expired and
reused approvals; missing Recovery Contract; action-domain conflict; rollback
availability; change freeze; separation of duties; all staleness transitions;
invalid signatures and unknown versions; and deterministic policy replay.

Required commands:

```bash
task test:unit
task test:contract
task test:policy
task test:replay-deterministic
task requirements:check
```

## Completion report

Before declaring a policy task complete, report:

* Requirement IDs addressed.
* Policies and schemas changed.
* Policy decisions added or changed.
* Tests added.
* Commands run.
* Exact pass and failure results.
* Bundle compatibility impact.
* Remaining requirements and risks.
