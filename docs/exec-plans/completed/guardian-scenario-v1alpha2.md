# Guardian Scenario Schema v1alpha2 Implementation Plan

Status: completed

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a parallel, strict Guardian Scenario v1alpha2 testbed contract, explicit v1alpha1 upgrader, capability-derived compatibility projections, and deterministic pure preflight without implementing scenario files or a live runner.

**Architecture:** Preserve v1alpha1 as an unchanged public branch and add focused v1alpha2 modules under `testbeds/scenarios`. Environment declarations become typed compatibility facts; pure functions compare them with scenario requirements and generate canonical compatibility/index projections. Compatibility status remains outside scenario documents.

**Tech Stack:** Python 3.13, Pydantic v2, PyYAML, pytest, Ruff, Pyright, Task.

**Decision:** `docs/architecture/decisions/guardian-scenario-v1alpha2.md`

---

## Scope

- Frozen strict v1alpha2 typed models and closed enums.
- Exact preservation tests for v1alpha1.
- Explicit pure v1alpha1-to-v1alpha2 upgrader.
- Typed operational capability declarations for environments.
- Pure compatibility derivation and preflight.
- Canonical generation and exact validation of `compatibility.yaml` and
  `index.yaml` using schema fixtures, not the 15 production scenario files.
- Focused Task targets and architecture protection.
- Design-capability traceability using an existing registry mechanism; do not
  invent a normative `GRD-*` or `AT-*` identifier.

## Out of scope

- The 15 critical scenario YAML files and their golden outputs.
- Live Kubernetes, SigNoz, OPA, NATS, Temporal, or demo-environment execution.
- A scenario orchestration runner, installer, or fault injector.
- Production packages, policies, reasoner behavior, action authorization, or
  Kubernetes credentials.
- Demo-specific service bindings or new cloud resources.
- Changes to the normative production action taxonomy. The testbed-only typed
  action assertion may distinguish scale direction.
- Claims that any demo environment pairing has passed end-to-end validation.
- Repair of unrelated baseline failures.

## Applicable normative requirements and tests

The implementation supports, but does not itself satisfy end-to-end, the
following existing obligations: `GRD-CLS-001`–`006`, `GRD-ACT-001`–`005`,
`GRD-WF-001`–`004`, `GRD-TTL-003`–`006`, `GRD-TEN-001`–`008`,
`GRD-SCL-001`–`007`, `GRD-OPA-001`–`007`, `GRD-DRIFT-001`–`005`, and
`GRD-REC-001`–`007`. The overlapping acceptance tests are `AT-CLS-001`,
`AT-CLS-002`, `AT-ACT-001`, `AT-WF-001`, `AT-TTL-001`, `AT-TEN-001`,
`AT-SCL-002`, `AT-OPA-001`, `AT-DRIFT-001`, and `AT-REC-001`.

This task implements testbed contract vocabulary only. It must not mark these
normative production obligations implemented. Add a new design capability such
as `DESIGN-SCENARIO-002` only through the existing requirements registry.

## Exact files

### Create

- `testbeds/scenarios/v1alpha2.py` — v1alpha2 enums and strict model tree.
- `testbeds/scenarios/upgrade.py` — explicit pure v1alpha1 upgrader.
- `testbeds/scenarios/compatibility.py` — declaration, compatibility, index,
  capability-definition registry, and preflight models plus pure derivation
  functions.
- `testbeds/scenarios/catalog.py` — file-boundary loading/generation/validation
  separated from pure derivation.
- `testbeds/environments/capabilities.py` — five typed environment declarations.
- `testbeds/scenarios/compatibility.yaml` — generated projection from testbed
  scenario catalog present at implementation time.
- `testbeds/scenarios/index.yaml` — generated projection.
- `tests/unit/test_guardian_scenario_v1alpha2.py` — schema and invariant tests.
- `tests/unit/test_guardian_scenario_upgrade.py` — migration tests.
- `tests/unit/test_scenario_compatibility.py` — pure derivation/preflight tests.
- `tests/unit/test_scenario_catalog.py` — projection/index consistency tests.
- `tests/contract/test_environment_capability_declarations.py` — declaration
  versus adapter protocol/fixture responsibility contract tests.

### Modify

- `testbeds/scenarios/loader.py` — add version dispatch without changing the
  existing `load_scenario` v1alpha1 behavior.
- `testbeds/scenarios/__init__.py` — expose new APIs additively while preserving
  existing names.
- `tests/unit/test_guardian_scenario_schema.py` — v1alpha1 golden/API regression
  assertions only.
