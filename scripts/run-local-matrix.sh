#!/usr/bin/env bash
set -euo pipefail

[ "${1:-}" = --full ] || {
  printf '%s\n' '[usage] run-local-matrix.sh --full' >&2
  exit 64
}
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_id=${GUARDIAN_CLUSTER_RUN_ID:-$(date +%Y%m%d%H%M%S)}
. "$root/scripts/lib/guardian-run-id.sh"
if ! guardian_validate_run_id "$run_id"; then
  printf '[usage] invalid GUARDIAN_CLUSTER_RUN_ID: %s\n' "$run_id" >&2
  exit 64
fi

export GUARDIAN_CLUSTER_RUN_ID=$run_id
cleanup() {
  "$root/scripts/local-down.sh" "$run_id"
}
trap cleanup EXIT

"$root/scripts/local-up.sh"
env_file="$root/artifacts/local/${run_id}/env"
[ -f "$env_file" ] || {
  printf '[prerequisite] local environment file is missing: %s\n' "$env_file" >&2
  exit 2
}
# shellcheck disable=SC1090
set -a
. "$env_file"
set +a
task test:matrix
