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

printf '[baseline] test:env: no tests are configured (%s)\n' "$1" >&2
exit 3
