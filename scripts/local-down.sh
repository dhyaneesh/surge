#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_id=${1:-${GUARDIAN_CLUSTER_RUN_ID:?provide a run ID or GUARDIAN_CLUSTER_RUN_ID}}
. "$root/scripts/lib/guardian-run-id.sh"
if ! guardian_validate_run_id "$run_id"; then
  printf '[usage] invalid run ID: %s\n' "$run_id" >&2
  exit 64
fi

pids_file="$root/artifacts/local/${run_id}/pids"
artifact_dir=$(dirname "$pids_file")
cluster_name="guardian-${run_id}"
kind="$root/.tools/bin/kind"

process_starttime() {
  awk '{print $22}' "/proc/${1}/stat"
}

process_fingerprint() {
  sha256sum "/proc/${1}/cmdline" | awk '{print $1}'
}

if [ -f "$pids_file" ]; then
  while read -r pid expected_starttime expected_fingerprint; do
    case "$pid:$expected_starttime:$expected_fingerprint" in
      *[!0-9a-f:]*|::) continue;;
    esac
    if [ ! -r "/proc/${pid}/stat" ] || [ ! -r "/proc/${pid}/cmdline" ]; then
      printf '[cleanup] PID identity mismatch; not signaling PID %s\n' "$pid" >&2
      continue
    fi
    if [ "$(process_starttime "$pid")" != "$expected_starttime" ] ||
      [ "$(process_fingerprint "$pid")" != "$expected_fingerprint" ]; then
      printf '[cleanup] PID identity mismatch; not signaling PID %s\n' "$pid" >&2
      continue
    fi
    kill "$pid" 2>/dev/null || true
  done <"$pids_file"
fi
if [ "${GUARDIAN_CLUSTER_RETAIN:-0}" = 1 ]; then
  printf 'Retaining owned cluster %s\n' "$cluster_name"
  exit 0
fi
"$kind" delete cluster --name "$cluster_name" || true
