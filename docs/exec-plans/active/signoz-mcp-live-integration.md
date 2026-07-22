# SigNoz MCP Live Integration Plan

**Goal:** Add an optional agent-facing SigNoz MCP integration without changing
deterministic Guardian assertions, actions, eligibility, scores, recovery proof,
or KEDA polling.

**Status:** Deferred. The architectural boundary, typed contracts, static
import rules, and integration-only Task targets exist; this plan must not be
executed as part of the schema architecture task.

## Preconditions

- An approved credential-delivery design exists outside repository files and
  test artifacts.
- The diagnostic service is separate from `apps/`, `packages/`, and
  `services/` production execution paths.
- The approved deterministic Query Contract and Guardian SigNoz gateway paths
  remain the sole normative assertion providers.
- E2E verdict freezing happens before diagnostics are scheduled.

## Proposed targets

| Target | Scope | Ordinary unit tests |
| --- | --- | --- |
| `mcp:signoz:check` | Static boundary and configuration validation without MCP loading | Never runs MCP |
| `mcp:signoz:smoke` | Explicit integration smoke check with disposable credentials | Not invoked |
| `diagnostics:signoz` | Explicit post-verdict failure-diagnostic collection | Not invoked |

## Future implementation tasks

1. Write failing integration tests showing credentials are injected only by the
   approved runtime secret mechanism and are absent from logs, artifacts, and
   serialized reports.
2. Implement the MCP client in an integration-only diagnostic adapter outside
   production services and the KEDA scaler. Do not expose its client type to
   reasoner, policy, action-controller, or scaler code.
3. Schedule the adapter only with a frozen `ScenarioVerdict`; capture provider
   failures as `DiagnosticRunResult.warnings` and preserve the verdict exactly.
4. Persist only redacted `DiagnosticReport` data with `authoritative: false`.
   Do not use reports for recovery verification without independently captured
   deterministic evidence through an approved Query Contract.
5. Convert candidate queries only into `QueryContractProposal` drafts. Require
   normal review, approval, versioning, and gateway validation before any query
   becomes active.
6. Implement `mcp:signoz:smoke` and `diagnostics:signoz` as integration-only
   targets with no dependency from `test:unit`, `test:contract`, or KEDA
   polling. Keep `mcp:signoz:check` client-free.
7. Add contract and integration coverage for tenant isolation, freshness and
   policy validation; no MCP result may bypass or substitute these checks.

## Required verification for the future change

```bash
task format:check
task lint
task typecheck
task test:unit
task test:contract
task test:architecture
task mcp:signoz:check
task mcp:signoz:smoke
task diagnostics:signoz
task requirements:check
```

The last two commands are explicit integration runs and must never be added as
dependencies of ordinary unit tests.
