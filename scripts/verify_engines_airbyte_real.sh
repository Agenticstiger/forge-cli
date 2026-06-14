#!/usr/bin/env bash
# End-to-end verify the airbyte runner against a real Airbyte OSS server
# running on k3s (provisioned by scripts/install_airbyte_on_k3s.sh).
#
# Run inside the verify image, attached to the compose network:
#   AIRBYTE_SERVER_URL=http://k3s:${AIRBYTE_NODEPORT:-30229} \
#     docker run --rm --network forge-verify_verify \
#     -v "$PWD:/repo" -w /repo --entrypoint bash forge-verify \
#     scripts/verify_engines_airbyte_real.sh
#
# The verify image runs as non-root (UID 1000); on Linux hosts with a
# different UID build it with --build-arg UID=$(id -u) --build-arg GID=$(id -g)
# so the /repo/.verify-logs writes below succeed.
set -u
WORK=/repo/.verify-logs/airbyte-real
rm -rf "$WORK"; mkdir -p "$WORK"
SERVER="${AIRBYTE_SERVER_URL:-http://k3s:30229}"

echo "── 1/3 server reachable? ──"
if ! curl -fsS "$SERVER/api/v1/health" >/dev/null; then
  echo "✗ Airbyte API not reachable at $SERVER/api/v1/health"
  exit 1
fi
echo "✓ $SERVER healthy"

echo "── 2/3 fetch workspace + faker source-definition ──"
# Airbyte 1.x exposes the public API at /api/public/v1/workspaces; the
# legacy /api/v1/workspaces/list path was removed.
WORKSPACE_ID="$(curl -fsS "$SERVER/api/public/v1/workspaces" \
  | python -c 'import json,sys;print(json.load(sys.stdin)["data"][0]["workspaceId"])')"
echo "  workspaceId=${WORKSPACE_ID}"

# Source definitions still live under /api/v1/source_definitions/list
# (no workspace filter needed in 1.x — they are global).
SRC_DEF_ID="$(curl -fsS -X POST "$SERVER/api/v1/source_definitions/list" \
  -H 'Content-Type: application/json' -d '{}' \
  | python -c '
import json,sys
d=json.load(sys.stdin)
for sd in d.get("sourceDefinitions", []):
    if (sd.get("name") or "") == "Postgres":
        print(sd["sourceDefinitionId"]); break
' || true)"
if [ -z "${SRC_DEF_ID}" ]; then
  echo "  postgres source not found; runner will short-circuit"
  exit 1
fi
echo "  postgres sourceDefinitionId=${SRC_DEF_ID}"

DEST_DEF_ID="$(curl -fsS -X POST "$SERVER/api/v1/destination_definitions/list" \
  -H 'Content-Type: application/json' -d '{}' \
  | python -c '
import json,sys
d=json.load(sys.stdin)
preferred = ["Local JSON", "Local CSV"]
by_name = {x.get("name"): x for x in d.get("destinationDefinitions", [])}
for name in preferred:
    if name in by_name:
        print(by_name[name]["destinationDefinitionId"]); break
else:
    print(d["destinationDefinitions"][0]["destinationDefinitionId"])
')"
echo "  destinationDefinitionId=${DEST_DEF_ID} (Local JSON if available)"

echo "── 3/3 dispatch the airbyte runner against the real server ──"
mkdir -p "$WORK"
cat > "$WORK/contract.fluid.yaml" <<YAML
fluidVersion: "0.7.3"
kind: DataProduct
id: bronze.airbyte_real
name: Airbyte Real
domain: sales
metadata: {layer: Bronze, productType: SDP, owner: {team: data, email: data@x}}
builds:
  - id: ingest
    pattern: acquisition
    engine: airbyte
    capabilities: [full_refresh]
    properties:
      source:
        kind: postgres
        connection:
          # Connector pods inside k3s can't reach Docker DNS;
          # ``external-postgres`` is the k8s Service we registered to
          # forge-verify-pg's bridge IP via Endpoints.
          host: external-postgres.airbyte.svc.cluster.local
          port: 5432
          database: forge
          username: forge
          password: forge
          schemas: [public]
          ssl_mode: {mode: disable}
          replication_method: {method: Standard}
          tunnel_method: {tunnel_method: NO_TUNNEL}
        mode: full_refresh
        streams: [public.orders]
      sink: {format: jsonl}
      airbyte:
        deployment:
          mode: bring-your-own
          server_url: "${SERVER}"
          poll_interval_seconds: 3
          job_timeout_seconds: 300
        workspace_id: "${WORKSPACE_ID}"
        source_definition_id: "${SRC_DEF_ID}"
        destination_definition_id: "${DEST_DEF_ID}"
        # Local JSON destination needs a /local-rooted path; the runner
        # builds this from binding.location.path automatically, but we
        # pin it explicitly here so the smoke is deterministic.
        destination_config:
          destination_path: "/local/forge_real"
exposes:
  - exposeId: users
    kind: table
    binding: {platform: local, format: jsonl, location: {path: /tmp/airbyte_local}}
    contract: {schema: [], schemaPolicy: discover_and_freeze}
YAML

cd "$WORK"
timeout 360 python -m fluid_build.cli apply --build ingest contract.fluid.yaml 2>&1 | tail -15
echo
echo "── run record ──"
cat .fluid/runs/*/ingest/runs/*.json 2>/dev/null \
  | python -c 'import sys,json;d=json.load(sys.stdin);print("state=",d.get("state"),"records_total=",d.get("records_total"),"final_status=",d["facets"].get("final_status"))'
