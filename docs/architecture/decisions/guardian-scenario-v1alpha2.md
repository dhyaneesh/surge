# Guardian Scenario Schema v1alpha2

- Status: Proposed
- Date: 2026-07-22
- Decision owners: Guardian testbed maintainers
- Scope: Testbed scenario contracts, compatibility derivation, and preflight only
- Normative source: `docs/spec/guardian-production-v1.md`

## Context

`tests.guardian.io/v1alpha1` is a strict, frozen Pydantic schema for portable
scenario setup and a small expected-outcome vocabulary. It must remain valid and
unchanged. It cannot express enough observable safety behavior for the first 15
critical scenarios: operational environment capabilities, mutation and audit
cardinality, supporting versus contradicting evidence, tenant-isolation gates,
policy staleness, workflow cardinality, or deterministic preflight.

Compatibility is also currently descriptive. A scenario names applicable
environments and target workload capabilities, while environment declarations
do not expose the operational capabilities needed to prove compatibility.
Treating either environment names or workload capabilities as runtime
compatibility would fail open.

This decision introduces a testbed-only v1alpha2 contract. It does not implement
the scenario pack, change production behavior, or build a live runner.

## Field derivation

The fields below are derived from the 15 requested scenarios and sections
20–24.7 of the production specification. No field is included solely for one
demo application.

### Scenario-to-capability analysis

`candidate environments` are discovery hints, not compatibility decisions.
The compatibility matrix is derived from the required capabilities and the
environment declarations.

| Scenario | Required environment capabilities | Deterministic assertions that drive v1alpha2 fields | Candidate environments from fixture responsibilities |
| --- | --- | --- | --- |
| healthy-load-no-action | `healthy-baseline`, `load-generation`, `workflow-observation`, `mutation-observation` | healthy telemetry; no actionable mutation; zero approvals and mutations; observation audit | otel-demo, aws-retail, online-boutique, argo-rollouts, keda-rabbitmq |
| legitimate-demand-scale-up | `healthy-baseline`, `load-generation`, `horizontal-scaling`, `dependency-observation`, `workflow-observation`, `mutation-observation`, `recovery-observation` | pressure support; healthy-dependency contradiction; scale eligible; rollback forbidden; at most one mutation; fresh recovery | otel-demo, online-boutique, keda-rabbitmq |
| deployment-error-regression | `healthy-baseline`, `deployment-transition`, `progressive-delivery`, `workflow-observation`, `mutation-observation`, `recovery-observation` | deployment/version/error support; missing identity blocks rollback; rollback eligible; fresh error/version recovery | argo-rollouts |
| deployment-latency-regression | `healthy-baseline`, `deployment-transition`, `progressive-delivery`, `dependency-observation`, `workflow-observation`, `mutation-observation`, `recovery-observation` | version/latency support; dependency contradiction; rollback eligible; fresh latency recovery | argo-rollouts; otel-demo only if declarations later prove all capabilities |
| dependency-failure-do-not-scale-caller | `healthy-baseline`, `fault-injection`, `dependency-observation`, `workflow-observation`, `mutation-observation` | topology/dependency support; healthy-caller contradiction; caller scale forbidden; rejection audit | otel-demo, aws-retail, online-boutique |
| resource-saturation | `healthy-baseline`, `resource-pressure`, `dependency-observation`, `horizontal-scaling`, `workflow-observation`, `mutation-observation`, `recovery-observation` | local pressure support; healthy dependencies/no deployment contradiction; scale eligible; fresh recovery | otel-demo, aws-retail, online-boutique, keda-rabbitmq |
| telemetry-failure | `healthy-baseline`, `telemetry-interruption`, `workflow-observation`, `mutation-observation`, `recovery-observation` | failed/stale/incomplete telemetry; no mutation or scale-down; explicit audit; fresh evidence before re-eligibility | environments whose declarations expose telemetry interruption; no name-based default |
| healthy-telemetry-unknown-cause | `healthy-baseline`, `ambiguous-symptom`, `workflow-observation`, `mutation-observation` | healthy telemetry; unknown class; no eligible known cause; zero mutations; distinct serialization | otel-demo when declared |
| scale-versus-rollback-conflict | `healthy-baseline`, `load-generation`, `deployment-transition`, `horizontal-scaling`, `progressive-delivery`, `workflow-observation`, `mutation-observation`, `recovery-observation` | both candidates represented; `CONFLICT_RESOLUTION`; unresolved conflict creates no action; at most one mutation | argo-rollouts; otel-demo only if declared |
| duplicate-alert-single-workflow | `incident-ingress-control`, `workflow-observation`, `mutation-observation` | exactly one tenant-scoped parent; bounded proposal/mutation count; duplicate-receipt audit | a Guardian component fixture or any declared environment |
| expired-approval-no-mutation | `approval-control`, `workflow-observation`, `mutation-observation` | expired/non-executable state; exact zero mutations; denial reason; replay remains denied | a deterministic action-controller fixture or any declared environment |
| operator-drift-supersedes-action | `manual-workload-mutation`, `workflow-observation`, `mutation-observation` | drift checked before mutation; superseded state/reason; zero stale writes; audit | argo-rollouts, keda-rabbitmq when declared |
| stale-signoz-no-scale-down | `telemetry-interruption`, `scale-to-zero`, `scaler-observation`, `mutation-observation` | stale quality; scale-down forbidden; error/hold result; no fabricated zero; consistent gateway result | keda-rabbitmq |
| stale-opa-fail-closed | `policy-bundle-control`, `workflow-observation`, `mutation-observation` | unusable policy; fail-closed decision; zero mutations; no retroactive execution | any fixture declaring policy control and mutation observation |
| cross-tenant-evidence-rejected | `multi-tenant-fixture`, `workflow-observation`, `mutation-observation` | reject foreign evidence before scoring/external I/O; tenant-scoped workflow/cache/topology/approval; sanitized audit | dedicated fixture or an explicitly isolated declared environment |

