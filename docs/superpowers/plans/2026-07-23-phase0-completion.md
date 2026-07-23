# Phase 0 Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close Phase 0 gaps—live evidence provider wiring, active deterministic replay, one-command local KinD orchestration, and PR/nightly CI—in one GitHub PR with four ordered commits.

**Architecture:** Extend the existing minimal Guardian runtime and testbed executor. A testbeds-only `ScenarioEvidenceProvider` becomes the sole evidence authority for assessment and recovery. Replay exercises the real in-memory store. Local scripts own pinned KinD + Guardian lifecycle. CI gates non-KinD suites on PRs and runs the KinD matrix nightly/manual.

**Tech Stack:** Python 3.13, pytest, Task, Bash, kind v0.31.0, Kubernetes kind node v1.35.0, metrics-server v0.9.0, SigNoz chart v0.133.0, GitHub Actions.

**Approved design:** `docs/superpowers/specs/2026-07-23-phase0-completion-design.md`

**Parent design:** `docs/superpowers/specs/2026-07-23-minimal-guardian-runtime-design.md`

**Branching:** Create branch `phase0-completion` from current `main` before Task 1. Open one PR at the end containing exactly four feature commits (plus any earlier design commits already on the branch). Do not add Postgres, Temporal, or NATS.

**Skills:** @superpowers/test-driven-development @superpowers/verification-before-completion

---

## File structure

- `testbeds/scenarios/evidence_provider.py` — `ScenarioEvidenceProvider` Protocol + `CollectorScenarioEvidenceProvider`
- `testbeds/scenarios/execution.py` — require provider; collect live; write evidence artifacts; delete `_refresh_sample_times` and static sample fields
- `testbeds/scenarios/registry.py` — construct env-specific provider in `build_adapter_registration`
- `tests/unit/test_scenario_execution.py` — fake providers instead of static samples
- `tests/unit/test_evidence_provider.py` — collector provider unit tests
- `tests/integration/test_live_evidence_lifecycle.py` — assessment vs recovery timestamps/provenance + artifact files
- `tests/replay/test_deterministic_replay.py` — canonical replay suite
- `Taskfile.yml` / `tools/verification-tools.yaml` — activate replay; add `local:*` / `test:phase0`
- `scripts/bootstrap-kind.sh`, `create-test-cluster.sh`, `install-test-observability.sh`, `local-up.sh`, `local-down.sh`, `test-phase0.sh`, `run-local-matrix.sh`
- `testbeds/observability/signoz-values.yaml`, `signoz-images.lock.yaml`
- `tests/unit/test_local_cluster_scripts.py`
- `tests/unit/test_github_workflows.py`
- `.github/workflows/pull-request.yml`, `kind-matrix.yml`
- `docs/requirements/requirements.yaml` — only requirements directly proven by new tests

---

## Commit 1 — Live evidence collection

### Task 1: Add ScenarioEvidenceProvider and fail closed without it

**Files:**
- Create: `testbeds/scenarios/evidence_provider.py`
- Modify: `testbeds/scenarios/execution.py`
- Modify: `tests/unit/test_scenario_execution.py`
- Test: `tests/unit/test_scenario_execution.py`

- [ ] **Step 1: Write the failing unit tests**

Replace `registration(..., evidence_samples=...)` helpers with a fake provider:

```python
@dataclass
class FakeEvidenceProvider:
    assessment: tuple[EvidenceSample | UnavailableEvidence, ...]
    recovery: tuple[EvidenceSample | UnavailableEvidence, ...]
    assessment_calls: int = 0
    recovery_calls: int = 0

    async def collect_assessment_evidence(self, **kwargs):
        self.assessment_calls += 1
        return self.assessment

    async def collect_recovery_evidence(self, **kwargs):
        self.recovery_calls += 1
        return self.recovery
```

Add tests that:
1. Executor calls `collect_assessment_evidence` after stimulus and before incident submit.
2. Executor calls `collect_recovery_evidence` after reset (not before).
3. Empty assessment return / missing required signals raises and fails closed.
4. Writing artifacts includes `assessment-evidence.json` and `recovery-evidence.json` when recovery runs.
5. `AdapterRegistration` construction without `evidence_provider` fails type/construction checks (required field).

