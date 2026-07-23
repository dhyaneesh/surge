# Minimal Guardian Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic recommendation-only Guardian API, activate nonempty integration/security/replay gates, and execute the five-environment scenario matrix on an isolated local kind cluster.

**Architecture:** A production-owned Pydantic domain core evaluates normalized incident facts without importing testbed code. A lock-protected service and small standard-library HTTP transport expose authenticated ingestion and observation. Testbed-owned evidence normalization independently probes observable effects, while repository scripts manage a pinned kind cluster and local API process.

**Tech Stack:** Python 3.13, Pydantic 2, asyncio, standard-library HTTP server/client, pytest, Bash, Docker Desktop, kind v0.31.0, Kubernetes v1.35 kind node image, Task.

**Normative source:** `docs/spec/guardian-production-v1.md`

**Approved design:** `docs/superpowers/specs/2026-07-23-minimal-guardian-runtime-design.md`

**Per-task traceability rule:** Every implementation task updates `docs/requirements/requirements.yaml` for only the requirements its tests directly implement, runs `requirements:render`, inspects generated changes, and runs `requirements:check`. Excluded infrastructure requirements remain incomplete. No task defers its traceability update to a later task.

---

## File structure

- `apps/guardian_api/models.py`: strict transport-independent fact, evidence, assessment, and snapshot models.
- `apps/guardian_api/rules.py`: immutable scoring configuration `guardian-rules/v1` and evidence-to-hypothesis mappings.
- `apps/guardian_api/domain.py`: deterministic classification, evidence eligibility, policy, expiry, drift, scaler-safe-hold, and recovery rules.
- `apps/guardian_api/store.py`: tenant-scoped in-memory idempotency and incident storage.
- `apps/guardian_api/service.py`: authentication-independent application operations and error types.
- `apps/guardian_api/http.py`: loopback HTTP transport and bearer-token authentication.
- `apps/guardian_api/__main__.py`: local runtime CLI.
- `testbeds/evidence/contracts.py`: scenario evidence requirements without production imports.
- `testbeds/evidence/collector.py`: allowlisted endpoint/Kubernetes/metrics probes and provenance.
- `testbeds/evidence/signoz.py`: SigNoz baseline queries and OTLP/HTTP black-box probe export for non-instrumented fixtures.
- `testbeds/scenarios/facts.py`: testbed-to-production fact normalization.
- `testbeds/scenarios/execution.py`: submit initial facts and later recovery observations.
- `testbeds/scenarios/guardian_client.py`: authenticated HTTP client and observation update.
- `scripts/bootstrap-kind.sh`: verified repository-local kind installation.
- `scripts/create-test-cluster.sh`: isolated kubeconfig, owned kind lifecycle, resource preflight.
- `scripts/run-local-matrix.sh`: API/cluster orchestration and cleanup trap.
- `tests/integration/`, `tests/security/`, `tests/replay/`: active nonempty suites.

### Task 1: Correct mutation contract semantics

**Files:**
- Modify: `testbeds/scenarios/v1alpha2.py`
- Modify: `testbeds/scenarios/assertions.py`
- Modify: `testbeds/scenarios/guardian_client.py`
- Modify: `docs/architecture/decisions/guardian-scenario-v1alpha2.md`
- Test: `tests/unit/test_guardian_scenario_v1alpha2.py`
- Test: `tests/unit/test_scenario_execution.py`

- [ ] **Step 1: Write failing schema and assertion tests**

Add tests proving an `atMost: 1` mutation contract accepts zero executed actions, rejects an unexpected executed action, and an exact positive count requires the expected executed action. Add `executed_mutations` to the test snapshot fixture and keep recommendations in `eligible_actions`/`proposed_action`.

- [ ] **Step 2: Run RED**

Run:

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/unit/test_guardian_scenario_v1alpha2.py \
  tests/unit/test_scenario_execution.py -q
