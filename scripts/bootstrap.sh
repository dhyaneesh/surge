#!/usr/bin/env bash
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  printf '%s\n' 'uv is required to bootstrap this repository.' >&2
  exit 1
fi

uv sync --locked
