import copy
import tempfile
import unittest
from pathlib import Path

from tools.requirements_registry import (
    render_coverage,
    render_dependency_graph,
    validate_traceability,
    validate_registry,
)


def valid_registry() -> dict:
    provenance = {
        "component_ownership": "inferred_from_service_responsibilities",
        "dependencies": "unresolved",
        "test_obligations": "implementation_recommendation",
        "environment_mappings": "explicit_environment_mapping",
        "evidence_artifacts": "implementation_recommendation",
    }
    requirement = {
        "id": "GRD-REA-001",
        "source": {
            "document": "docs/spec/guardian-production-v1.md",
            "section": "4.7.1 Deterministic hypothesis authority",
            "line": 339,
        },
        "summary": "Scores use a versioned deterministic implementation.",
        "component_owners": ["guardian-reasoner"],
        "dependencies": [],
        "test_obligations": ["TST-GRD-REA-001-UNIT"],
        "applicable_environments": ["otel-demo"],
        "evidence_artifacts": ["deterministic-scoring-record"],
        "provenance": provenance,
        "ambiguities": [
            {
                "field": "dependencies",
                "reason": "No explicit requirement-ID dependency is stated.",
            }
        ],
        "implementation_status": "not_started",
        "test_status": "not_implemented",
    }
    obligation = {
        "id": "TST-GRD-REA-001-UNIT",
        "test_type": "unit",
        "description": "Assert GRD-REA-001 deterministic scoring is versioned.",
        "applicable_requirements": ["GRD-REA-001"],
        "applicable_environments": ["otel-demo"],
        "expected_evidence": ["deterministic-scoring-record"],
        "status": "not_implemented",
        "evidence": [],
    }
    return {
        "schema_version": "guardian.requirements/v1",
        "source_document": "docs/spec/guardian-production-v1.md",
        "provenance_values": [
            "explicit_normative",
            "explicit_acceptance_test",
            "explicit_environment_mapping",
            "inferred_from_service_responsibilities",
            "inferred_prerequisite",
            "implementation_recommendation",
            "unresolved",
        ],
        "environments": [
            {"id": "otel-demo", "name": "OpenTelemetry Demo"},
            {"id": "aws-retail", "name": "AWS Containers Retail Sample"},
            {"id": "online-boutique", "name": "Online Boutique"},
            {"id": "argo-rollouts", "name": "Argo Rollouts Demo"},
            {"id": "keda-rabbitmq", "name": "KEDA RabbitMQ Sample"},
        ],
        "requirements": [requirement],
        "acceptance_tests": [],
        "test_obligations": [obligation],
    }


def issue_codes(registry: dict) -> set[str]:
    return {issue.code for issue in validate_registry(registry)}