### Capability vocabulary

The initial operational vocabulary is exactly the union justified by the table:

```text
healthy-baseline              load-generation
fault-injection               deployment-transition
progressive-delivery          horizontal-scaling
scale-to-zero                 dependency-observation
resource-pressure             telemetry-interruption
policy-bundle-control         manual-workload-mutation
multi-tenant-fixture          workflow-observation
approval-control              recovery-observation
mutation-observation          scaler-observation
incident-ingress-control      ambiguous-symptom
action-controller-execution
```

These are environment capabilities, not service-selector capabilities and not
adapter method names. A capability is added only when a scenario needs a
distinct prerequisite that cannot be proven by an existing capability. The
five demo environment declarations must state only capabilities supported by
their fixture responsibilities and implemented adapters; desired future support
is represented as a blocking reason in derived compatibility input, not as a
declared capability.

`action-controller-execution` is a preflight-only runtime capability. It means
that a real action controller can execute and report a mutation; mutation
observation alone does not imply execution. The minimal Guardian runtime does
not declare this capability.

## Decision

### Versioning and loading

- Keep the v1alpha1 classes, aliases, validation, loader behavior, and serialized
  output unchanged.
- Add a distinct frozen `GuardianScenarioV1Alpha2` root with exact API version
  `tests.guardian.io/v1alpha2`.
- `load_scenario` continues returning the existing v1alpha1 type for v1alpha1.
  A version-dispatching `load_guardian_scenario` returns a discriminated union.
- Unknown API versions fail with a path-aware error. No loader silently upgrades.
- Provide an explicit pure `upgrade_v1alpha1` function. Upgrade is opt-in and
  deterministic.

### Proposed typed model tree

All models are frozen, reject unknown fields, use snake_case in Python and
camelCase YAML aliases, and serialize deterministically.