- `tests/architecture/test_boundaries.py` — ensure production cannot import new
  testbed modules and generic logic contains no demo service names.
- `Taskfile.yml` — add honest focused schema, compatibility, and scenario targets.
- `docs/requirements/requirements.yaml` — add one non-normative design capability
  record if required by registry conventions.
- `docs/requirements/implementation.yaml` — link only the design capability and
  its tests; do not claim normative production completion.
- Generated requirement views only if `requirements:check` requires regeneration.

No adapter implementation, production, policy, deployment, or lockfile file is
planned for modification.

## Exact red/green sequence

### Task 1: Freeze v1alpha1 behavior

- [ ] Add v1alpha1 golden serialization, exact public API, frozen-model, alias,
  unknown-field, loader-error, and canonical-file regression tests.
- [ ] Run `./.tools/bin/uv run --locked pytest tests/unit/test_guardian_scenario_schema.py -q`.
- [ ] Expected RED only if current behavior is not fully characterized; correct
  test defects without changing implementation. Establish GREEN before adding
  v1alpha2.
- [ ] Record the canonical v1alpha1 dump as an in-test stable value, excluding no
  fields and normalizing only YAML formatting.

### Task 2: Define v1alpha2 schema tests

- [ ] Add positive construction/serialization tests for every model branch in
  the ADR model tree.
- [ ] Add parameterized rejection tests for unknown fields/enums, duplicate
  values, action overlap, evidence overlap, invalid cardinality, missing
  baseline/requirements, non-actionable mutation, mutation without recovery,
  conflict without conflict state, telemetry-failure/unknown conflation,
  stale-policy fail-open, expired approval mutation, drift mutation, duplicate
  workflow cardinality, incomplete tenant isolation, invalid scaler results,
  missing ordered safety gates, unscoped evidence, inconsistent typed stimuli,
  unpinned recovery, audit events without cardinality, action-direction overlap,
  and proposed/observed mutation direction mismatch.
- [ ] Run `./.tools/bin/uv run --locked pytest tests/unit/test_guardian_scenario_v1alpha2.py -q`.
- [ ] Expected RED: import of `testbeds.scenarios.v1alpha2` fails.
- [ ] Implement the smallest complete frozen v1alpha2 model tree in
  `testbeds/scenarios/v1alpha2.py`.
- [ ] Re-run the focused test until GREEN.

### Task 3: Add version-dispatch loading without changing v1

- [ ] Add tests that the existing `load_scenario` still returns v1alpha1, the new
  dispatcher returns the correct union branch, unknown versions are path-aware,
  and neither branch silently upgrades.
- [ ] Run the two schema test modules; expected RED from missing dispatcher.
- [ ] Add the minimal dispatcher and additive exports.
- [ ] Re-run both modules; expected GREEN with identical v1alpha1 golden output.

### Task 4: Add the explicit upgrader

- [ ] Test lossless mappings listed in the ADR, immutable input, deterministic
  repeated output, mandatory reviewed environment requirements/description,
  conservative mutation cardinality, no invented contradictions/audit/tenant
  semantics, and path-aware failure when v2 invariants cannot be met.
- [ ] Run `./.tools/bin/uv run --locked pytest tests/unit/test_guardian_scenario_upgrade.py -q`.
- [ ] Expected RED: upgrader module is absent.
- [ ] Implement only the pure explicit upgrader.
- [ ] Re-run upgrader and v1 regression tests; expected GREEN.

### Task 5: Define environment declarations and compatibility derivation

- [ ] Add tests for closed capability/operation/observation enums, unique IDs,
  frozen declarations, lexical canonical output, exact capability-definition
  expansion, declarations missing an entailed operation/observation, and
  conservative declarations for all five fixtures.
- [ ] Add pure derivation tests for supported, unsupported with every missing
  item, blocked with reason, missing-requirement precedence, same environment ID
  with different capabilities, non-candidate direct preflight, unknown IDs
  rejected before adapter lookup, and sixth-environment extensibility.
- [ ] Run `./.tools/bin/uv run --locked pytest tests/unit/test_scenario_compatibility.py tests/contract/test_environment_capability_declarations.py -q`.
- [ ] Expected RED: compatibility and declaration modules are absent.
- [ ] Implement typed declarations and pure set-difference derivation.
- [ ] Re-run focused tests; expected GREEN.

### Task 6: Add deterministic preflight

