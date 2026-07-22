from __future__ import annotations

from collections.abc import Iterable
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path, PurePosixPath

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


def applicable_agent_files(tracked_paths: Iterable[str] | None = None) -> set[Path]:
    if tracked_paths is None:
        output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        tracked_paths = output.decode("utf-8").split("\0")
    return {
        ROOT / path
        for path in tracked_paths
        if path and PurePosixPath(path).name.casefold() == "agents.md"
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


def test_agent_discovery_recognizes_an_arbitrary_nested_tracked_file() -> None:
    discovered = applicable_agent_files(
        ["apps/operator/docs/AGENTS.md", "packages/example/README.md"]
    )
    assert discovered == {ROOT / "apps/operator/docs/AGENTS.md"}


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

    assert {name: target["capabilities"] for name, target in targets.items()} == {
        name: [name] for name in MANDATORY_TARGETS
    }

    baseline_targets = {
        "test:action-controller",
        "test:env",
        "test:integration",
        "test:keda-scaler",
        "test:matrix",
        "test:policy",
        "test:reasoner",
        "test:replay",
        "test:replay-deterministic",
        "test:security",
    }
    assert {
        name
        for name, capability in capabilities.items()
        if capability["availability"] == "baseline"
    } == baseline_targets
    for name in baseline_targets:
        assert capabilities[name] == {
            "availability": "baseline",
            "reason": "no tests are configured",
        }

    for name in MANDATORY_TARGETS - baseline_targets:
        assert capabilities[name] == {"availability": "active"}


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


def write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\nset -eu\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def write_harness_repo(
    root: Path,
    *,
    uv_version: str | None = "0.11.31",
    capability: str = "active",
    children: list[str] | None = None,
) -> None:
    tools = root / "tools"
    tools.mkdir(parents=True)
    child_yaml = f"\n    children: {children!r}" if children is not None else ""
    (tools / "verification-tools.yaml").write_text(
        f"""schema_version: 1
tools:
  task:
    version: 3.52.0
  uv:
    version: 0.11.31
python_commands:
  pytest: pytest
python_libraries: {{}}
capabilities:
  check: {{availability: {capability}, reason: no tests are configured}}
  aggregate: {{availability: active}}
targets:
  check:
    dependencies: [uv, pytest]
    capabilities: [check]
  aggregate:
    dependencies: []
    capabilities: [aggregate]{child_yaml}
""",
        encoding="utf-8",
    )
    (root / "Taskfile.yml").write_text(
        """version: '3'
tasks:
  check:
    cmds: [.tools/bin/uv run --locked pytest tests/unit]
  aggregate:
    cmds: [{task: check}]
""",
        encoding="utf-8",
    )
    if uv_version is not None:
        write_executable(
            root / ".tools/bin/uv",
            f"""if [ "${{1:-}}" = "--version" ]; then echo 'uv {uv_version}'; exit 0; fi
if [ "${{1:-}}" = run ]; then
  shift
  [ "${{1:-}}" = --locked ] && shift
  echo 'pytest 9.0.0'
fi""",
        )
    write_executable(
        root / ".venv/bin/pytest", "[ \"${1:-}\" = --version ] && echo 'pytest 9.0.0'",
    )


def run_harness(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["VERIFICATION_REPO_ROOT"] = str(root)
    return subprocess.run(
        [sys.executable, "-m", "tools.verification_harness", *arguments],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_preflight_classifies_missing_local_uv_as_prerequisite(tmp_path: Path) -> None:
    write_harness_repo(tmp_path, uv_version=None)

    result = run_harness(tmp_path, "preflight", "check")

    assert result.returncode == 2
    assert "[prerequisite] check: missing .tools/bin/uv" in result.stderr


def test_shell_preflight_rejects_wrong_uv_before_python_child(tmp_path: Path) -> None:
    write_harness_repo(tmp_path, uv_version="0.0.1")
    sentinel = tmp_path / "sentinel"
    env = os.environ.copy()
    env["VERIFICATION_REPO_ROOT"] = str(tmp_path)
    env["VERIFICATION_SENTINEL"] = str(sentinel)

    result = subprocess.run(
        [
            "bash",
            "-c",
            '"$1" "$2" && touch "$3"',
            "preflight-test",
            str(ROOT / "scripts/verification-preflight.sh"),
            "check",
            str(sentinel),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "[prerequisite] check: uv version" in result.stderr
    assert not sentinel.exists()


def test_shell_preflight_invokes_python_without_syncing(tmp_path: Path) -> None:
    write_harness_repo(tmp_path)
    log = tmp_path / "uv.log"
    sentinel = tmp_path / "uv-synced"
    write_executable(
        tmp_path / ".tools/bin/uv",
        f"""printf '%s\\n' "$*" >> {log}
if [ "${{1:-}}" = --version ]; then echo 'uv 0.11.31'; exit 0; fi
case " $* " in
  *" --no-sync "*) ;;
  *) touch {sentinel} ;;
esac""",
    )
    env = os.environ.copy()
    env["VERIFICATION_REPO_ROOT"] = str(tmp_path)

    result = subprocess.run(
        [str(ROOT / "scripts/verification-preflight.sh"), "check"],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "run --locked --no-sync python -m tools.verification_harness preflight check" in (
        log.read_text(encoding="utf-8")
    )
    assert not sentinel.exists()


def test_suite_classifies_an_empty_or_absent_suite_as_baseline(tmp_path: Path) -> None:
    write_harness_repo(tmp_path)
    empty = tmp_path / "tests/empty"
    empty.mkdir(parents=True)

    for suite in ("tests/empty", "tests/absent"):
        result = run_harness(tmp_path, "suite", "check", suite)
        assert result.returncode == 3
        assert "[baseline] check: no tests are configured" in result.stderr


def test_suite_classifies_a_non_test_file_as_baseline(tmp_path: Path) -> None:
    write_harness_repo(tmp_path)
    readme = tmp_path / "tests/README.md"
    readme.parent.mkdir(parents=True)
    readme.write_text("suite notes", encoding="utf-8")

    result = run_harness(tmp_path, "suite", "check", "tests/README.md")

    assert result.returncode == 3
    assert "[baseline] check: no tests are configured" in result.stderr


def test_aggregate_preflight_checks_union_and_never_runs_children(tmp_path: Path) -> None:
    write_harness_repo(tmp_path, uv_version=None, children=["check"])
    sentinel = tmp_path / "child-ran"
    (tmp_path / "Taskfile.yml").write_text(
        f"version: '3'\ntasks:\n  check:\n    cmds: [touch {sentinel}]\n  aggregate:\n    cmds: [{{task: check}}]\n",
        encoding="utf-8",
    )

    result = run_harness(tmp_path, "aggregate", "aggregate")

    assert result.returncode == 2
    assert "[prerequisite] check: missing .tools/bin/uv" in result.stderr
    assert not sentinel.exists()


def test_aggregate_reports_child_baseline_without_running_children(tmp_path: Path) -> None:
    write_harness_repo(tmp_path, capability="baseline", children=["check"])

    result = run_harness(tmp_path, "aggregate", "aggregate")

    assert result.returncode == 3
    assert "[baseline] check: no tests are configured" in result.stderr


def test_valid_preflight_uses_uv_run_locked_for_python_commands(tmp_path: Path) -> None:
    write_harness_repo(tmp_path)
    log = tmp_path / "uv.log"
    uv = tmp_path / ".tools/bin/uv"
    write_executable(
        uv,
        f"""printf '%s\\n' "$*" >> {log}
if [ "${{1:-}}" = --version ]; then echo 'uv 0.11.31'; exit 0; fi
shift; [ "${{1:-}}" = --locked ] && shift
echo 'pytest 9.0.0'""",
    )

    result = run_harness(tmp_path, "preflight", "check")

    assert result.returncode == 0, result.stderr
    assert "run --locked --no-sync pytest --version" in log.read_text(encoding="utf-8")


def test_baseline_preflight_does_not_allow_uv_to_sync_before_classification(
    tmp_path: Path,
) -> None:
    write_harness_repo(tmp_path, capability="baseline")
    sentinel = tmp_path / "uv-synced"
    uv = tmp_path / ".tools/bin/uv"
    write_executable(
        uv,
        f"""if [ "${{1:-}}" = --version ]; then echo 'uv 0.11.31'; exit 0; fi
case " $* " in
  *" --no-sync "*) ;;
  *) touch {sentinel} ;;
esac
echo 'pytest 9.0.0'""",
    )

    result = run_harness(tmp_path, "preflight", "check")

    assert result.returncode == 3
    assert not sentinel.exists()


def test_manifest_check_requires_commands_to_be_covered(tmp_path: Path) -> None:
    write_harness_repo(tmp_path)

    result = run_harness(tmp_path, "manifest-check")

    assert result.returncode == 0, result.stderr


def test_manifest_check_rejects_an_undeclared_task_command(tmp_path: Path) -> None:
    write_harness_repo(tmp_path)
    taskfile = tmp_path / "Taskfile.yml"
    taskfile.write_text(
        taskfile.read_text(encoding="utf-8").replace(
            ".tools/bin/uv run --locked pytest", "mystery-checker"
        ),
        encoding="utf-8",
    )

    result = run_harness(tmp_path, "manifest-check")

    assert result.returncode == 4
    assert "uses undeclared commands: ['mystery-checker']" in result.stderr