Remove tests that pass bare `evidence_samples=` / `recovery_evidence_samples=`.

- [ ] **Step 2: Run RED**

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/unit/test_scenario_execution.py -q
```

Expected: FAIL — `evidence_provider` / Protocol / artifact names absent; static sample path still present.

- [ ] **Step 3: Implement Protocol + executor wiring (minimal)**

Create `testbeds/scenarios/evidence_provider.py`:

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

Use `from __future__ import annotations` and `TYPE_CHECKING` to avoid import cycles with `AdapterRegistration`.

In `execution.py`:
- Add required `evidence_provider: ScenarioEvidenceProvider` to `AdapterRegistration`.
- Delete `evidence_samples` and `recovery_evidence_samples`.
- Delete `_refresh_sample_times`, `_evidence_for_context`, `_recovery_evidence_for_context`, `_telemetry_only_healthy_samples`, and `_SPIKE_VALUE_KEYS`.
- After stimulus observations, `await registration.evidence_provider.collect_assessment_evidence(...)`.
- After reset baseline, `await registration.evidence_provider.collect_recovery_evidence(...)`.
- `writer.write("assessment-evidence.json", assessment_samples)` and same for recovery.
- If required signals missing from samples, raise existing `MissingEvidenceError`.

- [ ] **Step 4: Run GREEN on unit execution tests**

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/unit/test_scenario_execution.py -q
```

Expected: PASS.

- [ ] **Step 5: Update other call sites that construct AdapterRegistration**

Update in place (same commit, still TDD via existing failures):
- `tests/unit/test_scenario_facts.py` — facts still take samples; no registration change unless it builds one
- `tests/integration/test_legitimate_demand_scale_up.py`
- `tests/integration/test_scenario_guardian_lifecycle.py` (if it builds registration)
- `tests/security/test_scenario_guardian_security.py`
- `tests/contract/test_scenario_execution_contract.py`
- `testbeds/scenarios/registry.py` — temporary: raise `NotImplementedError("evidence provider required")` until Task 2 if needed; prefer implementing Task 2 immediately after

Run:

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/unit/test_scenario_execution.py \
  tests/contract/test_scenario_execution_contract.py \
  tests/integration/test_legitimate_demand_scale_up.py \
  tests/security/test_scenario_guardian_security.py -q
```

Fix until green with fakes only (collector provider is Task 2).

---

### Task 2: Environment-specific CollectorScenarioEvidenceProvider

**Files:**
- Modify: `testbeds/scenarios/evidence_provider.py`
- Modify: `testbeds/scenarios/registry.py`
- Modify: `tests/contract/test_scenario_execution_contract.py`
- Test: `tests/unit/test_evidence_provider.py` (create)

- [ ] **Step 1: Write failing provider tests**

```python
@pytest.mark.asyncio
async def test_collector_provider_samples_after_assessment_request(fake_collector_deps):
    provider = CollectorScenarioEvidenceProvider(...)
    samples = await provider.collect_assessment_evidence(...)
    assert samples  # provenance_ref non-empty; observed_at from clock
    assert all(getattr(s, "provenance_ref", None) for s in samples)

@pytest.mark.asyncio
async def test_recovery_collection_uses_distinct_clock_and_provenance(fake_collector_deps):
    ...
```

Prove recovery call does not return the assessment object identity / same provenance for spike signals. Fail closed when collector returns unavailable for a required signal.

- [ ] **Step 2: Run RED**

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/unit/test_evidence_provider.py -q
```

Expected: FAIL — `CollectorScenarioEvidenceProvider` missing.

- [ ] **Step 3: Implement CollectorScenarioEvidenceProvider**

Wrap existing `EvidenceCollector` methods. Map scenario required evidence types via `required_evidence_sources_for_scenario` / `_required_signals`. Probe targets come from environment-specific config constructed in `build_adapter_registration()` (namespace, workload, endpoint URL placeholders from adapter release). Do **not** fabricate SigNoz samples; only call SigNoz paths when the env provider config includes an approved deterministic SigNoz contract handle.

