#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
run_id=${GUARDIAN_CLUSTER_RUN_ID:?GUARDIAN_CLUSTER_RUN_ID is required}
. "$root/scripts/lib/guardian-run-id.sh"
if ! guardian_validate_run_id "$run_id"; then
  printf '[usage] invalid GUARDIAN_CLUSTER_RUN_ID: %s\n' "$run_id" >&2
  exit 64
fi
cluster_name="guardian-${run_id}"
kubeconfig="$root/artifacts/local/${run_id}/kubeconfig"
artifact_dir=$(dirname "$kubeconfig")
kind="$root/.tools/bin/kind"
node_image="kindest/node:v1.35.0@sha256:452d707d4862f52530247495d180205e029056831160e22870e37e3f6c1ac31f"
metrics_url="https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.9.0/components.yaml"
metrics_sha256="1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b"
created=0

cleanup_partial() {
  if [ "$created" -eq 1 ]; then
    "$kind" delete cluster --name "$cluster_name" || true
  fi
}
trap cleanup_partial ERR

if ! docker info >/dev/null 2>&1; then
  printf '%s\n' \
    '[prerequisite] Docker Desktop is unavailable. Start Docker Desktop on Windows, then enable Settings > Resources > WSL Integration for this distribution and retry.' \
    >&2
  exit 2
fi
available_memory_bytes=$(docker info --format '{{.MemTotal}}')
available_disk_bytes=$(docker system df --format '{{json .}}' >/dev/null 2>&1 && df -Pk "$root" | awk 'NR == 2 { print $4 * 1024 }')
[ "$available_memory_bytes" -ge 2684354560 ]
[ "$available_disk_bytes" -ge 12884901888 ]

mkdir -p "$artifact_dir"
printf '{"dockerMemoryBytes":%s,"availableDiskBytes":%s}\n' \
  "$available_memory_bytes" "$available_disk_bytes" >"$artifact_dir/preflight.json"
if "$kind" get clusters | grep -Fxq "$cluster_name"; then
  printf '[prerequisite] cluster already exists: %s\n' "$cluster_name" >&2
  exit 2
fi
[ ! -e "$kubeconfig" ] || { printf '%s\n' '[prerequisite] run artifact kubeconfig already exists' >&2; exit 2; }

"$kind" create cluster --name "$cluster_name" --image "$node_image" --kubeconfig "$kubeconfig"
created=1
docker update --memory 6g --memory-swap 8g "${cluster_name}-control-plane"
docker inspect "${cluster_name}-control-plane" --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}' | grep -Fxq '6442450944 8589934592'

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"; cleanup_partial' ERR EXIT
curl --fail --location --silent --show-error "$metrics_url" --output "$tmpdir/components.yaml"
printf '%s  %s\n' "$metrics_sha256" "$tmpdir/components.yaml" | sha256sum --check --status
sed -i '/- --secure-port=4443/a\        - --kubelet-insecure-tls' "$tmpdir/components.yaml"
KUBECONFIG="$kubeconfig" kubectl apply -f "$tmpdir/components.yaml"
KUBECONFIG="$kubeconfig" kubectl wait --for=condition=Available apiservice/v1beta1.metrics.k8s.io --timeout=120s
trap - ERR EXIT
created=0
