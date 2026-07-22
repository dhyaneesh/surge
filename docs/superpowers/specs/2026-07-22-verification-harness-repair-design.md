# Verification Harness Repair Design

## Goal

Make a fresh Linux/WSL `amd64` checkout able to run one documented bootstrap
command and then invoke every verification command named by `agents.md`, nested
`AGENTS.md` files, and `Taskfile.yml`. Toolchain failures must be distinct from
existing product and coverage baseline failures.

## Supported Platform

The first version supports Linux and WSL on `amd64` only. Bootstrap rejects
unsupported operating systems and architectures before downloading anything.
Downloaded tools live under ignored `.tools/bin`; the harness never installs
system packages or changes shell profiles.

## Toolchain Boundary

The active repository is Python-only: it has no Go module, Node package
manifest, Buf configuration, Rego policy source, or corresponding production
implementation. Bootstrap therefore installs the tools required by the current
checkout rather than a speculative future stack:

- checksum-verified, pinned `uv`;
- checksum-verified, pinned `task`;
- Python test, lint, format, and type-check dependencies declared in
  `pyproject.toml` and locked in `uv.lock`.

Task commands invoke repository-local binaries explicitly. They do not depend
on whichever versions happen to be on `PATH`. Bootstrap itself may use only a
small documented host prerequisite set: POSIX shell utilities, `bash`, `curl`,
`tar`, and checksum verification. A missing host prerequisite or incomplete
local installation produces a concise `[prerequisite]` error before any check
partially executes.

## Bootstrap Contract

`./scripts/bootstrap.sh` is the single setup command. It:

1. validates the supported platform and host prerequisites;
2. creates `.tools/bin` and a tool-version manifest;
3. downloads pinned archives or executables to temporary files;
4. verifies pinned SHA-256 checksums before installation;
5. installs `uv` and `task` atomically into `.tools/bin`;
6. runs `.tools/bin/uv sync --locked`;
7. runs a repository-toolchain preflight and prints the exact Task invocation.

Repeated bootstrap runs are idempotent. Existing tools are reused only when
their reported versions match the pinned versions. Failed downloads or checksum
mismatches leave no executable at the final path.

The bootstrap script supports injectable tool and download locations for unit
tests. Tests use fake local artifacts and never require network access.

## Task Architecture

`Taskfile.yml` delegates prerequisite checks and suite-presence checks to small,
focused scripts. Every target starts with preflight, so missing local tools fail
before formatters or tests run.

The required core targets are:

- `format:check`
- `lint`
- `typecheck`
- `test:unit`
- `test:contract`
- `test:integration`
- `test:architecture`
- `test:testbeds-unit`
- `test:testbeds-contract`
- `requirements:check`

The Taskfile also defines every command named by nested instructions:

- `test:policy`
- `test:replay` and `test:replay-deterministic`
- `test:security`
- `test:reasoner`
- `test:keda-scaler`
- `test:action-controller`
- `test:requirements`
- `test:env` and `test:matrix`

Core Python targets run their existing suites. Testbed targets select the
adapter/scenario unit and contract suites explicitly. Component targets whose
implementation or test directories do not exist do not silently pass: a suite
guard emits `[baseline] <target>: no tests are configured` and exits nonzero.

Commands for absent ecosystems are removed from active targets. For example,
`test:contract` does not invoke Buf or OPA when the repository contains neither
a Buf module nor Rego policies. When a future manifest is introduced, its tools
and checks must be added together through bootstrap and the prerequisite
manifest.

## Failure Classification

Harness messages use two stable categories:

- `[prerequisite]`: setup is incomplete, a pinned tool is missing or has the
  wrong version, or a host download/checksum utility is unavailable;
- `[baseline]`: the harness is operational, but source validation fails or a
  required product test suite has not been implemented.

Task does not rewrite failures from Ruff, Pyright, pytest, architecture, or the
requirements checker. Their native nonzero statuses remain authoritative. The
documentation records known baseline failures separately from prerequisite
failures so a developer can tell whether to repair setup or product state.

## Dependency Declaration

All Python tooling used by Task is declared in the development dependency
group. This includes pytest, Ruff, Pyright, PyYAML, Pydantic, and type stubs
needed for deterministic type checking. `uv.lock` is regenerated and bootstrap
uses `--locked`, preventing undeclared or drifting test tools.

## Tests

Tests are written before harness implementation and cover:

- every required Task target exists;
- Task targets invoke preflight before check commands;
- the preflight distinguishes missing/wrong tools from baseline failures;
- bootstrap rejects unsupported platforms and missing host prerequisites;
- bootstrap reuses correct pinned tools and rejects version mismatches;
- checksum failure does not install an executable;
- locked dependency synchronization is invoked;
- empty component suites fail with `[baseline]` rather than pytest's ambiguous
  exit code 5;
- existing unit, contract, architecture, testbed-unit, and testbed-contract
  selections collect the intended tests;
- documentation lists the one-command bootstrap and repository-local Task
  invocation.

The tests use temporary directories and fake executables. They do not modify
the real `.tools` directory or require internet access.

## Documentation and Baseline Record

`README.md` documents:

```bash
./scripts/bootstrap.sh
.tools/bin/task <target>
```

It lists the supported platform and explains failure categories.
`docs/verification-baseline.md` records the observed source-format, lint,
type-check, empty-suite, and normative traceability failures separately from
tool installation. The baseline record is descriptive evidence, not an
allowlist and not a mechanism for converting failures into passing checks.

## Safety and Non-goals

- Do not mass-format existing source.
- Do not repair unrelated adapter or product-code lint violations.
- Do not add no-op tests or make absent component suites pass.
- Do not weaken architecture, policy, requirement, or safety checks.
- Do not install system-global tools or mutate user shell configuration.
- Do not introduce Go, Node, Buf, or OPA merely to satisfy stale Task commands
  when the repository has no matching source or manifest.
