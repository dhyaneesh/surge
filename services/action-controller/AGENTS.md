# Action Controller Rules

## Scope

This service is the only authorized Guardian production component that performs
approved Kubernetes or provider mutations. It reconciles typed
`GuardianAction` resources and executes only actions that pass evidence,
policy, expiry, approval and concurrency validation.

## Source of truth

Preserve every applicable `GRD-*` and `AT-*` requirement from:

* `docs/spec/guardian-production-v1.md`
* `docs/requirements/requirements.yaml`

## Authorization boundary

* The controller must never authorize an action or delegate authorization to a
  model.
* Model output is not an authorization decision.
* Only typed, allowlisted actions may execute.
* Verify the immutable proposal, policy decision, approval and Recovery
  Contract binding before mutation.
* Reevaluate OPA within the normative execution window immediately before
  provider execution.
* Missing, expired, stale, malformed, ambiguous or conflicting inputs fail
  closed.

## Required execution preconditions

Before every action, validate authenticated tenant identity; matching proposal,
action, evidence and incident tenants; unexpired proposal and approval; valid,
unconsumed approval and approver identity; current executable policy decision;
permitted OPA staleness tier; sufficient evidence confidence; healthy
telemetry; resolved target identity; available rollback target; no superseding
deployment; pinned Recovery Contract and registry versions; complete
action-domain keys; no incompatible active action; and unchanged provider
preconditions. No external mutation may occur first.

## Action-domain locking

* Lock action domains, not only direct workloads.
* Include the target, owning controller, HPA/KEDA/`ScaledObject`, traffic and
  rollback objects, and dependencies marked `actionCoupled`.
* Calculate all keys before execution; sort and acquire them atomically.
* Roll back partial acquisition.
* Lock loss stops all new mutation steps and escalates.
* Lock ownership and heartbeat must be observable.

## Idempotency

* Every action has a stable idempotency key.
* Duplicate reconciliation, approvals and retries must not duplicate mutation.
* Provider calls must be safe to retry or provider-idempotency protected.
* Restart resumes persisted state rather than replaying completed mutations.
* Record exactly one final execution outcome.

## Operator drift

Before every mutation step, compare protected fields, generation/resource
version, provider revision and approved preconditions. Expected changes carrying
the current Guardian execution identity may continue. Unexpected operator or
external changes must transition to `SUPERSEDED_BY_OPERATOR`, stop writes,
release locks, preserve the change, emit audit, and require fresh evidence and
a new proposal. Never restore prior desired state over an operator change.

## Approval consumption

* Approvals are single-use.
* Consume only after all preconditions pass and immediately before the first
  mutation.
* Failed validation must not consume approval.
* Consumed or expired approvals cannot be reused or revived.
* Approval transitions must be durable and auditable.

## Recovery handoff

* Provider success is not incident recovery.
* Signal the owning Temporal incident workflow.
* Evaluate recovery using the pinned Recovery Contract.
* Never reuse pre-action evidence as recovery evidence.
* Collect recovery evidence after convergence, observation delay and the
  configured evaluation window.
* The controller must not independently declare final recovery success.

## Provider boundaries

* Use approved typed, bounded provider adapters.
* No arbitrary shell execution, unvalidated patches, model-generated manifests
  or dynamic provider selection from untrusted text.
* Credentials must be tenant scoped, provider scoped and least privilege.
* Cross-tenant credential fallback is prohibited.

## Audit and observability

Record tenant, incident, assessment, proposal, policy decision, approval,
evidence IDs, Recovery Contract version, action-domain keys, provider request
and response IDs, state transitions, lock state, operator-drift result and final
execution outcome. Emit traces, metrics and Kubernetes Events for major
transitions.

## Prohibited behavior

* No direct execution from API, NATS consumer, reasoner or workflow worker.
* No bypass when NATS, Temporal, OPA or PostgreSQL is unavailable.
* No stale identity/topology execution or unresolved-conflict execution.
* No second mutation after lock loss or action after expiry.
* No demo-specific production execution logic.
* No swallowed provider failures or provider-success-as-recovery-success.

## Required tests

At minimum, test duplicate reconciliation and approval; expiries and reuse;
stale policy/OPA; missing Recovery Contract/evidence; cross-tenant evidence;
action-domain overlap, partial acquisition and lock loss; restart; provider
timeout/retry; operator drift before and between mutations; superseded rollback;
conflict resolution; and rejection of recovery success from pre-action data.

Required commands:

```bash
task test:action-controller
task test:contract
task test:integration
task test:security
task requirements:check
```

## Completion report

Before declaring completion, report requirement IDs, action types and provider
adapters affected, changed mutation preconditions, tests, commands and exact
results, idempotency/restart behavior and remaining risks.
