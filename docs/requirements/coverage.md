# Guardian Production V1 Requirement Coverage

Generated from `docs/requirements/requirements.yaml`.
Normative source: `docs/spec/guardian-production-v1.md`.

## Validation summary

- Normative requirements: **121**
- Normative acceptance tests: **28**
- Supporting design capabilities: **4**
- Implementation test obligations: **149**
- Structured ambiguities: **222**
- Duplicate IDs: **0** (required for generation)
- Invalid references: **0** (required for generation)
- Dependency cycles: **0** (required for generation)

## Test-obligation types

| Type | Count |
| --- | ---: |
| `acceptance` | 28 |
| `contract` | 4 |
| `integration` | 39 |
| `replay` | 8 |
| `security` | 4 |
| `unit` | 66 |

## Family coverage

| Family | GRD | AT | Test obligations | Normative IDs |
| --- | ---: | ---: | ---: | --- |
| `ACT` | 5 | 1 | 6 | `AT-ACT-001`, `GRD-ACT-001`, `GRD-ACT-002`, `GRD-ACT-003`, `GRD-ACT-004`, `GRD-ACT-005` |
| `AST` | 6 | 1 | 7 | `AT-AST-001`, `GRD-AST-001`, `GRD-AST-002`, `GRD-AST-003`, `GRD-AST-004`, `GRD-AST-005`, `GRD-AST-006` |
| `CLS` | 6 | 2 | 8 | `AT-CLS-001`, `AT-CLS-002`, `GRD-CLS-001`, `GRD-CLS-002`, `GRD-CLS-003`, `GRD-CLS-004`, `GRD-CLS-005`, `GRD-CLS-006` |
| `CON` | 5 | 1 | 6 | `AT-CON-001`, `GRD-CON-001`, `GRD-CON-002`, `GRD-CON-003`, `GRD-CON-004`, `GRD-CON-005` |
| `DRIFT` | 5 | 1 | 6 | `AT-DRIFT-001`, `GRD-DRIFT-001`, `GRD-DRIFT-002`, `GRD-DRIFT-003`, `GRD-DRIFT-004`, `GRD-DRIFT-005` |
| `ID` | 4 | 1 | 5 | `AT-ID-001`, `GRD-ID-001`, `GRD-ID-002`, `GRD-ID-003`, `GRD-ID-004` |
| `INF` | 7 | 2 | 9 | `AT-INF-001`, `AT-INF-002`, `GRD-INF-001`, `GRD-INF-002`, `GRD-INF-003`, `GRD-INF-004`, `GRD-INF-005`, `GRD-INF-006`, `GRD-INF-007` |
| `MLO` | 7 | 1 | 8 | `AT-MLO-001`, `GRD-MLO-001`, `GRD-MLO-002`, `GRD-MLO-003`, `GRD-MLO-004`, `GRD-MLO-005`, `GRD-MLO-006`, `GRD-MLO-007` |
| `OPA` | 7 | 1 | 8 | `AT-OPA-001`, `GRD-OPA-001`, `GRD-OPA-002`, `GRD-OPA-003`, `GRD-OPA-004`, `GRD-OPA-005`, `GRD-OPA-006`, `GRD-OPA-007` |
| `OUT` | 7 | 1 | 8 | `AT-OUT-001`, `GRD-OUT-001`, `GRD-OUT-002`, `GRD-OUT-003`, `GRD-OUT-004`, `GRD-OUT-005`, `GRD-OUT-006`, `GRD-OUT-007` |
| `POL` | 4 | 1 | 5 | `AT-POL-001`, `GRD-POL-001`, `GRD-POL-002`, `GRD-POL-003`, `GRD-POL-004` |
| `QC` | 8 | 2 | 10 | `AT-QC-001`, `AT-QC-002`, `GRD-QC-001`, `GRD-QC-002`, `GRD-QC-003`, `GRD-QC-004`, `GRD-QC-005`, `GRD-QC-006`, `GRD-QC-007`, `GRD-QC-008` |
| `REA` | 7 | 3 | 10 | `AT-REA-001`, `AT-REA-002`, `AT-REA-003`, `GRD-REA-001`, `GRD-REA-002`, `GRD-REA-003`, `GRD-REA-004`, `GRD-REA-005`, `GRD-REA-006`, `GRD-REA-007` |
| `REC` | 7 | 1 | 8 | `AT-REC-001`, `GRD-REC-001`, `GRD-REC-002`, `GRD-REC-003`, `GRD-REC-004`, `GRD-REC-005`, `GRD-REC-006`, `GRD-REC-007` |
| `RPL` | 1 | 2 | 3 | `AT-RPL-001`, `AT-RPL-002`, `GRD-RPL-001` |
| `SCL` | 7 | 2 | 9 | `AT-SCL-001`, `AT-SCL-002`, `GRD-SCL-001`, `GRD-SCL-002`, `GRD-SCL-003`, `GRD-SCL-004`, `GRD-SCL-005`, `GRD-SCL-006`, `GRD-SCL-007` |
| `TEN` | 8 | 1 | 9 | `AT-TEN-001`, `GRD-TEN-001`, `GRD-TEN-002`, `GRD-TEN-003`, `GRD-TEN-004`, `GRD-TEN-005`, `GRD-TEN-006`, `GRD-TEN-007`, `GRD-TEN-008` |
| `TLS` | 4 | 1 | 5 | `AT-TLS-001`, `GRD-TLS-001`, `GRD-TLS-002`, `GRD-TLS-003`, `GRD-TLS-004` |
| `TOP` | 6 | 1 | 7 | `AT-TOP-001`, `GRD-TOP-001`, `GRD-TOP-002`, `GRD-TOP-003`, `GRD-TOP-004`, `GRD-TOP-005`, `GRD-TOP-006` |
| `TTL` | 6 | 1 | 7 | `AT-TTL-001`, `GRD-TTL-001`, `GRD-TTL-002`, `GRD-TTL-003`, `GRD-TTL-004`, `GRD-TTL-005`, `GRD-TTL-006` |
| `WF` | 4 | 1 | 5 | `AT-WF-001`, `GRD-WF-001`, `GRD-WF-002`, `GRD-WF-003`, `GRD-WF-004` |