- [ ] Add tests for exact result fields, supported/unsupported/blocked refusal,
  required and missing collections, no time/random/path/environment access,
  stable repeated dumps, and no adapter method invocation.
- [ ] Run `./.tools/bin/uv run --locked pytest tests/unit/test_scenario_compatibility.py -q`.
- [ ] Expected RED: preflight API is absent.
- [ ] Implement `preflight_scenario` as a pure wrapper over compatibility
  derivation and canonical tuple sorting.
- [ ] Re-run focused tests; expected GREEN.

### Task 7: Generate and validate projections

- [ ] Add tests that compatibility and index models are derived, generated files
  exactly equal fresh derivation, extra/missing/changed rows fail, serialization
  is stable, and neither artifact can grant support absent a declaration.
- [ ] Add a regression proving a cataloged v1alpha1 scenario is not upgraded and
  yields blocked rows with `scenario-requires-explicit-v1alpha2-upgrade` while
  its index entry retains the v1alpha1 API version.
- [ ] Run `./.tools/bin/uv run --locked pytest tests/unit/test_scenario_catalog.py -q`.
- [ ] Expected RED: catalog module and projections are absent.
- [ ] Implement the file-boundary catalog separately from pure functions.
- [ ] Generate `compatibility.yaml` and `index.yaml` only from the scenarios then
  present in the repository. Do not add the 15-scenario pack.
- [ ] Re-run catalog and compatibility tests; expected GREEN.

### Task 8: Add Task targets and architecture coverage

- [ ] Add failing tests or harness assertions for the required target commands
  and testbed-only import boundary.
- [ ] Add `test:scenario-schema`, `test:scenario-compatibility`, and
  `test:scenarios`; make `test:scenarios` invoke schema, migration,
  compatibility, catalog, relevant contract, and architecture tests without
  installing environments.
- [ ] Preserve `test:testbeds-unit`, `test:testbeds-contract`, and existing target
  semantics.
- [ ] Run focused Task targets and architecture tests; expected GREEN.

### Task 9: Traceability and changed-file quality

- [ ] Add only a design-capability traceability record for v1alpha2.
- [ ] Run `./.tools/bin/task requirements:check` and regenerate approved views if
  necessary.
- [ ] Run Ruff format only on changed Python files.
- [ ] Run focused Ruff and Pyright checks on changed Python files before broader
  repository checks.

### Task 10: Required verification

- [ ] Run `./scripts/bootstrap.sh --check`.
- [ ] Run `./.tools/bin/task test:scenario-schema`.
- [ ] Run `./.tools/bin/task test:scenario-compatibility`.
- [ ] Run `./.tools/bin/task test:scenarios`.
- [ ] Run `./.tools/bin/task test:testbeds-unit`.
- [ ] Run `./.tools/bin/task test:testbeds-contract`.
- [ ] Run `./.tools/bin/task test:architecture`.
- [ ] Run `./.tools/bin/task requirements:check`.
- [ ] Run mandatory repository checks independently:
  `./.tools/bin/task format:check`, `./.tools/bin/task lint`,
  `./.tools/bin/task typecheck`, `./.tools/bin/task test:unit`, and
  `./.tools/bin/task test:contract`.
- [ ] Run `git diff --check`, `git status --short`, and `git diff --stat`.
- [ ] Record every exit status independently and classify pre-existing failures
  honestly. Do not repair unrelated failures.

## Negative and adversarial test inventory

- Same environment name with a capability removed becomes unsupported.
- Candidate environment with missing capability remains unsupported.
- Full scenario/environment Cartesian-product rows are generated even for
  non-candidate environments.
- A missing requirement with typed planned support is blocked; the same missing
  requirement without planned support is unsupported.
- A declaration claiming both implemented and planned support for one
  requirement is rejected.
- Matrix says supported but derivation says unsupported; validation fails.
- Blocked row without reason is rejected.
- Unsupported row omitting a missing item is rejected.
- v1alpha1 input and serialization change after v2 import; regression fails.
- Implicit loader upgrade is attempted; regression fails.
- Upgrader is called without reviewed requirements; typed call/runtime validation
  fails.
- Upgrader encounters a scale assertion without caller-supplied reviewed
  direction; upgrade fails.
- Mutating action lacks recovery, fresh evidence, mutation observation, or
  recovery observation; validation fails.
- Telemetry failure allows mutation or unknown uses unhealthy telemetry;
  validation fails.
- Stale policy allows a write; validation fails.
- Restricted policy permits rollback, scale-down, scaler pause, policy
  activation, or approval issuance; validation fails.
