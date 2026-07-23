"""Command-line entry point for the authenticated local Guardian API."""

from __future__ import annotations

import argparse
import math
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


class GuardianServerRuntimeError(RuntimeError):
    """The serving thread exited unexpectedly or could not stop safely."""


DEFAULT_SERVING_STARTUP_TIMEOUT = 1.0


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
    serving_startup_timeout: float = DEFAULT_SERVING_STARTUP_TIMEOUT,
) -> int:
    """Start and stop one runtime while cleaning every partial startup state."""

    server: GuardianHTTPServer | None = None
    thread: Thread | None = None
    thread_started = False
    stopped = Event()
    serving_entered = Event()
    serving_failures: list[BaseException] = []
    previous_sigterm_handler: object | None = None
    sigterm_handler_installed = False
    try:
        if (
            not isinstance(serving_startup_timeout, int | float)
            or isinstance(serving_startup_timeout, bool)
            or not math.isfinite(serving_startup_timeout)
            or serving_startup_timeout < 1.0
        ):
            raise ValueError("serving startup timeout must be at least one second")
        server = server_factory(
            token_tenants=config.token_tenants,
            host=config.host,
            port=config.port,
            max_request_body=config.max_request_body,
            allow_non_loopback=config.allow_non_loopback,
        )

        def request_stop(_signal_number: int, _frame: object) -> None:
            stopped.set()

        previous_sigterm_handler = signal_installer(signal.SIGTERM, request_stop)
        sigterm_handler_installed = True

        def serve() -> None:
            serving_entered.set()
            try:
                server.serve_forever()
            except BaseException as error:
                serving_failures.append(error)
                stopped.set()
            else:
                if not stopped.is_set():
                    serving_failures.append(
                        GuardianServerRuntimeError(
                            "Guardian HTTP serving exited unexpectedly"
                        )
                    )
                    stopped.set()

        def raise_if_serving_failed() -> None:
            if serving_failures or (
                thread_started and thread is not None and not thread.is_alive()
            ):
                raise GuardianServerRuntimeError(
                    "Guardian HTTP serving failed"
                ) from None

        thread = thread_factory(
            target=serve,
            name="guardian-http",
            daemon=False,
        )
        thread.start()
        thread_started = True
        if not serving_entered.wait(timeout=serving_startup_timeout):
            raise GuardianServerRuntimeError(
                "Guardian HTTP serving did not start within bound"
            )
        thread.join(timeout=0.05)
        raise_if_serving_failed()
        bound_host, bound_port = server.listening_address
        readiness_writer(_readiness_url(bound_host, bound_port))
        raise_if_serving_failed()
        try:
            while not stopped.wait(0.25):
                raise_if_serving_failed()
        except KeyboardInterrupt:
            stopped.set()
        raise_if_serving_failed()
        return 0
    finally:
        stopped.set()
        try:
            if server is not None:
                if thread_started:
                    server.drain_and_close()
                else:
                    server.server_close()
            if thread is not None and thread_started and server is not None:
                thread.join(timeout=server.drain_timeout)
                if thread.is_alive():
                    raise GuardianServerRuntimeError(
                        "Guardian server thread did not stop within bound"
                    )
        finally:
            if sigterm_handler_installed:
                signal_installer(signal.SIGTERM, previous_sigterm_handler)


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
