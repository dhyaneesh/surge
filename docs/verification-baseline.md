# Verification Baseline

Observed on 2026-07-22 UTC on Linux/WSL `amd64`. This record is descriptive
evidence, not an allowlist. The repository must not be called green unless all
required targets are invoked in the same run and every target exits zero. A
missing, skipped, `[baseline]`, `[prerequisite]`, or native nonzero check makes
that run non-green.

## Fresh-checkout contract

```bash
./scripts/bootstrap.sh
.tools/bin/task <target>
```

Bootstrap installs checksum-verified, repository-local pinned `uv` and Task
binaries in `.tools/bin` and performs a locked dependency sync. The supported
host is Linux/WSL `amd64`; host prerequisites are `bash`, `curl`, `tar`, and a
supported SHA-256 utility.

`[prerequisite]` identifies incomplete setup or missing/wrong pinned tools.
`[baseline]` identifies an operational harness with an unavailable required
suite or environment harness. Native source and traceability failures remain
nonzero and are recorded below. Missing component and environment suites fail; they do not pass or skip.

## Harness and toolchain status

The environment-neutral `test:scenario-schema`, `test:scenario-compatibility`,
and `test:scenarios` targets are active when their checked-in suites exist.
The `mcp:signoz:check` target delegates to the architecture suite. The
`mcp:signoz:smoke` and `diagnostics:signoz` targets remain fail-closed baseline
aliases for the not-yet-configured `test:integration` suite.

Every command below was invoked independently on 2026-07-22 UTC. Task reports
child failures through process exit status 201; the underlying child status is
shown in the observation where available.

| Target | Exact command | Exit status | Observation |
| --- | --- | ---: | --- |
| `bootstrap` | `./scripts/bootstrap.sh` | 0 | Pinned tools reused/installed; locked sync and preflight completed. |
| `format` | `.tools/bin/task format FILES=/tmp/surge-format.sJBWw4.py` | 0 | Only a disposable temporary Python file was reformatted; no repository source was passed. |
| `test:unit` | `PYTEST_ADDOPTS=-s .tools/bin/task test:unit` | 0 | 203 passed. `-s` avoided an external shared-temporary capture cleanup anomaly from the first invocation. |
| `test:contract` | `PYTEST_ADDOPTS=-s .tools/bin/task test:contract` | 0 | 7 passed, 6 skipped by the suite's existing conditional tests. |
| `test:testbeds-unit` | `PYTEST_ADDOPTS=-s .tools/bin/task test:testbeds-unit` | 0 | 148 passed. |
| `test:testbeds-contract` | `PYTEST_ADDOPTS=-s .tools/bin/task test:testbeds-contract` | 0 | 7 passed, 6 skipped by the suite's existing conditional tests. |

No invocation above reported a missing or wrong pinned repository-local tool.
The first pytest-backed invocations encountered a shared-temporary capture
cleanup `FileNotFoundError`; the recorded bounded reruns disabled capture and
exposed the authoritative suite results above.

## Source, suite, and traceability status

| Target | Exact command | Exit status | Observation |
| --- | --- | ---: | --- |
| `format:check` | `.tools/bin/task format:check` | 201 | Ruff child exit 1: 16 files would be reformatted; repository source was not changed. |
| `lint` | `.tools/bin/task lint` | 201 | Ruff child exit 1: 56 errors. |
| `lint:online-boutique` | `.tools/bin/task lint:online-boutique` | 0 | Focused lint passed. |
| `typecheck` | `.tools/bin/task typecheck` | 201 | Pyright child exit 1: 34 errors. |
| `test:architecture` | `PYTEST_ADDOPTS=-s .tools/bin/task test:architecture` | 201 | Pytest child exit 1: 1 repository-governance assertion failed and 21 passed; the assertion expects the former unittest command. |
| `test:integration` | `.tools/bin/task test:integration` | 201 | `[baseline]`: no tests are configured. |
| `test:policy` | `.tools/bin/task test:policy` | 201 | `[baseline]`: no tests are configured. |
| `test:replay` | `.tools/bin/task test:replay` | 201 | `[baseline]`: no tests are configured. |
| `test:replay-deterministic` | `.tools/bin/task test:replay-deterministic` | 201 | `[baseline]`: no tests are configured. |
| `test:security` | `.tools/bin/task test:security` | 201 | `[baseline]`: no tests are configured. |
| `test:reasoner` | `.tools/bin/task test:reasoner` | 201 | `[baseline]`: no tests are configured. |
| `test:keda-scaler` | `.tools/bin/task test:keda-scaler` | 201 | `[baseline]`: no tests are configured. |
| `test:action-controller` | `.tools/bin/task test:action-controller` | 201 | `[baseline]`: no tests are configured. |
| `test:requirements` | `.tools/bin/task test:requirements` | 0 | Requirement-registry unit tests passed. |
| `test:env` | `.tools/bin/task test:env ENV=otel-demo` | 201 | Valid environment ID; `[baseline]`: no environment tests are configured. |
| `test:matrix` | `.tools/bin/task test:matrix` | 201 | Aggregate preflight failed closed with `[baseline]`; no environment child ran. |
| `requirements:check` | `.tools/bin/task requirements:check` | 201 | Checker child exit 1: 149 traceability issues: 121 missing implemented normative test markers and 28 acceptance tests without implementations. |
| `final` | `.tools/bin/task final` | 201 | Aggregate preflight failed closed on missing integration, security, replay, and matrix capabilities; no child target ran. |

## Structurally validated mutating target

`requirements:render` is defined as
`.tools/bin/task requirements:render`. It is intentionally not executed as a
routine baseline check because it mutates generated traceability artifacts.
The Taskfile definition and manifest coverage were validated by unit tests,
`requirements:check` was run, and the pre-existing generated files had no diff.
Render is appropriate only when those generated files are intentionally in
scope and its resulting diff is inspected for unrelated changes.

The `format` target likewise requires an explicit `FILES` value. Its baseline
probe used only the disposable file shown above; it was never aimed at `.`, the
repository, or unrelated product source.
