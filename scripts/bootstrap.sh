#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf '[prerequisite] %s\n' "$*" >&2
  exit 1
}

if [[ "${BOOTSTRAP_TEST_MODE:-}" == "1" ]]; then
  repo_root="${BOOTSTRAP_TEST_REPO_ROOT:?test repository root is required}"
  manifest="${BOOTSTRAP_TEST_MANIFEST:?test manifest is required}"
  tools_dir="${BOOTSTRAP_TEST_TOOLS_DIR:?test tools directory is required}"
  platform_os="${BOOTSTRAP_TEST_OS:?test OS is required}"
  platform_arch="${BOOTSTRAP_TEST_ARCH:?test architecture is required}"
  artifact_base="${BOOTSTRAP_TEST_ARTIFACT_BASE:?test artifact base is required}"
else
  repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
  manifest="$repo_root/tools/verification-tools.yaml"
  tools_dir="$repo_root/.tools"
  platform_os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  platform_arch="$(uname -m)"
  artifact_base=""
fi

[[ "$platform_os" == "linux" ]] || fail "unsupported operating system: $platform_os (expected linux)"
case "$platform_arch" in
  amd64|x86_64) ;;
  *) fail "unsupported architecture: $platform_arch (expected amd64)" ;;
esac

# Check the complete host boundary before creating directories or downloading.
for prerequisite in bash curl tar; do
  command -v "$prerequisite" >/dev/null 2>&1 || fail "missing host $prerequisite"
done
if command -v sha256sum >/dev/null 2>&1; then
  checksum_command=sha256sum
elif command -v shasum >/dev/null 2>&1; then
  checksum_command=shasum
else
  fail "missing host checksum utility (expected sha256sum or shasum -a 256)"
fi

[[ -r "$manifest" ]] || fail "cannot read tool manifest: $manifest"

tool_field() {
  local tool="$1" field="$2"
  awk -v tool="$tool" -v field="$field" '
    $0 == "  " tool ":" { in_tool=1; next }
    in_tool && /^  [^ ]/ { exit }
    in_tool && $0 ~ "^    " field ":" {
      sub("^    " field ":[[:space:]]*", "")
      gsub(/^"|"$/, "")
      print
      exit
    }
  ' "$manifest"
}

for tool in task uv; do
  version="$(tool_field "$tool" version)"
  url="$(tool_field "$tool" url)"
  checksum="$(tool_field "$tool" sha256)"
  [[ -n "$version" && -n "$url" && "$checksum" =~ ^[0-9a-f]{64}$ ]] || \
    fail "invalid $tool metadata in $manifest"
  printf -v "${tool}_version" '%s' "$version"
  printf -v "${tool}_url" '%s' "$url"
  printf -v "${tool}_checksum" '%s' "$checksum"
done

mkdir -p "$tools_dir/bin"
temp_dir="$(mktemp -d "$tools_dir/.bootstrap.XXXXXXXX")"
case "$temp_dir" in
  "$tools_dir"/.bootstrap.*) ;;
  *) fail "temporary directory is outside the tools directory" ;;
esac
cleanup() {
  case "${temp_dir:-}" in
    "$tools_dir"/.bootstrap.*) rm -rf -- "$temp_dir" ;;
  esac
}
trap cleanup EXIT HUP INT TERM

reported_version() {
  local tool="$1" executable="$2"
  "$executable" --version 2>/dev/null | sed -n \
    's/.*[^0-9]\([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' | sed -n '1p'
}

install_tool() {
  local tool="$1" expected_version="$2" url="$3" expected_checksum="$4"
  local final="$tools_dir/bin/$tool" archive="$temp_dir/$tool.tar.gz"
  local extracted="$temp_dir/$tool-extracted" candidate member actual_checksum source_url

  if [[ -x "$final" && "$(reported_version "$tool" "$final")" == "$expected_version" ]]; then
    return
  fi

  if [[ -n "$artifact_base" ]]; then
    source_url="file://$artifact_base/${url##*/}"
  else
    source_url="$url"
  fi
  curl -fsSL --output "$archive" "$source_url" || fail "download failed for $tool"
  if [[ "$checksum_command" == sha256sum ]]; then
    actual_checksum="$(sha256sum "$archive" | awk '{print $1}')"
  else
    actual_checksum="$(shasum -a 256 "$archive" | awk '{print $1}')"
  fi
  [[ "$actual_checksum" == "$expected_checksum" ]] || fail "checksum mismatch for $tool"

  mkdir "$extracted"
  if [[ "$tool" == task ]]; then
    member=task
  else
    member=uv-x86_64-unknown-linux-gnu/uv
  fi
  tar -xzf "$archive" -C "$extracted" -- "$member" || fail "cannot extract $tool"
  candidate="$extracted/$member"
  [[ -f "$candidate" ]] || fail "archive does not contain expected $tool executable"
  chmod 0755 "$candidate"
  [[ "$(reported_version "$tool" "$candidate")" == "$expected_version" ]] || \
    fail "$tool archive version does not match $expected_version"
  mv -f -- "$candidate" "$final"
}

install_tool task "$task_version" "$task_url" "$task_checksum"
install_tool uv "$uv_version" "$uv_url" "$uv_checksum"

cd "$repo_root"
"$tools_dir/bin/uv" sync --locked
VERIFICATION_REPO_ROOT="$repo_root" "$repo_root/scripts/verification-preflight.sh" manifest-check
printf '%s\n' 'Bootstrap complete. Run verification with:'
printf '%s\n' '.tools/bin/task <target>'
