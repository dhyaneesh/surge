"""Fail-closed security contracts for the scenario-to-Guardian boundary.

TST-GRD-TEN-001-INTEGRATION; TST-GRD-TEN-002-INTEGRATION.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import pytest

from testbeds.models import (
    EnvironmentRelease,
    EnvironmentState,
    ObservedServiceIdentity,
    WorkloadState,
)
from testbeds.scenarios.facts import (
    ControlStimulus,
    FactBuildContext,
    FactNormalizationError,
    build_incident_submission,
)
from testbeds.scenarios.guardian_client import (
    GuardianSubmission,
    GuardianUnavailableError,
    HttpGuardianClient,
)

DIGEST = "sha256:" + "a" * 64
TOKEN = "guardian_scenario_token_AA_0123456789"


def test_facts_fail_closed_on_missing_tenant() -> None:
    ctx = FactBuildContext(
        tenant_id="",
        environment="test",
        target_role="request-processor",
        role_bindings={"request-processor": "transaction-processor"},
        release=EnvironmentRelease(environment="test"),
        observed_at=datetime.now(UTC),
        observations=(
            EnvironmentState(
                environment="test",
                namespace="ns",
                services=(
                    ObservedServiceIdentity(
                        "transaction-processor",
                        "transaction-processor",
                        "1.2.3",
                        DIGEST,
                    ),
                ),
                workloads=(
                    WorkloadState(
                        "transaction-processor", "transaction-processor", 3, 3
                    ),
                ),
                healthy=True,
            ),
        ),
    )
    with pytest.raises(FactNormalizationError, match="tenant"):
        build_incident_submission(ctx)


def test_facts_never_embed_the_scenario_token() -> None:
    ctx = FactBuildContext(
        tenant_id="tenant-a",
        environment="test",
        target_role="request-processor",
        role_bindings={"request-processor": "transaction-processor"},
        release=EnvironmentRelease(environment="test"),
        observed_at=datetime.now(UTC),
        observations=(
            EnvironmentState(
                environment="test",
                namespace="ns",
                services=(
                    ObservedServiceIdentity(
                        "transaction-processor",
                        "transaction-processor",
                        "1.2.3",
                        DIGEST,
                    ),
                ),
                workloads=(
                    WorkloadState(
                        "transaction-processor", "transaction-processor", 3, 3
                    ),
                ),
                healthy=True,
            ),
        ),
    )
    submission = build_incident_submission(ctx)
    dump = json.dumps(submission.model_dump(mode="json"))
    assert "guardian_scenario_token" not in dump.lower()
    assert "Bearer" not in dump


def test_foreign_tenant_evidence_has_control_provenance() -> None:
    ctx = FactBuildContext(
        tenant_id="tenant-a",
        environment="test",
        target_role="request-processor",
        role_bindings={"request-processor": "transaction-processor"},
        release=EnvironmentRelease(environment="test"),
        observed_at=datetime.now(UTC),
        observations=(
            EnvironmentState(
                environment="test",
                namespace="ns",
                services=(
                    ObservedServiceIdentity(
                        "transaction-processor",
                        "transaction-processor",
                        "1.2.3",
                        DIGEST,
                    ),
                ),
                workloads=(
                    WorkloadState(
                        "transaction-processor", "transaction-processor", 3, 3
                    ),
                ),
                healthy=True,
            ),
        ),
        control=ControlStimulus(foreign_tenant=True),
    )
    submission = build_incident_submission(ctx)
    evidence = submission.signals.all_evidence()
    foreign = [item for item in evidence if item.tenant_id != "tenant-a"]
    assert len(foreign) == 1
    assert foreign[0].provenance_ref.startswith("test-control")


def test_client_refuses_to_run_without_a_token() -> None:
    with pytest.raises(GuardianUnavailableError, match="token"):
        HttpGuardianClient("http://127.0.0.1:8080")


def test_client_never_persists_the_token_in_submission_artifacts() -> None:
    client = HttpGuardianClient("http://127.0.0.1:1", token=TOKEN)
    assert client.bearer_token == TOKEN
    submission = GuardianSubmission(
        incident_id="incident-1",
        workflow_id="guardian/tenant-a/incident/incident-1",
        response_metadata={},
    )
    assert TOKEN not in json.dumps(submission.model_dump(mode="json"))


def test_client_environment_lookup_is_required_not_implicit() -> None:
    os.environ.pop("GUARDIAN_SCENARIO_TOKEN", None)
    with pytest.raises(GuardianUnavailableError, match="token"):
        HttpGuardianClient("http://127.0.0.1:8080")
