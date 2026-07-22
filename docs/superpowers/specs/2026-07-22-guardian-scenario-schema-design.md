# GuardianScenario Schema Design

## Goal

Implement the generalized scenario format from section 22 of
`docs/spec/guardian-production-v1.md` as the canonical, strict Python schema
used by testbed adapters and assertions. Scenarios describe portable intent
through normalized roles, capabilities, and selectors; environment adapters
remain responsible for mapping those concepts to concrete workloads.

## Public API

The `testbeds.scenarios` package exports `GuardianScenario` and
`load_scenario`:

```python
from testbeds.scenarios import GuardianScenario, load_scenario

scenario = load_scenario("testbeds/scenarios/legitimate-demand-scale-up.yaml")
assert isinstance(scenario, GuardianScenario)
```

`load_scenario` accepts a filesystem path, parses YAML with `yaml.safe_load`,
requires a mapping document, and validates it through the canonical Pydantic
model. YAML and Pydantic errors remain distinct and readable at the boundary.

## Package Structure

- `testbeds/scenarios/models.py` owns strict Pydantic models and normalized
  enums.
- `testbeds/scenarios/validation.py` owns reusable semantic constraints that
  are not simple field types.
- `testbeds/scenarios/loader.py` owns file and YAML parsing.
- `testbeds/scenarios/__init__.py` exposes the supported public API.
- `testbeds/scenarios/legitimate-demand-scale-up.yaml` demonstrates the
  canonical format.
- `tests/unit/test_guardian_scenario_schema.py` covers structural, enum, and
  semantic validation.

No handwritten JSON Schema is maintained in V1. If an external consumer later
needs one, it can be generated from the canonical Pydantic models.

## Model Shape

Every model is frozen and rejects unknown fields. YAML camelCase names are the
serialization aliases while Python callers use snake_case attributes.

The root requires the exact API identity
`tests.guardian.io/v1alpha1 / GuardianScenario`, DNS-style metadata names, and
a `spec`. A spec supports:

- non-empty, unique applicable environments;
- unsupported-environment entries with a reason, plus explicit
  required capabilities, fault support, telemetry signals, and providers;
- a target selected by normalized service role, workload role, and portable
  capabilities or semantic labels;
- baseline health requirements;
- load, fault, and deployment stimuli;
- expected incident class, proposed/allowed/forbidden actions, evidence types,
  policy decision, workflow states, and recovery conditions.

Enums use these explicit vocabularies:

- incident class: `load_spike`, `deployment_regression`,
  `dependency_failure`, `resource_saturation`, `telemetry_failure`, `unknown`;
- action: `scale`, `rollback`, `alert_only`, `continue_investigation`,
  `pause_scaler`;
- service role: `request-processor`, `api-gateway`, `background-worker`,
  `data-store`, `cache`, `message-broker`, `dependency`;
- capability: `horizontally-scalable`, `version-deployable`,
  `fault-injectable`, `queue-backed`, `stateful`, `stateless`;
- fault type: the existing `testbeds.models.FaultType` values;
- evidence type: `metrics`, `traces`, `logs`, `exceptions`,
  `deployment-event`, `service-identity`, `workload-state`, `topology`, `load`,
  `resource-utilization`, `dependency-health`, `telemetry-quality`,
  `policy-decision`, `action-result`, `recovery-telemetry`,
  `identity-conflict`;
- policy decision: `allowed`, `denied`, `approval-required`;
- workflow state: `active`, `telemetry-validation`, `assessment`, `classified`,
  `telemetry-failure`, `unknown`, `conflict-resolution`, `action-proposed`,
  `policy-allowed`, `policy-denied`, `approval-pending`, `executing`,
  `recovery-verification`, `recovered`, `closed`,
  `superseded-by-operator`.

The production specification supplies the incident and action vocabularies.
The remaining enums are the V1 test-harness assertion vocabulary. Adding a new
normalized concept extends the schema; adding a new environment or mapping an
existing concept to a workload does not. Unknown values fail validation rather
than silently changing scenario intent.

## Semantic Validation

Validation enforces positive durations, a load multiplier greater than one,
unique environments and workflow states, and disjoint allowed and forbidden
action sets. Deployment transitions require different non-empty versions.
Mutating expected actions require recovery expectations, and recovery that
requires fresh telemetry must describe an observable healthy/recovered end
condition.

Selectors never expose direct runtime identity fields. Strict unknown-field
rejection therefore rejects `name`, `serviceName`, `deploymentName`,
`podName`, and similar identifiers. Portable semantic labels, when present,
must use normalized DNS-style keys and non-empty scalar values. Exact reserved
runtime-identity keys are rejected, including `name`, `serviceName`,
`workloadName`, `deploymentName`, `podName`, `namespace`, `instance`,
`service.name`, `service.instance.id`, `k8s.workload.name`,
`k8s.deployment.name`, `k8s.pod.name`, and `app.kubernetes.io/name`.
Portable semantic keys such as `service-tier` remain valid. Values are not
compared against a list of demo names. The schema contains no
environment-specific service-name vocabulary or classification branches.

A scenario may combine load, deployment, and fault stimuli when the test intent
requires it. Validation checks each stimulus independently and does not infer
causality or prohibit a combination merely because multiple stimuli exist.

Environment compatibility keeps the concise `applicableEnvironments` list from
the requested format. `unsupportedEnvironments` records an environment and
non-empty incompatibility reason. Requirement sets declare capabilities,
injectable faults, telemetry signals, and action providers needed by the
scenario. These compatibility fields default to explicit empty collections so
the approved concise YAML remains valid, while every validated
`GuardianScenario` model carries all declarations and emits them during
canonical serialization. The two environment lists must be internally unique
and disjoint. An unsupported entry, when present, always requires its reason.

## Errors and Safety

Malformed YAML raises a loader error with path context. Structurally valid YAML
that violates the schema raises a Pydantic validation error with field paths.
No loader hook performs arbitrary object construction, environment lookup, or
adapter execution. The package remains test-only and production packages must
not import it.

## Testing and Traceability

Unit tests first prove a minimal and a full scenario, then cover exact API
identity, unknown fields at multiple levels, malformed YAML, invalid names and
durations, bad load multipliers, duplicate environments and workflow states,
unknown enum values, direct runtime selectors, action overlap, incompatible
environment declarations, invalid deployment transitions, and missing recovery
for mutating actions. A positive test proves that combined fault, load, and
deployment stimuli remain valid.

The implementation traceability record will link the new unit test to the
applicable generalized-test-scenario obligation if the requirements registry
contains one. Section 22 prose and the testbed scenario-design rules remain the
source of truth where no stable `GRD-*` identifier exists.
