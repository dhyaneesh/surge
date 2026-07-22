from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import testbeds.scenarios as scenarios_package
from testbeds.scenarios import GuardianScenario, load_scenario
from testbeds.scenarios.loader import ScenarioLoadError


def minimal_scenario() -> dict:
    return {
        "apiVersion": "tests.guardian.io/v1alpha1",
        "kind": "GuardianScenario",
        "metadata": {"name": "minimal-scenario"},
        "spec": {
            "applicableEnvironments": ["otel-demo"],
            "target": {
                "serviceSelector": {"role": "request-processor"},
            },
        },
    }


def complete_scenario() -> dict:
    document = minimal_scenario()
    document["metadata"]["name"] = "complete-scenario"
    document["spec"].update(
        {
            "applicableEnvironments": ["otel-demo", "aws-retail"],
            "unsupportedEnvironments": [
                {"environment": "keda-rabbitmq", "reason": "no deployment support"}
            ],
            "requiredCapabilities": [
                "horizontally-scalable",
                "version-deployable",
            ],
            "requiredFaults": ["artificial_latency"],
            "requiredTelemetry": ["metrics", "traces"],
            "requiredProviders": ["kubernetes", "deployment-provider"],
            "target": {
                "serviceSelector": {
                    "role": "request-processor",
                    "capabilities": [
                        "horizontally-scalable",
                        "version-deployable",
                    ],
                    "semanticLabels": {"service-tier": "transactional"},
                },
                "workloadSelector": {"role": "background-worker"},
            },
            "baseline": {"healthyFor": "5m"},
            "stimulus": {
                "load": {"pattern": "step", "multiplier": 4, "duration": "10m"},
                "fault": {
                    "type": "artificial_latency",
                    "duration": "2m",
                    "magnitude": 0.5,
                },
                "deployment": {
                    "fromVersion": "healthy",
                    "toVersion": "error-regression",
                    "recordDeploymentEvent": True,
                },
            },
            "expected": {
                "incidentClass": "deployment_regression",
                "proposedAction": {"type": "rollback", "approvalRequired": True},
                "allowedActions": ["rollback"],
                "forbiddenActions": ["scale"],
                "evidenceTypes": [
                    "metrics",
                    "traces",
                    "deployment-event",
                ],
                "policyDecision": "approval-required",
                "workflowStates": [
                    "active",
                    "assessment",
                    "action-proposed",
                    "approval-pending",
                    "recovery-verification",
                    "recovered",
                ],
                "recovery": {
                    "contractRef": "service-error-recovery",
                    "requireFreshTelemetry": True,
                    "expectedResult": "recovered",
                },
            },
        }
    )
    return document


def validation_error(document: dict) -> ValidationError:
    with pytest.raises(ValidationError) as captured:
        GuardianScenario.model_validate(document)
    return captured.value


def set_nested(document: dict, path: tuple[str, ...], value: object) -> None:
    target = document
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = value


def test_minimal_valid_scenario_has_explicit_compatibility_defaults() -> None:
    scenario = GuardianScenario.model_validate(minimal_scenario())

    assert scenario.spec.applicable_environments == ("otel-demo",)
    assert scenario.spec.unsupported_environments == ()
    assert scenario.spec.required_capabilities == frozenset()
    assert scenario.spec.required_faults == frozenset()
    assert scenario.spec.required_telemetry == frozenset()
    assert scenario.spec.required_providers == frozenset()


def test_complete_scenario_uses_snake_case_attributes_and_parses_durations() -> None:
    scenario = GuardianScenario.model_validate(complete_scenario())

    assert scenario.spec.baseline is not None
    assert scenario.spec.stimulus.load is not None
    assert scenario.spec.stimulus.fault is not None
    assert scenario.spec.stimulus.deployment is not None
    assert scenario.spec.expected.proposed_action is not None
    assert scenario.api_version == "tests.guardian.io/v1alpha1"
    assert scenario.spec.target.service_selector.role.value == "request-processor"
    assert scenario.spec.baseline.healthy_for == timedelta(minutes=5)
    assert scenario.spec.stimulus.load.duration == timedelta(minutes=10)
    assert scenario.spec.stimulus.fault.fault_type.value == "artificial_latency"
    assert scenario.spec.stimulus.deployment.from_version == "healthy"
    assert scenario.spec.expected.proposed_action.approval_required is True


def test_serialization_uses_camel_case_aliases() -> None:
    serialized = GuardianScenario.model_validate(complete_scenario()).model_dump(
        by_alias=True, mode="json"
    )

    assert "apiVersion" in serialized
    assert "api_version" not in serialized
    assert "applicableEnvironments" in serialized["spec"]
    assert "serviceSelector" in serialized["spec"]["target"]
    assert "fromVersion" in serialized["spec"]["stimulus"]["deployment"]
    assert "requireFreshTelemetry" in serialized["spec"]["expected"]["recovery"]