```text
GuardianScenarioV1Alpha2
├── api_version: Literal["tests.guardian.io/v1alpha2"]
├── kind: Literal["GuardianScenario"]
├── metadata: ScenarioMetadata
└── spec: ScenarioSpecV1Alpha2
    ├── description: NonEmptyString
    ├── candidate_environments: tuple[EnvironmentId, ...]
    ├── environment_requirements: EnvironmentRequirements
    │   ├── capabilities: frozenset[EnvironmentCapability]
    ├── target: ScenarioTarget                         # retained shape
    ├── baseline: BaselineRequirements                 # required in v1alpha2
    ├── stimulus: ScenarioStimulusV1Alpha2
    │   ├── load: LoadStimulus | None                  # retained
    │   ├── fault: FaultStimulus | None                # retained
    │   ├── deployment: DeploymentStimulus | None      # retained
    │   ├── telemetry: TelemetryStimulus | None
    │   ├── incident_delivery: IncidentDeliveryStimulus | None
    │   ├── approval: ApprovalStimulus | None
    │   ├── operator_drift: OperatorDriftStimulus | None
    │   ├── policy_bundle: PolicyBundleStimulus | None
    │   ├── tenant_injection: TenantInjectionStimulus | None
    │   └── ambiguous_symptom: AmbiguousSymptomStimulus | None
    └── expected: ExpectedOutcomeV1Alpha2
        ├── incident: IncidentExpectation
        │   ├── incident_class: IncidentClass | None
        │   ├── actionable: bool
        │   └── telemetry_quality: TelemetryQualityExpectation | None
        ├── evidence: EvidenceExpectation
        │   ├── supporting: tuple[EvidenceAssertion, ...]
        │   ├── contradicting: tuple[EvidenceAssertion, ...]
        │   └── required_fresh: tuple[EvidenceAssertion, ...]
        ├── actions: ActionExpectation
        │   ├── eligible: tuple[ActionAssertion, ...]
        │   ├── forbidden: tuple[ActionAssertion, ...]
        │   ├── proposed: ProposedActionAssertion | None
        │   └── conflict: ConflictExpectation | None
        ├── policy: PolicyExpectation
        │   ├── decision: PolicyDecision
        │   ├── bundle_state: PolicyBundleState | None
        │   ├── fail_closed: bool
        │   ├── reason: PolicyReason | None
        │   ├── permitted_operations: tuple[PolicyOperation, ...]
        │   └── forbidden_operations: tuple[PolicyOperation, ...]
        ├── workflow: WorkflowExpectation
        │   ├── required_states: tuple[WorkflowState, ...]
        │   ├── terminal_reason: WorkflowReason | None
        │   ├── parent_count: CardinalityExpectation
        │   ├── proposal_count: CardinalityExpectation
        │   └── approval_count: CardinalityExpectation
        ├── mutations: MutationExpectation
        │   ├── count: CardinalityExpectation
        │   ├── allowed_actions: tuple[ActionAssertion, ...] # YAML: actions
        │   └── target: ScenarioTarget | None
        ├── audit: AuditExpectation
        │   └── events: tuple[AuditEventExpectation, ...]
        │       ├── event_type: AuditEventType
        │       └── count: CardinalityExpectation
        ├── tenant_isolation: TenantIsolationExpectation | None
        │   ├── reject_foreign_evidence_before_scoring: bool
        │   ├── reject_mismatch_before_external_io: bool
        │   ├── tenant_scoped_workflow_identity: bool
        │   ├── tenant_scoped_deduplication: bool
        │   ├── tenant_scoped_cache_and_topology: bool
        │   └── tenant_scoped_approval: bool
        ├── safety_gates: tuple[SafetyGate, ...]
        ├── scaler: ScalerExpectation | None
        │   ├── result: MetricValueResult | ErrorResult | SafeHoldResult
        │   ├── fabricated_zero_forbidden: bool
        │   ├── scale_down_forbidden: bool
        │   └── gateway_convergence_required: bool
        └── recovery: RecoveryExpectationV1Alpha2 | None
            ├── contract_ref: NonEmptyString
            ├── contract_version: PositiveInt
            ├── registry_version: NonEmptyString
            ├── require_fresh_telemetry: Literal[true]
            ├── evidence: tuple[EvidenceAssertion, ...]
            ├── conditions: tuple[RecoveryCondition, ...]
            └── minimum_post_action_windows: PositiveInt

EnvironmentDeclaration
├── environment: EnvironmentId
├── capabilities: frozenset[EnvironmentCapability]
├── adapter_operations: frozenset[AdapterOperation]
├── observations: frozenset[ObservationType]
└── planned_support: tuple[PlannedRequirementSupport, ...]
    ├── requirement: RequirementReference
    └── reason: BlockingReason

CompatibilityEntry                              # generated artifact model
├── scenario: ScenarioName
├── environment: EnvironmentId
├── status: CompatibilityStatus                  # supported/unsupported/blocked
├── missing_capabilities: tuple[EnvironmentCapability, ...]
├── missing_adapter_operations: tuple[AdapterOperation, ...]
├── missing_observations: tuple[ObservationType, ...]
└── blocking_reasons: tuple[BlockingReason, ...]

ScenarioPreflightResult
├── scenario: ScenarioName
├── scenario_api_version: ApiVersion
├── environment: EnvironmentId
├── status: CompatibilityStatus
├── required_capabilities: tuple[EnvironmentCapability, ...]
├── required_adapter_operations: tuple[AdapterOperation, ...]
├── required_observations: tuple[ObservationType, ...]
├── missing_capabilities: tuple[EnvironmentCapability, ...]
├── missing_adapter_operations: tuple[AdapterOperation, ...]
├── missing_observations: tuple[ObservationType, ...]
└── blocking_reasons: tuple[BlockingReason, ...]
```

