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

All existing Task commands are retained or deliberately repaired:

- `bootstrap` delegates to `scripts/bootstrap.sh` without requiring an already
  bootstrapped Task environment;
- `format` requires an explicit `FILES` argument and refuses an empty value, so
  it cannot mass-format the repository accidentally;
- `lint:online-boutique` retains its existing focused scope;
- `requirements:render` regenerates only the two declared generated artifacts;
- `final` performs a complete aggregate prerequisite preflight before invoking
  any child target.

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

`test:env` delegates to a checked-in `scripts/test-environment.sh`. In the
current checkout, no executable cluster acceptance harness exists, so the
script validates the environment ID and then exits before external I/O with a
clear `[baseline] test:env: environment execution harness is not implemented`
message. It does not require or attempt `kubectl`, Helm, Kind, provider CLIs, or
network access. `test:matrix` performs the full aggregate prerequisite check
and suite-availability check before invoking the first environment; while the
environment harness is absent it emits one baseline failure and runs zero
environments. When cluster acceptance is implemented later, its executables,
versions, credentials/state preflight, and target dependency profile must be
added in the same change.

Core Python targets run their existing suites. Testbed targets select the
adapter/scenario unit and contract suites explicitly. Component targets whose
implementation or test directories do not exist do not silently pass: a suite
guard emits `[baseline] <target>: no tests are configured` and exits nonzero.

Commands for absent ecosystems are removed from active targets. For example,
`test:contract` does not invoke Buf or OPA when the repository contains neither
a Buf module nor Rego policies. When a future manifest is introduced, its tools
and checks must be added together through bootstrap and the prerequisite
manifest.

Aggregate targets such as `final`, `test:matrix`, and future grouped service
checks use a two-phase contract: validate the union of every child prerequisite
and required suite first, then execute children. A missing prerequisite or
suite therefore causes zero child commands and zero external side effects.

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

Completion reports and documentation must not describe the repository as
green unless every required target was invoked in the same verification run
and every target exited zero. Aggregate reporting lists every target and exact
exit status; skipped, absent, baseline-failing, or prerequisite-failing targets
make the aggregate non-green.

## Dependency Declaration

All Python tooling used by Task is declared in the development dependency
group. This includes pytest, Ruff, Pyright, PyYAML, Pydantic, and type stubs
needed for deterministic type checking. `uv.lock` is regenerated and bootstrap
uses `--locked`, preventing undeclared or drifting test tools.

`tools/verification-tools.yaml` is the machine-readable command manifest. It
records the supported platform, pinned local tool versions and checksums, host
bootstrap prerequisites, and the executable/suite dependencies of every Task
target. Bootstrap and Task preflight read the same manifest, preventing their
dependency knowledge from drifting. Host prerequisites are `bash`, `curl`,
`tar`, and a supported SHA-256 command; active target prerequisites are the
repository-local `uv` and `task` plus commands installed by the locked Python
environment. Environment and absent-ecosystem commands are recorded as
unimplemented baseline capabilities, not undeclared executables.

## Tests

Tests are written before harness implementation and cover:

- every required Task target exists;
- every pre-existing Task target (`bootstrap`, `format`, focused lint,
  requirements rendering, and `final`) has the documented repaired behavior;
- Task targets invoke preflight before check commands;
- the preflight distinguishes missing/wrong tools from baseline failures;
- bootstrap rejects unsupported platforms and missing host prerequisites;
- bootstrap reuses correct pinned tools and rejects version mismatches;
- checksum failure does not install an executable;
- locked dependency synchronization is invoked;
- empty component suites fail with `[baseline]` rather than pytest's ambiguous
  exit code 5;
- aggregate preflight failure runs no earlier child command or environment;
- the dependency manifest covers every Task target and every command invoked by
  those targets;
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
It also states the no-green rule and provides an aggregate status table with
the last observed exit code for every required target.

## Safety and Non-goals

- Do not mass-format existing source.
- Do not repair unrelated adapter or product-code lint violations.
- Do not add no-op tests or make absent component suites pass.
- Do not weaken architecture, policy, requirement, or safety checks.
- Do not install system-global tools or mutate user shell configuration.
- Do not introduce Go, Node, Buf, or OPA merely to satisfy stale Task commands
  when the repository has no matching source or manifest.
