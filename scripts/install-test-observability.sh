#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_id=${GUARDIAN_CLUSTER_RUN_ID:?GUARDIAN_CLUSTER_RUN_ID is required}
. "$root/scripts/lib/guardian-run-id.sh"
if ! guardian_validate_run_id "$run_id"; then
  printf '[usage] invalid GUARDIAN_CLUSTER_RUN_ID: %s\n' "$run_id" >&2
  exit 64
fi
artifact_dir="$root/artifacts/local/${run_id}"
kubeconfig="$artifact_dir/kubeconfig"
values="$root/testbeds/observability/signoz-values.yaml"
lockfile="$root/testbeds/observability/signoz-images.lock.yaml"
chart_version=v0.133.0
chart_url="https://github.com/SigNoz/charts/releases/download/signoz-0.133.0/signoz-0.133.0.tgz"
chart_sha256="103f127d1efe3e5f7c9ca87f224ce66b75bb7e688b72608530d11bcd72dbb6dc"

[ -f "$kubeconfig" ] || { printf '%s\n' '[prerequisite] isolated kubeconfig is required' >&2; exit 2; }
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
curl --fail --location --silent --show-error "$chart_url" --output "$tmpdir/signoz.tgz"
printf '%s  %s\n' "$chart_sha256" "$tmpdir/signoz.tgz" | sha256sum --check --status

rendered="$tmpdir/rendered.yaml"
KUBECONFIG="$kubeconfig" helm template guardian-observability "$tmpdir/signoz.tgz" \
  --namespace guardian-observability --values "$values" >"$rendered"
images="$tmpdir/images"
awk '/^[[:space:]]*image:/{print $2}' "$rendered" | tr -d '"' | sort -u >"$images"
[ -s "$images" ] || { printf '%s\n' '[prerequisite] chart rendered no workload images' >&2; exit 2; }

while IFS= read -r image; do
  digest=$(
    IMAGE="$image" LOCKFILE="$lockfile" python3 - <<'PY'
import os, re, sys
image = os.environ["IMAGE"]
text = open(os.environ["LOCKFILE"], encoding="utf-8").read()
# Match "  <image>: <digest>" or "  <image>: \"<digest>\"" under images:
pattern = rf"(?m)^  {re.escape(image)}:\s*\"?(sha256:[0-9a-f]+)\"?\s*$"
match = re.search(pattern, text)
if not match:
    sys.exit(1)
print(match.group(1))
PY
  ) || {
    printf '[prerequisite] rendered image is absent from lock: %s\n' "$image" >&2
    exit 2
  }
  case "$digest" in sha256:*) ;; *) printf '[prerequisite] image lock is not digest-pinned: %s\n' "$image" >&2; exit 2;; esac
done <"$images"

cat >"$tmpdir/post-renderer.py" <<'PY'
#!/usr/bin/env python3
import os
import re
import sys
from pathlib import Path

lock_text = Path(os.environ["LOCKFILE"]).read_text(encoding="utf-8")


def locked_digest(image: str) -> str:
    pattern = rf"(?m)^  {re.escape(image)}:\s*\"?(sha256:[0-9a-f]{{64}})\"?\s*$"
    match = re.search(pattern, lock_text)
    if not match:
        raise SystemExit(f"[prerequisite] rendered image is absent from lock: {image}")
    return match.group(1)


def rewrite_image(match: re.Match[str]) -> str:
    image = match.group(2)
    digest = locked_digest(image)
    repository = image.split("@", 1)[0].rsplit(":", 1)[0]
    return f"{match.group(1)}{repository}@{digest}{match.group(3)}"


text = sys.stdin.read()
rewritten = re.sub(
    r"(?m)^(\s*image:\s*[\"']?)([^ \t\"']+)([\"']?\s*(?:#.*)?)$",
    rewrite_image,
    text,
)
sys.stdout.write(rewritten)
PY
chmod 0755 "$tmpdir/post-renderer.py"

LOCKFILE="$lockfile" KUBECONFIG="$kubeconfig" helm upgrade --install guardian-observability "$tmpdir/signoz.tgz" \
  --namespace guardian-observability --create-namespace --values "$values" \
  --post-renderer "$tmpdir/post-renderer.py" --wait --timeout 10m

wait_for_component() {
  component=$1
  selector="app.kubernetes.io/component=${component}"
  KUBECONFIG="$kubeconfig" kubectl get pods -n guardian-observability \
    --selector "$selector" -o name | grep -q .
  KUBECONFIG="$kubeconfig" kubectl wait -n guardian-observability \
    --for=condition=Ready pod --selector "$selector" --timeout=10m
}
wait_for_component clickhouse
wait_for_component zookeeper
wait_for_component signoz
wait_for_component otel-collector

lock_digests="$tmpdir/lock-digests"
LOCKFILE="$lockfile" python3 - <<'PY' >"$lock_digests"
import os
import re
from pathlib import Path

print(
    "\n".join(
        sorted(
            set(
                re.findall(
                    r"(?m)^  [^:]+(?::[^:]+)?:\s*\"?(sha256:[0-9a-f]{64})\"?\s*$",
                    Path(os.environ["LOCKFILE"]).read_text(encoding="utf-8"),
                )
            )
        )
    )
)
PY
KUBECONFIG="$kubeconfig" kubectl get pods -n guardian-observability \
  -o jsonpath='{range .items[*]}{range .status.initContainerStatuses[*]}{.imageID}{"\n"}{end}{range .status.containerStatuses[*]}{.imageID}{"\n"}{end}{end}' \
  >"$tmpdir/imageIDs"
LOCK_DIGESTS="$lock_digests" IMAGE_IDS="$tmpdir/imageIDs" python3 - <<'PY'
import os
import re
import sys
from pathlib import Path

locked = set(Path(os.environ["LOCK_DIGESTS"]).read_text(encoding="utf-8").splitlines())
image_ids = Path(os.environ["IMAGE_IDS"]).read_text(encoding="utf-8").splitlines()
if not image_ids:
    raise SystemExit("[prerequisite] no live pod imageIDs were reported")
for image_id in image_ids:
    match = re.search(r"sha256:[0-9a-f]{64}", image_id)
    if not match or match.group(0) not in locked:
        raise SystemExit(f"[prerequisite] live pod image is absent from lock: {image_id}")
PY
