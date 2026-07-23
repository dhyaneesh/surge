#!/usr/bin/env bash
set -eu

usage() {
  printf '%s\n' \
    '[usage] test:env: expected exactly one registered environment ID' >&2
  exit 64
}

[ "$#" -eq 1 ] || usage

case "$1" in
  otel-demo | aws-retail | online-boutique | argo-rollouts | keda-rabbitmq)
    ;;
  *)
    printf '[usage] test:env: unknown environment ID: %s\n' "$1" >&2
    exit 64
    ;;
esac

environment=$1
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ -z "${GUARDIAN_BASE_URL:-}" ]; then
  printf '%s\n' '[prerequisite] test:env: GUARDIAN_BASE_URL is required' >&2
  exit 2
fi

for command in kubectl helm curl; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf '[prerequisite] test:env: required command is unavailable: %s\n' "$command" >&2
    exit 2
  fi
done

if ! kubectl cluster-info >/dev/null 2>&1; then
  printf '%s\n' '[prerequisite] test:env: Kubernetes cluster access is unavailable' >&2
  exit 2
fi

if ! curl --fail --silent --show-error "${GUARDIAN_BASE_URL%/}/health" >/dev/null; then
  printf '[prerequisite] test:env: Guardian health endpoint is unavailable: %s\n' "$GUARDIAN_BASE_URL" >&2
  exit 2
fi

artifact_root=${GUARDIAN_SCENARIO_ARTIFACT_ROOT:-"$root/artifacts/environments/$environment"}
exec "$root/.tools/bin/uv" run --locked python -m testbeds.scenarios.environment_suite \
  --environment "$environment" \
  --guardian-url "$GUARDIAN_BASE_URL" \
  --artifacts "$artifact_root"
