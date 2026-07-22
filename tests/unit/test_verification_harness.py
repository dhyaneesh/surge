from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "tools" / "verification-tools.yaml"

MANDATORY_TARGETS = {
    "bootstrap",
    "format",
    "format:check",
    "lint",
    "lint:online-boutique",
    "typecheck",
    "test:unit",
    "test:contract",
    "test:integration",
    "test:architecture",
    "test:testbeds-unit",
    "test:testbeds-contract",
    "test:policy",
    "test:replay",
    "test:replay-deterministic",
    "test:security",
    "test:reasoner",
    "test:keda-scaler",
    "test:action-controller",
    "test:requirements",
    "test:env",
    "test:matrix",
    "requirements:render",
    "requirements:check",
    "final",
}

EXPECTED_AGENT_FILES = {
    "agents.md",
    "policies/AGENTS.md",
    "services/action-controller/AGENTS.md",
    "services/keda-scaler/AGENTS.md",
    "services/reasoner/AGENTS.md",
    "testbeds/AGENTS.md",
}


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def applicable_agent_files() -> set[Path]:
    candidates = {
        ROOT / "agents.md",
        ROOT / "policies" / "AGENTS.md",
        ROOT / "testbeds" / "AGENTS.md",
    }
    candidates.update((ROOT / "services").glob("*/AGENTS.md"))
    return {
        path
        for path in candidates
        if path.is_file() and path.name.casefold() == "agents.md"
    }


def agent_task_references(paths: set[Path]) -> set[str]:
    references: set[str] = set()
    for path in paths:
        references.update(
            re.findall(r"(?m)^task\s+([a-z][a-z0-9:-]*)(?:\s|$)", path.read_text())
        )
    return references


def declared_dev_dependencies() -> set[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = set()
    for requirement in project["dependency-groups"]["dev"]:
        names.add(re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0].casefold())
    return names


def test_all_applicable_agent_instructions_are_discovered() -> None:
    discovered = {str(path.relative_to(ROOT)) for path in applicable_agent_files()}
    assert discovered == EXPECTED_AGENT_FILES


def test_mandatory_and_referenced_targets_are_defined_by_taskfile() -> None:
    taskfile_targets = set(load_yaml(ROOT / "Taskfile.yml")["tasks"])
    referenced = agent_task_references(applicable_agent_files())
    assert MANDATORY_TARGETS | referenced <= taskfile_targets


def test_current_and_referenced_targets_are_represented_in_manifest() -> None:
    taskfile_targets = set(load_yaml(ROOT / "Taskfile.yml")["tasks"])
    referenced = agent_task_references(applicable_agent_files())
    manifest_targets = set(load_yaml(MANIFEST_PATH)["targets"])
    assert MANDATORY_TARGETS | taskfile_targets | referenced <= manifest_targets


def test_manifest_pins_supported_platform_and_bootstrap_tools() -> None:
    manifest = load_yaml(MANIFEST_PATH)
    assert manifest["platform"] == {"os": "linux", "architecture": "amd64"}
    assert manifest["host_prerequisites"] == ["bash", "curl", "tar"]
    assert manifest["sha256_utilities"] == {
        "required": 1,
        "any_of": [
            {"command": "sha256sum", "arguments": []},
            {"command": "shasum", "arguments": ["-a", "256"]},
        ],
    }
    assert manifest["tools"] == {
        "task": {
            "version": "3.52.0",
            "url": "https://github.com/go-task/task/releases/download/v3.52.0/task_linux_amd64.tar.gz",
            "sha256": "02c679ffae53dca791804847d78b31731615894e292948397c971c87ac9e95bd",
        },
        "uv": {
            "version": "0.11.31",
            "url": "https://github.com/astral-sh/uv/releases/download/0.11.31/uv-x86_64-unknown-linux-gnu.tar.gz",
            "sha256": "8cc1cd82d434ec565376f98bd938d4b715b5791a80ff2d3aa78821cf85091b4b",
        },
    }


def test_manifest_targets_declare_dependencies_and_capabilities() -> None:
    manifest = load_yaml(MANIFEST_PATH)
    targets = manifest["targets"]
    active_dependencies = set(manifest["tools"]) | set(manifest["python_commands"])
    capabilities = manifest["capabilities"]
    for name, target in targets.items():
        assert isinstance(target["dependencies"], list), name
        assert isinstance(target["capabilities"], list) and target["capabilities"], name
        assert set(target["dependencies"]) <= active_dependencies, name
        assert set(target["capabilities"]) <= set(capabilities), name

    assert {
        name for name, value in capabilities.items() if value["status"] == "active"
    } == {
        "python",
        "requirements",
        "tooling",
    }
    assert {
        name for name, value in capabilities.items() if value["status"] == "baseline"
    } == {"buf", "environment", "go", "kubernetes", "node", "policy"}


def test_tools_directory_is_ignored() -> None:
    patterns = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".tools/" in patterns


def test_manifest_python_commands_have_explicit_dev_dependencies() -> None:
    manifest = load_yaml(MANIFEST_PATH)
    packages = set(manifest["python_commands"].values())
    packages.update(manifest["python_libraries"].values())
    declared = declared_dev_dependencies()
    assert packages <= declared
    assert {"pyright", "types-pyyaml"} <= declared
