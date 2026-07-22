# Critical Guardian Scenario Pack Implementation Record

Status: completed

## Scope

Implement the 15 requested environment-neutral GuardianScenario v1alpha2 YAML
contracts, capability-derived compatibility matrix, generated index, semantic
tests, and preflight metadata. No live environment execution is included.

## Out of scope

Production classifiers, policies, credentials, Kubernetes execution, live demo
installation, a full scenario runner, and demo-specific production logic.

## Normative mapping

The scenario files carry typed `normativeRequirements` and `acceptanceTests`.
The principal mappings are `AT-CLS-001/002`, `AT-ACT-001`, `AT-WF-001`,
`AT-TTL-001`, `AT-SCL-002`, `AT-OPA-001`, `AT-DRIFT-001`, and `AT-TEN-001`.
No new normative identifier was created.

## Scenario and capability matrix

The source analysis is the table in
`docs/architecture/decisions/guardian-scenario-v1alpha2.md`. Scenario YAML owns
requirements; `testbeds/environments/capabilities.py` owns fixture facts;
`compatibility.yaml` is a generated Cartesian projection. Missing declared
capabilities produce `unsupported`; reviewed planned support produces `blocked`.

## Schema and validation additions

v1alpha2 adds operational capabilities, typed control stimuli, evidence subject
and tenant relations, directional action assertions, policy operations and
staleness, workflow/audit/mutation cardinality, ordered safety gates, scaler
safe-hold assertions, pinned recovery, traceability, pure compatibility, and
pure preflight. v1alpha1 remains separately loadable and explicitly upgradeable.

## Files

Scenario schema and helpers live under `testbeds/scenarios/`; fixture capability
facts live under `testbeds/environments/`; tests live under `tests/unit/`,
`tests/contract/`, and existing architecture suites. The 15 YAML files,
`compatibility.yaml`, and `index.yaml` are generated deterministically from typed
testbed inputs.

## Test-first sequence

1. Assert all 15 files load as v1alpha2 and fail while absent.
2. Assert pack safety invariants and observe failures.
3. Add the smallest typed stimuli and assertion fields.
4. Generate the scenario files and make semantic tests pass.
5. Derive matrix/index and validate exact equivalence.
6. Run schema, compatibility, scenario, unit, contract, architecture,
   requirements, format, lint, typecheck, and diff checks independently.

## Negative and adversarial cases

- Healthy traffic cannot mutate.
- Caller scale is forbidden for dependency failure.
- Telemetry failure and unknown serialize differently.
- Conflict executes no action when unresolved.
- Duplicate alerts require exactly one parent.
- Expiry, drift, stale policy, and foreign tenant evidence require zero writes.
- Stale scaler evidence forbids scale-down and fabricated zero.
- Mutating scenarios pin recovery and require fresh post-action evidence.
- Environment names never override capability differences.

## Known baseline failures

Before this pack, repository-wide Ruff formatting/lint, Pyright, requirements
traceability, and one stale architecture governance assertion failed. These are
recorded independently and are not represented as passing.

## Assumptions

- Candidate environments are discoverability hints only.
- Exact timing is not asserted beyond positive deterministic windows.
- Fixture responsibilities do not prove live environment execution.
- Scale direction is testbed assertion vocabulary, not a production enum change.

## Focused adversarial review

Reviewed for demo-name leakage, inflated capabilities, duplicate truth,
mutation without recovery, fail-open policy behavior, stale scale-down,
cross-tenant ambiguity, non-deterministic metadata, name-only compatibility,
missing recovery, and unobservable assertions. Generic modules contain no demo
service names; matrix/index are derived; all write-capable scenarios require
typed cardinality and recovery; fail-closed and tenant gates are explicit.

Open baseline failures do not weaken scenario semantics. No Critical or
Important scenario-pack finding remains unresolved.
