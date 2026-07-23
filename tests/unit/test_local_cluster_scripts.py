from __future__ import annotations

from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
OBSERVABILITY = ROOT / "testbeds" / "observability"
RUN_ID_HELPER = SCRIPTS / "lib" / "guardian-run-id.sh"

KIND_SHA256 = "eb244cbafcc157dff60cf68693c14c9a75c4e6e6fedaf9cd71c58117cb93e3fa"
NODE_IMAGE = (
    "kindest/node:v1.35.0@sha256:"
    "452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
)
METRICS_SERVER_SHA256 = (
    "1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b"
)
SIGNOZ_SHA256 = "103f127d1efe3e5f7c9ca87f224ce66b75bb7e688b72608530d11bcd72dbb6dc"


def read_script(name: str) -> str:
    path = SCRIPTS / name
    assert path.is_file(), f"missing script: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def test_bootstrap_kind_downloads_and_verifies_the_pinned_binary() -> None:
    script = read_script("bootstrap-kind.sh")

    assert "v0.31.0" in script
    assert KIND_SHA256 in script
    assert (
        "github.com/kubernetes-sigs/kind/releases/download/v0.31.0/kind-linux-amd64"
        in script
    )
    assert "mktemp -d" in script
    assert ".tools/bin/kind" in script
    assert re.search(r"\bsha256sum\b|\bshasum\b", script)
    assert re.search(r"\bmv\b.*\bkind\b", script)
    assert "--version" in script


def test_cluster_script_uses_an_isolated_owned_cluster_with_resource_guards() -> None:
    script = read_script("create-test-cluster.sh")

    assert NODE_IMAGE in script
    assert "GUARDIAN_CLUSTER_RUN_ID" in script
    assert "guardian-${run_id}" in script
    assert "artifacts/local/${run_id}/kubeconfig" in script
    assert "KUBECONFIG" in script
    assert "docker info" in script
    assert "2684354560" in script
    assert "12884901888" in script
    assert "preflight.json" in script
    assert '"$kind" get clusters' in script
    assert "docker update --memory 6g --memory-swap 8g" in script
    assert "docker inspect" in script
    assert '"$kind" delete cluster' in script
    assert "trap" in script
    assert "metrics-server/releases/download/v0.9.0/components.yaml" in script
    assert METRICS_SERVER_SHA256 in script
    assert "--kubelet-insecure-tls" in script
    assert "apiservice/v1beta1.metrics.k8s.io" in script
    assert "Docker Desktop" in script
    assert "WSL Integration" in script


def test_observability_installer_verifies_immutable_chart_and_image_lock() -> None:
    script = read_script("install-test-observability.sh")
    values = OBSERVABILITY / "signoz-values.yaml"
    lock = OBSERVABILITY / "signoz-images.lock.yaml"

    assert "v0.133.0" in script
    assert SIGNOZ_SHA256 in script
    assert "mktemp -d" in script
    assert "helm template" in script
    assert "guardian-observability" in script
    assert "signoz-images.lock.yaml" in script
    assert "imageID" in script
    assert "--post-renderer" in script
    assert "image lock is not digest-pinned" in script
    assert "live pod image is absent from lock" in script
    assert "sys.stdin.read()" in script
    assert "sys.stdout.write(rewritten)" in script
    for component in ("clickhouse", "zookeeper", "signoz", "otel-collector"):
        assert f"wait_for_component {component}" in script
    assert values.is_file()
    assert lock.is_file()
    assert yaml.safe_load(lock.read_text(encoding="utf-8"))["images"]


def test_observability_values_bound_the_local_footprint() -> None:
    values = yaml.safe_load(
        (OBSERVABILITY / "signoz-values.yaml").read_text(encoding="utf-8")
    )
    serialized = yaml.safe_dump(values)

    for required in (
        "clickhouse",
        "zookeeper",
        "signoz",
        "otelCollector",
        "persistence",
        "enabled: false",
        "200Mi",
        "1200Mi",
        "128Mi",
        "384Mi",
        "100Mi",
        "512Mi",
        "500m",
    ):
        assert required in serialized