class RegistryValidationTests(unittest.TestCase):
    def test_minimal_registry_is_valid(self) -> None:
        self.assertEqual([], validate_registry(valid_registry()))

    def test_rejects_unknown_requirement_reference(self) -> None:
        registry = valid_registry()
        registry["requirements"][0]["dependencies"] = ["GRD-REA-999"]
        self.assertIn("unknown_requirement_reference", issue_codes(registry))

    def test_rejects_unknown_test_obligation_reference(self) -> None:
        registry = valid_registry()
        registry["requirements"][0]["test_obligations"] = ["TST-UNKNOWN"]
        self.assertIn("unknown_test_obligation_reference", issue_codes(registry))

    def test_rejects_unknown_environment_id(self) -> None:
        registry = valid_registry()
        registry["requirements"][0]["applicable_environments"] = ["demo"]
        self.assertIn("unknown_environment", issue_codes(registry))

    def test_rejects_duplicate_ids(self) -> None:
        registry = valid_registry()
        registry["requirements"].append(copy.deepcopy(registry["requirements"][0]))
        self.assertIn("duplicate_id", issue_codes(registry))

    def test_rejects_dependency_cycle(self) -> None:
        registry = valid_registry()
        second = copy.deepcopy(registry["requirements"][0])
        second["id"] = "GRD-REA-002"
        second["dependencies"] = ["GRD-REA-001"]
        second["test_obligations"] = []
        second["ambiguities"].append(
            {"field": "test_obligations", "reason": "No test is assigned."}
        )
        registry["requirements"][0]["dependencies"] = ["GRD-REA-002"]
        registry["requirements"].append(second)
        self.assertIn("dependency_cycle", issue_codes(registry))

    def test_rejects_ambiguity_text_in_typed_array(self) -> None:
        registry = valid_registry()
        registry["requirements"][0]["dependencies"] = [
            "AMBIGUOUS: no dependency"
        ]
        self.assertIn("invalid_typed_array_value", issue_codes(registry))

    def test_rejects_absolute_parent_relative_and_machine_paths(self) -> None:
        for path in [
            "/mnt/c/Users/name/repo/spec.md",
            "../spec/guardian.md",
            "C:\\Users\\name\\spec.md",
        ]:
            with self.subTest(path=path):
                registry = valid_registry()
                registry["source_document"] = path
                self.assertIn("invalid_repository_path", issue_codes(registry))

    def test_rejects_requirement_without_test_or_test_ambiguity(self) -> None:
        registry = valid_registry()
        registry["requirements"][0]["test_obligations"] = []
        registry["requirements"][0]["ambiguities"] = []
        self.assertIn("missing_direct_test", issue_codes(registry))

    def test_rejects_unknown_provenance(self) -> None:
        registry = valid_registry()
        registry["requirements"][0]["provenance"]["dependencies"] = "guessed"
        self.assertIn("unknown_provenance", issue_codes(registry))

    def test_renderers_are_deterministic_and_exclude_ambiguity_nodes(self) -> None:
        registry = valid_registry()
        coverage = render_coverage(registry)
        graph = render_dependency_graph(registry)
        self.assertEqual(coverage, render_coverage(registry))
        self.assertEqual(graph, render_dependency_graph(registry))
        self.assertIn("GRD-REA-001", coverage)
        self.assertNotIn("AMBIGUOUS", graph)


def add_acceptance_test(registry: dict) -> None:
    acceptance = copy.deepcopy(registry["requirements"][0])
    acceptance.update(
        {
            "id": "AT-REA-001",
            "summary": "Given an ineligible hypothesis, it remains ineligible.",
            "given": "An ineligible hypothesis is ranked first.",
            "when": "Assessment is finalized.",
            "then": "It remains ineligible.",
            "dependencies": ["GRD-REA-001"],
            "test_obligations": ["TST-AT-REA-001-ACCEPTANCE"],
        }
    )
    acceptance["provenance"] = copy.deepcopy(acceptance["provenance"])
    acceptance["provenance"]["dependencies"] = "explicit_acceptance_test"
    registry["acceptance_tests"].append(acceptance)
    obligation = copy.deepcopy(registry["test_obligations"][0])
    obligation.update(
        {
            "id": "TST-AT-REA-001-ACCEPTANCE",
            "test_type": "acceptance",
            "description": "Execute AT-REA-001.",
            "applicable_requirements": ["AT-REA-001"],
        }
    )
    registry["test_obligations"].append(obligation)


def implementation_metadata(test_file: str, *obligation_ids: str) -> dict:
    return {
        "schema_version": "guardian.requirements-implementation/v1",
        "test_implementations": [
            {"test_obligation_id": identifier, "test_file": test_file}
            for identifier in obligation_ids
        ],
        "demo_scenarios": [],
        "waivers": [],
    }


