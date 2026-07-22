from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any

import yaml


PREREQUISITE_EXIT = 2
BASELINE_EXIT = 3
MANIFEST_EXIT = 4


def repo_root() -> Path:
    override = os.environ.get("VERIFICATION_REPO_ROOT")
    return Path(override).resolve() if override else Path.cwd().resolve()


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return value


def manifest(root: Path) -> dict[str, Any]:
    return load_yaml_mapping(root / "tools/verification-tools.yaml")


def fail(category: str, target: str, message: str) -> None:
    print(f"[{category}] {target}: {message}", file=sys.stderr)


def target_config(data: dict[str, Any], target: str) -> dict[str, Any] | None:
    targets = data.get("targets")
    value = targets.get(target) if isinstance(targets, dict) else None
    return value if isinstance(value, dict) else None


def tool_version(output: str) -> str | None:
    match = re.search(r"(?<![0-9])v?(\d+\.\d+\.\d+)(?![0-9])", output)
    return match.group(1) if match else None


def run_version(command: list[str], root: Path) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def preflight_target(root: Path, data: dict[str, Any], target: str) -> int:
    config = target_config(data, target)
    if config is None:
        fail("prerequisite", target, "target is not declared in the manifest")
        return PREREQUISITE_EXIT
    dependencies = config.get("dependencies")
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) for item in dependencies
    ):
        fail("prerequisite", target, "dependencies are not configured")
        return PREREQUISITE_EXIT

    tools = data.get("tools", {})
    python_commands = data.get("python_commands", {})
    uv = root / ".tools/bin/uv"
    for dependency in dependencies:
        if dependency in tools:
            executable = root / ".tools/bin" / dependency
            if not executable.is_file() or not os.access(executable, os.X_OK):
                fail("prerequisite", target, f"missing .tools/bin/{dependency}")
                return PREREQUISITE_EXIT
            expected = tools[dependency].get("version")
            ok, output = run_version([str(executable), "--version"], root)
            actual = tool_version(output)
            if not ok or actual != str(expected):
                fail(
                    "prerequisite",
                    target,
                    f"{dependency} version {actual or 'unknown'} does not match {expected}",
                )
                return PREREQUISITE_EXIT
        elif dependency in python_commands:
            if not uv.is_file() or not os.access(uv, os.X_OK):
                fail("prerequisite", target, "missing .tools/bin/uv")
                return PREREQUISITE_EXIT
            command = str(python_commands[dependency])
            ok, output = run_version(
                [str(uv), "run", "--locked", "--no-sync", command, "--version"],
                root,
            )
            if not ok:
                fail("prerequisite", target, f"Python command {command} is unavailable")
                return PREREQUISITE_EXIT
    return 0


def capability_status(data: dict[str, Any], target: str) -> int:
    config = target_config(data, target)
    if config is None:
        return PREREQUISITE_EXIT
    capabilities = data.get("capabilities", {})
    for name in config.get("capabilities", []):
        capability = capabilities.get(name, {})
        if capability.get("availability") == "baseline":
            fail(
                "baseline",
                target,
                str(capability.get("reason", "capability unavailable")),
            )
            return BASELINE_EXIT
    return 0


def preflight(root: Path, data: dict[str, Any], target: str) -> int:
    status = preflight_target(root, data, target)
    return status or capability_status(data, target)


def aggregate(root: Path, data: dict[str, Any], target: str) -> int:
    config = target_config(data, target)
    if config is None:
        return preflight(root, data, target)
    children = config.get("children")
    if not isinstance(children, list) or not children:
        fail("prerequisite", target, "aggregate children are not configured")
        return PREREQUISITE_EXIT
    ordered_targets = [target]
    seen = {target}
    invalid_child = False
    index = 0
    while index < len(ordered_targets):
        current = ordered_targets[index]
        index += 1
        current_config = target_config(data, current)
        if current_config is None:
            continue
        for child in current_config.get("children", []):
            if isinstance(child, str) and child not in seen:
                seen.add(child)
                ordered_targets.append(child)
            elif not isinstance(child, str):
                invalid_child = True

    prerequisite_failed = invalid_child
    baseline_failed = False
    for child in ordered_targets:
        if preflight_target(root, data, child):
            prerequisite_failed = True
        elif capability_status(data, child):
            baseline_failed = True
    if prerequisite_failed:
        return PREREQUISITE_EXIT
    return BASELINE_EXIT if baseline_failed else 0


def contains_tests(path: Path) -> bool:
    if path.is_file():
        return path.suffix == ".py" and (
            path.name.startswith("test_") or path.name.endswith("_test.py")
        )
    if not path.is_dir():
        return False
    return any(
        candidate.is_file()
        for pattern in ("test_*.py", "*_test.py")
        for candidate in path.rglob(pattern)
    )