```

Expected: failure because the evaluator currently requires every declared mutation action to appear even when the cardinality allows zero.

- [ ] **Step 3: Implement the minimal semantic split**

Rename the model field internally to `allowed_actions` with validation alias `actions` for compatible YAML loading. Change `GuardianSnapshot.mutations` to `executed_mutations` with alias `mutations`. Evaluate actual count and require every executed mutation to be in the allowed set; require allowed actions to occur only when the lower cardinality bound demands them. Extend capability preflight to reject exact-positive mutation cardinality unless the selected runtime declares a real action-controller execution capability; the minimal runtime never declares it.

- [ ] **Step 4: Document and verify GREEN**

Update the architecture decision with recommendation-only semantics. Run the focused tests plus `task test:scenario-schema` and `task test:scenarios`.

- [ ] **Step 5: Commit**

```bash
git add testbeds/scenarios tests/unit docs/architecture/decisions/guardian-scenario-v1alpha2.md
git commit -m "fix(scenarios): distinguish allowed and executed mutations"
```

### Task 2: Define normalized incident facts and deterministic domain evaluation

**Files:**
- Create: `apps/__init__.py`
- Create: `apps/guardian_api/__init__.py`
- Create: `apps/guardian_api/models.py`
- Create: `apps/guardian_api/rules.py`
- Create: `apps/guardian_api/domain.py`
- Create: `tests/unit/test_guardian_domain.py`

- [ ] **Step 1: Write failing domain tests**

Cover healthy no-action, telemetry failure precedence, healthy unknown only after the evidence budget, load/resource evidence proposing scale-up, version-plus-error evidence proposing rollback, conflicting candidates denying action, fail-closed policy, expired approval, operator drift, foreign evidence rejection, stale scaler safe-hold, and fresh recovery observations. Assert no scenario ID or expected-result field exists in `IncidentFacts`.

Use an evidence helper shaped like:

```python
EvidenceFact(
    evidence_type="resource-utilization",
    subject_role="request-processor",
    tenant_id="tenant-a",
    freshness="fresh",
    source="kubernetes-metrics-api",
    observed_value={"cpuUtilization": 0.92},
    provenance="apis/metrics.k8s.io/v1beta1/...",
    independence_group="kubernetes-resource-sample",
    confidence=0.95,
)
```

- [ ] **Step 2: Run RED**

Run `TMPDIR=/tmp .tools/bin/uv run --locked pytest tests/unit/test_guardian_domain.py -q`.

Expected: import failure because `apps.guardian_api` does not exist.

- [ ] **Step 3: Implement strict models**

Define enums and strict Pydantic models for evidence freshness/source, telemetry quality score, critical integrity failures, evidence pass number, policy state, control timestamps/fingerprints, scaler facts, `IncidentFacts`, `ObservationUpdate`, and deterministic `GuardianProjection`. Reject extra keys, naive timestamps, mutable image tags presented as identity, empty tenant IDs, and evidence without provenance, confidence, or independence group.

- [ ] **Step 4: Implement fail-closed evaluator**

Order rules exactly as the design specifies. `rules.py` pins `guardian-rules/v1` and these conversions: sustained request rate `>= 2.0 ×` baseline creates load support `0.40`; CPU or memory utilization `>= 0.85` creates utilization support `0.40`; throttling `>= 0.20`, an OOM, or restart delta `>= 1` creates pressure support `0.35`; an immutable observed version transition creates deployment support `0.40`; error rate `>= max(0.05, 2.0 × baseline)` creates exception support `0.45`; p95 latency `>= 1.5 × baseline` with absolute increase `>= 100ms` creates latency support `0.45`; a fresh topology edge creates topology support `0.35`; an unhealthy dependency creates dependency support `0.45`. Fresh healthy dependencies contradict dependency failure at `0.30`; unchanged immutable version contradicts deployment regression at `0.40`; request rate `< 1.25 ×` baseline contradicts load spike at `0.40`; utilization `< 0.70` without pressure contradicts resource saturation at `0.40`. Load-spike requires load plus utilization groups; deployment-regression requires deployment plus exceptions or latency; resource-saturation requires utilization plus pressure; dependency-failure requires topology plus unhealthy dependency. Collector confidence is `usable_samples / expected_samples`, capped at `1.0`, and every required group must be `>= 0.85`. Within each hypothesis, retain only the highest `confidence × configured_weight` item per independence group. Compute `support = min(1, sum(retained_support))`, `contradiction = min(1, sum(retained_contradiction))`, `deterministic_score = max(0, support - contradiction)`, and `evidence_confidence = min(confidence of required supporting groups)`. Eligibility requires score `>= 0.70`, confidence `>= 0.85`, contradiction `<= 0.25`, healthy telemetry, and resolved identity. Telemetry is healthy only at quality `>= 0.80` with no critical failure; sample age, skew, zero samples, pipeline outage, identity conflict, and cardinality/sampling changes implement `GRD-CLS-002`. Unknown requires two completed evidence passes or ten elapsed minutes with no eligible hypothesis. Proposal TTL is `min(configured or 15 minutes, 30 minutes)`; approval validity is `min(10 minutes, proposal expiry - approval issuance)` and never revives an expired proposal. Never use scenario IDs, environment IDs, or service names. Return proposals but always zero executed mutations.

- [ ] **Step 5: Verify GREEN and architecture boundary**

Run the focused test and add an architecture assertion that `apps/guardian_api` cannot import `testbeds`. Update traceability for the directly implemented classification/TTL rules and run `task requirements:check`, without marking excluded infrastructure requirements complete.

- [ ] **Step 6: Commit**

```bash
git add apps tests/unit/test_guardian_domain.py tests/architecture
git commit -m "feat(guardian): add deterministic incident evaluator"
```

### Task 3: Add tenant-scoped idempotent service storage

**Files:**
- Create: `apps/guardian_api/store.py`
- Create: `apps/guardian_api/service.py`
- Test: `tests/unit/test_guardian_service.py`

- [ ] **Step 1: Write failing service tests**

Prove duplicate concurrent submissions converge on one incident and parent workflow projection; the same key with different canonical facts raises conflict; tenant B cannot observe tenant A; observation updates cannot change tenant; and snapshots are stable copies.

- [ ] **Step 2: Run RED**

Run `TMPDIR=/tmp .tools/bin/uv run --locked pytest tests/unit/test_guardian_service.py -q`.

- [ ] **Step 3: Implement store and service**

Use one `threading.RLock` around tenant-keyed idempotency and incident dictionaries. Canonicalize facts with sorted JSON before hashing. Generate workflow identity as `guardian/{tenant_id}/incident/{incident_id}`. Check tenant before lookup. Make observation updates append-only and re-evaluate from the complete fact history.

- [ ] **Step 4: Verify GREEN and commit**

Run domain and service tests, then commit:

```bash
git add apps/guardian_api tests/unit/test_guardian_service.py
git commit -m "feat(guardian): add tenant-scoped incident service"
```

### Task 4: Expose the authenticated local HTTP API

**Files:**
- Create: `apps/guardian_api/http.py`
- Create: `apps/guardian_api/__main__.py`
- Create: `tests/integration/test_guardian_http.py`
- Create: `tests/security/test_guardian_http_security.py`

- [ ] **Step 1: Write failing loopback integration tests**

Start the real server on port `0`. Test `/health`, authenticated POST, duplicate POST, observation update, snapshot GET, malformed JSON, missing incident, and graceful shutdown.

- [ ] **Step 2: Write failing security tests**

Test missing/invalid bearer token returns 401, token-derived tenant cannot be overridden by body/header, a tenant-scoped opaque lookup returns a non-disclosing 404 without evaluating another tenant's record, foreign-tenant evidence is rejected before external I/O, conflicting idempotency returns 409, and responses/logs redact authorization and secret-like input values.

- [ ] **Step 3: Run RED**

Run:

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/integration/test_guardian_http.py \
  tests/security/test_guardian_http_security.py -q
```

