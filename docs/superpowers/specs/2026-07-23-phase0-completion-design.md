# Phase 0 Completion Design

## Goal

Close Phase 0 gaps that remain after the minimal Guardian runtime foundation:
live scenario evidence collection (no static injection), an active deterministic
replay suite, one-command local KinD orchestration, and CI that gates pull
requests without requiring the full five-environment matrix on every change.

This design is an addendum to
`docs/superpowers/specs/2026-07-23-minimal-guardian-runtime-design.md`. It does
not reopen settled domain, HTTP, or recommendation-only mutation semantics.

## Delivery model

Ship as **one pull request** with **four ordered commits**:

1. Wire live evidence collection
2. Activate deterministic replay
3. Add one-command local orchestration
4. Add CI

Each commit is test-first, keeps its focused gates green, and updates
requirement traceability only for requirements its tests directly implement.
Do not add Postgres, Temporal, NATS, or further production service decomposition
in this PR.

### Done definition for this PR

This PR is mergeable when commits 1–4 are complete and verified as follows:

- Commit 1–2 gates pass without KinD (unit, contract, integration, security,
  replay, architecture, requirements as applicable).
- Commit 3 is complete when structural script tests pass **and** a real
  `local:up` → at least one environment end-to-end → `local:down` proof has
  been run on a machine with Docker and reported in the PR (CI does not run
  KinD yet).
- Commit 4 lands the PR and nightly workflows; PR CI stays green on the
  non-KinD gate set.

A green five-environment matrix and a green `task final` are **Phase 0 exit
conditions**, not merge blockers for this PR. Nightly/manual matrix may still
be red or flaky at merge time; remaining gaps must be reported honestly and
must not be papered over by marking matrix requirements complete.

## Normative boundaries

Preserve all safety rules from `AGENTS.md` and the parent design:

- Production code must not import `testbeds`.
- The reasoner must never receive Kubernetes write credentials.
- The model must not authorize actions or change scores/eligibility.
- Missing, stale, or conflicting evidence fails closed.
- No demo-specific service names in production decision logic.
- No secrets in prompts, logs, fixtures, or audit payloads.

Deterministic replay covers only the local subset of `GRD-MLO-002`. Scenario
tests that mention Temporal, durable tenant isolation, or real mutation remain
projection/contract checks and must not mark those requirements complete.

## Commit 1 — Live evidence collection

### Problem

`ScenarioExecutor` still reads static `evidence_samples` /
`recovery_evidence_samples` from `AdapterRegistration` and stamps them with
`_refresh_sample_times()`. `EvidenceCollector` exists under `testbeds/evidence/`
but is not the lifecycle authority. Recovery may fall back to filtering
assessment samples rather than collecting a new window.

### Interface

Introduce a testbeds-only asynchronous provider:

```python
class ScenarioEvidenceProvider(Protocol):
    async def collect_assessment_evidence(
        self,
        *,
        scenario: GuardianScenarioV1Alpha2,
        registration: AdapterRegistration,
        observations: Sequence[EnvironmentState],
        control_results: Mapping[str, Any],
    ) -> tuple[EvidenceSample | UnavailableEvidence, ...]: ...

    async def collect_recovery_evidence(
        self,
        *,
        scenario: GuardianScenarioV1Alpha2,
        registration: AdapterRegistration,
        post_reset_state: EnvironmentState,
    ) -> tuple[EvidenceSample | UnavailableEvidence, ...]: ...
```

`EvidenceResult` in prose maps to the existing
`EvidenceSample | UnavailableEvidence` union; do not invent a parallel type
unless serialization requires a thin wrapper.

### Wiring

- `AdapterRegistration` gains a required `evidence_provider`.
- Delete `evidence_samples` and `recovery_evidence_samples` from
  `AdapterRegistration`; the executor must not accept injected sample tuples.
- `build_adapter_registration()` constructs an environment-specific provider
  that wraps `EvidenceCollector` with environment probe targets declared by
  that environment's evidence contracts (endpoint, Kubernetes workload, metrics
  API, rollout, queue/scaler, and SigNoz only when an approved deterministic
  SigNoz evidence contract is installed for that path).
- Unit and integration tests may supply Protocol fakes. Fakes must not reuse
  assessment tuples as recovery evidence.
- Align with the parent design: SigNoz-dependent scenarios remain unsupported
  unless that deterministic SigNoz contract is present; the provider must not
  fabricate SigNoz samples to force compatibility.

### Lifecycle

1. After applying load, fault, and/or deployment (and observing state), call
   `collect_assessment_evidence`.
2. After reset and healthy baseline, call `collect_recovery_evidence` for an
   entirely new sample set.
3. Persist raw samples and provenance to `assessment-evidence.json` and
   `recovery-evidence.json` under the execution artifact directory.
4. Delete `_refresh_sample_times()` and the telemetry-only recovery fallback
   that reuses assessment spike samples.
5. If required signals cannot be collected, raise `MissingEvidenceError` and
   fail the run closed.

### Verification

Add one lifecycle integration test proving assessment and recovery evidence
differ in `observed_at` and `provenance_ref`, and that both artifact files
exist. Use the real executor plus a Protocol provider backed by controlled
collector runners. KinD is not required for this commit.

## Commit 2 — Deterministic replay

### Problem

`test:replay` and `test:replay-deterministic` are baseline placeholders. The
verification manifest marks them as having no tests. There is no replay suite.

### Approach

Replay against the real in-memory `IncidentStore` and `evaluate_incident` path.
Fixtures are canonical `IncidentSubmission` and ordered `ObservationUpdate`
events only—no scenario identifiers and no expected blocks.

