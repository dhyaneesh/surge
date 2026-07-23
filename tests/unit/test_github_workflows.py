from __future__ import annotations

from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

PR_GATES = [
    "format:check",
    "lint",
    "typecheck",
    "test:unit",
    "test:contract",
    "test:architecture",
    "test:integration",
    "test:security",
    "test:replay",
    "requirements:check",
]


def read_workflow(name: str) -> tuple[dict[str, object], str]:
    path = WORKFLOWS / name
    assert path.is_file(), f"missing workflow: {path.relative_to(ROOT)}"
    text = path.read_text(encoding="utf-8")
    return yaml.load(text, Loader=yaml.BaseLoader), text


def task_invocations(text: str) -> list[str]:
    return re.findall(r"\.tools/bin/task ([a-z][a-z0-9:-]*)", text)


def test_pull_request_workflow_runs_exactly_the_required_non_kind_gates() -> None:
    workflow, text = read_workflow("pull-request.yml")

    assert workflow["on"] == {"pull_request": ""}
    assert task_invocations(text) == PR_GATES
    assert "test:matrix" not in text
    assert "local:up" not in text


def test_pull_request_workflow_always_uploads_artifacts() -> None:
    _, text = read_workflow("pull-request.yml")

    assert "actions/upload-artifact@v4" in text
    assert "if: always()" in text
    assert "path: artifacts/" in text


def test_kind_matrix_workflow_is_scheduled_manual_and_cleans_up() -> None:
    workflow, text = read_workflow("kind-matrix.yml")

    triggers = workflow["on"]
    assert isinstance(triggers, dict)
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert "retain_cluster" in text
    assert ".tools/bin/task local:up" in text
    assert ".tools/bin/task test:matrix" in text
    assert "actions/upload-artifact@v4" in text
    assert "if: always()" in text
    assert "path: artifacts/" in text
    assert ".tools/bin/task local:down" in text
    assert "GUARDIAN_CLUSTER_RETAIN" in text