def resolve_suite_path(root: Path, path: str) -> Path | None:
    candidate = Path(path)
    if candidate.is_absolute():
        return None
    resolved = (root / candidate).resolve()
    return resolved if resolved == root or root in resolved.parents else None


def suite(root: Path, data: dict[str, Any], target: str, paths: list[str]) -> int:
    status = preflight_target(root, data, target)
    if status:
        return status
    capability = capability_status(data, target)
    if capability:
        return capability
    resolved_paths = [resolve_suite_path(root, path) for path in paths]
    if any(path is None for path in resolved_paths):
        fail("prerequisite", target, "suite paths must be repository-relative")
        return PREREQUISITE_EXIT
    if not resolved_paths or not any(
        contains_tests(path) for path in resolved_paths if path is not None
    ):
        fail("baseline", target, "no tests are configured")
        return BASELINE_EXIT
    return 0


def command_dependencies(command: str) -> set[str]:
    if "\n" in command or "\r" in command:
        raise ValueError("uses unsupported multiline shell command")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError as error:
        raise ValueError(f"has invalid command syntax: {error}") from error
    unsupported = {"&&", "||", ";", "|", "&", "(", ")", ">", ">>", "<", "<<"}
    for word in words:
        if word in unsupported or "`" in word:
            raise ValueError(f"uses unsupported shell control operator: {word}")
    if not words:
        return set()
    first = Path(words[0]).name
    dependencies = (
        {first}
        if first not in {"python", "touch", "test", "verification-preflight.sh"}
        and not words[0].startswith("./")
        else set()
    )
    if first == "uv" and "run" in words:
        index = words.index("run") + 1
        while index < len(words) and words[index].startswith("-"):
            index += 1
        if index < len(words):
            nested = Path(words[index]).name
            if nested not in {"python"}:
                dependencies.add(nested)
    return dependencies


def manifest_check(root: Path, data: dict[str, Any]) -> int:
    try:
        taskfile = load_yaml_mapping(root / "Taskfile.yml")
    except ValueError as error:
        fail("prerequisite", "manifest-check", str(error))
        return MANIFEST_EXIT
    targets = data.get("targets")
    tasks = taskfile.get("tasks")
    if not isinstance(targets, dict) or not isinstance(tasks, dict):
        fail("prerequisite", "manifest-check", "targets or tasks are not mappings")
        return MANIFEST_EXIT
    errors: list[str] = []
    known = set(data.get("tools", {})) | set(data.get("python_commands", {}))
    raw_variables = taskfile.get("vars", {})
    variables = (
        {
            str(name): str(value).replace("{{.ROOT_DIR}}", str(root))
            for name, value in raw_variables.items()
        }
        if isinstance(raw_variables, dict)
        else {}
    )
    for name, task in tasks.items():
        config = target_config(data, str(name))
        if config is None:
            errors.append(f"Task target {name} is missing from the manifest")
            continue
        declared = set(config.get("dependencies", []))
        commands = task.get("cmds", []) if isinstance(task, dict) else []
        if not isinstance(commands, list) or not commands:
            errors.append(f"Task target {name} has no commands")
            continue
        used: set[str] = set()
        for command in commands:
            if isinstance(command, str):
                expanded = command
                for variable, value in variables.items():
                    expanded = expanded.replace(f"{{{{.{variable}}}}}", value)
                try:
                    used |= command_dependencies(expanded)
                except ValueError as error:
                    errors.append(f"Task target {name} {error}")
        missing = used - declared
        unknown = used - known
        if missing:
            errors.append(f"Task target {name} omits dependencies: {sorted(missing)}")
        if unknown:
            errors.append(
                f"Task target {name} uses undeclared commands: {sorted(unknown)}"
            )
    for name, config in targets.items():
        if name not in tasks:
            errors.append(f"manifest target {name} has no Task command")
        if not isinstance(config, dict) or not isinstance(
            config.get("dependencies"), list
        ):
            errors.append(f"manifest target {name} has no dependencies")
    for error in errors:
        fail("prerequisite", "manifest-check", error)
    return MANIFEST_EXIT if errors else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "aggregate"):
        child = subparsers.add_parser(command)
        child.add_argument("target")
    suite_parser = subparsers.add_parser("suite")
    suite_parser.add_argument("target")
    suite_parser.add_argument("paths", nargs="*")
    subparsers.add_parser("manifest-check")
    args = parser.parse_args(argv)
    root = repo_root()
    try:
        data = manifest(root)
    except ValueError as error:
        fail("prerequisite", args.command, str(error))
        return PREREQUISITE_EXIT
    if args.command == "preflight":
        return preflight(root, data, args.target)
    if args.command == "aggregate":
        return aggregate(root, data, args.target)
    if args.command == "suite":
        return suite(root, data, args.target, args.paths)
    return manifest_check(root, data)


if __name__ == "__main__":
    raise SystemExit(main())