Wire in `registry.py` for **every** entry in `SUPPORTED_ENVIRONMENTS` (not only
otel-demo). Each environment gets its own targets config (namespace, workload,
endpoint). Sketch:

```python
return AdapterRegistration(
    ...,
    evidence_provider=CollectorScenarioEvidenceProvider(
        collector=EvidenceCollector(...),
        targets=EVIDENCE_TARGETS_BY_ENV[environment],
    ),
)
```

Contract tests call `build_adapter_registration` per environment; leaving any
env without a provider will fail closed incorrectly or raise at construction.
- [ ] **Step 4: GREEN**

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/unit/test_evidence_provider.py \
  tests/contract/test_scenario_execution_contract.py -q
```

Expected: PASS.

---

### Task 3: Lifecycle integration test for distinct assessment vs recovery evidence

**Files:**
- Create: `tests/integration/test_live_evidence_lifecycle.py`
- Modify: `docs/requirements/requirements.yaml` (only if a testbed evidence requirement is newly proven)

- [ ] **Step 1: Write the failing integration test**

Use real `ScenarioExecutor`, loopback Guardian API (same pattern as `test_legitimate_demand_scale_up.py`), controlled adapter, and a provider that records distinct assessment/recovery samples with different `observed_at` and `provenance_ref`.

Assert:
- `result.status` passed (or at least evidence artifacts written before any later failure — prefer full pass)
- `(artifact / "assessment-evidence.json").exists()`
- `(artifact / "recovery-evidence.json").exists()`
- parsed assessment[0].observed_at != recovery[0].observed_at
- assessment provenance_ref != recovery provenance_ref
- provider.assessment_calls == 1 and provider.recovery_calls == 1

- [ ] **Step 2: Run RED**

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/integration/test_live_evidence_lifecycle.py -q
```

Expected: FAIL until executor writes both artifacts and uses provider (should already be green after Tasks 1–2; if green immediately, the test is wrong—ensure it would fail on the old static-refresh path by asserting call counts / provenance inequality the old path cannot satisfy).

- [ ] **Step 3: Fix any remaining executor/artifact gaps; GREEN**

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/integration/test_live_evidence_lifecycle.py \
  tests/integration/test_legitimate_demand_scale_up.py \
  tests/unit/test_scenario_execution.py -q
```

- [ ] **Step 4: Commit 1**

```bash
git add testbeds/scenarios/evidence_provider.py \
  testbeds/scenarios/execution.py \
  testbeds/scenarios/registry.py \
  tests/unit/test_scenario_execution.py \
  tests/unit/test_evidence_provider.py \
  tests/integration/test_live_evidence_lifecycle.py \
  tests/integration/test_legitimate_demand_scale_up.py \
  tests/integration/test_scenario_guardian_lifecycle.py \
  tests/security/test_scenario_guardian_security.py \
  tests/contract/test_scenario_execution_contract.py \
  docs/requirements/requirements.yaml docs/requirements/coverage.md
git commit -m "$(cat <<'EOF'
feat(testbeds): collect live assessment and recovery evidence

Replace static sample injection with ScenarioEvidenceProvider, persist
assessment/recovery evidence artifacts, and fail closed on missing evidence.
EOF
)"
```

Run before commit if requirements changed:

```bash
.tools/bin/task requirements:render
.tools/bin/task requirements:check
```

---

## Commit 2 — Deterministic replay

### Task 4: Replay suite against empty IncidentStore

**Files:**
- Create: `tests/replay/__init__.py`
- Create: `tests/replay/test_deterministic_replay.py`
- Create: `tests/replay/fixtures/` (optional JSON; prefer in-test builders)

- [ ] **Step 1: Write failing replay tests**

```python
def projection_hash(projection: GuardianProjection) -> str:
    canonical = projection.model_dump_json(by_alias=True)
    # Prefer store-style sorted canonical JSON if a helper exists;
    # otherwise use the same canonical_json pattern as apps.guardian_api.store.
    return hashlib.sha256(canonical.encode()).hexdigest()

