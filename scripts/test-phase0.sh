#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_id=${1:-${GUARDIAN_CLUSTER_RUN_ID:?provide a run ID or GUARDIAN_CLUSTER_RUN_ID}}
. "$root/scripts/lib/guardian-run-id.sh"
if ! guardian_validate_run_id "$run_id"; then
  printf '[usage] invalid run ID: %s\n' "$run_id" >&2
  exit 64
fi

env_file="$root/artifacts/local/${run_id}/env"
[ -f "$env_file" ] || { printf '%s\n' '[prerequisite] local environment is not running' >&2; exit 2; }
# shellcheck disable=SC1090
. "$env_file"
export KUBECONFIG GUARDIAN_BASE_URL GUARDIAN_SCENARIO_TOKEN

# Phase 0 smoke: nonempty replay gate + one real environment E2E.
# Full five-env matrix remains scripts/run-local-matrix.sh / test:matrix.
# Scenario reset or cleanup failure exits nonzero; set -e stops later work.
"$root/.tools/bin/task" test:replay
"$root/.tools/bin/task" test:env ENV=otel-demo