def test_portable_semantic_labels_are_allowed() -> None:
    document = minimal_scenario()
    document["spec"]["target"]["serviceSelector"]["semanticLabels"] = {
        "guardian.io/service-tier": "transactional"
    }

    scenario = GuardianScenario.model_validate(document)

    assert scenario.spec.target.service_selector.semantic_labels == {
        "guardian.io/service-tier": "transactional"
    }


def test_load_fault_and_deployment_stimuli_may_coexist() -> None:
    scenario = GuardianScenario.model_validate(complete_scenario())

    assert scenario.spec.stimulus.load is not None
    assert scenario.spec.stimulus.fault is not None
    assert scenario.spec.stimulus.deployment is not None


def test_package_declares_only_the_primary_public_api() -> None:
    assert scenarios_package.__all__ == ["GuardianScenario", "load_scenario"]


def test_load_scenario_returns_guardian_scenario(tmp_path: Path) -> None:
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(minimal_scenario()), encoding="utf-8")

    assert isinstance(load_scenario(path), GuardianScenario)


def test_canonical_example_file_is_valid() -> None:
    path = (
        Path(__file__).parents[2]
        / "testbeds"
        / "scenarios"
        / "legitimate-demand-scale-up.yaml"
    )

    scenario = load_scenario(path)

    assert scenario.spec.expected.incident_class is not None
    assert scenario.spec.expected.proposed_action is not None
    assert scenario.metadata.name == "legitimate-demand-scale-up"
    assert scenario.spec.expected.incident_class.value == "load_spike"
    assert scenario.spec.expected.proposed_action.action_type.value == "scale"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("apiVersion",), "tests.guardian.io/v1"),
        (("kind",), "Scenario"),
        (("metadata", "name"), "Not_DNS_Compatible"),
    ],
)
def test_rejects_invalid_root_identity(path: tuple[str, ...], value: str) -> None:
    document = minimal_scenario()
    set_nested(document, path, value)

    validation_error(document)


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ((), "unexpected"),
        (("spec",), "unexpected"),
        (("spec", "target", "serviceSelector"), "unexpected"),
    ],
)
def test_rejects_unknown_fields(path: tuple[str, ...], field: str) -> None:
    document = minimal_scenario()
    target = document
    for component in path:
        target = target[component]
    target[field] = True

    error = validation_error(document)

    assert "extra_forbidden" in str(error)


@pytest.mark.parametrize("duration", ["later", "0s", "-1m", "5", ""])
def test_rejects_invalid_or_non_positive_duration(duration: str) -> None:
    document = minimal_scenario()
    document["spec"]["baseline"] = {"healthyFor": duration}

    validation_error(document)


def test_out_of_range_duration_is_a_schema_validation_error() -> None:
    document = minimal_scenario()
    document["spec"]["baseline"] = {"healthyFor": f"{'9' * 400}d"}

    validation_error(document)


@pytest.mark.parametrize("multiplier", [1, 0.5, 0, -2])
def test_rejects_load_multiplier_not_greater_than_one(multiplier: float) -> None:
    document = minimal_scenario()
    document["spec"]["stimulus"] = {
        "load": {"pattern": "step", "multiplier": multiplier, "duration": "1m"}
    }

    validation_error(document)


def test_rejects_duplicate_applicable_environments() -> None:
    document = minimal_scenario()
    document["spec"]["applicableEnvironments"] = ["otel-demo", "otel-demo"]

    validation_error(document)


def test_rejects_duplicate_unsupported_environments() -> None:
    document = minimal_scenario()
    document["spec"]["unsupportedEnvironments"] = [
        {"environment": "aws-retail", "reason": "missing provider"},
        {"environment": "aws-retail", "reason": "missing fault"},
    ]

    validation_error(document)


def test_rejects_overlapping_applicable_and_unsupported_environments() -> None:
    document = minimal_scenario()
    document["spec"]["unsupportedEnvironments"] = [
        {"environment": "otel-demo", "reason": "not actually supported"}
    ]

    validation_error(document)


@pytest.mark.parametrize("reason", ["", "   "])
def test_rejects_unsupported_environment_without_reason(reason: str) -> None:
    document = minimal_scenario()
    document["spec"]["unsupportedEnvironments"] = [
        {"environment": "aws-retail", "reason": reason}
    ]

    validation_error(document)


def test_rejects_duplicate_workflow_states() -> None:
    document = minimal_scenario()
    document["spec"]["expected"] = {
        "workflowStates": ["active", "assessment", "active"]
    }

    validation_error(document)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("spec", "expected", "incidentClass"), "network_failure"),
        (("spec", "expected", "allowedActions"), ["restart"]),
        (
            ("spec", "target", "serviceSelector", "role"),
            "checkout-service",
        ),
        (
            ("spec", "target", "serviceSelector", "capabilities"),
            ["magic"],
        ),
        (("spec", "requiredFaults"), ["packet_loss"]),
        (("spec", "expected", "evidenceTypes"), ["screenshots"]),
        (("spec", "expected", "policyDecision"), "maybe"),
        (("spec", "expected", "workflowStates"), ["starting"]),
    ],
)
def test_rejects_unknown_enum_values(path: tuple[str, ...], value: object) -> None:
    document = complete_scenario()
    set_nested(document, path, value)

    validation_error(document)