- [ ] **Step 4: Implement transport and CLI**

Use `ThreadingHTTPServer`; bind to `127.0.0.1` by default. Load `GUARDIAN_LOCAL_TOKENS_JSON` as `{token: tenant}` and fail startup if empty. Cap request bodies, set JSON content types, suppress raw headers, and map typed service errors to 400/401/403/404/409. Incident IDs are random opaque IDs; storage lookup is always `(authenticated_tenant, incident_id)`, so another tenant receives 404 without disclosing existence.

- [ ] **Step 5: Verify GREEN and commit**

```bash
git add apps/guardian_api tests/integration tests/security
git commit -m "feat(guardian): expose authenticated local incident API"
```

### Task 5: Collect independent testbed evidence

**Files:**
- Create: `testbeds/evidence/__init__.py`
- Create: `testbeds/evidence/contracts.py`
- Create: `testbeds/evidence/collector.py`
- Create: `testbeds/evidence/signoz.py`
- Create: `tests/unit/test_evidence_collector.py`
- Modify: `testbeds/environments/capabilities.py`
- Modify: `testbeds/scenarios/compatibility.py`

- [ ] **Step 1: Write failing collector tests with allowlisted runners**

Test endpoint latency/status sampling, Kubernetes workload identity and replicas, metrics API CPU/memory, rollout state, RabbitMQ queue depth, KEDA scaler health, SigNoz telemetry arrival/identity/version queries, timestamp/provenance attachment, secret redaction, timeout diagnostics, and unavailable-source results. Ensure a successful control result alone never becomes symptom evidence.