def test_leave_up_orchestration_records_environment_and_pids_without_tokens() -> None:
    script = read_script("local-up.sh")

    assert "create-test-cluster.sh" in script
    assert "install-test-observability.sh" in script
    assert "artifacts/local/${run_id}/env" in script
    assert "pids" in script
    assert "kubectl port-forward" in script
    assert "python3 -m apps.guardian_api" in script
    assert "GUARDIAN_LOCAL_TOKENS_JSON" in script
    assert "GUARDIAN_SCENARIO_TOKEN" in script
    assert "wait_for_http" in script
    assert "wait_for_tcp" in script
    assert "SigNoz HTTP endpoint" in script
    assert "Guardian health endpoint" in script
    assert "OTLP HTTP port-forward" in script
    assert "did not become ready" in script
    assert "cleanup_failed_startup" in script
    assert "trap cleanup_failed_startup EXIT" in script
    assert "trap - EXIT" in script
    assert "guardian_port=$(select_port)" in script
    assert 'GUARDIAN_PORT="$guardian_port"' in script
    assert "http://127.0.0.1:${guardian_port}/health" in script
    assert "http://127.0.0.1:${guardian_port}" in script
    assert "process_starttime" in script
    assert "process_fingerprint" in script
    assert script.index('record_process "$signoz_pid"') < script.index(
        'wait_for_http "http://127.0.0.1:${signoz_port}/"'
    )
    assert "guardian process exited before readiness" in script
    assert "guardian_validate_run_id" in script


def test_all_local_lifecycle_scripts_share_run_id_validation() -> None:
    assert RUN_ID_HELPER.is_file()
    helper = RUN_ID_HELPER.read_text(encoding="utf-8")
    assert "guardian_validate_run_id" in helper
    assert "A-Za-z0-9._-" in helper
    assert "*..*" in helper

    for name in (
        "local-up.sh",
        "local-down.sh",
        "test-phase0.sh",
        "run-local-matrix.sh",
        "create-test-cluster.sh",
        "install-test-observability.sh",
    ):
        script = read_script(name)
        assert "scripts/lib/guardian-run-id.sh" in script
        assert "guardian_validate_run_id" in script
    user_output = re.findall(r"(?m)^printf .*$|^echo .*$", script)
    assert all("TOKEN" not in line and "token" not in line for line in user_output)


def test_local_down_kills_owned_processes_and_deletes_unless_retained() -> None:
    script = read_script("local-down.sh")

    assert "artifacts/local/${run_id}/pids" in script
    assert "kill" in script
    assert "GUARDIAN_CLUSTER_RETAIN" in script
    assert "GUARDIAN_CLUSTER_RETAIN:-0" in script
    assert 'delete cluster --name "$cluster_name"' in script
    assert "guardian-${run_id}" in script
    assert "guardian_validate_run_id" in script
    assert "/proc/${pid}/stat" in script
    assert "/proc/${pid}/cmdline" in script
    assert "PID identity mismatch" in script


def test_phase0_script_refuses_to_continue_after_reset_or_cleanup_failure() -> None:
    script = read_script("test-phase0.sh")

    assert "set -euo pipefail" in script or "set -e" in script
    assert "test:replay" in script
    assert "test:env" in script
    assert "ENV=otel-demo" in script
    assert "guardian_validate_run_id" in script
    # Smoke path is one environment; full matrix belongs to run-local-matrix.sh.
    assert not re.search(r"(?m)^[^#]*task test:matrix", script)
    assert re.search(r"reset.*cleanup|cleanup.*reset", script, flags=re.IGNORECASE)


def test_optional_matrix_wrapper_delegates_to_leave_up_lifecycle() -> None:
    script = read_script("run-local-matrix.sh")

    assert "local-up.sh" in script
    assert "local-down.sh" in script
    assert "--full" in script
    assert "task test:matrix" in script
    assert 'env_file="$root/artifacts/local/${run_id}/env"' in script
    assert '[ -f "$env_file" ]' in script
    assert '. "$env_file"' in script
    assert "guardian_validate_run_id" in script
    assert script.index("trap cleanup EXIT") < script.index(
        '"$root/scripts/local-up.sh"'
    )
