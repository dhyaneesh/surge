#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_id=${GUARDIAN_CLUSTER_RUN_ID:-$(date +%Y%m%d%H%M%S)}
. "$root/scripts/lib/guardian-run-id.sh"
if ! guardian_validate_run_id "$run_id"; then
  printf '[usage] invalid GUARDIAN_CLUSTER_RUN_ID: %s\n' "$run_id" >&2
  exit 64
fi

env_file="$root/artifacts/local/${run_id}/env"
artifact_dir=$(dirname "$env_file")
pids_file="$artifact_dir/pids"
signoz_pid=
otlp_pid=
guardian_pid=
startup_complete=0

cleanup_failed_startup() {
  status=$?
  if [ "$startup_complete" -eq 0 ]; then
    if [ -f "$pids_file" ]; then
      while read -r pid expected_starttime expected_fingerprint; do
        [ -r "/proc/${pid}/stat" ] && [ -r "/proc/${pid}/cmdline" ] || continue
        if [ "$(process_starttime "$pid")" = "$expected_starttime" ] &&
          [ "$(process_fingerprint "$pid")" = "$expected_fingerprint" ]; then
          kill "$pid" 2>/dev/null || true
        else
          printf '[cleanup] PID identity mismatch; not signaling PID %s\n' "$pid" >&2
        fi
      done <"$pids_file"
    fi
    printf '%s\n' '[cleanup] local processes stopped after startup failure; run local-down to remove the cluster' >&2
  fi
  return "$status"
}
trap cleanup_failed_startup EXIT

mkdir -p "$artifact_dir"
chmod 0700 "$artifact_dir"
[ ! -e "$env_file" ] && [ ! -e "$pids_file" ] || {
  printf '[prerequisite] local run already exists: %s\n' "$run_id" >&2
  exit 2
}

export GUARDIAN_CLUSTER_RUN_ID=$run_id
"$root/scripts/bootstrap-kind.sh"
"$root/scripts/create-test-cluster.sh"
"$root/scripts/install-test-observability.sh"

select_port() {
  python3 - <<'PY'
import socket
sock = socket.socket()
sock.bind(("127.0.0.1", 0))
print(sock.getsockname()[1])
sock.close()
PY
}

wait_for_http() {
  url=$1
  label=$2
  for _ in $(seq 1 30); do
    if curl --fail --silent "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  printf '[prerequisite] %s did not become ready\n' "$label" >&2
  exit 2
}

wait_for_tcp() {
  port=$1
  label=$2
  for _ in $(seq 1 30); do
    if python3 - "$port" <<'PY'
import socket
import sys

with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=1):
    pass
PY
    then
      return 0
    fi
    sleep 1
  done
  printf '[prerequisite] %s did not become ready\n' "$label" >&2
  exit 2
}

process_starttime() {
  awk '{print $22}' "/proc/${1}/stat"
}

process_fingerprint() {
  sha256sum "/proc/${1}/cmdline" | awk '{print $1}'
}

record_process() {
  pid=$1
  printf '%s %s %s\n' "$pid" "$(process_starttime "$pid")" "$(process_fingerprint "$pid")"
}

signoz_port=$(select_port)
otlp_port=$(select_port)
guardian_port=$(select_port)
token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
kubeconfig="$artifact_dir/kubeconfig"
: >"$pids_file"
chmod 0600 "$pids_file"

KUBECONFIG="$kubeconfig" kubectl port-forward --namespace guardian-observability svc/guardian-observability-signoz 127.0.0.1:"$signoz_port":8080 >"$artifact_dir/signoz-port-forward.log" 2>&1 &
signoz_pid=$!
record_process "$signoz_pid" >>"$pids_file"
KUBECONFIG="$kubeconfig" kubectl port-forward --namespace guardian-observability svc/guardian-observability-otel-collector 127.0.0.1:"$otlp_port":4318 >"$artifact_dir/otlp-port-forward.log" 2>&1 &
otlp_pid=$!
record_process "$otlp_pid" >>"$pids_file"
GUARDIAN_LOCAL_TOKENS_JSON=$(TOKEN="$token" python3 -c 'import json, os; print(json.dumps({os.environ["TOKEN"]: "local"}))') \
  GUARDIAN_PORT="$guardian_port" python3 -m apps.guardian_api >"$artifact_dir/guardian.log" 2>&1 &
guardian_pid=$!
record_process "$guardian_pid" >>"$pids_file"

wait_for_http "http://127.0.0.1:${signoz_port}/" "SigNoz HTTP endpoint"
wait_for_tcp "$otlp_port" "OTLP HTTP port-forward"
wait_for_http "http://127.0.0.1:${guardian_port}/health" "Guardian health endpoint"
if ! kill -0 "$guardian_pid" 2>/dev/null; then
  printf '%s\n' '[prerequisite] guardian process exited before readiness' >&2
  exit 2
fi

{
  printf 'KUBECONFIG=%q\n' "$kubeconfig"
  printf 'GUARDIAN_BASE_URL=%q\n' "http://127.0.0.1:${guardian_port}"
  printf 'GUARDIAN_SIGNOZ_HTTP_URL=%q\n' "http://127.0.0.1:${signoz_port}"
  printf 'GUARDIAN_OTLP_HTTP_URL=%q\n' "http://127.0.0.1:${otlp_port}"
  printf 'GUARDIAN_SCENARIO_TOKEN=%q\n' "$token"
} >"$env_file"
chmod 0600 "$env_file"
startup_complete=1
trap - EXIT
printf 'Local Guardian cluster is running. Source %s and run scripts/local-down.sh %s when finished.\n' "$env_file" "$run_id"