- Stale telemetry scenario allows scale-down, emits a numeric zero with an
  error/hold result, or omits gateway convergence when replica consistency is
  asserted; validation fails.
- Caller/dependency or cross-tenant evidence omits its normalized role or tenant
  relation; validation fails.
- Duplicate ingress, expiry, drift, telemetry interruption, policy transition,
  or tenant injection is encoded without its typed stimulus; validation fails.
- Recovery omits contract version, registry version, fresh evidence, a typed
  condition, or a post-action window; validation fails.
- Audit expectation omits cardinality; validation fails.
- Proposed scale-up is paired with an observed scale-down mutation, or a
  forbidden `scale(any)` overlaps an eligible directional scale; validation
  fails.
- Expiry, policy, drift, identity, tenant, or recovery assertions omit their
  required ordered safety gate; validation fails.
- Cross-tenant assertion omits pre-scoring or pre-I/O gate; validation fails.
- Duplicate alert allows two parents; validation fails.
- Preflight reads clock, random, environment, file, or adapter; purity test fails.
- Generated output contains unsorted sets, timestamps, UUIDs, or machine paths;
  deterministic dump test fails.
- Demo service names appear in generic scenario modules; architecture test fails.
- Production imports any new testbed module; architecture test fails.

## Compatibility and index rules

The implementation must reproduce the ADR rules exactly. Scenario documents
contain requirements, environment declarations contain facts, and derived files
contain projections. No hand-authored status is accepted. A generated entry is
supported only when all required capabilities, operations, and observations are
declared and no typed blocker applies.

## Known baseline

- `docs/baselines/repository-verification.md` is absent at planning time.
- Current Taskfile already defines `test:testbeds-unit`,
  `test:testbeds-contract`, and `test:architecture`, but not the three focused
  scenario targets.
- The current repository has only one v1alpha1 scenario file and no compatibility
  matrix, index, compatibility resolver, preflight model, or operational
  capability declarations.
- Generated projections initially represent that v1alpha1 scenario as blocked;
  they do not silently upgrade it or claim environment support.
- No repository command was run during this design-only task; implementation
  must establish actual baseline results before attributing any failure.

## Assumptions

- Section 22 permits additive test-harness schema evolution without creating a
  new normative identifier.
- Existing service capabilities remain workload-selector facts and are not
  converted into operational capabilities.
- `candidateEnvironments` is a discoverability filter only in v1alpha2.
- The existing production direction-neutral `scale` action remains unchanged.
  A testbed-only `ActionAssertion` adds scale direction so scenarios can express
  scale-up eligibility and scale-down prohibition precisely.
- Fixture-only scenarios can use a typed testbed environment declaration; the
  v1alpha2 schema does not require one of the five demo IDs.
- Human-readable descriptions and blocking details are non-executable text;
  machine decisions use closed enums and typed fields.

## Focused design review

The ADR's adversarial review was repeated against this plan:

- Demo-specific leakage: file boundaries permit demo IDs only in environment
  declarations and fixture analysis, never generic validation or derivation.
- Capability inflation: every initial capability maps to at least one row in the
  analysis table; no convenience aliases are planned.
- Duplicate truth: matrix/index generation and exact-equivalence validation are
  mandatory; statuses cannot be authored in scenarios.
- Silent v1 change: Task 1 establishes green v1 regression before v2 code.
- Mutation safety: model validation requires recovery and observable mutation
  cardinality.
- Fail-open policy/stale telemetry: explicit negative tests precede models.
- Tenant ambiguity: typed ordered isolation gates are required.
- Non-determinism: pure results exclude clock/random/path data and sort all
  collections.
- Name-only compatibility: the same-ID/different-capabilities test proves names
  have no authority.
- Scaler safety: typed result/zero/convergence fields cover stale SigNoz beyond
  merely forbidding an action.
- Ordered gates: a closed enum represents checks that must precede scoring,
  external I/O, eligibility, mutation, or recovery.
- Stimulus observability: every non-legacy control used by the 15 scenarios has
  a closed typed stimulus branch.
- Evidence and recovery: normalized subject/tenant relations and pinned
  recovery versions remove causal, tenancy, and post-action ambiguity.

Review result: no unresolved Critical or Important plan finding.

## Completion and handoff

This plan is complete when all scoped models, pure helpers, projection checks,
focused targets, architecture checks, and traceability pass, with v1alpha1
goldens unchanged. Keep this file in `active/` until then. Do not begin the 15
scenario files as part of this plan.
