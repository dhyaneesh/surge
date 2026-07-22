#!/usr/bin/env bash
set -eu

operation="preflight"
if [ "${1:-}" = "aggregate" ]; then
  operation="aggregate"
  shift
fi
target="${1:-}"
if [ -z "$target" ]; then
  echo "[prerequisite] preflight: target is required" >&2
  exit 2
fi

if [ -n "${VERIFICATION_REPO_ROOT:-}" ]; then
  repo_root="$VERIFICATION_REPO_ROOT"
else
  repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
fi
manifest="$repo_root/tools/verification-tools.yaml"
uv="$repo_root/.tools/bin/uv"

if [ ! -x "$uv" ]; then
  echo "[prerequisite] $target: missing .tools/bin/uv" >&2
  exit 2
fi

expected="$(sed -n '/^  uv:$/ {n; s/^    version: *//p;}' "$manifest")"
if ! raw_actual="$($uv --version 2>/dev/null)"; then
  echo "[prerequisite] $target: uv version unknown does not match ${expected:-unknown}" >&2
  exit 2
fi
actual="$(printf '%s\n' "$raw_actual" | sed -n 's/^uv v*\([0-9][0-9.]*\).*$/\1/p')"
if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
  echo "[prerequisite] $target: uv version ${actual:-unknown} does not match ${expected:-unknown}" >&2
  exit 2
fi

cd "$repo_root"
exec "$uv" run --locked --no-sync python -m tools.verification_harness "$operation" "$target"