## Supporting design capabilities

These records are non-normative implementation mappings and do not participate in `GRD-*` or `AT-*` dependency validation.

| ID | Source | Status | Implementation | Tests |
| --- | --- | --- | --- | --- |
| `DESIGN-HARNESS-001` | `docs/superpowers/specs/2026-07-22-verification-harness-repair-design.md` §Goal, Bootstrap Contract, Task Architecture, and Tests | `implemented` | `scripts/bootstrap.sh`, `scripts/verification-preflight.sh`, `tools/verification_harness.py`, `Taskfile.yml` | `tests/unit/test_bootstrap.py`, `tests/unit/test_verification_harness.py` |
| `DESIGN-RUNTIME-001` | `docs/superpowers/specs/2026-07-23-minimal-guardian-runtime-design.md` §Store and service boundary | `implemented` | `apps/guardian_api/models.py`, `apps/guardian_api/store.py`, `apps/guardian_api/service.py` | `tests/unit/test_guardian_service.py` |
| `DESIGN-SCENARIO-001` | `docs/spec/guardian-production-v1.md` §22 | `implemented` | `testbeds/scenarios/models.py`, `testbeds/scenarios/validation.py`, `testbeds/scenarios/loader.py` | `tests/unit/test_guardian_scenario_schema.py` |
| `DESIGN-SCENARIO-002` | `docs/architecture/decisions/guardian-scenario-v1alpha2.md` §Decision | `implemented` | `testbeds/scenarios/v1alpha2.py`, `testbeds/scenarios/upgrade.py`, `testbeds/scenarios/compatibility.py`, `testbeds/scenarios/catalog.py`, `testbeds/scenarios/assertions.py`, `testbeds/scenarios/guardian_client.py`, `testbeds/scenarios/matrix.py`, `testbeds/environments/capabilities.py` | `tests/unit/test_guardian_scenario_v1alpha2.py`, `tests/unit/test_guardian_scenario_upgrade.py`, `tests/unit/test_scenario_compatibility.py`, `tests/unit/test_scenario_catalog.py`, `tests/unit/test_scenario_execution.py`, `tests/unit/test_critical_scenario_pack.py`, `tests/contract/test_environment_capability_declarations.py` |

## Status

All normative implementation items remain `not_started`; all test obligations remain `not_implemented`. Status may advance only with validator-approved passing evidence.

This file is generated. Run `task requirements:render`; do not edit it independently.