def test_full_replay_hashes_are_stable():
    events = canonical_event_sequence()
    first = replay_all(events)
    second = replay_all(events)
    assert first == second

def test_prefix_replays_match_full_history_prefixes():
    ...

def test_duplicate_idempotent_delivery_unchanged_projection():
    ...

def test_reordered_duplicate_delivery_converges():
    # Example: accept incident; deliver the same observation twice with the
    # same idempotency key; also try observation-then-duplicate in swapped
    # scheduling relative to a no-op second submit of the same incident key.
    ...

def test_changing_one_fact_changes_hash():
    ...
```

`replay_all` builds empty `IncidentStore` / `GuardianService`, applies events, returns tuple of projection hashes from `projection_history`.

- [ ] **Step 2: Run RED**

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest tests/replay -q
```

Expected: FAIL (module/assertions) — directory may be empty today.

- [ ] **Step 3: Implement fixtures + helpers until GREEN**

Use real models from `apps.guardian_api.models`. No scenario IDs. Prefer copying shapes from existing unit domain tests.

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest tests/replay -q
```

Expected: PASS.

---

### Task 5: Activate test:replay and test:replay-deterministic

**Files:**
- Modify: `Taskfile.yml`
- Modify: `tools/verification-tools.yaml`
- Modify: `tests/unit/test_verification_harness.py`
- Modify: `docs/requirements/requirements.yaml` (TST-GRD-MLO-002-REPLAY paths only)

- [ ] **Step 1: Write failing harness expectations**

Remove `test:replay` and `test:replay-deterministic` from `baseline_targets` in `test_verification_harness.py`. Expect `availability: active` and dependencies `[task, uv, pytest]`.

- [ ] **Step 2: Run RED**

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/unit/test_verification_harness.py -q
```

Expected: FAIL on baseline set mismatch.

- [ ] **Step 3: Wire Task + manifest**

`Taskfile.yml`:

```yaml
  test:replay:
    cmds:
      - "{{.PREFLIGHT}} test:replay"
      - "{{.UV}} run --locked --no-sync python -m tools.verification_harness suite test:replay tests/replay"
      - "{{.UV}} run --locked pytest tests/replay"

  test:replay-deterministic:
    cmds:
      - "{{.PREFLIGHT}} test:replay-deterministic"
      - "{{.UV}} run --locked --no-sync python -m tools.verification_harness suite test:replay-deterministic tests/replay"
      - "{{.UV}} run --locked pytest tests/replay"
```

Update `tools/verification-tools.yaml` capabilities to `active` and dependencies to `[task, uv, pytest]`.

Point `TST-GRD-MLO-002-REPLAY` at `tests/replay/test_deterministic_replay.py`. Do not mark Temporal/durable requirements complete.

- [ ] **Step 4: GREEN + Task targets**

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/unit/test_verification_harness.py -q
.tools/bin/task requirements:render
.tools/bin/task requirements:check
.tools/bin/task test:replay
.tools/bin/task test:replay-deterministic
```

Expected: all PASS / exit 0.

- [ ] **Step 5: Commit 2**

```bash
git add tests/replay Taskfile.yml tools/verification-tools.yaml \
  tests/unit/test_verification_harness.py \
  docs/requirements/requirements.yaml docs/requirements/coverage.md
git commit -m "$(cat <<'EOF'
feat(replay): activate deterministic projection replay suite

