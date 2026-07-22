import tempfile
import unittest
from pathlib import Path

from tools.architecture_rules import Violation, check_repository


def write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class ArchitectureBoundaryTests(unittest.TestCase):
    def scan(self, files: dict[str, str]) -> list[Violation]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative, content in files.items():
                write(root, relative, content)
            return check_repository(root)

    def assert_rule(
        self, files: dict[str, str], rule_id: str, path: str, line: int
    ) -> Violation:
        matches = [item for item in self.scan(files) if item.rule_id == rule_id]
        self.assertEqual(1, len(matches), matches)
        violation = matches[0]
        self.assertEqual(Path(path), violation.path)
        self.assertEqual(line, violation.line)
        self.assertTrue(violation.remediation)
        return violation

    def assert_clean(self, files: dict[str, str]) -> None:
        self.assertEqual([], self.scan(files))

    def test_python_production_imports_testbeds(self) -> None:
        self.assert_rule(
            {"packages/domain/model.py": "\nfrom testbeds.fixtures import incident\n"},
            "ARCH-PROD-NO-TESTBEDS",
            "packages/domain/model.py",
            2,
        )

    def test_apps_and_services_cannot_import_testbeds(self) -> None:
        violations = self.scan(
            {
                "apps/api/main.py": "from testbeds.models import EnvironmentState\n",
                "services/worker/main.py": "import testbeds.adapters.base\n",
            }
        )
        matches = [
            item for item in violations if item.rule_id == "ARCH-PROD-NO-TESTBEDS"
        ]
        self.assertEqual(2, len(matches), matches)

    def test_typescript_and_go_production_imports_testbeds(self) -> None:
        violations = self.scan(
            {
                "apps/api/index.ts": "import { fixture } from '../../testbeds/fixture';\n",
                "services/worker/main.go": 'import "surge/testbeds/helpers"\n',
            }
        )
        self.assertEqual(
            ["ARCH-PROD-NO-TESTBEDS", "ARCH-PROD-NO-TESTBEDS"],
            sorted(item.rule_id for item in violations),
        )

    def test_testbed_words_in_python_comments_and_strings_are_clean(self) -> None:
        self.assert_clean(
            {
                "packages/domain/model.py": (
                    "# from testbeds.fixtures import incident\n"
                    "MESSAGE = 'from testbeds.fixtures import incident'\n"
                )
            }
        )

    def test_reasoner_imports_action_provider(self) -> None:
        self.assert_rule(
            {
                "services/reasoner/agent.py": (
                    "from services.action_controller.providers import RollbackProvider\n"
                )
            },
            "ARCH-REASONER-NO-ACTION-PROVIDER",
            "services/reasoner/agent.py",
            1,
        )

    def test_reasoner_imports_or_initializes_write_client(self) -> None:
        violations = self.scan(
            {
                "services/reasoner/direct.py": (
                    "from kubernetes.client import AppsV1Api\n"
                ),
                "services/reasoner/indirect.py": (
                    "import kubernetes.client as client\napi = client.CustomObjectsApi()\n"
                ),
            }
        )
        matches = [
            item
            for item in violations
            if item.rule_id == "ARCH-REASONER-NO-WRITE-CLIENT"
        ]
        self.assertEqual(2, len(matches), matches)
        self.assertEqual([1, 2], sorted(item.line for item in matches))

    def test_reasoner_read_only_kubernetes_protocol_is_clean(self) -> None:
        self.assert_clean(
            {
                "services/reasoner/read.py": (
                    "from packages.kubernetes_readonly import WorkloadReader\n"
                )
            }
        )

    def test_scaler_imports_model_client(self) -> None:
        self.assert_rule(
            {"services/keda-scaler/poll.py": "\nimport openai\n"},
            "ARCH-SCALER-NO-MODEL-CLIENT",
            "services/keda-scaler/poll.py",
            2,
        )

    def test_scaler_model_name_in_comment_is_clean(self) -> None:
        self.assert_clean(
            {"services/keda-scaler/poll.py": "# import openai is forbidden here\n"}
        )

    def test_production_service_imports_signoz_mcp_client(self) -> None:
        self.assert_rule(
            {"services/reasoner/diagnostics.py": "import signoz_mcp.client\n"},
            "ARCH-PROD-NO-MCP-CLIENT",
            "services/reasoner/diagnostics.py",
            1,
        )

    def test_scaler_imports_signoz_mcp_client(self) -> None:
        self.assert_rule(
            {"services/keda-scaler/poll.py": "from signoz_mcp import Client\n"},
            "ARCH-SCALER-NO-MCP-CLIENT",
            "services/keda-scaler/poll.py",
            1,
        )

    def test_scaler_inline_and_block_write_rbac(self) -> None:
        violations = self.scan(
            {
                "services/keda-scaler/deploy/role.yaml": (
                    'kind: Role\nrules:\n  - verbs: ["get", "bind"]\n'
                ),
                "deploy/keda-scaler/cluster-role.yaml": (
                    "kind: ClusterRole\nrules:\n  - verbs:\n      - get\n      - impersonate\n"
                ),
            }
        )
        matches = [
            item for item in violations if item.rule_id == "ARCH-SCALER-NO-WRITE-RBAC"
        ]
        self.assertEqual(2, len(matches), matches)

    def test_scaler_read_only_rbac_is_clean(self) -> None:
        self.assert_clean(
            {
                "deploy/keda-scaler/role.yaml": (
                    'kind: Role\nrules:\n  - resources: ["configmaps"]\n'
                    '    verbs: ["get", "list", "watch"]\n'
                )
            }
        )

    def test_scaler_wildcard_rbac_is_rejected(self) -> None:
        self.assert_rule(
            {
                "deploy/keda-scaler/role.yaml": (
                    'kind: Role\nrules:\n  - resources: ["*"]\n    verbs: ["*"]\n'
                )
            },
            "ARCH-SCALER-NO-WRITE-RBAC",
            "deploy/keda-scaler/role.yaml",
            4,
        )

    def test_policy_demo_name_is_rejected(self) -> None:
        self.assert_rule(
            {"policies/action.rego": '\nblocked_service := "checkoutservice"\n'},
            "ARCH-POLICY-NO-DEMO-NAMES",
            "policies/action.rego",
            2,
        )

    def test_policy_comment_and_testbed_configuration_are_clean(self) -> None:
        self.assert_clean(
            {
                "policies/action.rego": "# checkoutservice is a testbed example\nallow := false\n",
                "testbeds/environments/demo.yaml": "service: checkoutservice\n",
            }
        )

    def test_mutation_provider_implementation_outside_controller_is_rejected(
        self,
    ) -> None:
        self.assert_rule(
            {
                "services/reasoner/provider.py": (
                    "class GitOpsRollbackProvider:\n    pass\n"
                )
            },
            "ARCH-MUTATION-PROVIDER-PLACEMENT",
            "services/reasoner/provider.py",
            1,
        )

    def test_controller_provider_and_shared_protocol_are_clean(self) -> None:
        self.assert_clean(
            {
                "services/action-controller/providers/rollback.py": (
                    "class RollbackProvider:\n    pass\n"
                ),
                "packages/provider-sdk/interfaces.py": (
                    "class RollbackProvider:\n    pass\n"
                ),
            }
        )

    def test_provider_test_double_is_clean(self) -> None:
        self.assert_clean(
            {"tests/unit/fake_provider.py": "class ScaleProvider:\n    pass\n"}
        )

    def test_multiple_clean_files_remain_clean(self) -> None:
        self.assert_clean(
            {
                "apps/api/main.py": "from packages.domain import Incident\n",
                "services/reasoner/read.py": "from packages.evidence import Reader\n",
                "services/keda-scaler/poll.ts": "import { gateway } from './gateway';\n",
            }
        )

    def test_repository_satisfies_architecture_boundaries(self) -> None:
        root = Path(__file__).resolve().parents[2]
        self.assertEqual([], check_repository(root))


if __name__ == "__main__":
    unittest.main()
