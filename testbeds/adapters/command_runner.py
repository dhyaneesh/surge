"""Restricted subprocess boundary for disposable testbed adapters."""

import asyncio
import os
import re
import subprocess
import time
from urllib.parse import urlsplit
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Sequence


class CommandRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


_SECRET_PATTERNS = (
    re.compile(r"(?i)((?:https?|all)_proxy\s*=\s*)(\S+)"),
    re.compile(r"(?i)(proxy(?:\s+url)?\s+)(https?://\S+)"),
    re.compile(r"(?i)https?://[^/\s:@]+:[^@\s]+@\S+"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)(\S+)"),
    re.compile(r"(?i)((?:password|client_secret)\s*[:=]\s*[\"']?)([^\s\"']+)"),
    re.compile(r"(?i)((?:token|api_key|secret)\s*=\s*)(\S+)"),
)


def redact(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            marker = (
                "[REDACTED_PROXY_URL]"
                if "proxy" in pattern.pattern.lower()
                else "[REDACTED]"
            )
            value = pattern.sub(lambda match: match.group(1) + marker, value)
        else:
            value = pattern.sub("[REDACTED_PROXY_URL]", value)
    return value


class AllowlistedCommandRunner:
    _ENVIRONMENT_ALLOWLIST = frozenset(
        {
            "PATH",
            "HOME",
            "KUBECONFIG",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "REQUESTS_CA_BUNDLE",
            "CURL_CA_BUNDLE",
        }
    )
    _SUBCOMMANDS = {
        "git": frozenset({"clone", "checkout", "rev-parse"}),
        "helm": frozenset({"upgrade", "get", "uninstall", "version"}),
        "kubectl": frozenset({"apply", "delete", "get", "describe", "logs", "rollout", "wait", "patch", "scale", "set"}),
    }
    _FORBIDDEN_TOKENS = frozenset({";", "&&", "||", "|", ">", ">>", "<", "`", "$()"})

    def __init__(self, *, base_environment: dict[str, str] | None = None):
        source = dict(os.environ if base_environment is None else base_environment)
        path = source.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        self._environment = {
            key: source[key]
            for key in self._ENVIRONMENT_ALLOWLIST
            if source.get(key)
        }
        self._environment["PATH"] = path
        self._environment.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
        bypasses = self._merge_no_proxy(source)
        joined = ",".join(bypasses)
        self._environment["NO_PROXY"] = joined
        self._environment["no_proxy"] = joined

    @classmethod
    def _merge_no_proxy(cls, source: dict[str, str]) -> list[str]:
        entries: list[str] = []

        def add(value: str) -> None:
            value = value.strip()
            if value and value not in entries:
                entries.append(value)

        for name in ("NO_PROXY", "no_proxy"):
            for item in source.get(name, "").split(","):
                add(item)
        for required in ("127.0.0.1", "localhost", "::1"):
            add(required)
        for hostname in cls._kubernetes_api_hostnames(source):
            add(hostname)
        service_host = source.get("KUBERNETES_SERVICE_HOST", "")
        if service_host:
            add(service_host)
        return entries

    @staticmethod
    def _kubernetes_api_hostnames(source: dict[str, str]) -> tuple[str, ...]:
        home = source.get("HOME")
        configured = source.get("KUBECONFIG")
        if configured:
            paths = [Path(item).expanduser() for item in configured.split(os.pathsep) if item]
        elif home:
            paths = [Path(home) / ".kube" / "config"]
        else:
            paths = []
        hostnames: list[str] = []
        server_pattern = re.compile(
            r'^\s*server\s*:\s*["\']?([^\s"\']+)', re.MULTILINE
        )
        for path in paths:
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            for match in server_pattern.finditer(content):
                hostname = urlsplit(match.group(1)).hostname
                if hostname and hostname not in hostnames:
                    hostnames.append(hostname)
        return tuple(hostnames)

    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout: timedelta,
        cwd: Path | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        if isinstance(argv, (str, bytes)):
            raise TypeError("commands must be argv arrays")
        command = tuple(argv)
        self._validate(command)
        if timeout.total_seconds() <= 0:
            raise ValueError("timeout must be positive")
        started = time.monotonic()

        def invoke():
            return subprocess.run(
                command,
                shell=False,
                cwd=cwd,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout.total_seconds(),
                env=dict(self._environment),
                check=False,
            )

        completed = await asyncio.to_thread(invoke)
        result = CommandResult(
            command,
            completed.returncode,
            completed.stdout,
            completed.stderr,
            time.monotonic() - started,
        )
        if result.returncode:
            raise RuntimeError(
                f"command failed ({result.returncode}): {redact(result.stderr)}"
            )
        return result

    def _validate(self, argv: tuple[str, ...]) -> None:
        if len(argv) < 2 or argv[0] not in self._SUBCOMMANDS:
            raise CommandRejected("executable is not allowlisted")
        if argv[1] not in self._SUBCOMMANDS[argv[0]]:
            raise CommandRejected("subcommand is not allowlisted")
        if argv[0] == "kubectl" and argv[1] in {"exec", "run", "cp", "port-forward"}:
            raise CommandRejected("interactive/arbitrary execution is prohibited")
        for token in argv:
            if token in self._FORBIDDEN_TOKENS or "\n" in token or "\x00" in token:
                raise CommandRejected("shell syntax is prohibited")
