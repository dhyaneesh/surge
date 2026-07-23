import asyncio
import json
import subprocess
from datetime import timedelta

import pytest

from testbeds.adapters.command_runner import (
    AllowlistedCommandRunner,
    CommandRejected,
    CommandResult,
    redact,
)
from testbeds.adapters.otel_demo import OpenTelemetryDemoAdapter
from testbeds.environments.otel_demo import OTEL_DEMO_ENVIRONMENT
from testbeds.models import (
    DeploymentSpecification,
    FaultSpecification,
    FaultType,
    EnvironmentState,
    LoadProfile,
    WorkloadSelector,
    WorkloadState,
)


class FakeRunner:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    async def run(self, argv, *, timeout, cwd=None, input_text=None):
        self.calls.append((tuple(argv), timeout, cwd, input_text))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return CommandResult(tuple(argv), 0, "", "", 0.01)


def result(argv, stdout="", stderr="", returncode=0):
    return CommandResult(tuple(argv), returncode, stdout, stderr, 0.01)


def test_environment_pin_is_full_sha_and_documents_baseline_provenance():
    assert OTEL_DEMO_ENVIRONMENT.repository == (
        "https://github.com/open-telemetry/opentelemetry-demo.git"
    )
    assert len(OTEL_DEMO_ENVIRONMENT.commit_sha) == 40
    int(OTEL_DEMO_ENVIRONMENT.commit_sha, 16)
    assert OTEL_DEMO_ENVIRONMENT.source_derived_checks
    assert OTEL_DEMO_ENVIRONMENT.inferred_checks


def test_runner_accepts_argv_only_and_rejects_injection(tmp_path):
    runner = AllowlistedCommandRunner()
    with pytest.raises(TypeError):
        asyncio.run(runner.run("kubectl get pods", timeout=timedelta(seconds=1)))
    with pytest.raises(CommandRejected):
        asyncio.run(
            runner.run(
                ["kubectl", "get", "pods", ";", "rm", "-rf", "/"],
                timeout=timedelta(seconds=1),
            )
        )
    with pytest.raises(CommandRejected):
        asyncio.run(
            runner.run(
                ["kubectl", "exec", "pod", "--", "sh", "-c", "id"],
                timeout=timedelta(seconds=1),
            )
        )