### Suite requirements

`tests/replay/test_deterministic_replay.py` must:

1. Capture a fixed canonical incident and observation event sequence.
2. Replay it into an empty store and hash every `GuardianProjection` in
   `projection_history` (canonical JSON → SHA-256, consistent with existing
   store hashing helpers).
3. Test every prefix of the event sequence; prefix hashes must match the
   corresponding full-run history prefix.
4. Run the same full replay at least twice and require identical hash sequences.
5. Verify duplicate events (same tenant-scoped idempotency key and payload) do
   not change the projection and preserve a single parent `workflow_id`.
6. Verify **reordered duplicate** delivery (duplicate payloads presented in a
   different order than the first acceptance path allows) still converges to
   the same projection hashes and a single parent workflow, matching the parent
   design's replay gate.
7. Change one fact and confirm the hash changes so the oracle is not vacuous.

### Activation

- `Taskfile.yml`: both replay targets run verification-harness nonempty suite
  checks and `pytest tests/replay`.
- `tools/verification-tools.yaml`: mark capabilities active; drop “no tests are
  configured”; declare `uv` and `pytest` dependencies.
- Update harness unit tests accordingly.
- Update requirement traceability only for the local replay subset.

## Commit 3 — One-command local orchestration

### Problem

`scripts/test-environment.sh` and `scripts/test-matrix.sh` expect
`GUARDIAN_BASE_URL`, `GUARDIAN_SCENARIO_TOKEN`, tools, and a Kubernetes cluster
to already exist. They do not create KinD or launch the complete stack.

### Task surface

| Target | Behavior |
| --- | --- |
| `task local:up` | Create owned KinD cluster; install pinned operators and telemetry; start Guardian; create token configuration; perform readiness checks; leave the stack up and write an env file under the run artifact directory |
| `task test:phase0` | Against a live `local:up` stack, run Phase 0 proof gates including at least one real environment end-to-end; fail closed on reset/cleanup invalidation |
| `task local:down` | Stop Guardian and port-forwards; delete the exact run-owned cluster unless retention was explicitly set |

### Scripts and pins

Reuse the parent plan pins and isolation rules:

- kind v0.31.0 with verified SHA-256 into `.tools/bin`
- digest-pinned `kindest/node:v1.35.0`
- metrics-server v0.9.0 manifest with verified SHA-256
- SigNoz chart v0.133.0 archive with verified SHA-256 and digest-locked images
- Isolated kubeconfig; cluster name `guardian-<run-id>`
- Resource preflight (≥2.5 GiB available memory, ≥12 GiB free storage)
- 6 GiB Docker node memory cap when supported
- No mutation of the user kubeconfig
- Collision refusal and partial-create cleanup
- EXIT traps own API, port-forwards, and cluster cleanup
- Never print bearer tokens

State lives under `artifacts/local/<run-id>/` (kubeconfig, PIDs, env file,
preflight JSON, logs). Keep a full-matrix wrapper for sequential five-environment
runs; `test:phase0` may exercise a smoke subset (one environment) while the
matrix remains the heavy path.

### Verification

Structural unit tests cover pins, isolation, and cleanup contracts. Before this
PR merges, run a real `local:up` → at least one environment end-to-end →
`local:down` on a Docker-capable machine and report the result in the PR. That
manual/local proof is required for commit 3 completeness even though PR CI does
not execute KinD.

## Commit 4 — CI

### Problem

The repository has no pull-request workflow beyond `.github/CODEOWNERS`. The
KinD five-environment matrix is too expensive to require on every PR until
stable.

### Pull-request workflow

`.github/workflows/pull-request.yml` runs on `pull_request` and executes:

- `format:check`
- `lint`
- `typecheck`
- `test:unit`
- `test:contract`
- `test:architecture`
- `test:integration`
- `test:security`
- `test:replay`
- `requirements:check`

Upload scenario and integration artifacts on failure (and optionally always).
Do not require KinD, `test:matrix`, or `local:up` on pull requests in this
commit.

### Nightly and manual matrix

`.github/workflows/kind-matrix.yml` runs on a nightly schedule and
`workflow_dispatch`. It bootstraps tools, brings the stack up, runs the
five-environment matrix, publishes artifacts and diagnostics, and tears the
stack down. Failures preserve diagnostics; retention requires an explicit flag.

After nightly is stable, a later change may add a smaller smoke environment as a
required PR check. That promotion is out of scope for the initial CI commit.

## Phase 0 exit conditions

These are the criteria for starting the full production architecture. They are
**broader than this PR's merge Done definition** (see Delivery model).

Do not begin the full production architecture until all of the following are
true, and report remaining gaps honestly if any are incomplete after this PR
merges:

- Live evidence is collected rather than injected (this PR commit 1)
- Recovery uses genuinely new evidence (this PR commit 1)
- Deterministic replay is active (this PR commit 2)
- A fresh machine can launch the stack with one command (this PR commit 3)
- At least one real environment passes end to end (this PR commit 3 proof)
- All five environment suites pass (may land via nightly hardening after merge)
- CI publishes scenario artifacts and diagnostics (this PR commit 4)
- `task final` passes (includes matrix; may remain incomplete until five-env
  nightly is stable)

## Explicit exclusions

This PR does not implement NATS, Temporal, PostgreSQL, OPA distribution,
production SigNoz Query Contract execution as a substitute for collector
contracts, Kubernetes mutation controllers, KEDA scaler gRPC, Slack, web UI,
model inference, or production authentication providers.
