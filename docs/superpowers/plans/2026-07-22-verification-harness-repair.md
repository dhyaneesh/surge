# Verification Harness Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a pinned repository-local Linux/WSL `amd64` toolchain and deterministic Task targets that distinguish prerequisite defects from existing repository baseline failures.

**Architecture:** A machine-readable `tools/verification-tools.yaml` is the shared source for pinned tool metadata, target prerequisites, and suite capabilities. `scripts/bootstrap.sh` installs checksum-verified `uv` and `task` into `.tools/bin`; a Python harness invoked through repository-local `uv` validates target prerequisites and suite availability before Task executes checks. Aggregate targets preflight every child before running any child.

**Tech Stack:** Bash, Task 3, Python 3.13, PyYAML, pytest, unittest, Ruff, Pyright, pinned `uv` and `task` release archives.

---

### Task 0: Normative applicability and traceability decision

**Files:**
- Modify: `docs/requirements/requirements.yaml` if the audit confirms a supporting design mapping
- Modify: `docs/requirements/coverage.md` only through intentional scoped rendering

- [ ] Search `docs/spec/guardian-production-v1.md` and `docs/requirements/requirements.yaml` for normative requirements governing repository bootstrap, local developer tooling, or Task orchestration.
- [ ] Record the evidence-backed conclusion. Do not invent a `GRD-*` or `AT-*` identifier when none applies.
- [ ] If no normative ID applies, add a separate non-normative `DESIGN-HARNESS-001` capability using the existing `design_capabilities` schema, linking the harness implementation and tests while keeping normative requirements separate.
- [ ] Run registry validation and `requirements:check`; record existing unrelated traceability failures honestly.

### Task 1: Harness contract tests and dependency manifest

**Files:**
- Create: `tools/verification-tools.yaml`
- Create: `tests/unit/test_verification_harness.py`
- Modify: `.gitignore`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] Add failing tests that parse `Taskfile.yml`, every applicable `AGENTS.md`, and the tool manifest, then assert every referenced/current target is defined and represented in the manifest.
- [ ] Test the mandatory target set: `bootstrap`, `format`, `format:check`, `lint`, `lint:online-boutique`, `typecheck`, `test:unit`, `test:contract`, `test:integration`, `test:architecture`, `test:testbeds-unit`, `test:testbeds-contract`, `test:policy`, `test:replay`, `test:replay-deterministic`, `test:security`, `test:reasoner`, `test:keda-scaler`, `test:action-controller`, `test:requirements`, `test:env`, `test:matrix`, `requirements:render`, `requirements:check`, and `final`.
- [ ] Test that `.tools/` is ignored and that the manifest declares `linux/amd64`, host prerequisites, pinned `uv`/`task` versions, release URLs, SHA-256 values, and target dependency/capability mappings.
- [ ] Run `uv run pytest tests/unit/test_verification_harness.py -q`; verify RED because the manifest and target definitions are absent.
- [ ] Add the minimal manifest schema and `.gitignore` entry. Pin exact current release versions and literal checksums obtained from official release checksum assets; do not use `latest` URLs.
- [ ] Add a failing dependency-consistency test, then explicitly declare Pyright, PyYAML typing support, and every Python command named by the manifest. Regenerate `uv.lock` and run `uv sync --locked` before any Pyright verification in later tasks.
- [ ] Rerun the target/manifest tests until only missing harness behavior fails.

### Task 2: Prerequisite and baseline classifier

**Files:**
- Create: `tools/verification_harness.py`
- Create: `scripts/verification-preflight.sh`
- Modify: `tests/unit/test_verification_harness.py`

- [ ] Add failing subprocess/unit tests for:
  - missing `.tools/bin/uv` emits `[prerequisite]` and exits nonzero;
  - wrong local tool version emits `[prerequisite]` before a sentinel child command runs;
  - an empty or absent suite emits `[baseline] <target>: no tests are configured`;
  - aggregate preflight with any missing prerequisite or capability runs zero children;
  - a valid target returns zero and identifies its command prerequisites;
  - every manifest target has commands/dependencies and every Task command is covered.