def test_runner_sanitizes_environment_captures_output_and_forbids_shell(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "out", "err")

    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setattr(subprocess, "run", fake_run)
    command = AllowlistedCommandRunner(base_environment={"PATH": "/bin"})
    response = asyncio.run(
        command.run(["git", "rev-parse", "HEAD"], timeout=timedelta(seconds=2))
    )
    assert captured["shell"] is False
    assert captured["timeout"] == 2
    assert captured["capture_output"] is True
    assert captured["env"] == {
        "PATH": "/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }
    assert response.stdout == "out"
    assert response.stderr == "err"


def test_redaction_removes_common_secret_forms():
    dirty = (
        "Authorization: Bearer abc.def\npassword: hunter2\n"
        'client_secret="value"\nTOKEN=something'
    )
    clean = redact(dirty)
    assert "abc.def" not in clean
    assert "hunter2" not in clean
    assert "value" not in clean
    assert "something" not in clean
    assert clean.count("[REDACTED]") == 4


def test_runner_preserves_only_safe_connectivity_variables(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("clusters: []\n")
    source = {
        "PATH": "/safe/bin",
        "HOME": str(tmp_path),
        "KUBECONFIG": str(kubeconfig),
        "HTTP_PROXY": "http://proxy.example:8080",
        "https_proxy": "http://proxy.example:8443",
        "SSL_CERT_FILE": "/certs/ca.pem",
        "SSL_CERT_DIR": "/certs",
        "REQUESTS_CA_BUNDLE": "/certs/requests.pem",
        "CURL_CA_BUNDLE": "/certs/curl.pem",
        "AWS_SECRET_ACCESS_KEY": "must-not-leak",
        "RANDOM_SETTING": "must-not-leak",
    }
    captured = []

    def fake_run(argv, **kwargs):
        captured.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = AllowlistedCommandRunner(base_environment=source)
    asyncio.run(runner.run(["kubectl", "get", "nodes"], timeout=timedelta(seconds=1)))
    asyncio.run(runner.run(["helm", "version"], timeout=timedelta(seconds=1)))
    assert captured[0] == captured[1]
    for key in (
        "PATH",
        "HOME",
        "KUBECONFIG",
        "HTTP_PROXY",
        "https_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    ):
        assert captured[0][key] == source[key]
    assert "AWS_SECRET_ACCESS_KEY" not in captured[0]
    assert "RANDOM_SETTING" not in captured[0]


def test_runner_adds_api_hostname_from_kubeconfig_without_kubectl(
    monkeypatch, tmp_path
):
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text(
        "apiVersion: v1\nclusters:\n- cluster:\n"
        "    server: https://active-api.example.internal:6443\n"
        "  name: active\ncurrent-context: active\n"
    )
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = AllowlistedCommandRunner(
        base_environment={
            "PATH": "/bin",
            "HOME": str(tmp_path),
            "KUBECONFIG": str(kubeconfig),
        }
    )
    asyncio.run(runner.run(["kubectl", "get", "nodes"], timeout=timedelta(seconds=1)))
    assert captured["NO_PROXY"] == captured["no_proxy"]
    assert captured["NO_PROXY"].split(",") == [
        "127.0.0.1",
        "localhost",
        "::1",
        "active-api.example.internal",
    ]


def test_runner_merges_upper_and_lower_no_proxy_consistently(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = AllowlistedCommandRunner(
        base_environment={
            "PATH": "/bin",
            "NO_PROXY": "upper.example,localhost",
            "no_proxy": "lower.example,upper.example",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
        }
    )
    asyncio.run(runner.run(["helm", "version"], timeout=timedelta(seconds=1)))
    assert captured["NO_PROXY"] == captured["no_proxy"]
    assert captured["NO_PROXY"].split(",") == [
        "upper.example",
        "localhost",
        "lower.example",
        "127.0.0.1",
        "::1",
        "10.0.0.1",
    ]
    assert "KUBERNETES_SERVICE_HOST" not in captured


def test_redaction_removes_proxy_credentials_and_urls():
    dirty = (
        "HTTPS_PROXY=https://user:password@proxy.example:8443 "
        "failed via proxy http://proxy.example:8080"
    )
    clean = redact(dirty)
    assert "user" not in clean
    assert "password" not in clean
    assert "proxy.example" not in clean
    assert "[REDACTED_PROXY_URL]" in clean


def test_install_verifies_head_before_helm_and_uses_run_namespace(tmp_path):
    (tmp_path / "source" / ".git").mkdir(parents=True)
    runner = FakeRunner(
        [
            result(["git"]),
            result(["git"], OTEL_DEMO_ENVIRONMENT.commit_sha + "\n"),
            result(["helm"]),
            result(["kubectl"], json.dumps({"items": []})),
        ]
    )
    adapter = OpenTelemetryDemoAdapter(
        runner=runner,
        workspace=tmp_path,
        run_id="run-123",
    )
    state = asyncio.run(adapter.install(OTEL_DEMO_ENVIRONMENT.release()))
    assert state.namespace == "guardian-otel-demo-run-123"
    assert runner.calls[1][0] == ("git", "rev-parse", "HEAD")
    helm = runner.calls[2][0]
    assert helm[:3] == ("helm", "upgrade", "--install")
    assert "guardian-otel-demo-run-123" in helm
    assert "grafana.enabled=false" in helm
    assert "jaeger.enabled=false" in helm
    assert "prometheus.enabled=false" in helm
    assert "opensearch.enabled=false" in helm
    assert state.release.commit_sha == OTEL_DEMO_ENVIRONMENT.commit_sha


def test_install_rejects_wrong_checked_out_head_before_cluster_change(tmp_path):
    (tmp_path / "source" / ".git").mkdir(parents=True)
    runner = FakeRunner([result(["git"]), result(["git"], "0" * 40 + "\n")])
    adapter = OpenTelemetryDemoAdapter(runner=runner, workspace=tmp_path)
    with pytest.raises(RuntimeError, match="pinned commit"):
        asyncio.run(adapter.install(OTEL_DEMO_ENVIRONMENT.release()))
    assert all(call[0][:2] != ("helm", "upgrade") for call in runner.calls)


def test_install_preserves_primary_error_when_cleanup_also_fails(tmp_path):
    (tmp_path / "source" / ".git").mkdir(parents=True)

    class FailingInstallRunner(FakeRunner):
        async def run(self, argv, *, timeout, cwd=None, input_text=None):
            self.calls.append((tuple(argv), timeout, cwd, input_text))
            if tuple(argv[:2]) == ("git", "rev-parse"):
                return result(argv, OTEL_DEMO_ENVIRONMENT.commit_sha + "\n")
            if tuple(argv[:2]) == ("helm", "upgrade"):
                raise RuntimeError("primary install failure")
            if tuple(argv[:3]) == ("kubectl", "delete", "namespace"):
                raise RuntimeError("cleanup failure")
            return result(argv)

    adapter = OpenTelemetryDemoAdapter(
        runner=FailingInstallRunner(), workspace=tmp_path
    )
    with pytest.raises(RuntimeError, match="primary install failure") as caught:
        asyncio.run(adapter.install(OTEL_DEMO_ENVIRONMENT.release()))
    assert any("cleanup failure" in note for note in caught.value.__notes__)


def test_unsupported_fault_is_rejected_without_command(tmp_path):
    runner = FakeRunner()
    adapter = OpenTelemetryDemoAdapter(runner=runner, workspace=tmp_path)
    fault = FaultSpecification(
        fault_type=FaultType.NETWORK_PARTITION,
        target=WorkloadSelector(role="transaction-processor"),
    )
    with pytest.raises(ValueError, match="unsupported fault"):
        asyncio.run(adapter.inject_fault(fault))
    assert runner.calls == []


def test_supported_load_and_fault_are_normalized_and_adapter_private(tmp_path):
    runner = FakeRunner()
    adapter = OpenTelemetryDemoAdapter(runner=runner, workspace=tmp_path)
    load = asyncio.run(adapter.apply_load(LoadProfile(concurrent_users=25)))
    fault = asyncio.run(
        adapter.inject_fault(
            FaultSpecification(
                fault_type=FaultType.SERVICE_FAILURE,
                target=WorkloadSelector(role="transaction-processor"),
                magnitude=1.0,
            )
        )
    )
    assert load.profile.concurrent_users == 25
    assert load.active
    assert fault.fault.fault_type is FaultType.SERVICE_FAILURE
    assert fault.active
    assert all("paymentFailure" not in str(item) for item in (load, fault))
    assert any(
        call[0][:5]
        == ("kubectl", "set", "env", "deployment/load-generator", "LOCUST_USERS=25")
        for call in runner.calls
    )
    assert any("paymentFailure" in str(call) for call in runner.calls)


def test_observe_state_returns_normalized_workloads_and_versions(tmp_path):
    payload = {
        "items": [
            {
                "metadata": {"name": "demo-checkout", "generation": 2},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "metadata": {
                            "labels": {"app.kubernetes.io/component": "checkout"}
                        },
                        "spec": {"containers": [{"image": "otel/demo:v2@sha256:abc"}]},
                    },
                },
                "status": {"availableReplicas": 1, "observedGeneration": 2},
            }
        ]
    }
    runner = FakeRunner([result(["kubectl"], json.dumps(payload))])
    adapter = OpenTelemetryDemoAdapter(runner=runner, workspace=tmp_path)
    state = asyncio.run(adapter.observe_state())
    assert state.workloads[0].role == "transaction-processor"
    assert state.workloads[0].ready_replicas == 1
    assert state.services[0].version == "v2"
    assert state.services[0].image_digest == "sha256:abc"


def test_observe_state_is_unhealthy_while_fault_is_active(tmp_path):
    payload = {
        "items": [
            {
                "metadata": {"name": "demo-checkout", "generation": 1},
                "spec": {
                    "replicas": 1,
                    "template": {
                        "metadata": {
                            "labels": {"app.kubernetes.io/component": "checkout"}
                        },
                        "spec": {
                            "containers": [{"image": "otel/demo:v2@sha256:" + "a" * 64}]
                        },
                    },
                },
                "status": {"availableReplicas": 1, "observedGeneration": 1},
            }
        ]
    }
    endpoints = json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": "frontendproxy"},
                    "subsets": [{"addresses": [{"ip": "10.0.0.1"}]}],
                }
            ]
        }
    )
    adapter = OpenTelemetryDemoAdapter(
        runner=FakeRunner(
            [
                result(["kubectl"], json.dumps(payload)),
                result(["kubectl"], endpoints),
                result(["kubectl"], json.dumps(payload)),
                result(["kubectl"], endpoints),
            ]
        ),
        workspace=tmp_path,
    )

    assert asyncio.run(adapter.observe_state()).healthy
    adapter._active_faults.add(FaultType.SERVICE_FAILURE)

    state = asyncio.run(adapter.observe_state())

    assert not state.healthy


