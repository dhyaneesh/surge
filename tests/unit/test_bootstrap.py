from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).parents[2]
BOOTSTRAP = ROOT / "scripts/bootstrap.sh"


def executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def archive(path: Path, member: str, content: str) -> str:
    source = path.parent / member
    source.parent.mkdir(parents=True, exist_ok=True)
    executable(source, content)
    with tarfile.open(path, "w:gz") as bundle:
        bundle.add(source, arcname=member)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def bootstrap_repo(tmp_path: Path) -> dict[str, Path | dict[str, str]]:
    repo = tmp_path / "repo"
    artifacts = tmp_path / "artifacts"
    host_bin = tmp_path / "host-bin"
    (repo / "scripts").mkdir(parents=True)
    (repo / "tools").mkdir()
    artifacts.mkdir()
    host_bin.mkdir()

    log = tmp_path / "commands.log"
    task_sha = archive(
        artifacts / "task.tar.gz",
        "task",
        "#!/bin/sh\n[ \"${1:-}\" = --version ] && echo 'Task version: v3.52.0'\n",
    )
    uv_sha = archive(
        artifacts / "uv.tar.gz",
        "uv-x86_64-unknown-linux-gnu/uv",
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = --version ]; then echo 'uv 0.11.31'; exit 0; fi\n"
        f"printf 'uv %s\\n' \"$*\" >> {log}\n",
    )
    manifest = repo / "tools/verification-tools.yaml"
    manifest.write_text(
        f"""platform:
  os: linux
  architecture: amd64
host_prerequisites:
  - bash
  - curl
  - tar
sha256_utilities:
  required: 1
  any_of:
    - command: sha256sum
      arguments: []
    - command: shasum
      arguments: [-a, \"256\"]
tools:
  task:
    version: 3.52.0
    url: https://invalid.example/task.tar.gz
    sha256: {task_sha}
  uv:
    version: 0.11.31
    url: https://invalid.example/uv.tar.gz
    sha256: {uv_sha}
""",
        encoding="utf-8",
    )
    preflight = repo / "scripts/verification-preflight.sh"
    executable(preflight, f"#!/bin/sh\nprintf 'preflight %s\\n' \"$*\" >> {log}\n")

    required_commands = {
        "bash": Path("/bin/bash"),
        "curl": Path("/usr/bin/curl"),
        "tar": Path("/usr/bin/tar"),
        "gzip": Path("/usr/bin/gzip"),
        "sha256sum": Path("/usr/bin/sha256sum"),
        "awk": Path("/usr/bin/awk"),
        "sed": Path("/usr/bin/sed"),
        "mkdir": Path("/usr/bin/mkdir"),
        "mktemp": Path("/usr/bin/mktemp"),
        "chmod": Path("/usr/bin/chmod"),
        "mv": Path("/usr/bin/mv"),
        "rm": Path("/usr/bin/rm"),
        "dirname": Path("/usr/bin/dirname"),
        "uname": Path("/usr/bin/uname"),
    }
    for name, target in required_commands.items():
        (host_bin / name).symlink_to(target)

    env = {
        "PATH": str(host_bin),
        "BOOTSTRAP_TEST_MODE": "1",
        "BOOTSTRAP_TEST_REPO_ROOT": str(repo),
        "BOOTSTRAP_TEST_MANIFEST": str(manifest),
        "BOOTSTRAP_TEST_TOOLS_DIR": str(repo / ".tools"),
        "BOOTSTRAP_TEST_OS": "linux",
        "BOOTSTRAP_TEST_ARCH": "amd64",
        "BOOTSTRAP_TEST_ARTIFACT_BASE": str(artifacts),
    }
    return {
        "repo": repo,
        "artifacts": artifacts,
        "host_bin": host_bin,
        "log": log,
        "env": env,
    }


def run(
    case: dict[str, Path | dict[str, str]], **changes: str
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(case["env"])  # type: ignore[arg-type]
    env.update(changes)
    return subprocess.run(
        ["/bin/bash", str(BOOTSTRAP)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.mark.parametrize("os_name,arch", [("darwin", "amd64"), ("linux", "arm64")])
def test_rejects_unsupported_platform_before_download(
    bootstrap_repo: dict[str, Path | dict[str, str]], os_name: str, arch: str
) -> None:
    result = run(bootstrap_repo, BOOTSTRAP_TEST_OS=os_name, BOOTSTRAP_TEST_ARCH=arch)
    assert result.returncode != 0
    assert "[prerequisite]" in result.stderr
    assert not (bootstrap_repo["repo"] / ".tools").exists()  # type: ignore[operator]


@pytest.mark.parametrize("missing", ["bash", "curl", "tar", "sha256sum"])
def test_reports_each_missing_host_prerequisite_before_download(
    bootstrap_repo: dict[str, Path | dict[str, str]], missing: str
) -> None:
    host_bin = bootstrap_repo["host_bin"]
    assert isinstance(host_bin, Path)
    (host_bin / missing).unlink()
    result = run(bootstrap_repo)
    assert result.returncode != 0
    expected = "checksum utility" if missing == "sha256sum" else missing
    assert f"[prerequisite] missing host {expected}" in result.stderr
    assert not (bootstrap_repo["repo"] / ".tools").exists()  # type: ignore[operator]


def test_checksum_mismatch_leaves_no_executable(
    bootstrap_repo: dict[str, Path | dict[str, str]],
) -> None:
    repo = bootstrap_repo["repo"]
    assert isinstance(repo, Path)
    manifest = repo / "tools/verification-tools.yaml"
    manifest.write_text(
        re.sub(
            r"(task:\n(?:    .*\n){2}    sha256: )[0-9a-f]+",
            r"\1" + "f" * 64,
            manifest.read_text(),
        ),
        encoding="utf-8",
    )
    result = run(bootstrap_repo)
    assert result.returncode != 0
    assert "[prerequisite] checksum mismatch" in result.stderr
    assert not (repo / ".tools/bin/task").exists()


def test_installs_pinned_tools_runs_sync_and_preflight(
    bootstrap_repo: dict[str, Path | dict[str, str]],
) -> None:
    result = run(bootstrap_repo)
    repo = bootstrap_repo["repo"]
    assert isinstance(repo, Path)
    assert result.returncode == 0, result.stderr
    assert (repo / ".tools/bin/task").stat().st_mode & 0o111
    assert (repo / ".tools/bin/uv").stat().st_mode & 0o111
    log = bootstrap_repo["log"].read_text()  # type: ignore[union-attr]
    assert "uv sync --locked" in log
    assert "preflight manifest-check" in log
    assert ".tools/bin/task <target>" in result.stdout


def test_matching_tools_are_reused_and_wrong_versions_replaced(
    bootstrap_repo: dict[str, Path | dict[str, str]],
) -> None:
    assert run(bootstrap_repo).returncode == 0
    repo = bootstrap_repo["repo"]
    assert isinstance(repo, Path)
    task = repo / ".tools/bin/task"
    first_inode = task.stat().st_ino
    assert run(bootstrap_repo).returncode == 0
    assert task.stat().st_ino == first_inode

    executable(task, "#!/bin/sh\necho 'Task version: v0.0.1'\n")
    assert run(bootstrap_repo).returncode == 0
    assert "3.52.0" in subprocess.check_output([task, "--version"], text=True)
