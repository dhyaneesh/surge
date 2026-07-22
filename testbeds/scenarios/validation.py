"""Reusable validation primitives for generalized Guardian test scenarios."""

from __future__ import annotations

import re
from math import isfinite
from collections.abc import Hashable, Iterable
from datetime import timedelta
from typing import TypeVar


_DURATION_PATTERN = re.compile(r"^(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h|d)$")
_DURATION_FACTORS = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
}
_DNS_LABEL = r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?"
_DNS_SUBDOMAIN_PATTERN = re.compile(rf"^{_DNS_LABEL}(?:\.{_DNS_LABEL})*$")
_SEMANTIC_LABEL_NAME_PATTERN = re.compile(rf"^{_DNS_LABEL}$")

RESERVED_RUNTIME_IDENTITY_LABEL_KEYS = frozenset(
    {
        "name",
        "serviceName",
        "workloadName",
        "deploymentName",
        "podName",
        "namespace",
        "instance",
        "service.name",
        "service.instance.id",
        "k8s.workload.name",
        "k8s.deployment.name",
        "k8s.pod.name",
        "app.kubernetes.io/name",
    }
)

T = TypeVar("T", bound=Hashable)


def parse_positive_duration(value: object) -> timedelta:
    """Parse a compact duration and require a value greater than zero."""

    if isinstance(value, timedelta):
        duration = value
    elif isinstance(value, str):
        match = _DURATION_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError("duration must be a number followed by ms, s, m, h, or d")
        seconds = float(match.group("value")) * _DURATION_FACTORS[match.group("unit")]
        if not isfinite(seconds):
            raise ValueError("duration is outside the supported range")
        try:
            duration = timedelta(seconds=seconds)
        except OverflowError as error:
            raise ValueError("duration is outside the supported range") from error
    else:
        raise ValueError("duration must be a compact duration string")
    if duration <= timedelta(0):
        raise ValueError("duration must be greater than zero")
    return duration


def require_unique(values: Iterable[T], field_name: str) -> None:
    """Reject duplicate values while retaining the caller's original ordering."""

    seen: set[T] = set()
    duplicates: list[T] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    if duplicates:
        rendered = ", ".join(sorted(str(value) for value in duplicates))
        raise ValueError(f"{field_name} must be unique; duplicates: {rendered}")


def validate_dns_name(value: str) -> str:
    """Validate a Kubernetes-style DNS subdomain name."""

    if len(value) > 253 or not _DNS_SUBDOMAIN_PATTERN.fullmatch(value):
        raise ValueError("name must be a DNS-style lowercase name")
    if any(len(label) > 63 for label in value.split(".")):
        raise ValueError("each DNS name component must be at most 63 characters")
    return value


def validate_semantic_labels(labels: dict[str, str]) -> dict[str, str]:
    """Validate portable labels and reject direct runtime identity dimensions."""

    for key, value in labels.items():
        if key in RESERVED_RUNTIME_IDENTITY_LABEL_KEYS:
            raise ValueError(f"semantic label key {key!r} is a runtime identity field")
        parts = key.split("/")
        if len(parts) > 2:
            raise ValueError(f"semantic label key {key!r} is not normalized DNS-style")
        prefix, name = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
        if (
            len(name) > 63
            or not _SEMANTIC_LABEL_NAME_PATTERN.fullmatch(name)
            or (prefix is not None and validate_dns_name(prefix) != prefix)
        ):
            raise ValueError(f"semantic label key {key!r} is not normalized DNS-style")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"semantic label {key!r} must have a non-empty string value"
            )
    return labels


def is_mutating_action(action: object) -> bool:
    value = getattr(action, "value", action)
    return value in {"scale", "rollback"}
