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

echo "── waiting for k3s control plane ──"
until docker exec "$K3S" kubectl get nodes 2>/dev/null | grep -q " Ready "; do
  sleep 3
done
echo "k3s ready"

echo "── helm repo add ──"
docker exec "$K3S" sh -c "helm version >/dev/null 2>&1 || (apk add --no-cache curl bash && curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash)"
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