- [ ] **Step 2: Run RED**

Run `TMPDIR=/tmp .tools/bin/uv run --locked pytest tests/unit/test_evidence_collector.py -q`.

- [ ] **Step 3: Implement evidence contracts and collector**

Use structural runner protocols and fixed command argument construction. No shell execution and no model-controlled command fragments. Convert only independently sampled values into evidence. Return typed unavailable results rather than fabricated zeroes. For fixtures without native OTLP, run a testbed-owned black-box endpoint probe and export its measured availability/latency plus Kubernetes-resolved identity/version through OTLP/HTTP JSON to the real SigNoz collector; query SigNoz back and require the same tenant/environment/service/workload/version attributes before baseline succeeds.

- [ ] **Step 4: Make compatibility evidence-aware**

Map scenario evidence types to collector contracts. Reject a scenario before install when its adapter declaration lacks a required deterministic evidence source. Remove telemetry-interruption or resource-pressure declarations that the adapter cannot substantiate. Regenerate compatibility catalogs only from actual declarations. Update requirement traceability for testbed evidence behavior and run `task requirements:check` in this task.

- [ ] **Step 5: Verify every environment remains nonempty**

Run collector, compatibility, catalog, and scenario execution contract tests. Assert each of the five environments retains at least one independently observable scenario. Also run `task test:testbeds-unit`, `task test:testbeds-contract`, `task test:integration`, and `task test:architecture` as required by `testbeds/AGENTS.md`.

- [ ] **Step 6: Commit**

```bash
git add testbeds/evidence testbeds/environments testbeds/scenarios tests
git commit -m "feat(testbeds): collect provenance-backed scenario evidence"
```

### Task 6: Normalize facts and execute against the real API

**Files:**
- Create: `testbeds/scenarios/facts.py`
- Modify: `testbeds/scenarios/execution.py`
- Modify: `testbeds/scenarios/guardian_client.py`
- Modify: `testbeds/scenarios/environment_suite.py`
- Modify: `testbeds/scenarios/matrix.py`
- Test: `tests/unit/test_scenario_facts.py`
- Test: `tests/integration/test_scenario_guardian_lifecycle.py`

- [ ] **Step 1: Write failing fact-normalization tests**

Prove no scenario ID, expected block, secret, or demo service name enters facts; controls require later independent evidence; tenant/drift/approval/policy fixtures have explicit control provenance; and missing required observations fail normalization.

- [ ] **Step 2: Run RED**

Run fact tests and confirm the current incident payload is rejected because it includes `scenarioId` and raw stimulus.

- [ ] **Step 3: Implement normalization and client authentication**

Build facts from release identity, operation results, and collector samples. Extend `HttpGuardianClient` with bearer token and `submit_observations`. Read token from `GUARDIAN_SCENARIO_TOKEN`; never persist it in artifacts.

- [ ] **Step 4: Move recovery assertion after reset**

Evaluate initial assessment assertions before reset, excluding recovery. After reset and verified baseline, collect a fresh window, POST it, observe the new snapshot, and evaluate recovery plus final environment health. Persist both snapshots and the recovery evidence.

- [ ] **Step 5: Run integration lifecycle GREEN**

Use real adapter classes with controlled allowlisted runners, the real service, and loopback HTTP. Cover duplicate delivery, tenant rejection, approval expiry, drift, no-mutation, reset after assertion failure, and retained diagnostics. Add `reset_completed`, `cleanup_completed`, and `environment_invalidated` to `ExecutionResult`/`SuiteSummary`; raise a typed `EnvironmentInvalidatedError` on reset or cleanup failure. `run_environment` stops later scenarios, and `run_matrix` stops later environments while preserving summaries and diagnostics.

- [ ] **Step 6: Commit**