def test_baseline_requires_real_frontend_endpoint(tmp_path):
    adapter = OpenTelemetryDemoAdapter(runner=FakeRunner(), workspace=tmp_path)
    state = EnvironmentState(
        workloads=(
            WorkloadState("frontend", "frontendproxy", 1, 1, 1, 1),
            WorkloadState("load-generator", "load-generator", 1, 1, 1, 1),
        ),
        available_endpoints=frozenset(),
    )

    checks = {check.name: check for check in adapter._baseline_checks(state)}

    assert not checks["required_endpoints"].passed


@pytest.mark.parametrize(
    ("version", "digest"),
    [
        ("otel/demo:latest", "sha256:" + "a" * 64),
        ("otel/demo:v2", None),
        ("otel/demo:v2", "not-a-digest"),
    ],
)
def test_deploy_version_rejects_mutable_or_invalid_images_without_cluster_write(
    tmp_path, version, digest
):
    runner = FakeRunner()
    adapter = OpenTelemetryDemoAdapter(runner=runner, workspace=tmp_path)

    with pytest.raises(ValueError, match="immutable"):
        asyncio.run(
            adapter.deploy_version(
                DeploymentSpecification(
                    WorkloadSelector("transaction-processor"), version, digest
                )
            )
        )

    assert runner.calls == []