@pytest.mark.parametrize("field", ["name", "serviceName", "deploymentName", "podName"])
def test_rejects_direct_runtime_selector_fields(field: str) -> None:
    document = minimal_scenario()
    document["spec"]["target"]["serviceSelector"][field] = "concrete-service"

    validation_error(document)


@pytest.mark.parametrize(
    "key",
    [
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
    ],
)
def test_rejects_reserved_runtime_identity_semantic_label_keys(key: str) -> None:
    document = minimal_scenario()
    document["spec"]["target"]["serviceSelector"]["semanticLabels"] = {
        key: "concrete-service"
    }

    validation_error(document)


@pytest.mark.parametrize("value", ["", "   "])
def test_rejects_empty_semantic_label_values(value: str) -> None:
    document = minimal_scenario()
    document["spec"]["target"]["serviceSelector"]["semanticLabels"] = {
        "service-tier": value
    }

    validation_error(document)


@pytest.mark.parametrize(
    "key",
    [
        f"{'a' * 64}.guardian.io/tier",
        f"guardian.io/{'a' * 64}",
    ],
)
def test_rejects_semantic_label_keys_with_oversized_dns_components(key: str) -> None:
    document = minimal_scenario()
    document["spec"]["target"]["serviceSelector"]["semanticLabels"] = {
        key: "transactional"
    }

    validation_error(document)


def test_accepts_semantic_label_with_maximum_length_dns_prefix() -> None:
    prefix = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])
    document = minimal_scenario()
    document["spec"]["target"]["serviceSelector"]["semanticLabels"] = {
        f"{prefix}/tier": "transactional"
    }

    GuardianScenario.model_validate(document)


def test_rejects_overlapping_allowed_and_forbidden_actions() -> None:
    document = minimal_scenario()
    document["spec"]["expected"] = {
        "allowedActions": ["pause_scaler"],
        "forbiddenActions": ["pause_scaler"],
    }

    validation_error(document)


@pytest.mark.parametrize(
    ("from_version", "to_version"),
    [("same", "same"), ("", "new"), ("old", "   ")],
)
def test_rejects_invalid_deployment_transition(
    from_version: str, to_version: str
) -> None:
    document = minimal_scenario()
    document["spec"]["stimulus"] = {
        "deployment": {"fromVersion": from_version, "toVersion": to_version}
    }

    validation_error(document)


@pytest.mark.parametrize("action", ["scale", "rollback"])
@pytest.mark.parametrize("location", ["proposedAction", "allowedActions"])
def test_mutating_actions_require_recovery(action: str, location: str) -> None:
    document = minimal_scenario()
    if location == "proposedAction":
        expected = {location: {"type": action}}
    else:
        expected = {location: [action]}
    document["spec"]["expected"] = expected

    validation_error(document)


def test_fresh_telemetry_recovery_requires_observable_end_condition() -> None:
    document = minimal_scenario()
    document["spec"]["expected"] = {"recovery": {"requireFreshTelemetry": True}}

    validation_error(document)


def test_schema_violations_from_loader_remain_pydantic_errors(tmp_path: Path) -> None:
    document = minimal_scenario()
    document["kind"] = "WrongKind"
    path = tmp_path / "invalid-schema.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(ValidationError):
        load_scenario(path)


def test_malformed_yaml_is_a_path_aware_loader_error(tmp_path: Path) -> None:
    path = tmp_path / "malformed.yaml"
    path.write_text("spec: [unterminated", encoding="utf-8")

    with pytest.raises(ScenarioLoadError, match=str(path)):
        load_scenario(path)


@pytest.mark.parametrize("content", ["- one\n- two\n", "plain text\n", "null\n"])
def test_non_mapping_yaml_root_is_a_loader_error(tmp_path: Path, content: str) -> None:
    path = tmp_path / "not-a-mapping.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ScenarioLoadError, match=str(path)):
        load_scenario(path)


def test_missing_file_is_a_path_aware_loader_error(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"

    with pytest.raises(ScenarioLoadError, match=str(path)):
        load_scenario(path)


def test_unreadable_path_is_a_path_aware_loader_error(tmp_path: Path) -> None:
    path = tmp_path / "directory-instead-of-file"
    path.mkdir()

    with pytest.raises(ScenarioLoadError, match=str(path)):
        load_scenario(path)


def test_models_are_frozen() -> None:
    scenario = GuardianScenario.model_validate(minimal_scenario())

    with pytest.raises(ValidationError):
        setattr(scenario, "kind", "SomethingElse")


def test_model_validation_does_not_mutate_input() -> None:
    document = complete_scenario()
    original = copy.deepcopy(document)

    GuardianScenario.model_validate(document)

    assert document == original