`ActionAssertion` contains an `action_type` and an optional typed
`scale_direction` (`up`, `down`, or `any`), which is permitted only for `scale`. This is
testbed assertion vocabulary; it does not change the production `ActionType`
enum. It is necessary to express scale-up eligibility and stale-data
scale-down prohibition without treating all scale operations as equivalent.
`any` overlaps both directional assertions for disjointness checks. Proposed
and observed scale assertions must use an exact direction; `any` is permitted
only in a forbidden assertion that intentionally prohibits both directions.

`EvidenceAssertion` contains `evidence_type`, optional normalized
`subject_role`, `tenant_relation` (`same-tenant` or `foreign-tenant`), and
`freshness` (`fresh`, `stale`, `missing`, or `conflicting`). It contains no
runtime service name or concrete tenant identifier. This distinguishes caller
from dependency and local from foreign evidence without demo leakage.

The added stimulus branches are closed typed controls for telemetry state,
incident delivery count/mode, approval timing relative to expiry, protected
field drift timing, policy-bundle tier, tenant injection kind/relation, and an
ambiguous symptom. They describe setup and never execute it.

`PolicyOperation` covers `read-only-investigation`, `alert-only`, `rollback`,
`scale-up`, `scale-down`, `scaler-pause`, `policy-activation`, and
`approval-issuance`.

Scaler results are a discriminated union. Only `MetricValueResult` carries a
finite non-negative decimal value and gateway result identity. `ErrorResult`
and `SafeHoldResult` carry closed reasons and cannot contain a numeric value, so
they cannot encode a fabricated zero.

`EnvironmentRequirements` authors only operational capabilities. A single
typed `EnvironmentCapabilityDefinition` registry maps every capability to the
operations and observations it entails. This avoids three independently
authored requirement sets while still allowing declarations and preflight to
report exact operations and observations. The initial operation enum is:

```text
install reset wait-for-healthy-baseline apply-load inject-fault deploy-version
observe-state cleanup interrupt-telemetry control-policy-bundle
mutate-workload emit-incident control-approval
```

The initial observation enum is:

```text
baseline-health telemetry-quality incident-assessment workflow-state
mutation-count audit-event workload-state dependency-topology
deployment-version recovery-evidence scaler-result policy-state tenant-rejection
```

Capability definitions are code-owned schema vocabulary, not environment facts.
Environment declarations remain authoritative for which capabilities,
operations, and observations are actually implemented. Declaration validation
rejects a capability unless the declaration also supplies every operation and
observation entailed by its definition.

`CardinalityExpectation` is typed as exactly one of `exact`, `at_most`, or
`at_least`, each a non-negative integer. It avoids ambiguous min/max pairs.
Audit, workflow-reason, policy-reason, adapter-operation, and observation values
are closed enums. New values require a schema change; no opaque dictionaries or
free-form decision metadata are accepted. Human descriptions and blocking
reason details may be strings, but they cannot drive evaluation.

### Compatibility stays outside GuardianScenario

`CompatibilityStatus` never appears in `GuardianScenarioV1Alpha2`. A scenario
states requirements and optional discovery candidates. Environment declarations
state facts. A pure derivation function produces compatibility entries. The
checked-in `compatibility.yaml` is generated output and is validated by exact
canonical equivalence to a fresh derivation.

`index.yaml` is likewise generated from scenario documents plus derived
compatibility. It contains no independently authored semantic values.

## Validation invariants

1. v1alpha1 validation and serialization remain byte-for-byte compatible.
2. Every v1alpha2 scenario has a non-empty description, baseline, target, and
   environment requirement set.
3. Candidate environment identifiers are unique and known when supplied, but
   they never grant compatibility.
