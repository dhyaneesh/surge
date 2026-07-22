import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class RepositoryGovernanceTests(unittest.TestCase):
    def test_scoped_agent_markdown_fences_are_balanced(self) -> None:
        scoped_files = [
            ROOT / "policies/AGENTS.md",
            ROOT / "services/action-controller/AGENTS.md",
            ROOT / "services/keda-scaler/AGENTS.md",
            ROOT / "services/reasoner/AGENTS.md",
            ROOT / "testbeds/AGENTS.md",
        ]
        for path in scoped_files:
            with self.subTest(path=path.relative_to(ROOT)):
                fence_count = sum(
                    line.startswith("```")
                    for line in path.read_text(encoding="utf-8").splitlines()
                )
                self.assertEqual(0, fence_count % 2)

    def test_codeowners_covers_sensitive_paths(self) -> None:
        content = (ROOT / ".github/CODEOWNERS").read_text(encoding="utf-8")
        required = {
            "/.github/CODEOWNERS",
            "/.github/workflows/",
            "/AGENTS.md",
            "/Taskfile.yml",
            "/crds/",
            "/docs/superpowers/plans/",
            "/migrations/",
            "/proto/",
            "/testbeds/",
        }
        owned = {
            line.split()[0]
            for line in content.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertEqual(set(), required - owned)

    def test_taskfile_exposes_architecture_target(self) -> None:
        content = (ROOT / "Taskfile.yml").read_text(encoding="utf-8")
        self.assertIn("test:architecture:", content)
        self.assertIn(
            "python -m tools.verification_harness suite test:architecture tests/architecture",
            content,
        )
        self.assertIn("run --locked pytest tests/architecture", content)


if __name__ == "__main__":
    unittest.main()
