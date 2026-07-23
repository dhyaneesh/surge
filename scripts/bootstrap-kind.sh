#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
kind_version=v0.31.0
kind_sha256=eb244cbafcc157dff60cf68693c14c9a75c4e6e6fedaf9cd71c58117cb93e3fa
kind_url="https://github.com/kubernetes-sigs/kind/releases/download/v0.31.0/kind-linux-amd64"
destination="$root/.tools/bin/kind"

if [ -x "$destination" ] && "$destination" --version 2>/dev/null | grep -Fq "$kind_version"; then
  exit 0
fi
if [ -e "$destination" ]; then
  printf '%s\n' "[prerequisite] existing kind is not pinned to ${kind_version}; refusing overwrite" >&2
  exit 2
fi

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
curl --fail --location --silent --show-error "$kind_url" --output "$tmpdir/kind"
printf '%s  %s\n' "$kind_sha256" "$tmpdir/kind" | sha256sum --check --status
chmod 0755 "$tmpdir/kind"
mkdir -p "$(dirname "$destination")"
mv "$tmpdir/kind" "$destination"
"$destination" --version | grep -Fq "$kind_version"
