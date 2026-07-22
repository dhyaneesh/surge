# GuardianScenario Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the strict, environment-neutral `GuardianScenario` Pydantic v2 schema, YAML loader, canonical example, validation, tests, and traceability required by section 22.

**Architecture:** `models.py` is the canonical typed schema and owns field-local and cross-model Pydantic validation. `validation.py` contains reusable duration, normalized-label, uniqueness, and action helpers without I/O. `loader.py` is the only YAML/file boundary and preserves loader errors separately from Pydantic `ValidationError`. The package remains test-only and exports only `GuardianScenario` and `load_scenario` as its primary API.

**Tech Stack:** Python 3.13, Pydantic v2, PyYAML, pytest, Ruff, Pyright, Task.

---

### Task 1: Dependency and failing schema tests

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/unit/test_guardian_scenario_schema.py`

- [ ] Add `pydantic>=2,<3` to the dev dependency group beside the test-only PyYAML dependency and update the lockfile with `uv lock`.
- [ ] Write positive tests for minimal/full documents, canonical example loading, snake-case access, alias serialization, empty compatibility defaults, semantic labels, combined stimuli, return type, and package `__all__` containing exactly `GuardianScenario` and `load_scenario`.
- [ ] Write parameterized negative tests for API identity, DNS metadata, unknown fields, duration syntax/value, multipliers, environment uniqueness/disjointness/reasons, workflow uniqueness, every enum family, forbidden selector fields and label keys, empty labels, action overlap, deployment transitions, recovery gates, malformed/non-mapping YAML, and unreadable paths.
- [ ] Mark the file with the applicable stable test-obligation ID if one exists; otherwise document section 22 traceability without inventing a `GRD-*` ID.
- [ ] Run `uv run pytest tests/unit/test_guardian_scenario_schema.py -q` and verify collection fails because `testbeds.scenarios` does not exist.

### Task 2: Reusable validation primitives

**Files:**
- Create: `testbeds/scenarios/validation.py`
- Test: `tests/unit/test_guardian_scenario_schema.py`

- [ ] Implement a strict positive-duration parser accepting integer/decimal values followed by `ms`, `s`, `m`, `h`, or `d`, returning `datetime.timedelta` and rejecting booleans, unitless values, malformed strings, and non-positive results.
- [ ] Implement uniqueness validation that reports duplicate values deterministically.
- [ ] Implement normalized semantic-label-key validation using DNS-style qualified-name syntax and exact reserved runtime-identity keys from the approved design.
- [ ] Implement non-empty label-value validation and the mutating-action predicate for `scale`/`rollback`.
- [ ] Run the focused tests and confirm failures have moved from missing imports to missing schema models.

### Task 3: Strict Pydantic model hierarchy

**Files:**
- Create: `testbeds/scenarios/models.py`
- Test: `tests/unit/test_guardian_scenario_schema.py`

- [ ] Define a frozen `StrictModel` with `extra="forbid"`, `populate_by_name=True`, and camelCase alias serialization.
- [ ] Define the exact enums from the approved design and import `FaultType` from `testbeds.models`.
- [ ] Define metadata, normalized service/workload selectors, unsupported environment and compatibility requirements, baseline, load/fault/deployment stimulus, action expectation, recovery expectation, expected outcomes, scenario spec, and root models.
- [ ] Use exact literal API identity, constrained DNS metadata names, strict numeric multiplier `> 1`, and positive duration fields.
- [ ] Add model validators for unique/disjoint environments, unique workflow states, disjoint action sets, distinct non-empty deployment versions, recovery required by mutating proposed/allowed actions, and fresh-telemetry recovery requiring a healthy/recovered observable condition.
- [ ] Keep load, fault, and deployment independent so all may coexist.
- [ ] Run `uv run pytest tests/unit/test_guardian_scenario_schema.py -q` and make all model-validation tests pass.

### Task 4: YAML loader and public API

**Files:**
- Create: `testbeds/scenarios/loader.py`
- Create: `testbeds/scenarios/__init__.py`
- Test: `tests/unit/test_guardian_scenario_schema.py`

- [ ] Define `ScenarioLoadError` internally for file read, YAML parse, empty-document, and non-mapping-root failures, including the input path in its message.
- [ ] Parse only with `yaml.safe_load`; pass mapping data directly to `GuardianScenario.model_validate` without wrapping Pydantic `ValidationError`.
- [ ] Export `GuardianScenario` and `load_scenario` from the package and keep other symbols non-primary/internal.
- [ ] Run the focused tests and verify all loader and public API cases pass.

### Task 5: Canonical scenario and traceability

**Files:**
- Create: `testbeds/scenarios/legitimate-demand-scale-up.yaml`
- Modify: `docs/requirements/implementation.yaml` only if an existing obligation applies
- Test: `tests/unit/test_guardian_scenario_schema.py`

- [ ] Add the documented scale-up example with normalized `request-processor`, four applicable environments, a five-minute baseline, step load at multiplier four for ten minutes, expected `load_spike`, allowed/proposed `scale`, forbidden `rollback`, and recovery requiring fresh telemetry plus an observable recovered/healthy condition.
- [ ] Include explicit compatibility collections in canonical serialization through model defaults; include YAML declarations where useful for readability.
- [ ] Search the requirements registry for a section-22 obligation. Update implementation traceability only when a stable existing obligation exists; do not invent normative IDs.
- [ ] Run the focused test file and `uv run pytest tests/unit -q`.

### Task 6: Verification and cleanup

**Files:**
- Modify only files required by formatter or verified traceability generation.

- [ ] Run `uv run ruff format` only on changed Python files, then `task format:check`.
- [ ] Run `task lint`.
- [ ] Run `task typecheck`.
- [ ] Run `task test:unit`.
- [ ] Run `task test:contract` as mandated by repository completion rules.
- [ ] Run `task test:integration` as mandated by `testbeds/AGENTS.md`.
- [ ] Run `task test:architecture` to prove production packages do not import testbed code.
- [ ] Run `task requirements:check` and update generated traceability files only if the checker requires it.
- [ ] Record that `testbeds/AGENTS.md` names `task test:testbeds-unit` and `task test:testbeds-contract`, but the repository Taskfile does not define them; use the existing supersets `task test:unit` and `task test:contract` and report this instruction/task mismatch rather than adding unrelated task aliases.
- [ ] Run `git diff --check`, inspect the final diff, and report every exact result and any unrelated pre-existing failure honestly.