class TraceabilityCheckerTests(unittest.TestCase):
    def scan(
        self,
        registry: dict,
        metadata: dict,
        files: dict[str, str] | None = None,
        results: list[dict] | None = None,
    ) -> set[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, content in (files or {}).items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            return {
                issue.code
                for issue in validate_traceability(
                    registry, metadata, root, results or []
                )
            }

    def test_fails_when_grd_has_no_linked_test_marker(self) -> None:
        codes = self.scan(valid_registry(), implementation_metadata("tests/grd.py"))
        self.assertIn("grd_without_linked_test", codes)

    def test_fails_when_at_has_no_implementation(self) -> None:
        registry = valid_registry()
        add_acceptance_test(registry)
        metadata = implementation_metadata(
            "tests/grd.py", "TST-GRD-REA-001-UNIT"
        )
        codes = self.scan(
            registry,
            metadata,
            {"tests/grd.py": "# TST-GRD-REA-001-UNIT\n"},
        )
        self.assertIn("at_without_implementation", codes)

    def test_fails_when_complete_has_no_passing_result_evidence(self) -> None:
        registry = valid_registry()
        registry["requirements"][0]["implementation_status"] = "implemented"
        registry["requirements"][0]["test_status"] = "passing"
        metadata = implementation_metadata(
            "tests/grd.py", "TST-GRD-REA-001-UNIT"
        )
        codes = self.scan(
            registry,
            metadata,
            {"tests/grd.py": "# TST-GRD-REA-001-UNIT\n"},
        )
        self.assertIn("complete_without_passing_evidence", codes)

    def test_accepts_complete_with_passing_result_evidence(self) -> None:
        registry = valid_registry()
        registry["requirements"][0]["implementation_status"] = "implemented"
        registry["requirements"][0]["test_status"] = "passing"
        metadata = implementation_metadata(
            "tests/grd.py", "TST-GRD-REA-001-UNIT"
        )
        results = [
            {
                "schema_version": "guardian.test-result/v1",
                "test_obligation_id": "TST-GRD-REA-001-UNIT",
                "status": "passed",
                "evidence": ["test-results/requirements/grd-rea-001.json"],
            }
        ]
        codes = self.scan(
            registry,
            metadata,
            {"tests/grd.py": "# TST-GRD-REA-001-UNIT\n"},
            results,
        )
        self.assertNotIn("complete_without_passing_evidence", codes)

    def test_fails_when_referenced_test_file_does_not_exist(self) -> None:
        metadata = implementation_metadata(
            "tests/missing.py", "TST-GRD-REA-001-UNIT"
        )
        self.assertIn(
            "referenced_test_file_missing",
            self.scan(valid_registry(), metadata),
        )

    def test_fails_when_test_file_lacks_obligation_marker(self) -> None:
        metadata = implementation_metadata(
            "tests/grd.py", "TST-GRD-REA-001-UNIT"
        )
        codes = self.scan(
            valid_registry(), metadata, {"tests/grd.py": "def test_grd(): pass\n"}
        )
        self.assertIn("test_marker_missing", codes)

    def test_fails_when_demo_scenario_has_no_compatible_environment(self) -> None:
        registry = valid_registry()
        metadata = implementation_metadata(
            "tests/grd.py", "TST-GRD-REA-001-UNIT"
        )
        metadata["demo_scenarios"] = [
            {
                "requirement_id": "GRD-REA-001",
                "scenario_id": "scenario-reasoner",
                "compatible_environments": ["aws-retail"],
            }
        ]
        codes = self.scan(
            registry,
            metadata,
            {"tests/grd.py": "# TST-GRD-REA-001-UNIT\n"},
        )
        self.assertIn("demo_scenario_without_compatible_environment", codes)

    def test_fails_when_waived_without_reviewed_waiver(self) -> None:
        registry = valid_registry()
        registry["requirements"][0]["implementation_status"] = "waived"
        metadata = implementation_metadata(
            "tests/grd.py", "TST-GRD-REA-001-UNIT"
        )
        codes = self.scan(
            registry,
            metadata,
            {"tests/grd.py": "# TST-GRD-REA-001-UNIT\n"},
        )
        self.assertIn("waived_without_reviewed_waiver", codes)

    def test_accepts_explicit_reviewed_waiver(self) -> None:
        registry = valid_registry()
        registry["requirements"][0]["implementation_status"] = "waived"
        metadata = implementation_metadata(
            "tests/grd.py", "TST-GRD-REA-001-UNIT"
        )
        metadata["waivers"] = [
            {
                "requirement_id": "GRD-REA-001",
                "reason": "Not applicable to the selected deployment profile.",
                "reviewed_by": "guardian-sre-reviewer",
                "reviewed_at": "2026-07-22",
                "review_reference": "docs/requirements/waivers/grd-rea-001.md",
            }
        ]
        codes = self.scan(
            registry,
            metadata,
            {"tests/grd.py": "# TST-GRD-REA-001-UNIT\n"},
        )
        self.assertNotIn("waived_without_reviewed_waiver", codes)


if __name__ == "__main__":
    unittest.main()