- [ ] Run the focused tests and verify the failures are caused by the missing module.
- [ ] Implement a CLI with these stable operations:

```text
python -m tools.verification_harness preflight <target>
python -m tools.verification_harness aggregate <target>
python -m tools.verification_harness suite <target> [paths...]
python -m tools.verification_harness manifest-check
```

- [ ] Load YAML safely, resolve repository-relative paths, validate pinned local tool versions, resolve Python-environment commands with `uv run`, distinguish `prerequisite` and `baseline` failures, and return stable nonzero exit codes.
- [ ] Implement `scripts/verification-preflight.sh` as the bootstrap-independent first boundary. It checks repository-local `uv` existence and pinned version using host shell utilities before invoking Python through `uv`; a missing/wrong `uv` therefore emits `[prerequisite]` instead of a shell `command not found`. Every ordinary and aggregate Task target calls this script first.
- [ ] Ensure `aggregate` validates the union of child prerequisites and suite capabilities before returning success; it must never execute children itself.
- [ ] Run the focused tests to GREEN, then use the already-declared locked Ruff and Pyright dependencies on the helper/test.

### Task 3: Pinned bootstrap installer

**Files:**
- Modify: `scripts/bootstrap.sh`
- Create: `tests/unit/test_bootstrap.py`

- [ ] Add failing tests using temporary directories, fake release tarballs, fake checksums, and controlled `PATH` for:
  - unsupported OS/architecture rejection;
  - each missing host prerequisite producing `[prerequisite]` before download;
  - checksum mismatch leaving no installed executable;
  - correct pinned tools installed atomically;
  - matching tools reused idempotently;
  - wrong tool versions replaced;
  - `.tools/bin/uv sync --locked` invoked;
  - final manifest preflight invoked and exact `.tools/bin/task` usage printed.
- [ ] Run `uv run pytest tests/unit/test_bootstrap.py -q`; verify RED against the current uv-only prerequisite script.
- [ ] Implement `bootstrap.sh` with strict shell mode, Linux/WSL amd64 validation, manifest field loading, complete host prerequisite preflight, temporary extraction directories, literal checksum comparison, version verification, and atomic install.
- [ ] Support test-only overrides for repository root, manifest, tools directory, platform, and artifact base while keeping production defaults fixed.
- [ ] Add a trap that removes only the validated temporary directory; never remove the repository `.tools` root recursively.
- [ ] Run bootstrap tests to GREEN and run `bash -n scripts/bootstrap.sh`.

### Task 4: Environment and suite guards

**Files:**
- Create: `scripts/test-environment.sh`
- Modify: `tests/unit/test_verification_harness.py`

- [ ] Add failing tests that accept only the five registered environment IDs, reject unknown/missing IDs as usage errors, and emit the approved no-I/O `[baseline]` failure for supported IDs.
- [ ] Add a sentinel test proving `test:matrix` aggregate capability failure runs no environment script.
- [ ] Implement the environment script as an argument validator plus explicit unimplemented baseline failure; do not invoke cluster tools or network operations.
- [ ] Run focused tests and shell syntax validation.

### Task 5: Repair Taskfile targets without hiding baselines

**Files:**
- Modify: `Taskfile.yml`
- Modify: `tests/unit/test_verification_harness.py`

- [ ] Add failing structural tests that every non-bootstrap target invokes preflight before its first formatter/test command and that `final`/`test:matrix` invoke aggregate preflight before children.
- [ ] Add tests that `format` requires explicit `FILES`, active targets use `.tools/bin/uv`, and no active target invokes `go`, `npm`, `buf`, `opa`, `golangci-lint`, or `govulncheck` without a matching repository manifest.
- [ ] Replace ambient commands with repository-local variables such as:

