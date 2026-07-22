# Scoped Agent Guardrails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Strengthen scoped Guardian instructions and add machine-enforced architecture boundaries plus ownership review rules.

**Architecture:** Scoped `AGENTS.md` files define normative local constraints. A dependency-free Python architecture checker scans production source and deployment manifests for forbidden imports, mutation-provider placement, policy demo names, and scaler RBAC. Pytest tests exercise each boundary with isolated temporary repositories.

**Tech Stack:** Markdown, Python 3.13, pytest, GitHub CODEOWNERS.

---

### Task 1: Strengthen scoped instructions

**Files:**
- Modify: `policies/AGENTS.md`
- Modify: `services/action-controller/AGENTS.md`
- Modify: `services/keda-scaler/AGENTS.md`
- Modify: `services/reasoner/AGENTS.md`
- Modify: `testbeds/AGENTS.md`

- [x] Replace principle-only guidance with the supplied scopes, invariants, prohibited patterns, required tests, and completion reports.
- [x] Verify Markdown fences and source-of-truth references.

### Task 2: Add architecture boundary tests

**Files:**
- Create: `tests/architecture/test_boundaries.py`
- Create: `tests/architecture/__init__.py`
- Create: `tools/__init__.py`
- Create: `tools/architecture_rules.py`

- [x] Write tests for forbidden reasoner imports, production-to-testbed imports, scaler model clients, scaler write RBAC, policy demo names, and mutation providers outside the action controller.
- [x] Run the test file and verify it fails because the checker is absent.
- [x] Implement the smallest dependency-free checker that satisfies the tests.
- [x] Run the architecture tests and verify they pass.

### Task 3: Add ownership rules

**Files:**
- Create: `.github/CODEOWNERS`

- [x] Assign explicit review ownership to policies, action controller, KEDA scaler, reasoner, security tests, architecture tests, and production specification files.
- [x] Validate CODEOWNERS syntax structurally.

### Task 4: Verify and report

- [x] Run `uv run python -m unittest tests.architecture.test_boundaries -v`.
- [x] Run `uv run python -m unittest discover -s tests -p 'test_*.py' -v`.
- [x] Attempt mandatory Task commands and report unavailable commands or failures exactly.
- [x] Report files changed, boundaries enforced, and remaining gaps.
