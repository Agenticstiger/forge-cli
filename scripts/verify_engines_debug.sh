#!/usr/bin/env bash
# Verbose debug variant — captures full stdout+stderr per engine and the
# resulting run-record JSON if any.
set -u
WORK=/tmp/engine_debug; rm -rf "$WORK"; mkdir -p "$WORK"

show_record() {
  local p="$1"
  local rec
  rec=$(ls "$p/.fluid/runs"/*/ingest/runs/*.json 2>/dev/null | head -1)
  if [ -n "$rec" ]; then
    echo "── run record $rec ──"
    python -c "import json,sys;d=json.load(open(sys.argv[1]));print(json.dumps(d,indent=2,default=str))" "$rec" | head -40
  else
    echo "── no run record produced ──"
  fi
}

run_one() {
  local name="$1" dir="$2"
  echo
  echo "════════════════════ $name ════════════════════"
  ( cd "$dir" && python -m fluid_build.cli --log-level DEBUG apply --build ingest contract.fluid.yaml 2>&1 ) | tail -50
  show_record "$dir"
}

# 2. dlt
mkdir -p "$WORK/dlt"
cat > "$WORK/dlt/contract.fluid.yaml" <<'YAML'
fluidVersion: "0.7.3"
kind: DataProduct
id: bronze.dlt_orders
name: dlt
domain: sales
metadata: {layer: Bronze, owner: {team: data, email: data@x}, classification: confidential, experimental: [acquisition]}
builds:
  - id: ingest
    pattern: acquisition
    engine: dlt
    capabilities: [full_refresh]
    properties:
      source:
        kind: sql_database
        connection:
          credentials: "postgresql://forge:forge@postgres:5432/forge"
        mode: full_refresh
        streams: [orders]
      sink:
        kind: duckdb
        path: ./out/dlt.duckdb
exposes:
  - exposeId: orders
    kind: table
    binding: {platform: local, format: duckdb, location: {path: ./out/dlt.duckdb}}
    contract: {schema: [], schemaPolicy: discover_and_freeze}
YAML
run_one "dlt" "$WORK/dlt"

# 3. meltano
mkdir -p "$WORK/meltano"
cat > "$WORK/meltano/contract.fluid.yaml" <<'YAML'
fluidVersion: "0.7.3"
kind: DataProduct
id: bronze.meltano_orders
name: Meltano
domain: sales
metadata: {layer: Bronze, owner: {team: data, email: data@x}, classification: confidential, experimental: [acquisition]}
builds:
  - id: ingest
    pattern: acquisition
    engine: meltano
    capabilities: [full_refresh]
    properties:
      source:
        kind: singer
        tap: tap-postgres
        config:
          host: postgres
          port: 5432
          database: forge
          user: forge
          password: forge
          filter_schemas: [public]
        mode: full_refresh
        streams: [public-orders]
      sink: {format: parquet}
exposes:
  - exposeId: orders
    kind: table
    binding: {platform: local, format: parquet, location: {path: ./out/meltano.parquet}}
    contract: {schema: [], schemaPolicy: discover_and_freeze}
YAML
run_one "meltano" "$WORK/meltano"

# 4. airbyte
mkdir -p "$WORK/airbyte"
cat > "$WORK/airbyte/contract.fluid.yaml" <<'YAML'
fluidVersion: "0.7.3"
kind: DataProduct
id: bronze.airbyte_orders
name: Airbyte
domain: sales
metadata: {layer: Bronze, owner: {team: data, email: data@x}, classification: confidential, experimental: [acquisition]}
builds:
  - id: ingest
    pattern: acquisition
    engine: airbyte
    capabilities: [full_refresh]
    properties:
      source:
        kind: postgres
        connection: {host: postgres, port: 5432, database: forge, user: forge, password: forge}
        mode: full_refresh
        streams: [orders]
      sink: {kind: jsonl}
      deployment:
        mode: bring-your-own
        server_url: "http://airbyte-mock:8000/api/v1"
        source_definition_id: "decd338e-5647-4c0b-adf4-da0e75f5a750"
        destination_definition_id: "a625d593-bba5-4a1c-a53d-2d246268a155"
exposes:
  - exposeId: orders
    kind: table
    binding: {platform: local, format: jsonl, location: {path: ./out/airbyte.jsonl}}
    contract: {schema: [], schemaPolicy: discover_and_freeze}
YAML
run_one "airbyte" "$WORK/airbyte"

# 5. kafka-connect
mkdir -p "$WORK/kc"
cat > "$WORK/kc/contract.fluid.yaml" <<'YAML'
fluidVersion: "0.7.3"
kind: DataProduct
id: bronze.kc_orders
name: KC
domain: sales
metadata: {layer: Bronze, owner: {team: data, email: data@x}, classification: confidential, experimental: [acquisition]}
builds:
  - id: ingest
    pattern: acquisition
    engine: kafka-connect
    capabilities: [streaming, at_least_once]
    properties:
      source:
        kind: postgres
        connection: {host: postgres, port: 5432, database: forge, user: forge, password: forge}
        mode: full_refresh
        streams: [public.orders]
      sink: {kind: kafka, topic: bronze.orders}
      deployment:
        server_url: "http://connect:8083"
        connector_class: "io.debezium.connector.postgresql.PostgresConnector"
exposes:
  - exposeId: orders
    kind: stream
    binding: {platform: local, format: kafka, location: {topic: bronze.orders}}
    contract: {schema: [], schemaPolicy: discover_and_freeze}
YAML
run_one "kafka-connect" "$WORK/kc"

# 6. debezium — try mode=full_refresh since "incremental" was rejected
mkdir -p "$WORK/dbz"
cat > "$WORK/dbz/contract.fluid.yaml" <<'YAML'
fluidVersion: "0.7.3"
kind: DataProduct
id: bronze.dbz_orders
name: DBZ
domain: sales
metadata: {layer: Bronze, owner: {team: data, email: data@x}, classification: confidential, experimental: [acquisition]}
builds:
  - id: ingest
    pattern: acquisition
    engine: debezium
    capabilities: [streaming, cdc, at_least_once]
    properties:
      source:
        kind: postgres
        connection: {host: postgres, port: 5432, database: forge, user: forge, password: forge}
        mode: cdc
        snapshotMode: initial
        streams: [public.orders]
      sink: {kind: kafka, topic: cdc.orders}
      deployment:
        mode: kafka-connect
        server_url: "http://connect:8083"
exposes:
  - exposeId: orders
    kind: stream
    binding: {platform: local, format: kafka, location: {topic: cdc.orders}}
    contract: {schema: [], schemaPolicy: evolve_safe}
YAML
run_one "debezium" "$WORK/dbz"