EOF
)"
```

---

## Commit 3 — One-command local orchestration

### Task 6: Structural tests for pinned cluster scripts

**Files:**
- Create: `tests/unit/test_local_cluster_scripts.py`
- Create: `scripts/bootstrap-kind.sh`
- Create: `scripts/create-test-cluster.sh`
- Create: `scripts/install-test-observability.sh`
- Create: `scripts/local-up.sh`
- Create: `scripts/local-down.sh`
- Create: `scripts/test-phase0.sh`
- Create: `scripts/run-local-matrix.sh` (full sequential five-env wrapper; optional for smoke)
- Create: `testbeds/observability/signoz-values.yaml`
- Create: `testbeds/observability/signoz-images.lock.yaml`

Pins (must appear literally in scripts/tests):

| Component | Pin |
| --- | --- |
| kind | v0.31.0 / SHA-256 `eb244cbafcc157dff60cf68693c14c9a75c4e6e6fedaf9cd71c58117cb93e3fa` |
| node image | `kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f` |
| metrics-server | v0.9.0 / SHA-256 `1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b` |
| SigNoz chart | v0.133.0 / SHA-256 `103f127d1efe3e5f7c9ca87f224ce66b75bb7e688b72608530d11bcd72dbb6dc` |

- [ ] **Step 1: Write failing structural tests**

Assert scripts exist, contain pins, use isolated kubeconfig paths under `artifacts/local/`, refuse colliding cluster names, mention Docker Desktop/engine reachability, memory/disk preflight, `docker update --memory 6g`, never echo tokens, and that `local-up` / `local-down` are paired.

- [ ] **Step 2: Run RED**

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/unit/test_local_cluster_scripts.py -q
```

Expected: FAIL — scripts missing.

- [ ] **Step 3: Implement scripts (minimal complete)**

Use pin values and isolation rules from parent **plan**
`docs/superpowers/plans/2026-07-23-minimal-guardian-runtime.md` Task 8
**Steps 3–5 only** (bootstrap-kind, create-test-cluster, install-test-observability,
SigNoz values/lock). Do **not** copy that plan’s single EXIT-trap
`run-local-matrix.sh --full` lifecycle as the primary UX.

This plan’s orchestration model is **leave-up**:
- `local-up.sh` creates cluster, installs deps, starts Guardian + port-forwards,
  writes `artifacts/local/<run-id>/env`, and **exits 0 while leaving processes
  running** (record PIDs for later teardown).
- `local-down.sh` stops those PIDs and deletes the exact owned cluster unless
  `GUARDIAN_CLUSTER_RETAIN=1`.
- `run-local-matrix.sh` may still offer a trapped full-matrix mode for nightly,
  but `task local:up` / `local:down` must work as a separable pair.

`local-up.sh` must never print the bearer token. Include Docker engine
reachability (and Docker Desktop/WSL notes where relevant) in preflight.

- [ ] **Step 4: GREEN structural tests**

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/unit/test_local_cluster_scripts.py -q
```

Expected: PASS.

---

### Task 7: Task targets local:up / test:phase0 / local:down

**Files:**
- Modify: `Taskfile.yml`
- Modify: `tools/verification-tools.yaml`
- Modify: `tests/unit/test_verification_harness.py` (if new targets are mandatory; otherwise document as optional orchestration targets)
- Modify: `scripts/test-environment.sh` only if needed to source `artifacts/local/.../env`

- [ ] **Step 1: Add Task targets**

```yaml
  local:up:
    desc: Create KinD, install deps, start Guardian, write run env
    cmds:
      - ./scripts/local-up.sh

  local:down:
    desc: Stop Guardian and delete run-owned KinD cluster
    cmds:
      - ./scripts/local-down.sh

  test:phase0:
    desc: Phase 0 proof against a live local:up stack (one env E2E)
    cmds:
      - "{{.PREFLIGHT}} test:phase0"
      - ./scripts/test-phase0.sh
```

`scripts/test-phase0.sh` sources the active local env file, runs `task test:replay`, then `task test:env ENV=otel-demo` (or the first supported env with live evidence), fails closed on reset/cleanup invalidation.

- [ ] **Step 2: Verify Task parses**

```bash
.tools/bin/task --list
```

Expected: shows `local:up`, `local:down`, `test:phase0`.

- [ ] **Step 3: Real Docker proof (required for PR Done)**

On a Docker-capable machine:

```bash
.tools/bin/task local:up
.tools/bin/task test:phase0
.tools/bin/task local:down
```

Record exit codes and artifact paths in the PR description. If Docker is unavailable in the agent environment, stop and report the exact blocker; do not claim commit 3 complete.

- [ ] **Step 4: Commit 3**

```bash
git add scripts/bootstrap-kind.sh scripts/create-test-cluster.sh \
  scripts/install-test-observability.sh scripts/local-up.sh \
  scripts/local-down.sh scripts/test-phase0.sh scripts/run-local-matrix.sh \
  testbeds/observability Taskfile.yml tools/verification-tools.yaml \
  tests/unit/test_local_cluster_scripts.py \
  tests/unit/test_verification_harness.py \
  docs/requirements/requirements.yaml docs/requirements/coverage.md