def test_reset_reconciles_after_partial_failure_and_reinstalls_when_contaminated(
    tmp_path, monkeypatch
):
    runner = FakeRunner()
    adapter = OpenTelemetryDemoAdapter(runner=runner, workspace=tmp_path)
    adapter._active_load = True
    adapter._active_faults.add(FaultType.SERVICE_FAILURE)
    adapter._created_resources.add(("configmap", "temporary"))
    attempts = []

    async def baseline(timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise TimeoutError("not converged")
        return adapter._healthy_baseline_for_test()

    async def reinstall():
        attempts.append("reinstall")

    monkeypatch.setattr(adapter, "wait_for_healthy_baseline", baseline)
    monkeypatch.setattr(adapter, "_reinstall", reinstall)
    asyncio.run(adapter.reset())
    assert "reinstall" in attempts
    assert adapter.contaminated is False
    assert adapter._active_faults == set()
    assert adapter._created_resources == set()
    assert any(call[0][:2] == ("kubectl", "delete") for call in runner.calls)


def test_failure_writes_redacted_diagnostic_artifacts(tmp_path):
    runner = FakeRunner([RuntimeError("Authorization: Bearer top-secret")])
    adapter = OpenTelemetryDemoAdapter(runner=runner, workspace=tmp_path)
    with pytest.raises(RuntimeError):
        asyncio.run(adapter.observe_state())
    artifacts = list((tmp_path / "diagnostics").rglob("*"))
    assert artifacts
    contents = "\n".join(path.read_text() for path in artifacts if path.is_file())
    assert "top-secret" not in contents
    assert "[REDACTED]" in contents
    state = next((tmp_path / "diagnostics").rglob("adapter-state.json"))
    metadata = json.loads(state.read_text())
    assert metadata["executed_commands"][0]["argv"][:2] == [
        "kubectl",
        "get",
    ]
    assert metadata["executed_commands"][0]["outcome"] == "failed"


def test_cleanup_is_idempotent_and_removes_only_run_namespace(tmp_path):
    runner = FakeRunner()
    adapter = OpenTelemetryDemoAdapter(
        runner=runner, workspace=tmp_path, run_id="cleanup-me"
    )
    asyncio.run(adapter.cleanup())
    asyncio.run(adapter.cleanup())
    uninstalls = [call for call in runner.calls if call[0][:2] == ("helm", "uninstall")]
    deletes = [call for call in runner.calls if call[0][:2] == ("kubectl", "delete")]
    assert len(uninstalls) == 1
    assert runner.calls.index(uninstalls[0]) < runner.calls.index(deletes[0])
    assert len(deletes) == 1
    assert "guardian-otel-demo-cleanup-me" in deletes[0][0]
