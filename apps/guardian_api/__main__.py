"""Command-line entry point for the authenticated local Guardian API."""

from __future__ import annotations

import argparse
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from threading import Event, Thread

from apps.guardian_api.http import (
    DEFAULT_HOST,
    DEFAULT_MAX_REQUEST_BODY,
    DEFAULT_PORT,
    GuardianHTTPServer,
    create_server,
    load_token_tenants,
)


@dataclass(frozen=True)
class RuntimeConfig:
    """Validated local runtime settings without credential serialization."""

    token_tenants: Mapping[str, str] = field(repr=False)
    host: str
    port: int
    max_request_body: int
    allow_non_loopback: bool


def _integer_setting(value: str, *, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {name}") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"invalid {name}")
    return parsed


def _boolean_setting(value: str, *, name: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"invalid {name} opt-in")


def parse_runtime_config(
    environ: Mapping[str, str], argv: Sequence[str] | None = None
) -> RuntimeConfig:
    """Resolve environment and CLI settings while keeping tokens opaque."""

    parser = argparse.ArgumentParser(description="Run the local Guardian HTTP API")
    parser.add_argument("--host")
    parser.add_argument("--port")
    parser.add_argument("--max-request-body")
    parser.add_argument("--allow-non-loopback", action="store_true")
    arguments = parser.parse_args(argv)

    host = arguments.host or environ.get("GUARDIAN_HOST", DEFAULT_HOST)
    port = _integer_setting(
        arguments.port or environ.get("GUARDIAN_PORT", str(DEFAULT_PORT)),
        name="port",
        minimum=0,
        maximum=65535,
    )
    max_request_body = _integer_setting(
        arguments.max_request_body
        or environ.get("GUARDIAN_MAX_REQUEST_BODY", str(DEFAULT_MAX_REQUEST_BODY)),
        name="request body limit",
        minimum=1,
        maximum=64 * 1024 * 1024,
    )
    environment_opt_in = _boolean_setting(
        environ.get("GUARDIAN_ALLOW_NON_LOOPBACK", "false"),
        name="non-loopback",
    )
    return RuntimeConfig(
        token_tenants=load_token_tenants(environ.get("GUARDIAN_LOCAL_TOKENS_JSON")),
        host=host,
        port=port,
        max_request_body=max_request_body,
        allow_non_loopback=arguments.allow_non_loopback or environment_opt_in,
    )


def _readiness_url(host: str, port: int) -> str:
    display_host = f"[{host}]" if ":" in host else host
    return f"http://{display_host}:{port}/health"


def _write_readiness(message: str) -> None:
    print(message, flush=True)


def run_runtime(
    config: RuntimeConfig,
    *,
    server_factory: Callable[..., GuardianHTTPServer] = create_server,
    signal_installer: Callable[..., object] = signal.signal,
    thread_factory: Callable[..., Thread] = Thread,
    readiness_writer: Callable[[str], None] = _write_readiness,
) -> int:
    """Start and stop one runtime while cleaning every partial startup state."""

    server: GuardianHTTPServer | None = None
    thread: Thread | None = None
    thread_started = False
    stopped = Event()
    try:
        server = server_factory(
            token_tenants=config.token_tenants,
            host=config.host,
            port=config.port,
            max_request_body=config.max_request_body,
            allow_non_loopback=config.allow_non_loopback,
        )

        def request_stop(_signal_number: int, _frame: object) -> None:
            stopped.set()

        signal_installer(signal.SIGTERM, request_stop)
        thread = thread_factory(
            target=server.serve_forever,
            name="guardian-http",
            daemon=False,
        )
        thread.start()
        thread_started = True
        bound_host, bound_port = server.listening_address
        readiness_writer(_readiness_url(bound_host, bound_port))
        try:
            while not stopped.wait(0.25):
                pass
        except KeyboardInterrupt:
            stopped.set()
        return 0
    finally:
        stopped.set()
        if server is not None:
            if thread_started:
                server.shutdown()
            server.server_close()
        if thread is not None and thread_started:
            thread.join()


def main(argv: Sequence[str] | None = None) -> int:
    """Run until SIGTERM or keyboard interruption, then close the listener."""

    config = parse_runtime_config(os.environ, argv)
    return run_runtime(config)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError):
        print("guardian local API startup failed", file=sys.stderr)
        raise SystemExit(2) from None
