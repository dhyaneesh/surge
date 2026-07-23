from pathlib import Path

import pytest

from testbeds.scenarios.environment_suite import select_scenarios
from testbeds.scenarios.evidence_provider import CollectorScenarioEvidenceProvider
from testbeds.scenarios.loader import load_guardian_scenario
from testbeds.scenarios.registry import (
    SUPPORTED_ENVIRONMENTS,
    build_adapter_registration,
)
from testbeds.scenarios.v1alpha2 import GuardianScenarioV1Alpha2


def scenario_paths():
    return sorted(
        path
        for path in Path("testbeds/scenarios").glob("*.yaml")
        if path.name not in {"compatibility.yaml", "index.yaml"}
    )


def test_all_v1alpha2_scenarios_have_unique_ids_and_registered_environments(tmp_path):
    scenarios = [load_guardian_scenario(path) for path in scenario_paths()]
    names = [scenario.metadata.name for scenario in scenarios]
    assert len(names) == len(set(names))
    for scenario in scenarios:
        if not isinstance(scenario, GuardianScenarioV1Alpha2):
            continue
        assert set(scenario.spec.candidate_environments) <= set(SUPPORTED_ENVIRONMENTS)
        for environment in scenario.spec.candidate_environments:
            registration = build_adapter_registration(
                environment, workspace=tmp_path / environment, run_id="contract"
            )
            assert isinstance(
                registration.evidence_provider, CollectorScenarioEvidenceProvider
            )


def test_unknown_environment_is_rejected_before_adapter_construction(tmp_path):
    with pytest.raises(ValueError, match="unknown environment"):
        build_adapter_registration("unknown", workspace=tmp_path)


def test_every_environment_has_an_evidence_provider_and_selectable_scenario(
    monkeypatch,
):
    monkeypatch.delenv("GUARDIAN_EVIDENCE_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("GUARDIAN_EVIDENCE_ENDPOINT_BASE", raising=False)
    for environment in SUPPORTED_ENVIRONMENTS:
        registration = build_adapter_registration(environment, workspace=Path("."))
        assert isinstance(
            registration.evidence_provider, CollectorScenarioEvidenceProvider
        )
        assert registration.evidence_provider.targets.endpoint_url.endswith(
            ".svc.cluster.local"
        )
        selected, _ = select_scenarios(environment, Path("testbeds/scenarios"))
        assert selected, f"{environment} selected no executable scenarios"


def test_evidence_endpoint_full_url_override_applies_to_every_environment(
    monkeypatch, tmp_path
):
    endpoint = "http://127.0.0.1:8080/health"
    monkeypatch.setenv("GUARDIAN_EVIDENCE_ENDPOINT_URL", endpoint)
    for environment in SUPPORTED_ENVIRONMENTS:
        registration = build_adapter_registration(
            environment, workspace=tmp_path / environment
        )
        assert isinstance(
            registration.evidence_provider, CollectorScenarioEvidenceProvider
        )
        assert registration.evidence_provider.targets.endpoint_url == endpoint


def test_evidence_endpoint_base_override_uses_service_path(monkeypatch, tmp_path):
    monkeypatch.delenv("GUARDIAN_EVIDENCE_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("GUARDIAN_EVIDENCE_ENDPOINT_BASE", "http://127.0.0.1:8080")

    registration = build_adapter_registration("otel-demo", workspace=tmp_path)

    assert isinstance(registration.evidence_provider, CollectorScenarioEvidenceProvider)
    assert (
        registration.evidence_provider.targets.endpoint_url
        == "http://127.0.0.1:8080/frontend"
    )


def test_v1alpha1_remains_explicitly_non_executable():
    legacy = load_guardian_scenario(
        "tests/fixtures/scenarios/legitimate-demand-scale-up-v1alpha1.yaml"
    )
    assert not isinstance(legacy, GuardianScenarioV1Alpha2)
