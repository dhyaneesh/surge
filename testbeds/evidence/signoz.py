"""SigNoz baseline evidence queries and black-box OTLP export for testbeds."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping
from urllib import error, request

from testbeds.adapters.command_runner import redact
from testbeds.evidence.collector import HttpProbeRunner, ProbeResult

Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class SignozQueryResult:
    matched: bool
    observed_at: datetime
    provenance_ref: str
    values: Mapping[str, Any]
    diagnostics: str = ""


class SignozEvidenceClient:
    """Exports probe measurements via OTLP/HTTP JSON and queries them back."""

    def __init__(
        self,
        *,
        otlp_endpoint: str,
        query_endpoint: str,
        http_runner: HttpProbeRunner,
        clock: Clock | None = None,
        timeout: timedelta = timedelta(seconds=10),
    ) -> None:
        self._otlp = otlp_endpoint.rstrip("/")
        self._query = query_endpoint.rstrip("/")
        self._http = http_runner
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._timeout = timeout

    async def export_blackbox_probe(
        self,
        *,
        probe_url: str,
        identity: Mapping[str, Any],
    ) -> dict[str, Any]:
        observed_at = self._clock()
        try:
            probe = await self._http.probe(probe_url, timeout=self._timeout)
            available = 200 <= probe.status_code < 400
            latency_ms = float(probe.latency_ms)
        except Exception:
            available = False
            latency_ms = float(self._timeout.total_seconds() * 1000)
            probe = ProbeResult(status_code=0, latency_ms=latency_ms, body="")

        attributes = _identity_attributes(identity)
        payload = {
            "resourceMetrics": [
                {
                    "resource": {
                        "attributes": [
                            {"key": key, "value": {"stringValue": value}}
                            for key, value in attributes.items()
                        ]
                    },
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "guardian.testbed.endpoint.available",
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "asInt": "1" if available else "0",
                                                "timeUnixNano": str(
                                                    int(observed_at.timestamp() * 1e9)
                                                ),
                                            }
                                        ]
                                    },
                                },
                                {
                                    "name": "guardian.testbed.endpoint.latency_ms",
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "asDouble": latency_ms,
                                                "timeUnixNano": str(
                                                    int(observed_at.timestamp() * 1e9)
                                                ),
                                            }
                                        ]
                                    },
                                },
                            ]
                        }
                    ],
                }
            ]
        }
        await self._http.post_json(
            f"{self._otlp}/v1/metrics",
            payload,
            timeout=self._timeout,
            headers={"Content-Type": "application/json"},
        )
        return {
            "available": available,
            "latency_ms": latency_ms,
            "status_code": probe.status_code,
            "observed_at": observed_at.isoformat(),
            "attributes": attributes,
        }

    async def query_telemetry_arrival(
        self,
        *,
        identity: Mapping[str, Any],
        lookback: timedelta,
    ) -> SignozQueryResult:
        return await self._query_identity(
            identity=identity,
            lookback=lookback,
            provenance="signoz/telemetry-arrival",
        )

    async def query_service_identity(
        self,
        *,
        identity: Mapping[str, Any],
        lookback: timedelta,
    ) -> SignozQueryResult:
        return await self._query_identity(
            identity=identity,
            lookback=lookback,
            provenance="signoz/service-identity",
        )

    async def query_service_version(
        self,
        *,
        identity: Mapping[str, Any],
        lookback: timedelta,
    ) -> SignozQueryResult:
        return await self._query_identity(
            identity=identity,
            lookback=lookback,
            provenance="signoz/service-version",
        )

    async def _query_identity(
        self,
        *,
        identity: Mapping[str, Any],
        lookback: timedelta,
        provenance: str,
    ) -> SignozQueryResult:
        observed_at = self._clock()
        expected = _identity_attributes(identity)
        try:
            response = await self._http.probe(
                f"{self._query}/api/v1/telemetry/identity",
                timeout=self._timeout,
                headers={"Content-Type": "application/json"},
            )
            body = json.loads(response.body or "{}")
        except Exception as error:
            return SignozQueryResult(
                matched=False,
                observed_at=observed_at,
                provenance_ref=provenance,
                values={},
                diagnostics=redact(str(error)),
            )
        rows = body.get("data") or []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if all(str(row.get(key, "")) == value for key, value in expected.items()):
                return SignozQueryResult(
                    matched=True,
                    observed_at=observed_at,
                    provenance_ref=f"{provenance}/{lookback.total_seconds():.0f}s",
                    values=dict(row),
                )
        return SignozQueryResult(
            matched=False,
            observed_at=observed_at,
            provenance_ref=provenance,
            values={},
            diagnostics="identity attributes did not match",
        )


def _identity_attributes(identity: Mapping[str, Any]) -> dict[str, str]:
    return {
        "tenant.id": str(identity["tenant_id"]),
        "deployment.environment": str(identity["environment"]),
        "service.name": str(identity["service_name"]),
        "k8s.deployment.name": str(identity["workload_name"]),
        "service.version": str(identity.get("service_version") or ""),
    }


class UrllibHttpProbeRunner:
    """Standard-library HTTP probe/export used by real testbed runs."""

    async def probe(
        self,
        url: str,
        *,
        timeout: timedelta,
        headers: Mapping[str, str] | None = None,
    ) -> ProbeResult:
        return await self._request(
            url, method="GET", timeout=timeout, headers=headers, body=None
        )

    async def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: timedelta,
        headers: Mapping[str, str] | None = None,
    ) -> ProbeResult:
        request_headers = {"Content-Type": "application/json", **dict(headers or {})}
        return await self._request(
            url,
            method="POST",
            timeout=timeout,
            headers=request_headers,
            body=json.dumps(payload).encode("utf-8"),
        )

    async def _request(
        self,
        url: str,
        *,
        method: str,
        timeout: timedelta,
        headers: Mapping[str, str] | None,
        body: bytes | None,
    ) -> ProbeResult:
        def invoke() -> ProbeResult:
            started = time.monotonic()
            http_request = request.Request(
                url,
                data=body,
                method=method,
                headers=dict(headers or {}),
            )
            try:
                with request.urlopen(
                    http_request, timeout=timeout.total_seconds()
                ) as response:
                    response_body = response.read().decode("utf-8", errors="replace")
                    status = int(getattr(response, "status", 200))
            except error.HTTPError as exc:
                response_body = exc.read().decode("utf-8", errors="replace")
                status = int(exc.code)
            latency_ms = (time.monotonic() - started) * 1000.0
            return ProbeResult(
                status_code=status, latency_ms=latency_ms, body=response_body
            )

        return await asyncio.to_thread(invoke)
