# Minimal Guardian Runtime and Executable Verification Design

## Goal

Provide the smallest production-owned Guardian API that deterministically satisfies the current GuardianScenario v1alpha2 contracts, activate the integration, security, and replay gates with nonempty suites, and run the sequential five-environment matrix on a disposable local Kubernetes cluster.

This runtime is a foundation slice, not a claim that the full service decomposition in `docs/spec/guardian-production-v1.md` is implemented.

## Normative boundaries

The v1alpha2 pack references `GRD-ACT-001`, `GRD-ACT-002`, `GRD-ACT-005`, `GRD-CLS-001`, `GRD-CLS-003` through `GRD-CLS-005`, `GRD-DRIFT-001`, `GRD-DRIFT-003`, `GRD-OPA-003`, `GRD-OPA-006`, `GRD-SCL-006`, `GRD-TEN-001`, `GRD-TEN-006`, `GRD-TTL-003`, `GRD-TTL-005`, and `GRD-WF-001`. The minimal runtime directly implements only the deterministic classification, local policy, expiry, scaler-safe-hold, authenticated tenant isolation, in-process deduplication, and recommendation projections described below. Scenario tests for Temporal workflow identity, action-controller preconditions, durable tenant isolation, or real mutation are projection/contract tests and must not mark those normative requirements complete in the requirement registry. Deterministic replay covers the local subset of `GRD-MLO-002`; it is not statistical or model-in-the-loop replay.

Production Guardian code must not import `testbeds`, scenario fixtures, environment adapters, or demo service names. It must not accept expected results as input. No model participates in scoring, eligibility, policy, approval, or mutation decisions.

## Architecture

### Domain core

`apps/guardian_api/domain.py` owns typed normalized incident facts, deterministic assessment, policy gating, workflow projection, idempotency, and tenant isolation. The versioned `guardian.incident-facts/v1` schema contains no scenario identifier and no expected values. It contains:

- authenticated tenant and incident correlation identity;
- target role plus resolved namespace, workload kind/name, service version, and immutable image digest when known;
- telemetry quality, integrity failures, freshness timestamp, and observation window;
- typed evidence records with subject role, tenant identity, freshness, source, observed value, and provenance reference;
- successful load, fault, and deployment event records returned by the adapter;
- dependency state, desired and ready replicas, endpoints, rollout state, and normalized fault state from adapter observations;
- policy-bundle state and age;
- approval issuance, expiry, and attempt timestamps;
- protected-resource fingerprint before mutation and the current fingerprint;
- scaler source value, source expiry, and requested direction;
- post-action observations only when supplied by a separate lifecycle update.

Scenario-runner normalization accepts only successful adapter operation results and normalized observations as evidence. A declared stimulus by itself is not evidence. Missing fields stay missing and trigger fail-closed behavior. Every evidence item records whether it came from an adapter observation, deployment event, fault execution, load execution, or configured policy-control fixture.

### Independent testbed evidence collection

`testbeds/evidence/collector.py` independently samples observable effects after each control operation. It performs bounded endpoint probes for status, error rate, and latency; reads Kubernetes workload identity, desired/ready replicas, pod restarts, endpoints, and rollout status; reads resource utilization from the Kubernetes Metrics API when a scenario requires pressure evidence; and reads queue depth/scaler status from the KEDA/RabbitMQ fixture. Samples carry timestamps and the concrete query/probe provenance.

Load, fault, deployment, and version labels describe controls and identities only; they never prove symptoms. Classification evidence comes from later collector samples. A deployment-regression scenario, for example, requires both an observed immutable version transition and independently observed error/latency degradation. If metrics-server, an endpoint, or another required evidence source is unavailable, capability preflight rejects that scenario before installation or reports an infrastructure failure after installation; it does not manufacture evidence.

Before matrix execution, adapter capabilities are audited against collector evidence contracts. Capabilities such as telemetry interruption, resource pressure, latency/error observation, recovery observation, and scaler observation are removed when their deterministic evidence contract cannot execute. Every environment must still have at least one independently observable compatible scenario, but the matrix does not claim coverage for scenarios whose evidence source is absent. SigNoz-dependent scenarios remain unsupported unless an approved deterministic SigNoz evidence contract is installed; the minimal Guardian runtime does not substitute for SigNoz.

The evaluator applies rules in fail-closed order:

1. reject unauthenticated or conflicting tenant identity before evaluation;
2. collapse duplicate delivery by tenant and idempotency key;
3. classify critical telemetry failure before causal hypotheses;
4. score only normalized evidence and apply deterministic eligibility thresholds;
5. apply fresh/restricted/fail-closed policy gates;
6. enforce approval expiry and operator-drift preconditions before mutation;
7. permit at most one mutation and only after all safety gates pass;
8. require fresh post-action evidence for recovery.

Unknown, missing, stale, or conflicting facts deny mutation. The core returns a stable snapshot and audit projection suitable for deterministic byte-for-byte replay.

### Store and service boundary

`apps/guardian_api/store.py` provides a lock-protected in-memory incident store. Keys always contain tenant ID. The same critical section records incident state, idempotency ownership, workflow identity, and audit events, providing the minimal local equivalent of an atomic transactional boundary. Persistence, NATS, Temporal, and PostgreSQL are explicitly outside this slice.

`apps/guardian_api/service.py` exposes application operations independent of transport. `apps/guardian_api/http.py` exposes:

- `GET /health`;
- `POST /v1/incidents` with mandatory bearer authentication and `Idempotency-Key` header;
- `POST /v1/incidents/{incident_id}/observations` for a later authenticated observation window;
- `GET /v1/incidents/{incident_id}/scenario-snapshot` with mandatory matching bearer identity.

For this local runtime, an environment-supplied token map resolves opaque bearer tokens to tenant IDs; the request cannot assert its own tenant. Malformed payloads return 400, missing or invalid authentication returns 401, an explicit foreign-tenant evidence reference returns 403 before evaluation, an opaque incident lookup outside the authenticated tenant returns a non-disclosing 404, and idempotency conflicts return 409. Responses never contain secrets or credentials. The `scenario-snapshot` route is explicitly a test-observation projection, not a normative production endpoint.

### Scenario ingestion

The scenario executor remains the only testbed-aware component. It converts successful adapter operation results and normalized adapter observations into `guardian.incident-facts/v1`. Policy, approval, tenant-mismatch, and drift controls are typed test-control facts with explicit provenance; expected assertions are never submitted. The HTTP client supplies a configured bearer token and validates response schemas.

This boundary makes the same Guardian evaluator usable for every environment and allows adding a sixth adapter without changing incident logic.

### Recommendation-only action semantics

This slice creates assessments and action proposals but has no mutation credentials and no action controller. It therefore always reports zero executed mutations and an empty executed-mutation list. The v1alpha2 model and assertion evaluator are revised to distinguish `allowedActions` from `executedActions`; migration accepts the existing `mutations.actions` spelling as `allowedActions` only when the count permits zero. An exact positive count requires nonempty `executedActions` and is unsupported at capability preflight. Guardian snapshots expose proposed/eligible actions separately and cannot represent a recommendation as an executed mutation.

Recovery is not claimed by the initial assessment. After reset, the runner submits a later normalized observation window. The API may mark recovery only from fresh post-reset facts, while the executor independently verifies the adapter baseline. Without a fresh later window, recovery remains unverified and the assertion fails.

## Verification suites

`tests/integration` exercises the real application service and loopback HTTP transport, including lifecycle submission, duplicate convergence, snapshot observation, evidence-collector provenance, and rejection of a control result without an independent symptom observation.

`tests/security` proves missing tenant rejection, cross-tenant rejection before lookup/evaluation, fail-closed stale policy and telemetry behavior, secret-safe responses, approval expiry, and operator-drift no-mutation behavior.

`tests/replay` serializes canonical normalized facts and proves repeated and reordered duplicate processing produces byte-identical deterministic projections with one parent workflow.

Task targets run verification preflight, require a nonempty collected suite through `tools.verification_harness`, and then run pytest. Their manifest capabilities become active only after the tests exist. `final` continues to aggregate these targets and the real matrix.

## Local runtime and cluster

Repository scripts will:

1. verify or start Docker Desktop and confirm its WSL engine is reachable;
2. install a pinned `kind` binary under `.tools/bin` with a verified SHA-256 digest;
3. create a uniquely named disposable single-node cluster using a run-owned kubeconfig under the artifact directory;
4. start the Guardian API on loopback and wait for `/health`;
5. export the tenant and `GUARDIAN_BASE_URL` required by the runner;
6. execute environments sequentially to avoid namespace and memory contention;
7. preserve reports and failure diagnostics;
8. stop immediately after any reset or cleanup failure;
9. clean the API process and delete the run-owned cluster by default, with retention requiring an explicit environment setting.

No script silently installs global packages or mutates an unrelated Kubernetes context. The isolated kubeconfig prevents context mutation. Before creation the script rejects a colliding cluster name unless its ownership record matches the current run. Partial creation triggers bounded cleanup of that exact name. Cluster node container memory is capped at 6 GiB when the Docker backend supports it; preflight requires at least 2.5 GiB available WSL memory and 12 GiB free storage. Runtime resource measurements are written to prerequisite artifacts.

The matrix runs one environment at a time and cleans it before the next. A resource exhaustion is reported as a prerequisite/infrastructure failure, never converted to a test skip.

## Artifact and error behavior

The existing per-scenario and matrix artifact roots remain authoritative. API logs are written to a separate run directory and redact authorization, cookies, tokens, passwords, and secret-like values. Every target returns nonzero for zero collected tests, failed assertions, unavailable infrastructure, or incomplete cleanup.

The environment script distinguishes invalid usage, missing prerequisites, and test failure. The matrix fails if any environment selects or executes zero scenarios, if any required coverage report is empty, or if cleanup cannot be verified.

## Delivery sequence

Implementation proceeds test-first in four reviewable increments:

1. deterministic domain core and unit tests;
2. HTTP/service boundary plus integration, security, and replay suites;
3. scenario fact normalization and end-to-end local API tests;
4. pinned cluster bootstrap and real sequential environment/matrix execution.

Each increment updates requirement traceability and runs the narrow suite before all affected gates. Real cluster results are reported separately from controlled local tests.

## Explicit exclusions

This slice does not implement NATS, Temporal, PostgreSQL, OPA distribution, SigNoz Query Contract execution, Kubernetes mutation controllers, KEDA scaler gRPC, Slack, web UI, model inference, or production authentication providers. It does not claim full compliance with requirements that depend on those components.