```yaml
vars:
  UV: '{{.ROOT_DIR}}/.tools/bin/uv'
  PREFLIGHT: '{{.ROOT_DIR}}/scripts/verification-preflight.sh'
```

- [ ] Repair core target commands:
  - `format:check`: Ruff format check only;
  - `lint`: Ruff check only;
  - `typecheck`: Pyright only;
  - `test:unit`, `test:contract`, `test:architecture`: existing Python suites;
  - `test:integration`: guarded empty suite;
  - testbed unit/contract: explicit existing adapter/scenario selections;
  - requirements render/check: existing registry CLI.
- [ ] Add explicit guarded baseline targets for absent policy, replay, security, reasoner, scaler, and controller suites.
- [ ] Make `format` require `FILES`; preserve focused Online Boutique lint; make `bootstrap` directly call the shell script.
- [ ] Make `final` preflight the entire child set before invoking any child. Make `test:matrix` preflight every environment capability before invoking any environment.
- [ ] Run structural tests, `.tools/bin/task --list`, and focused Task targets. Confirm missing suites report `[baseline]`, not missing executables or pytest exit 5.

### Task 6: Locked-environment consistency

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `tests/unit/test_verification_harness.py`

- [ ] Re-run the dependency-consistency test comparing Python commands declared in the tool manifest with development dependency names.
- [ ] Prove `uv sync --locked` makes no manifest or lockfile changes and that every Python tool resolves only through the locked environment.
- [ ] Run Ruff, Pyright, and the full Python unit suite.

### Task 7: Bootstrap and baseline documentation

**Files:**
- Modify: `README.md`
- Create: `docs/verification-baseline.md`
- Modify: `tests/unit/test_verification_harness.py`

- [ ] Add failing documentation tests requiring the exact fresh-checkout sequence:

```bash
./scripts/bootstrap.sh
.tools/bin/task <target>
```

- [ ] Require documentation for Linux/WSL amd64, pinned local tools, `[prerequisite]` versus `[baseline]`, and the no-green rule.
- [ ] Document every target and explicitly state that missing component/environment suites fail rather than pass or skip.
- [ ] Run every non-mutating repaired target once and record its exact command/date/exit status in `docs/verification-baseline.md`, separating harness/tool failures from source/suite/traceability failures.
- [ ] Verify `format` only against a disposable temporary Python file passed through `FILES`; never point it at the repository or unrelated product files.
- [ ] Validate `requirements:render` structurally and inspect current traceability diffs before invocation. Run it only when the generated files are intentionally part of the already-scoped traceability update; snapshot the pre-render diff and confirm no unrelated content changes.
- [ ] Do not edit product source to change the recorded formatting or lint baseline.
- [ ] Run documentation tests and render requirement artifacts if the existing uncommitted traceability work requires regeneration.

### Task 8: End-to-end verification and review

**Files:**
- Modify only harness/tests/docs required by review findings.

- [ ] Run bootstrap from a clean temporary tool directory and verify it installs pinned tools and completes locked sync.
- [ ] Run all non-mutating repaired targets, capturing exact exit status without stopping after the first failure. Verify mutating `format` with a disposable file and `requirements:render` through its scoped artifact check rather than treating either as an ordinary read-only target.
- [ ] Verify no target fails because a declared tool is missing or because the target itself is undefined.
- [ ] Verify known source/suite/traceability baseline failures remain nonzero and clearly classified.
- [ ] Run `task format:check`, `task lint`, `task typecheck`, `task test:unit`, and `task test:contract` through `.tools/bin/task`; report exact results and do not claim green unless all required targets exit zero.
- [ ] Run changed-file Ruff/Pyright, harness/unit tests, architecture, testbed unit/contract, `requirements:check`, and `git diff --check`. Render requirements only under the scoped Task 7 conditions.
- [ ] Request a focused code review against the approved design; fix all Critical/Important harness issues and rerun affected checks.
- [ ] Inspect the final diff to ensure `.tools/` artifacts, temporary downloads, and unrelated product formatting/lint edits are absent.