4. Environment capability, operation, and observation identifiers are closed
   enums and unique after canonical sorting.
5. Supporting, contradicting, and fresh evidence collections are individually
   unique by complete assertion identity. The same evidence type may occur on
   both sides only when role, tenant relation, or freshness differs; identical
   assertions cannot.
6. Eligible and forbidden actions are disjoint. A proposed action is eligible
   and not forbidden.
7. A conflict expectation names at least two eligible, mutually conflicting
   actions. Scale/rollback conflict requires `conflict-resolution`; unresolved
   conflict requires no proposed mutating action.
8. Any eligible or proposed mutating action requires a Recovery Contract
   reference, fresh recovery evidence, `recovery-observation`, and mutation
   observation.
9. Mutation allowed-action assertions are a subset of eligible actions with
   exact direction agreement. They constrain executed mutations; they do not
   require execution when the cardinality lower bound is zero. Every executed
   mutation must be in the allowed set. When the lower bound is positive, at
   least one allowed action must occur; alternative allowed actions need not all
   execute. A proposed scale-up cannot be satisfied by an executed scale-down.
   Zero mutations may not declare a target.
10. Non-actionable incidents require zero mutations and no proposed mutating
    action.
11. `telemetry_failure` forbids scale and rollback, requires unusable telemetry
    quality, and cannot be encoded as `unknown`.
12. `unknown` requires healthy telemetry, no proposed mutation, and zero
    mutations.
13. Stale telemetry plus scaling assertions must explicitly forbid
    `scale(direction=down)` and require an error or safe-hold scaler result.
14. `FAIL_CLOSED` policy state requires `denied`, `fail_closed=true`, and zero
    mutations. `fail_closed=false` is rejected for unusable policy states.
    `RESTRICTED` permits only read-only investigation and alert-only behavior;
    it forbids new rollback, scale-down, scaler pause, policy activation, and
    approval issuance. `FRESH` alone permits normal policy evaluation.
15. Approval expiry requires an expired terminal reason and exact zero mutations.
16. Operator drift requires `superseded-by-operator`, a drift terminal reason,
    and exact zero subsequent mutations.
17. Duplicate-alert assertions require exact one parent workflow and tenant-
    scoped deduplication.
18. Any foreign-tenant rejection assertion requires all tenant-isolation booleans
    relevant to the tested path and a rejection audit event. Cross-tenant
    evidence rejection must be before scoring; tenant mismatch must be before
    external I/O.
19. Audit event expectations are unique by event type and each has a
    deterministic cardinality. Exact zero is the typed forbidden form.
20. Cardinalities are deterministic non-negative integers; no timestamps,
    random IDs, machine paths, or unordered output enter scenario, matrix,
    index, or preflight serialization.
21. Scaler `error` or `safe-hold` results cannot contain a numeric metric value.
    Stale or unavailable telemetry requires `fabricated_zero_forbidden=true`,
    `scale_down_forbidden=true`, and an error or safe-hold result. Multi-replica
    consistency requires gateway convergence.
22. Ordered safety requirements use a closed `SafetyGate` enum:
    `identity-before-version-action`, `fresh-evidence-before-eligibility`,
    `expiry-before-mutation`, `policy-before-mutation`,
    `drift-before-each-mutation`, `tenant-before-scoring`,
    `tenant-before-external-io`, and `post-action-evidence-for-recovery`.
    Scenario-specific validators require the relevant gate for identity,
    expiry, drift, stale-policy, cross-tenant, and recovery assertions.
23. Typed controls are internally consistent: duplicate delivery count is at
    least two, expiry attempts occur after expiry, drift occurs before its
    guarded mutation, and foreign injection uses a foreign-tenant relation.
24. Mutating recovery pins contract and registry versions, contains fresh
    post-action evidence, at least one typed condition, and one or more
    post-action windows. Conditions cover health, capacity convergence, error
    or latency improvement, desired-version restoration, pressure clearance,
    and policy restoration.
25. Policy permitted and forbidden operations are disjoint. `RESTRICTED` must
    encode its read-only allowance and every normative write denial;
    `FAIL_CLOSED` permits no write operation.

## Migration behavior

