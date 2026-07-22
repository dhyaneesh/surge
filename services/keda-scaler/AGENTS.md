# KEDA Scaler Rules

## Scope

This service is a deterministic KEDA external-scaler gRPC service. It resolves
an approved tenant-scoped scaling policy, verifies its immutable identity,
executes an approved scaling Query Contract through the SigNoz runtime gateway
and returns a numeric metric value to KEDA. It does not directly scale
Kubernetes workloads.

## Source of truth

Preserve every applicable `GRD-*` and `AT-*` requirement from:

* `docs/spec/guardian-production-v1.md`
* `docs/requirements/requirements.yaml`

## Deterministic boundary

* The scaler must never call an LLM or consume model output in the polling path.
* It must not calculate policy recommendations or create or modify Query
  Contracts.
* It must not generate runtime SQL, PromQL, ClickHouse SQL or arbitrary backend
  query fragments.
* It must execute only an approved immutable Query Contract version.
* Identical approved input and telemetry results must produce identical output.

## Allowed responsibilities

The scaler may implement KEDA external-scaler gRPC methods, authenticate KEDA
with mTLS, resolve a tenant-scoped active scaling policy, verify policy and
Query Contract identity and hashes, call the approved SigNoz runtime gateway,
validate result freshness, unit, sample count and scalar shape, apply bounded
numeric transformations explicitly defined by policy, return a metric value or
safe error/hold behavior, and emit scaler telemetry.

## Kubernetes permissions

* The scaler should not hold Kubernetes write credentials.
* It must not directly modify replicas, Deployments, StatefulSets, HPAs,
  `ScaledObject` resources or other workloads.
* Kubernetes scaling is performed by KEDA after consuming the scaler metric.
* Any Kubernetes read permission must be explicitly justified and least
  privilege.
* Resolve policies and Query Contracts through approved stores and APIs rather
  than broad Kubernetes access.

## Query Contract requirements

* Resolve by immutable ID, version and content hash.
* Scaling queries must return exactly one scalar with the expected unit.
* Enforce tenant predicates and bounded, allowlisted parameters.
* Arbitrary grouping is prohibited.
* Missing-data and stale-data behavior must be explicit.
* Result-shape violations and unknown AST nodes or backend fragments must fail.
* Never accept a mutable contract name without a pinned version.

## Cache consistency

* The SigNoz gateway result is the shared authoritative short-lived value.
* Local cache is an optimization only and must retain tenant, policy content
  hash, Query Contract identity, normalized parameters, gateway result ID and
  gateway expiry.
* A local result must never outlive the gateway result.
* Local/gateway disagreement is resolved in favor of the gateway and emits an
  integrity metric.
* Cache keys must begin with tenant identity; cross-tenant hits are security
  failures.

## Failure behavior

On unavailable, missing, stale, malformed or conflicting telemetry, fail safely
with an error or allowed hold behavior. Use last-known-valid values only within
the normative TTL. Never permit unsafe scale-down, manufacture zero unless the
approved policy defines it safely, reuse an expired value, or fall back to an
unapproved query. SigNoz or gateway failure beyond last-known-valid TTL must not
enable scale-down.

## Policy staleness behavior

* `FRESH` permits normal operation.
* `RESTRICTED` denies prohibited new scaling mutations according to policy.
* `FAIL_CLOSED` returns an error or safe hold behavior.
* Invalid signatures, unknown desired versions, policy hash mismatch, failed
  integrity verification or excessive clock uncertainty fail closed.

## mTLS

* KEDA-to-scaler transport must use mutual TLS with distinct identities.
* SAN and client/server EKU validation are mandatory.
* TLS 1.2 or newer is required; plaintext listeners are prohibited.
* Expired, untrusted, wrong-SAN and wrong-EKU certificates fail closed.
* Rotation must not silently downgrade transport security.

## Bounds

Respect approved minimum and maximum replicas, target metric, cooldown,
scale-up rate, scale-down stabilization, last-known-valid TTL, dependency
blockers, cost or capacity limits and shadow-mode state. Return the metric KEDA
requires; do not invent replica counts through model reasoning.

## Prohibited behavior

* No LLM calls, arbitrary shell execution or Kubernetes mutation.
* No direct action-controller dependency for normal metric polling.
* No arbitrary runtime query generation or plaintext gRPC listener.
* No shared unscoped cache keys or stale-local-over-fresh-gateway behavior.
* No scale-down when required telemetry is unhealthy.
* No demo-specific names in production scaler logic.
* No silent policy fallback after integrity failure unless the normative policy
  lifecycle explicitly permits that exact version.

## Required tests

At minimum, test `IsActive`, `GetMetricSpec`, `GetMetrics`, scalar shape, unit,
sample and freshness validation, gateway timeout/unavailability, cache expiry
and disagreement, multi-replica convergence, policy and Query Contract hash
mismatch, tenant isolation, last-known-valid inside and outside TTL, scale-down
safety, bounds and stabilization, restart, certificate failures, plaintext
rejection, stale OPA state and proof no LLM client is reachable from polling.

Required commands:

```bash
task test:keda-scaler
task test:contract
task test:integration
task test:security
task requirements:check
```

## Completion report

Before declaring a scaler task complete, report requirement IDs, gRPC methods,
policy and Query Contract behavior, cache behavior, failure modes tested,
commands and exact results, latency impact and remaining risks.