git commit -m "$(cat <<'EOF'
feat(local): add one-command KinD stack orchestration

EOF
)"
```

---

## Commit 4 — CI

### Task 8: Pull-request and nightly workflows

**Files:**
- Create: `.github/workflows/pull-request.yml`
- Create: `.github/workflows/kind-matrix.yml`
- Create: `tests/unit/test_github_workflows.py` (structural)

- [ ] **Step 1: Write failing workflow structure tests**

Assert PR workflow lists exactly the required Task invocations (`format:check`, `lint`, `typecheck`, `test:unit`, `test:contract`, `test:architecture`, `test:integration`, `test:security`, `test:replay`, `requirements:check`), uploads artifacts, and does **not** invoke `test:matrix` / `local:up`.

Assert nightly workflow has `schedule` + `workflow_dispatch`, runs matrix path, uploads diagnostics.

- [ ] **Step 2: Run RED**

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/unit/test_github_workflows.py -q
```

Expected: FAIL — workflows missing.

- [ ] **Step 3: Implement workflows**

PR workflow sketch:

```yaml
name: pull-request
on: [pull_request]
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ./scripts/bootstrap.sh
      - run: .tools/bin/task format:check
      - run: .tools/bin/task lint
      - run: .tools/bin/task typecheck
      - run: .tools/bin/task test:unit
      - run: .tools/bin/task test:contract
      - run: .tools/bin/task test:architecture
      - run: .tools/bin/task test:integration
      - run: .tools/bin/task test:security
      - run: .tools/bin/task test:replay
      - run: .tools/bin/task requirements:check
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: scenario-artifacts
          path: artifacts/
          if-no-files-found: ignore
```

Nightly/manual: bootstrap → `local:up` → matrix → upload artifacts → `local:down` (retain only if flag set).

- [ ] **Step 4: GREEN**

```bash
TMPDIR=/tmp .tools/bin/uv run --locked pytest \
  tests/unit/test_github_workflows.py -q
```

- [ ] **Step 5: Commit 4**

```bash
git add .github/workflows/pull-request.yml \
  .github/workflows/kind-matrix.yml \
  tests/unit/test_github_workflows.py
git commit -m "$(cat <<'EOF'
ci: add pull-request gates and nightly KinD matrix

EOF
)"
```

---

## Final verification before PR

- [ ] **Step 1: Non-KinD gates**

```bash
.tools/bin/task format:check
.tools/bin/task lint
.tools/bin/task typecheck
.tools/bin/task test:unit
.tools/bin/task test:contract
.tools/bin/task test:architecture
.tools/bin/task test:integration
.tools/bin/task test:security
.tools/bin/task test:replay
.tools/bin/task requirements:check
```

- [ ] **Step 2: Confirm four feature commits**

```bash
git log --oneline origin/main..HEAD
```

Expected: design commits (if present) + exactly four feature commits matching Tasks above.

- [ ] **Step 3: Open one PR**

```bash
git push -u origin HEAD
gh pr create --title "Phase 0: live evidence, replay, local orchestration, CI" --body "$(cat <<'EOF'
## Summary
- Live `ScenarioEvidenceProvider` with assessment/recovery artifacts
- Active deterministic replay suite
- `task local:up` / `test:phase0` / `local:down`
- PR CI + nightly/manual KinD matrix workflow

## Test plan
- [x] Non-KinD Task gates
- [ ] `local:up` → `test:phase0` → `local:down` (report results)
- [ ] Nightly workflow manual dispatch once

## Phase 0 exit gaps (honest)
- Five-env matrix / `task final` may still be incomplete at merge
EOF
)"
```

Use @superpowers/verification-before-completion before claiming any gate green.

---

## Out of scope

Postgres, Temporal, NATS, production auth providers, promoting smoke KinD to required PR status, claiming full `GRD-MLO-002` beyond local store replay.