`upgrade_v1alpha1(scenario, environment_requirements, description,
scale_directions)` returns a
new v1alpha2 value without mutating the input. The caller must supply reviewed
operational requirements and a description because neither can be inferred
safely from v1alpha1. If v1 contains scale assertions, the caller must also
supply reviewed directions for each mapped assertion. There are no defaults for
these arguments.

The upgrader performs only lossless mappings:

- `applicable_environments` → `candidate_environments`;
- metadata, target, baseline, and the three legacy stimulus branches retain
  their values; new control branches remain absent;
- incident class maps to `incident.incident_class`;
- v1 evidence types map to `evidence.supporting`;
- allowed/forbidden/proposed actions map to `actions`;
- policy decision maps to `policy.decision` with no invented bundle state;
- workflow states map to `workflow.required_states`;
- recovery maps without changing its contract or freshness semantics.

Fields unavailable in v1alpha1 receive conservative typed values: actionable is
true only when a mutating action was allowed or proposed; mutation cardinality
is `at_most: 1` for a mutating scenario and `exact: 0` otherwise; audit has no
invented event expectations; workflow cardinalities are left at typed neutral
defaults except where existing data proves them. Contradicting evidence,
tenant-isolation, policy staleness, and terminal reasons remain absent.

If these conservative values violate a v1alpha2 safety invariant, upgrade fails
with path-aware `UpgradeError`; it never weakens the invariant. Upgrade output
contains no timestamp or migration UUID. Loading never invokes the upgrader.

## Compatibility derivation rules

Given a v1alpha2 scenario and an environment declaration:

1. Normalize all enum sets and output tuples in enum-value lexical order.
2. Compute missing capabilities, adapter operations, and observations by pure
   set difference.
3. Partition missing requirements into those named by typed `planned_support`
   entries and those absent by design. A planned entry is not an implemented
   capability and cannot satisfy preflight.
4. If any missing requirement is absent by design, status is `unsupported` and
   every missing item is emitted. Environment names cannot change this result.
5. If every missing requirement has a planned-support entry, status is
   `blocked`, every missing item is emitted, and at least one concrete blocking
   reason is emitted.
6. If nothing is missing, status is `supported`; stale planned-support entries
   for already implemented requirements are declaration validation errors.
7. The matrix contains the full Cartesian product of all catalog scenarios and
   all known environment declarations. Candidate environments are index
   discoverability metadata only and never omit or alter compatibility rows.
8. Unknown scenario or environment identifiers fail before adapter lookup,
   installation, load, fault, or any other external I/O.
9. The five demo declarations and future sixth declarations use the same model.
   Adding an environment requires a declaration and adapter registration, not a
   scenario or production-code branch.
10. `compatibility.yaml` and `index.yaml` are canonical generated projections.
    Validators compare parsed typed content against a fresh derivation and fail
    on missing, extra, reordered, or changed semantic entries.
11. Catalog generation may read v1alpha1 scenarios but never upgrades them.
    Each v1alpha1/environment row is deterministically `blocked` with typed
    reason `scenario-requires-explicit-v1alpha2-upgrade`, because operational
    capabilities cannot be inferred. The index preserves its original API
    version and marks compatibility unresolved. Once an explicitly authored or
    upgraded v1alpha2 document replaces it, ordinary derivation applies.

Environment declarations are the source of compatibility facts. Adapter
protocol conformance tests verify declarations against implementations; an
adapter class is not introspected during pure derivation.

A positive mutation cardinality lower bound implicitly requires
`action-controller-execution` during preflight. A runtime that does not declare
that capability is unsupported for the scenario even when it can observe
mutations. This derived requirement is a test-harness compatibility fact; it
does not mark any normative action-controller requirement implemented.

## Recommendation-only execution semantics

`actions.eligible` and `actions.proposed` remain deterministic Guardian
recommendations. `mutations.actions` is retained on the YAML wire contract for
compatibility, but maps internally to `allowed_actions`: the set of mutations
that may have executed. A Guardian observation retains the `mutations` wire key
and maps it internally to `executed_mutations`.

Evaluation always checks mutation cardinality, requires the reported count to
equal the number of executed mutation records, and rejects every executed
mutation outside the allowed set. An `atMost: 1` contract therefore accepts
zero executions. Exact-positive and positive-lower-bound contracts additionally
require a non-empty allowed set, but they do not require every allowed
alternative to execute. The minimal runtime is recommendation-only, reports no
executed mutations, and cannot preflight a positive-lower-bound mutation
contract.

