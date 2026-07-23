#!/usr/bin/env bash
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ -z "${GUARDIAN_BASE_URL:-}" ]; then
  printf '%s\n' '[prerequisite] test:matrix: GUARDIAN_BASE_URL is required' >&2
  exit 2
fi
if [ -z "${GUARDIAN_SCENARIO_TOKEN:-}" ]; then
  printf '%s\n' '[prerequisite] test:matrix: GUARDIAN_SCENARIO_TOKEN is required' >&2
  exit 2
fi
if ! command -v kubectl >/dev/null 2>&1 || ! kubectl cluster-info >/dev/null 2>&1; then
  printf '%s\n' '[prerequisite] test:matrix: Kubernetes cluster access is unavailable' >&2
  exit 2
fi
if ! command -v helm >/dev/null 2>&1; then
  printf '%s\n' '[prerequisite] test:matrix: helm is unavailable' >&2
  exit 2
fi
if ! command -v curl >/dev/null 2>&1; then
  printf '%s\n' '[prerequisite] test:matrix: curl is unavailable' >&2
  exit 2
fi
if ! curl --fail --silent --show-error "${GUARDIAN_BASE_URL%/}/health" >/dev/null; then
  printf '[prerequisite] test:matrix: Guardian health endpoint is unavailable: %s\n' "$GUARDIAN_BASE_URL" >&2
  exit 2
fi

artifact_root=${GUARDIAN_MATRIX_ARTIFACT_ROOT:-"$root/artifacts/matrix"}
exec "$root/.tools/bin/uv" run --locked python -m testbeds.scenarios.matrix \
  --guardian-url "$GUARDIAN_BASE_URL" \
  --artifacts "$artifact_root"
