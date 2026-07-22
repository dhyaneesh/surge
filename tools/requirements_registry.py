"""Validate and render the Guardian requirement traceability registry."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "guardian.requirements/v1"
PROVENANCE_VALUES = {
    "explicit_normative",
    "explicit_acceptance_test",
    "explicit_environment_mapping",
    "inferred_from_service_responsibilities",
    "inferred_prerequisite",
    "implementation_recommendation",
    "unresolved",
}
PROVENANCE_FIELDS = {
    "component_ownership",
    "dependencies",
    "test_obligations",
    "environment_mappings",
    "evidence_artifacts",
}
ENVIRONMENT_IDS = {
    "otel-demo",
    "aws-retail",
    "online-boutique",
    "argo-rollouts",
    "keda-rabbitmq",
}
REQUIREMENT_ID = re.compile(r"^(?:GRD|AT)-[A-Z]+-\d{3}$")
TEST_ID = re.compile(
    r"^TST-(?:GRD|AT)-[A-Z]+-\d{3}-(?:UNIT|CONTRACT|INTEGRATION|ACCEPTANCE|REPLAY|SECURITY)$"
)
IMPLEMENTATION_STATUSES = {
    "not_started",
    "in_progress",
    "implemented",
    "blocked",
    "waived",
}
TEST_STATUSES = {"not_implemented", "failing", "passing", "blocked"}


@dataclass(frozen=True, order=True)
class ValidationIssue:
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


def _issue(
    issues: list[ValidationIssue], code: str, path: str, message: str
) -> None:
    issues.append(ValidationIssue(code, path, message))


def _invalid_repository_path(value: str) -> bool:
    if value.startswith(('/', '\\')):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return True
    return ".." in re.split(r"[\\/]", value)


def _walk_strings(value: Any, path: str = "$") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, str):
        found.append((path, value))
    elif isinstance(value, dict):
        for key, child in value.items():
            found.extend(_walk_strings(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_walk_strings(child, f"{path}[{index}]"))
    return found


def _typed_array(
    issues: list[ValidationIssue],
    values: Any,
    path: str,
    predicate: Any,
    unknown_code: str,
    known: set[str],
) -> None:
    if not isinstance(values, list):
        _issue(issues, "invalid_schema", path, "must be an array")
        return
    for index, value in enumerate(values):
        item_path = f"{path}[{index}]"
        if not isinstance(value, str) or value.startswith("AMBIGUOUS:"):
            _issue(
                issues,
                "invalid_typed_array_value",
                item_path,
                "typed arrays may contain only stable identifiers",
            )
        elif not predicate(value) or value not in known:
            _issue(issues, unknown_code, item_path, f"unknown identifier {value!r}")


def _cycles(items: list[dict[str, Any]], known_ids: set[str]) -> list[list[str]]:
    graph = {
        item.get("id"): [
            dependency
            for dependency in item.get("dependencies", [])
            if dependency in known_ids
        ]
        for item in items
        if item.get("id") in known_ids
    }
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for dependency in graph.get(node, []):
            if state.get(dependency, 0) == 0:
                visit(dependency)
            elif state.get(dependency) == 1:
                start = stack.index(dependency)
                cycle = stack[start:] + [dependency]
                if cycle not in cycles:
                    cycles.append(cycle)
        stack.pop()
        state[node] = 2

    for node in sorted(graph):
        if state.get(node, 0) == 0:
            visit(node)
    return cycles


def validate_registry(registry: dict[str, Any]) -> list[ValidationIssue]:
    """Return deterministic validation issues for a parsed registry."""

    issues: list[ValidationIssue] = []
    if registry.get("schema_version") != SCHEMA_VERSION:
        _issue(
            issues,
            "invalid_schema_version",
            "$.schema_version",
            f"expected {SCHEMA_VERSION!r}",
        )

    requirements = registry.get("requirements", [])
    acceptance_tests = registry.get("acceptance_tests", [])
    obligations = registry.get("test_obligations", [])
    environments = registry.get("environments", [])
    normative_items = [*requirements, *acceptance_tests]

    normative_ids = [item.get("id") for item in normative_items]
    obligation_ids = [item.get("id") for item in obligations]
    environment_ids = [item.get("id") for item in environments]
    all_ids = [*normative_ids, *obligation_ids, *environment_ids]
    for identifier, count in sorted(Counter(all_ids).items(), key=lambda pair: str(pair[0])):
        if count > 1:
            _issue(
                issues,
                "duplicate_id",
                "$",
                f"identifier {identifier!r} occurs {count} times",
            )

    known_requirements = {value for value in normative_ids if isinstance(value, str)}
    known_obligations = {value for value in obligation_ids if isinstance(value, str)}
    known_environments = {value for value in environment_ids if isinstance(value, str)}
    if known_environments != ENVIRONMENT_IDS:
        unknown = sorted(known_environments - ENVIRONMENT_IDS)
        missing = sorted(ENVIRONMENT_IDS - known_environments)
        if unknown or missing:
            _issue(
                issues,
                "unknown_environment",
                "$.environments",
                f"unknown={unknown}, missing={missing}",
            )

    for collection_name, collection in (
        ("requirements", requirements),
        ("acceptance_tests", acceptance_tests),
    ):
        for index, item in enumerate(collection):
            base = f"$.{collection_name}[{index}]"
            identifier = item.get("id")
            if not isinstance(identifier, str) or not REQUIREMENT_ID.fullmatch(identifier):
                _issue(issues, "invalid_id", f"{base}.id", "invalid normative ID")
            expected_prefix = "GRD-" if collection_name == "requirements" else "AT-"
            if isinstance(identifier, str) and not identifier.startswith(expected_prefix):
                _issue(
                    issues,
                    "invalid_id",
                    f"{base}.id",
                    f"{collection_name} IDs must start with {expected_prefix}",
                )

            _typed_array(
                issues,
                item.get("dependencies", []),
                f"{base}.dependencies",
                REQUIREMENT_ID.fullmatch,
                "unknown_requirement_reference",
                known_requirements,
            )
            _typed_array(
                issues,
                item.get("test_obligations", []),
                f"{base}.test_obligations",
                TEST_ID.fullmatch,
                "unknown_test_obligation_reference",
                known_obligations,
            )
            _typed_array(
                issues,
                item.get("applicable_environments", []),
                f"{base}.applicable_environments",
                lambda value: value in ENVIRONMENT_IDS,
                "unknown_environment",
                known_environments,
            )

            provenance = item.get("provenance", {})
            if set(provenance) != PROVENANCE_FIELDS:
                _issue(
                    issues,
                    "invalid_provenance",
                    f"{base}.provenance",
                    f"expected fields {sorted(PROVENANCE_FIELDS)}",
                )
            for field, value in provenance.items():
                if value not in PROVENANCE_VALUES:
                    _issue(
                        issues,
                        "unknown_provenance",
                        f"{base}.provenance.{field}",
                        f"unsupported provenance {value!r}",
                    )

            ambiguities = item.get("ambiguities", [])
            if not isinstance(ambiguities, list):
                _issue(issues, "invalid_schema", f"{base}.ambiguities", "must be an array")
                ambiguities = []
            for ambiguity_index, ambiguity in enumerate(ambiguities):
                if not isinstance(ambiguity, dict) or not ambiguity.get("field") or not ambiguity.get("reason"):
                    _issue(
                        issues,
                        "invalid_ambiguity",
                        f"{base}.ambiguities[{ambiguity_index}]",
                        "ambiguity requires field and reason",
                    )

            linked_tests = item.get("test_obligations", [])
            has_test_ambiguity = any(
                isinstance(ambiguity, dict)
                and ambiguity.get("field") == "test_obligations"
                for ambiguity in ambiguities
            )
            if not linked_tests and not has_test_ambiguity:
                _issue(
                    issues,
                    "missing_direct_test",
                    f"{base}.test_obligations",
                    "normative item needs a direct test obligation or structured ambiguity",
                )

            implementation_status = item.get("implementation_status")
            test_status = item.get("test_status")
            if implementation_status not in IMPLEMENTATION_STATUSES:
                _issue(
                    issues,
                    "invalid_status",
                    f"{base}.implementation_status",
                    f"unsupported status {implementation_status!r}",
                )
            if test_status not in TEST_STATUSES:
                _issue(
                    issues,
                    "invalid_status",
                    f"{base}.test_status",
                    f"unsupported status {test_status!r}",
                )
    for index, obligation in enumerate(obligations):
        base = f"$.test_obligations[{index}]"
        identifier = obligation.get("id")
        if not isinstance(identifier, str) or not TEST_ID.fullmatch(identifier):
            _issue(issues, "invalid_id", f"{base}.id", "invalid test-obligation ID")
        _typed_array(
            issues,
            obligation.get("applicable_requirements", []),
            f"{base}.applicable_requirements",
            REQUIREMENT_ID.fullmatch,
            "unknown_requirement_reference",
            known_requirements,
        )
        _typed_array(
            issues,
            obligation.get("applicable_environments", []),
            f"{base}.applicable_environments",
            lambda value: value in ENVIRONMENT_IDS,
            "unknown_environment",
            known_environments,
        )
        if obligation.get("status") not in TEST_STATUSES:
            _issue(
                issues,
                "invalid_status",
                f"{base}.status",
                f"unsupported status {obligation.get('status')!r}",
            )
        for requirement_id in obligation.get("applicable_requirements", []):
            if requirement_id in known_requirements:
                target = next(
                    item for item in normative_items if item.get("id") == requirement_id
                )
                if identifier not in target.get("test_obligations", []):
                    _issue(
                        issues,
                        "test_back_reference_mismatch",
                        f"{base}.applicable_requirements",
                        f"{requirement_id} does not reference {identifier}",
                    )

    for cycle in _cycles(normative_items, known_requirements):
        _issue(
            issues,
            "dependency_cycle",
            "$.requirements",
            " -> ".join(cycle),
        )

    for path, value in _walk_strings(registry):
        if _invalid_repository_path(value):
            _issue(
                issues,
                "invalid_repository_path",
                path,
                f"absolute, parent-relative, or machine-specific path {value!r}",
            )
    return sorted(set(issues))


def validate_traceability(
    registry: dict[str, Any],
    implementation: dict[str, Any],
    repository_root: Path,
    test_results: list[dict[str, Any]],
) -> list[ValidationIssue]:
    """Validate implemented test markers, results, scenarios, and waivers."""

    issues: list[ValidationIssue] = []
    repository_root = repository_root.resolve()
    requirements = registry.get("requirements", [])
    acceptance_tests = registry.get("acceptance_tests", [])
    obligations = registry.get("test_obligations", [])
    normative_items = [*requirements, *acceptance_tests]
    normative_by_id = {item.get("id"): item for item in normative_items}
    obligation_ids = {
        item.get("id") for item in obligations if isinstance(item.get("id"), str)
    }

    if implementation.get("schema_version") != "guardian.requirements-implementation/v1":
        _issue(
            issues,
            "invalid_implementation_schema",
            "$.implementation.schema_version",
            "expected 'guardian.requirements-implementation/v1'",
        )

    implementation_records = implementation.get("test_implementations", [])
    implementation_ids = [record.get("test_obligation_id") for record in implementation_records]
    for identifier, count in Counter(implementation_ids).items():
        if count > 1:
            _issue(
                issues,
                "duplicate_id",
                "$.implementation.test_implementations",
                f"test implementation {identifier!r} occurs {count} times",
            )

    valid_markers: set[str] = set()
    for index, record in enumerate(implementation_records):
        base = f"$.implementation.test_implementations[{index}]"
        identifier = record.get("test_obligation_id")
        if identifier not in obligation_ids:
            _issue(
                issues,
                "unknown_test_obligation_reference",
                f"{base}.test_obligation_id",
                f"unknown test obligation {identifier!r}",
            )
            continue
        test_file = record.get("test_file")
        if not isinstance(test_file, str) or _invalid_repository_path(test_file):
            _issue(
                issues,
                "invalid_repository_path",
                f"{base}.test_file",
                f"invalid repository-relative test path {test_file!r}",
            )
            continue
        resolved = (repository_root / test_file).resolve()
        if not resolved.is_relative_to(repository_root) or not resolved.is_file():
            _issue(
                issues,
                "referenced_test_file_missing",
                f"{base}.test_file",
                f"referenced test file {test_file!r} does not exist",
            )
            continue
        if identifier not in resolved.read_text(encoding="utf-8", errors="replace"):
            _issue(
                issues,
                "test_marker_missing",
                f"{base}.test_file",
                f"{test_file!r} does not contain marker {identifier!r}",
            )
            continue
        valid_markers.add(identifier)

    for index, requirement in enumerate(requirements):
        linked = set(requirement.get("test_obligations", []))
        if not linked or not linked & valid_markers:
            _issue(
                issues,
                "grd_without_linked_test",
                f"$.requirements[{index}].test_obligations",
                f"{requirement.get('id')} has no implemented test marker",
            )
    for index, acceptance_test in enumerate(acceptance_tests):
        linked = set(acceptance_test.get("test_obligations", []))
        if not linked or not linked & valid_markers:
            _issue(
                issues,
                "at_without_implementation",
                f"$.acceptance_tests[{index}].test_obligations",
                f"{acceptance_test.get('id')} has no implemented test marker",
            )

    result_by_obligation: dict[str, dict[str, Any]] = {}
    result_ids = [result.get("test_obligation_id") for result in test_results]
    for identifier, count in Counter(result_ids).items():
        if count > 1:
            _issue(
                issues,
                "duplicate_id",
                "$.test_results",
                f"test result {identifier!r} occurs {count} times",
            )
    for index, result in enumerate(test_results):
        base = f"$.test_results[{index}]"
        identifier = result.get("test_obligation_id")
        if result.get("__load_error__"):
            _issue(
                issues,
                "invalid_test_result",
                base,
                str(result["__load_error__"]),
            )
            continue
        if result.get("schema_version") != "guardian.test-result/v1":
            _issue(
                issues,
                "invalid_test_result",
                f"{base}.schema_version",
                "expected 'guardian.test-result/v1'",
            )
        if identifier not in obligation_ids:
            _issue(
                issues,
                "unknown_test_obligation_reference",
                f"{base}.test_obligation_id",
                f"unknown test obligation {identifier!r}",
            )
            continue
        if result.get("status") not in {"passed", "failed", "blocked"}:
            _issue(
                issues,
                "invalid_test_result",
                f"{base}.status",
                f"unsupported result status {result.get('status')!r}",
            )
        result_by_obligation[identifier] = result

    for collection_name, collection in (
        ("requirements", requirements),
        ("acceptance_tests", acceptance_tests),
    ):
        for index, item in enumerate(collection):
            if item.get("implementation_status") != "implemented":
                continue
            linked = item.get("test_obligations", [])
            passing = bool(linked) and all(
                identifier in result_by_obligation
                and result_by_obligation[identifier].get("status") == "passed"
                and bool(result_by_obligation[identifier].get("evidence"))
                for identifier in linked
            )
            if item.get("test_status") != "passing" or not passing:
                _issue(
                    issues,
                    "complete_without_passing_evidence",
                    f"$.{collection_name}[{index}]",
                    f"{item.get('id')} is implemented without passing evidence for every linked test",
                )

    scenarios_by_requirement: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, scenario in enumerate(implementation.get("demo_scenarios", [])):
        base = f"$.implementation.demo_scenarios[{index}]"
        requirement_id = scenario.get("requirement_id")
        if requirement_id not in normative_by_id:
            _issue(
                issues,
                "unknown_requirement_reference",
                f"{base}.requirement_id",
                f"unknown requirement {requirement_id!r}",
            )
            continue
        compatible = scenario.get("compatible_environments", [])
        for environment in compatible:
            if environment not in ENVIRONMENT_IDS:
                _issue(
                    issues,
                    "unknown_environment",
                    f"{base}.compatible_environments",
                    f"unknown environment {environment!r}",
                )
        applicable = set(
            normative_by_id[requirement_id].get("applicable_environments", [])
        )
        if not applicable.intersection(compatible):
            _issue(
                issues,
                "demo_scenario_without_compatible_environment",
                base,
                f"{scenario.get('scenario_id')!r} has no environment compatible with {requirement_id}",
            )
        scenarios_by_requirement[requirement_id].append(scenario)

    for collection_name, collection in (
        ("requirements", requirements),
        ("acceptance_tests", acceptance_tests),
    ):
        for index, item in enumerate(collection):
            if not item.get("demo_required", False):
                continue
            required_environments = set(item.get("applicable_environments", []))
            compatible = any(
                required_environments
                & set(scenario.get("compatible_environments", []))
                for scenario in scenarios_by_requirement.get(item.get("id"), [])
            )
            if not compatible:
                _issue(
                    issues,
                    "demo_scenario_without_compatible_environment",
                    f"$.{collection_name}[{index}]",
                    f"{item.get('id')} requires a demo scenario with a compatible environment",
                )

    waivers_by_requirement: dict[str, list[dict[str, Any]]] = defaultdict(list)
    reviewed_fields = {
        "reason",
        "reviewed_by",
        "reviewed_at",
        "review_reference",
    }
    for index, waiver in enumerate(implementation.get("waivers", [])):
        base = f"$.implementation.waivers[{index}]"
        requirement_id = waiver.get("requirement_id")
        if requirement_id not in normative_by_id:
            _issue(
                issues,
                "unknown_requirement_reference",
                f"{base}.requirement_id",
                f"unknown requirement {requirement_id!r}",
            )
            continue
        if not all(isinstance(waiver.get(field), str) and waiver[field].strip() for field in reviewed_fields):
            _issue(
                issues,
                "invalid_waiver",
                base,
                "waiver requires reason, reviewed_by, reviewed_at, and review_reference",
            )
        waivers_by_requirement[requirement_id].append(waiver)

    for collection_name, collection in (
        ("requirements", requirements),
        ("acceptance_tests", acceptance_tests),
    ):
        for index, item in enumerate(collection):
            if item.get("implementation_status") != "waived":
                continue
            reviewed = any(
                all(
                    isinstance(waiver.get(field), str) and waiver[field].strip()
                    for field in reviewed_fields
                )
                for waiver in waivers_by_requirement.get(item.get("id"), [])
            )
            if not reviewed:
                _issue(
                    issues,
                    "waived_without_reviewed_waiver",
                    f"$.{collection_name}[{index}]",
                    f"{item.get('id')} is waived without an explicit reviewed waiver",
                )

    for path, value in _walk_strings(implementation, "$.implementation"):
        if _invalid_repository_path(value):
            _issue(
                issues,
                "invalid_repository_path",
                path,
                f"absolute, parent-relative, or machine-specific path {value!r}",
            )
    return sorted(set(issues))


def ambiguity_count(registry: dict[str, Any]) -> int:
    return sum(
        len(item.get("ambiguities", []))
        for item in [*registry.get("requirements", []), *registry.get("acceptance_tests", [])]
    )


def render_coverage(registry: dict[str, Any]) -> str:
    """Render the human-readable coverage report deterministically."""

    requirements = registry.get("requirements", [])
    acceptance_tests = registry.get("acceptance_tests", [])
    obligations = registry.get("test_obligations", [])
    grouped: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: {"GRD": [], "AT": [], "TST": []}
    )
    for item in requirements:
        grouped[item["id"].split("-")[1]]["GRD"].append(item["id"])
    for item in acceptance_tests:
        grouped[item["id"].split("-")[1]]["AT"].append(item["id"])
    for item in obligations:
        grouped[item["id"].split("-")[2]]["TST"].append(item["id"])
    test_types = Counter(item["test_type"] for item in obligations)

    lines = [
        "# Guardian Production V1 Requirement Coverage",
        "",
        "Generated from `docs/requirements/requirements.yaml`.",
        "Normative source: `docs/spec/guardian-production-v1.md`.",
        "",
        "## Validation summary",
        "",
        f"- Normative requirements: **{len(requirements)}**",
        f"- Normative acceptance tests: **{len(acceptance_tests)}**",
        f"- Implementation test obligations: **{len(obligations)}**",
        f"- Structured ambiguities: **{ambiguity_count(registry)}**",
        "- Duplicate IDs: **0** (required for generation)",
        "- Invalid references: **0** (required for generation)",
        "- Dependency cycles: **0** (required for generation)",
        "",
        "## Test-obligation types",
        "",
        "| Type | Count |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| `{test_type}` | {count} |" for test_type, count in sorted(test_types.items())
    )
    lines.extend(
        [
            "",
            "## Family coverage",
            "",
            "| Family | GRD | AT | Test obligations | Normative IDs |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for family in sorted(grouped):
        values = grouped[family]
        normative_ids = sorted([*values["GRD"], *values["AT"]])
        lines.append(
            f"| `{family}` | {len(values['GRD'])} | {len(values['AT'])} | "
            f"{len(values['TST'])} | "
            + ", ".join(f"`{identifier}`" for identifier in normative_ids)
            + " |"
        )
    lines.extend(
        [
            "",
            "## Status",
            "",
            "All implementation items remain `not_started`; all test obligations remain `not_implemented`. Status may advance only with validator-approved passing evidence.",
            "",
            "This file is generated. Run `task requirements:render`; do not edit it independently.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_dependency_graph(registry: dict[str, Any]) -> str:
    """Render only valid normative dependency edges."""

    normative_items = [
        *registry.get("requirements", []),
        *registry.get("acceptance_tests", []),
    ]
    known = {item["id"] for item in normative_items}
    edges = sorted(
        (dependency, item["id"])
        for item in normative_items
        for dependency in item.get("dependencies", [])
        if dependency in known
    )
    nodes = sorted({node for edge in edges for node in edge})

    lines = [
        "# Guardian Production V1 Requirement Dependency Graph",
        "",
        "Generated from `docs/requirements/requirements.yaml`.",
        "Normative source: `docs/spec/guardian-production-v1.md`.",
        "",
        "Only valid `GRD-*` and `AT-*` dependency edges are rendered. Structured ambiguities are intentionally excluded from graph nodes.",
        "",
        "```mermaid",
        "flowchart LR",
    ]
    for node in nodes:
        mermaid_id = node.replace("-", "_")
        lines.append(f'  {mermaid_id}["{node}"]')
    for dependency, dependent in edges:
        lines.append(
            f"  {dependency.replace('-', '_')} --> {dependent.replace('-', '_')}"
        )
    lines.extend(
        [
            "```",
            "",
            "| Dependency | Dependent |",
            "| --- | --- |",
        ]
    )
    lines.extend(f"| `{source}` | `{target}` |" for source, target in edges)
    lines.extend(
        [
            "",
            "This file is generated. Run `task requirements:render`; do not edit it independently.",
        ]
    )
    return "\n".join(lines) + "\n"


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as error:
        raise SystemExit("PyYAML is required; run through uv with the dev dependencies") from error
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a YAML mapping")
    return data


def _load_test_results(results_path: Path) -> list[dict[str, Any]]:
    """Load result records in deterministic path order."""

    if not results_path.exists():
        return []
    paths = [results_path] if results_path.is_file() else sorted(results_path.rglob("*.json"))
    results: list[dict[str, Any]] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            results.append({"__load_error__": f"{path.as_posix()}: {error}"})
            continue
        records = data if isinstance(data, list) else [data]
        for record in records:
            if isinstance(record, dict):
                results.append(record)
            else:
                results.append(
                    {"__load_error__": f"{path.as_posix()}: result must be a JSON object"}
                )
    return results


def _summary(registry: dict[str, Any], issues: list[ValidationIssue]) -> list[str]:
    duplicate_count = sum(issue.code == "duplicate_id" for issue in issues)
    invalid_reference_count = sum(
        issue.code
        in {
            "unknown_requirement_reference",
            "unknown_test_obligation_reference",
            "unknown_environment",
        }
        for issue in issues
    )
    cycle_count = sum(issue.code == "dependency_cycle" for issue in issues)
    return [
        f"normative_requirements: {len(registry.get('requirements', []))}",
        f"acceptance_tests: {len(registry.get('acceptance_tests', []))}",
        f"test_obligations: {len(registry.get('test_obligations', []))}",
        f"unresolved_ambiguities: {ambiguity_count(registry)}",
        f"duplicate_ids: {duplicate_count}",
        f"invalid_references: {invalid_reference_count}",
        f"dependency_cycles: {cycle_count}",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "render", "check"))
    parser.add_argument(
        "registry",
        nargs="?",
        default="docs/requirements/requirements.yaml",
    )
    parser.add_argument(
        "--implementation",
        default="docs/requirements/implementation.yaml",
        help="repository-relative implementation metadata manifest",
    )
    parser.add_argument(
        "--results",
        default="test-results/requirements",
        help="repository-relative JSON result file or directory",
    )
    arguments = parser.parse_args(argv)
    registry_path = Path(arguments.registry)
    registry = _load_yaml(registry_path)
    issues = validate_registry(registry)
    if arguments.command == "check":
        implementation_path = Path(arguments.implementation)
        if implementation_path.exists():
            implementation = _load_yaml(implementation_path)
        else:
            implementation = {}
            _issue(
                issues,
                "implementation_metadata_missing",
                "$.implementation",
                f"implementation metadata {implementation_path.as_posix()!r} does not exist",
            )
        issues.extend(
            validate_traceability(
                registry,
                implementation,
                Path.cwd(),
                _load_test_results(Path(arguments.results)),
            )
        )
        issues = sorted(set(issues))
    for line in _summary(registry, issues):
        print(line)
    if issues:
        print("result: INVALID")
        for issue in issues:
            print(issue)
        return 1

    coverage = render_coverage(registry)
    graph = render_dependency_graph(registry)
    coverage_path = registry_path.with_name("coverage.md")
    graph_path = registry_path.with_name("dependency-graph.md")
    if arguments.command == "render":
        coverage_path.write_text(coverage, encoding="utf-8", newline="\n")
        graph_path.write_text(graph, encoding="utf-8", newline="\n")
    elif arguments.command == "check":
        stale = []
        if not coverage_path.exists() or coverage_path.read_text(encoding="utf-8") != coverage:
            stale.append(coverage_path.as_posix())
        if not graph_path.exists() or graph_path.read_text(encoding="utf-8") != graph:
            stale.append(graph_path.as_posix())
        if stale:
            print("result: INVALID")
            print("generated_artifacts_stale: " + ", ".join(stale))
            return 1
    print("result: VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