```bash
git add testbeds/scenarios testbeds/evidence tests
git commit -m "feat(scenarios): submit normalized facts to Guardian"
```

### Task 7: Activate integration, security, and replay gates

**Files:**
- Create: `tests/replay/test_deterministic_replay.py`
- Modify: `Taskfile.yml`
- Modify: `tools/verification-tools.yaml`
- Modify: `tests/unit/test_verification_harness.py`
- Modify: `docs/requirements/requirements.yaml`

- [ ] **Step 1: Write failing verification-harness tests**

Require all three targets to be active, invoke nonempty suite checks, and execute their directories. Assert `final` retains all three children.

- [ ] **Step 2: Run RED**

Run `TMPDIR=/tmp .tools/bin/uv run --locked pytest tests/unit/test_verification_harness.py -q`; expect baseline-capability assertions to fail.

- [ ] **Step 3: Add deterministic replay tests**

Run the same canonical facts repeatedly and with reordered duplicate delivery. Compare canonical projections byte-for-byte and assert one tenant-scoped parent. Confirm changing one fact changes the replay hash.

- [ ] **Step 4: Wire active Task targets**

Each target runs preflight, verification-harness nonempty collection, then pytest. `test:replay-deterministic` uses the same replay directory. Change manifest capabilities from baseline to active with `uv` and `pytest` dependencies.

- [ ] **Step 5: Update traceability without overclaiming**

Add concrete test paths only to requirements directly implemented by the local slice. Keep Temporal, controller, durable-store, and production-auth requirements incomplete. Run `requirements:render`, inspect generated diffs, then run `requirements:check` and report remaining unrelated normative gaps honestly.

- [ ] **Step 6: Verify and commit**

Run `task test:integration`, `task test:security`, `task test:replay`, and `task test:replay-deterministic`, then commit.

### Task 8: Provision pinned kind and orchestrate the local matrix

**Files:**
- Modify: `tools/verification-tools.yaml`
- Create: `scripts/bootstrap-kind.sh`
- Create: `scripts/create-test-cluster.sh`
- Create: `scripts/install-test-observability.sh`
- Create: `testbeds/observability/signoz-values.yaml`
- Create: `testbeds/observability/signoz-images.lock.yaml`
- Create: `scripts/run-local-matrix.sh`
- Create: `tests/unit/test_local_cluster_scripts.py`
- Modify: `scripts/test-environment.sh`
- Modify: `scripts/test-matrix.sh`
- Modify: `Taskfile.yml`

- [ ] **Step 1: Write failing structural/script tests**

Test pinned kind v0.31.0 URL and SHA-256 `eb244cbafcc157dff60cf68693c14c9a75c4e6e6fedaf9cd71c58117cb93e3fa`; digest-pinned node image `kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f`; metrics-server v0.9.0 manifest URL and SHA-256 `1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b`; SigNoz chart v0.133.0 archive URL and SHA-256 `103f127d1efe3e5f7c9ca87f224ce66b75bb7e688b72608530d11bcd72dbb6dc`; isolated kubeconfig; unique owned cluster name; collision refusal; partial-create cleanup; 6 GiB Docker node cap; default deletion; explicit retention; resource preflight artifact; cleanup traps; and stop-on-reset/cleanup failure.

- [ ] **Step 2: Run RED**

Run `TMPDIR=/tmp .tools/bin/uv run --locked pytest tests/unit/test_local_cluster_scripts.py -q`.

- [ ] **Step 3: Implement verified repository-local kind bootstrap**

Download to a `mktemp -d` directory, verify SHA-256 before install, and atomically move to `.tools/bin/kind`. Never overwrite an existing binary with a mismatched pinned version without reporting it.

- [ ] **Step 4: Implement isolated cluster lifecycle**

Use `GUARDIAN_CLUSTER_RUN_ID`, an artifact-owned kubeconfig, and exact cluster name `guardian-<run-id>`. Require Docker engine reachability, at least 2.5 GiB available memory, and 12 GiB free storage. Record preflight JSON. Immediately after kind creation run `docker update --memory 6g --memory-swap 8g guardian-<run-id>-control-plane` and verify the limits with `docker inspect`; failure invalidates and deletes the cluster. Download metrics-server `components.yaml` v0.9.0 into `mktemp -d`, verify the pinned SHA-256, patch only the kind-required kubelet TLS argument in the temporary manifest, apply it through the isolated kubeconfig, and wait for `apiservice/v1beta1.metrics.k8s.io` availability. Do not touch the user kubeconfig.