## Preflight contract

`preflight_scenario(scenario, environment_declaration)` is a pure total function
for validated inputs and returns `ScenarioPreflightResult`. It performs no file,
network, clock, environment-variable, adapter, Kubernetes, or random access.
All collections are lexically sorted tuples, so repeated calls and serialization
are byte-identical.

Only `status == supported` is executable by a future runner. `unsupported` and
`blocked` results are deterministic refusal results. The result includes all
requirements, all missing items, and all blocking reasons; it never includes
generated timestamps or machine-specific paths.

## Consequences

The schema becomes larger but remains explicit and reviewable. Scenario authors
cannot smuggle assertions into arbitrary mappings, and environment names cannot
grant support. Compatibility artifacts can drift only by failing validation.
The explicit upgrader makes migration work visible and prevents a loader change
from silently changing v1alpha1 semantics.

The design deliberately does not assert which pairings are currently supported;
that depends on capability declarations and adapter contract evidence added by
the later implementation.

## Alternatives rejected

- Overload `ServiceCapability`: rejected because workload selection and fixture
  operability are different domains.
- Put compatibility status in each scenario: rejected as a duplicate and stale
  source of environment facts.
- Infer capabilities from demo names or adapter class names: rejected as
  non-generalized and fail-open.
- Use `dict[str, Any]` assertions: rejected because unknown semantics would load
  without validation.
- Mutate v1alpha1 in place: rejected because existing consumers and serialized
  fixtures must remain stable.
- Add execution hooks to preflight: rejected because preflight must be pure and
  this task does not authorize a live runner.

## Adversarial review record

Review date: 2026-07-22. Scope: this decision and its proposed implementation
boundary.

| Risk | Review result | Required control |
| --- | --- | --- |
| Demo-specific leakage | No demo service name exists in model fields, enums, validation, or derivation. Demo IDs occur only as fixture candidates in this analysis. | Architecture test scans production and generic scenario logic. |
| Capability inflation | Initial vocabulary is the exact union derived above. | New enum values require a scenario obligation and review. |
| Duplicate sources of truth | Matrix and index are generated projections; scenario requirements plus environment declarations are inputs. | Exact-equivalence tests reject drift. |
| Silent v1alpha1 changes | v1 classes and loader branch remain unchanged; upgrade is explicit. | Golden and API regression tests run before v1alpha2 tests. |
| Mutation without recovery | Mutating eligibility/proposal requires contract, fresh evidence, and recovery capability. | Cross-model validation plus negative tests. |
| Fail-open policy | Unusable policy state forces denied/fail-closed/zero mutation. | Negative stale-policy tests. |
| Stale telemetry scale-down | Typed testbed-only action assertions distinguish scale-up from scale-down and require safe-hold. | Negative stale-telemetry tests. |
| Cross-tenant ambiguity | Tenant assertions are explicit typed booleans with ordered gates and audit requirement. | Cross-tenant validator tests. |
| Non-deterministic metadata | Time, UUID, path, and unordered container fields are absent from result contracts. | Repeat/byte-equivalence tests. |
| Name-only compatibility | Names only select declarations; requirements decide status. | Tests replace a capable declaration with the same ID and missing capabilities. |
| Missing typed stimuli | Every non-legacy control used by the 15 scenarios has a closed stimulus branch. | Stimulus/capability consistency tests. |
| Evidence subject ambiguity | Evidence carries normalized role, tenant relation, and freshness. | Caller/dependency and foreign-tenant tests. |
| Under-specified recovery | Mutating recovery pins contract/registry versions, fresh evidence, conditions, and post-action windows. | Negative recovery tests. |
| Scaler/audit observability | Scaler results are discriminated and audit events carry cardinality. | Fabricated-zero, convergence, and audit-count tests. |
| Directional mutation mismatch | Proposed and observed actions share directional assertions; wildcard scale is forbidden-only. | Scale-up versus observed scale-down regression test. |

No Critical or Important design finding remains unresolved. The testbed-only
scale-direction assertion is additive assertion vocabulary and does not alter
the production action taxonomy.

## Recommendation

Adopt v1alpha2 as a parallel, testbed-only schema with explicit upgrade,
capability-derived compatibility, generated projections, and pure preflight.
Implement the model and derivation infrastructure before authoring the 15
scenario files.
