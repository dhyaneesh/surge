# Guardian SRE Agent Instructions

## Source of truth

The normative source of truth is:

- `docs/spec/guardian-production-v1.md`

Do not silently weaken, reinterpret, or omit normative `GRD-*` or `AT-*`
requirements.

## Core development rule

Work test-first.

For every implementation task:

1. Identify the applicable normative requirements.
2. Add or update failing tests.
3. Implement the smallest complete solution.
4. Run the narrowest relevant test suite.
5. Run all affected contract and integration tests.
6. Update the requirement traceability record.
7. Report remaining failures honestly.

Do not claim the project or milestone is complete merely because code compiles.

## Safety boundaries

- Production reasoning code must not import testbed adapters.
- The reasoner must never receive Kubernetes write credentials.
- The model must not authorize actions.
- The model must not change deterministic scores or eligibility.
- No direct NATS, Temporal or Kubernetes execution bypass is permitted.
- Missing, stale or conflicting evidence must fail closed.
- Never add demo-specific service names to production decision logic.
- Never add arbitrary shell execution controlled by model output.
- Never place secrets in prompts, logs, fixtures or audit payloads.

## Repository areas

- `apps/`: user-facing API, UI and integrations.
- `services/`: deployable backend services.
- `packages/`: reusable domain libraries.
- `testbeds/`: demo environments and test-only controls.
- `tests/`: product test suites.
- `docs/exec-plans/`: implementation plans and completed work records.

## Mandatory commands

Run before declaring a task finished:

```bash
task format:check
task lint
task typecheck
task test:unit
task test:contract
```