- [ ] **Step 5: Implement local API/matrix orchestration**

Add `install-test-observability.sh`: download SigNoz chart 0.133.0 to `mktemp -d`, verify SHA-256, and install the archive (never a mutable repository reference) into namespace `guardian-observability`. First render the chart, enumerate every workload/init-container image, resolve each through Docker to a registry digest, commit the complete mapping in `signoz-images.lock.yaml`, and override chart image values to those digests; a rendered mutable or unlisted image aborts installation. Commit `signoz-values.yaml` with `clickhouse.persistence.enabled=false`, one ClickHouse and one ZooKeeper replica, ClickHouse request/limit `200Mi/1200Mi`, ZooKeeper `128Mi/384Mi`, `signoz.persistence.enabled=false`, one SigNoz replica at `100Mi/384Mi`, and one OTLP collector at `128Mi/512Mi`; cap each at `500m` CPU and disable optional self-telemetry collectors not required for ingestion/query. Wait for ClickHouse, ZooKeeper, SigNoz, and OTLP collector readiness and verify live pod image IDs against the lock. Select two unused loopback ports with a Python bind check, start isolated-kubeconfig `kubectl port-forward` processes for SigNoz HTTP `8080` and OTLP HTTP `4318`, record their PIDs, wait for both endpoints, and include both processes in the wrapper EXIT trap. Generate a random local bearer token in memory, start `python -m apps.guardian_api`, wait for `/health`, export token/base URL/kubeconfig/forwarded SigNoz endpoints, execute the selected wrapper mode, and trap API, port-forwards, plus exact owned-cluster cleanup. The wrapper itself creates the cluster so its trap owns every later failure path. Preserve logs and artifacts. Never print the token.

- [ ] **Step 6: Verify scripts and commit**

Run structural tests, `shellcheck` if available, prerequisite failure paths without Docker, `task test:testbeds-unit`, `task test:testbeds-contract`, and `task test:architecture`. Update requirement traceability for executable baseline evidence and run `task requirements:check`. Commit the provisioning slice.

### Task 9: Start Docker Desktop and run real gates

**Files:**
- Modify only if verification exposes a concrete defect.
- Output: `artifacts/matrix/**`

- [ ] **Step 1: Start Docker Desktop safely**

Use PowerShell to start the installed Docker Desktop application if `docker info` is unavailable. Poll with bounded retries. Do not change unrelated Docker settings automatically; if WSL integration is disabled and cannot be enabled through supported CLI/config safely, stop and report the exact user action.

- [ ] **Step 2: Invoke the single cleanup-owned full run**

Run `scripts/run-local-matrix.sh --full` exactly once. It owns cluster creation and its cleanup trap from the first mutating command through the last gate. It verifies nodes, CoreDNS, metrics-server, storage class, SigNoz/ClickHouse/OTLP readiness, and namespaces using only the isolated kubeconfig. Do not create or consume the cluster outside this wrapper.

- [ ] **Step 3: Run mandatory repository gates independently**

Inside `--full`, run format, lint, typecheck, unit, contract, architecture, scenarios, requirements, integration, security, and replay. Capture every exit code independently without leaving the wrapper.

- [ ] **Step 4: Run each environment independently**

Still inside that same `--full` invocation, start the local API and invoke `task test:env ENV=<id>` for all five IDs. Preserve per-environment artifacts. An assertion failure may allow diagnostic execution of later environments after verified reset; a typed reset/cleanup invalidation stops further mutation immediately.

- [ ] **Step 5: Run matrix and final**

Before `--full` returns, run `task test:matrix` and `task final` against the same verified prerequisites. Confirm all report files are nonempty and every environment executed at least one scenario. Only then may the wrapper exit; its EXIT trap stops the API and port-forwards and deletes the exact run-owned cluster on success, assertion failure, interruption, or any intermediate error unless explicit retention was set before startup.

- [ ] **Step 6: Final verification and completion commit**

Use `superpowers:verification-before-completion`. Commit only concrete fixes exposed during real execution. Report passes, failures, skips, exact prerequisites, artifact paths, and whether the cluster was deleted or explicitly retained.
