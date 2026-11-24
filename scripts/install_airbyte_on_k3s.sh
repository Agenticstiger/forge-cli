#!/usr/bin/env bash
# Install the Airbyte OSS Helm chart onto the k3s sidecar started by
# scripts/verify_compose_airbyte.yml. Polls until /api/v1/health = 200,
# then prints the in-cluster URL the verify runner can hit.
#
# Run on the host (not inside the verify container). Requires `docker`.
# kubectl + helm are exec'd inside the k3s container so the host
# doesn't need them installed.
set -euo pipefail

K3S=forge-verify-k3s
NAMESPACE=airbyte
RELEASE=airbyte
CHART_VERSION="${AIRBYTE_CHART_VERSION:-0.422.0}"
# Helm 3 line (the airbyte chart is installed with `helm upgrade --install`).
# A constant, not an environment override: the SHA-256 values in the helm
# install step below belong to exactly this version. Bumping it means
# re-pinning BOTH hashes (linux-amd64 and linux-arm64) from the release page.
HELM_VERSION=v3.22.1

echo "── waiting for k3s control plane ──"
until docker exec "$K3S" kubectl get nodes 2>/dev/null | grep -q " Ready "; do
  sleep 3
done
echo "k3s ready"

echo "── helm install (${HELM_VERSION}) ──"
# Download the pinned Helm release tarball from get.helm.sh and check it
# against the SHA-256 pinned in this script (not one fetched from the same
# host), and only then unpack and install it. Nothing downloaded is executed
# before it is verified. Runs inside the k3s container (Alpine, so busybox
# sha256sum); shasum covers a macOS shell.
# shellcheck disable=SC2016  # the script is deliberately expanded in the container
docker exec -e HELM_VERSION="$HELM_VERSION" "$K3S" sh -c '
  set -eu
  if helm version >/dev/null 2>&1; then
    exit 0
  fi
  apk add --no-cache curl >/dev/null
  case "$(uname -m)" in
    x86_64)
      arch=amd64
      sha=896cf63684a42a723d61c980369d6e588330927deb13676ca0d85006dd6da0ea
      ;;
    aarch64)
      arch=arm64
      sha=4c29e286b279d170fa048ddfc3e4e83dcd069ca9c52102766ba672d7149e694d
      ;;
    *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
  esac
  archive="helm-${HELM_VERSION}-linux-${arch}.tar.gz"
  workdir="$(mktemp -d)"
  trap '"'"'rm -rf "$workdir"'"'"' EXIT
  cd "$workdir"
  curl -fsSLO "https://get.helm.sh/${archive}"
  if command -v sha256sum >/dev/null 2>&1; then
    echo "${sha}  ${archive}" | sha256sum -c -
  else
    echo "${sha}  ${archive}" | shasum -a 256 -c -
  fi
  tar -xzf "$archive"
  mv "linux-${arch}/helm" /usr/local/bin/helm
  chmod 0755 /usr/local/bin/helm
  helm version --short
'
echo "── helm repo add ──"
docker exec "$K3S" helm repo add airbyte https://airbytehq.github.io/helm-charts >/dev/null 2>&1 || true
docker exec "$K3S" helm repo update >/dev/null

echo "── helm install airbyte (chart v$CHART_VERSION) ──"
docker exec "$K3S" kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml \
  | docker exec -i "$K3S" kubectl apply -f -
# Minimal values: disable webapp + minio (we only need the REST API);
# expose airbyte-server on a NodePort so the verify network can reach it.
docker exec -i "$K3S" sh -c "cat > /tmp/airbyte-values.yaml" <<'YAML'
global:
  deploymentMode: oss
webapp:
  enabled: false
metrics:
  enabled: false
worker:
  replicaCount: 1
server:
  service:
    type: NodePort
    nodePort: 30080
postgresql:
  enabled: true
minio:
  enabled: false
YAML
docker exec "$K3S" helm upgrade --install "$RELEASE" airbyte/airbyte \
  --namespace "$NAMESPACE" \
  --version "$CHART_VERSION" \
  --values /tmp/airbyte-values.yaml \
  --wait --timeout 10m

echo "── waiting for airbyte-server pod to be Ready ──"
docker exec "$K3S" kubectl -n "$NAMESPACE" rollout status deploy/airbyte-server --timeout=10m

echo "── probing /api/v1/health ──"
ELAPSED=0
until [ "$(docker exec "$K3S" wget -qO- http://localhost:30080/api/v1/health 2>/dev/null | grep -c "available\|true")" -ge 1 ] || [ $ELAPSED -ge 300 ]; do
  ELAPSED=$((ELAPSED+5))
  sleep 5
done
if [ $ELAPSED -ge 300 ]; then
  echo "✗ airbyte-server did not become healthy within 5 minutes"
  docker exec "$K3S" kubectl -n "$NAMESPACE" get pods
  exit 1
fi
echo "✓ airbyte-server healthy"
echo
echo "Reachable from inside forge-verify_verify network at:"
echo "    http://k3s:30080"
echo
echo "Run the airbyte engine smoke against real OSS:"
echo "    AIRBYTE_SERVER_URL=http://k3s:30080 docker run --rm \\"
echo "      --network forge-verify_verify \\"
echo "      -v \"\$PWD:/repo\" -w /repo --entrypoint bash forge-verify \\"
echo "      scripts/verify_engines_airbyte_real.sh"
