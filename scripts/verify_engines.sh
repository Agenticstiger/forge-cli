#!/usr/bin/env bash
# Engine-matrix verification: exercise each acquisition runner against the
# live infra spun by scripts/verify_compose.yml, using the ENGINE-NAMESPACED
# `properties.<engine>` shape the runners actually consume.
set -u
declare -A RESULT
fail=0; pass=0

note() { echo "── $* ──"; }
ok()   { echo "  ✓ $1"; RESULT[$2]="PASS"; pass=$((pass+1)); }
miss() { echo "  ✗ $1"; RESULT[$2]="FAIL"; fail=$((fail+1)); }

run_state() {
  local d="$1"
  local rec
  rec=$(ls "$d/.fluid/runs"/*/ingest/runs/*.json 2>/dev/null | head -1)
  [ -n "$rec" ] && python -c "import json,sys;print(json.load(open(sys.argv[1])).get('state',''))" "$rec" 2>/dev/null
}

run_error() {
  local d="$1"
  local rec
  rec=$(ls "$d/.fluid/runs"/*/ingest/runs/*.json 2>/dev/null | head -1)
  [ -n "$rec" ] && python -c "import json,sys;print((json.load(open(sys.argv[1])).get('error') or '')[:200])" "$rec" 2>/dev/null
}

WORK=/repo/.verify-logs/work
rm -rf "$WORK"; mkdir -p "$WORK"
mkdir -p /repo/.verify-logs

# ── 1/6 duckdb ───────────────────────────────────────────────────────
note "1/6 duckdb (postgres → parquet)"
mkdir -p "$WORK/duckdb"
cat > "$WORK/duckdb/contract.fluid.yaml" <<'YAML'
fluidVersion: "0.7.3"
kind: DataProduct
id: bronze.duckdb_orders
name: DuckDB
domain: sales
metadata: {layer: Bronze, owner: {team: data, email: data@x}, classification: confidential, experimental: [acquisition]}
builds:
  - id: ingest
    pattern: acquisition
    engine: duckdb
    capabilities: [full_refresh]
    properties:
      source:
        kind: postgres
        connection: {host: postgres, port: 5432, database: forge, user: forge, password: forge}
        mode: full_refresh
        streams: [public.orders]
      sink: {format: parquet}
exposes:
  - exposeId: orders
    kind: table
    binding: {platform: local, format: parquet, location: {path: ./out/orders.parquet}}
    contract: {schema: [], schemaPolicy: discover_and_freeze}
YAML
( cd "$WORK/duckdb" && python -m fluid_build.cli apply --build ingest contract.fluid.yaml >/repo/.verify-logs/duckdb.out 2>&1 )
state=$(run_state "$WORK/duckdb")
[ -f "$WORK/duckdb/out/orders.parquet" ] \
  && rows=$(python -c "import duckdb;print(duckdb.sql(\"select count(*) from '$WORK/duckdb/out/orders.parquet'\").fetchone()[0])" 2>/dev/null) \
  && [ "$rows" = "5" ] \
  && ok "5 rows landed in parquet (state=$state)" duckdb \
  || miss "duckdb state=$state rows=${rows:-?} err=$(run_error "$WORK/duckdb")" duckdb

# ── 2/6 dlt ──────────────────────────────────────────────────────────
note "2/6 dlt (postgres → duckdb via dlt sql_database source)"
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
          drivername: "postgresql+psycopg"
          host: postgres
          port: 5432
          database: forge
          user: forge
          password: forge
        mode: full_refresh
        streams: [orders]
      sink: {format: parquet}
      dlt:
        destination: duckdb
        dataset_name: bronze
exposes:
  - exposeId: orders
    kind: table
    binding: {platform: local, format: parquet}
    contract: {schema: [], schemaPolicy: discover_and_freeze}
YAML
( cd "$WORK/dlt" && timeout 60 python -m fluid_build.cli apply --build ingest contract.fluid.yaml >/repo/.verify-logs/dlt.out 2>&1 )
state=$(run_state "$WORK/dlt")
if [ "$state" = "succeeded" ]; then ok "state=succeeded" dlt
else miss "state=$state err=$(run_error "$WORK/dlt")" dlt; fi

# ── 3/6 meltano ──────────────────────────────────────────────────────
note "3/6 meltano (tap-postgres embedded Singer)"
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
        kind: postgres
        connection: {host: postgres, port: 5432, database: forge, user: forge, password: forge}
        mode: full_refresh
        streams: [public-orders]
      sink: {format: parquet}
      meltano:
        tap: tap-postgres
        config:
          host: postgres
          port: 5432
          database: forge
          user: forge
          password: forge
          filter_schemas: [public]
exposes:
  - exposeId: orders
    kind: table
    binding: {platform: local, format: parquet}
    contract: {schema: [], schemaPolicy: discover_and_freeze}
YAML
( cd "$WORK/meltano" && timeout 90 python -m fluid_build.cli apply --build ingest contract.fluid.yaml >/repo/.verify-logs/meltano.out 2>&1 )
state=$(run_state "$WORK/meltano")
if [ "$state" = "succeeded" ]; then ok "state=succeeded" meltano
else miss "state=$state err=$(run_error "$WORK/meltano")" meltano; fi

# ── 4/6 airbyte ──────────────────────────────────────────────────────
note "4/6 airbyte (REST → airbyte-mock)"
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
      sink: {format: jsonl}
      airbyte:
        deployment:
          mode: bring-your-own
          server_url: "http://airbyte-mock:8000"
        source_definition_id: "decd338e-5647-4c0b-adf4-da0e75f5a750"
        destination_definition_id: "a625d593-bba5-4a1c-a53d-2d246268a155"
exposes:
  - exposeId: orders
    kind: table
    binding: {platform: local, format: jsonl}
    contract: {schema: [], schemaPolicy: discover_and_freeze}
YAML
( cd "$WORK/airbyte" && timeout 30 python -m fluid_build.cli apply --build ingest contract.fluid.yaml >/repo/.verify-logs/airbyte.out 2>&1 )
state=$(run_state "$WORK/airbyte")
if [ "$state" = "succeeded" ]; then ok "state=succeeded against mock" airbyte
else miss "state=$state err=$(run_error "$WORK/airbyte")" airbyte; fi

# ── 5/6 kafka-connect ────────────────────────────────────────────────
note "5/6 kafka-connect (live Connect REST)"
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
        mode: streaming
        streams: [public.orders]
      sink: {format: kafka, topic: bronze.orders}
      kafka-connect:
        deployment:
          server_url: "http://connect:8083"
        connector_class: "io.debezium.connector.postgresql.PostgresConnector"
exposes:
  - exposeId: orders
    kind: stream
    binding: {platform: local, format: kafka, location: {topic: bronze.orders}}
    contract: {schema: [], schemaPolicy: discover_and_freeze}
YAML
( cd "$WORK/kc" && timeout 30 python -m fluid_build.cli apply --build ingest contract.fluid.yaml >/repo/.verify-logs/kc.out 2>&1 )
state=$(run_state "$WORK/kc")
created=$(curl -fsS http://connect:8083/connectors 2>/dev/null || echo "[]")
if [ "$state" = "succeeded" ]; then ok "state=succeeded; connectors=$created" kafka-connect
else miss "state=$state err=$(run_error "$WORK/kc")" kafka-connect; fi

# ── 6/6 debezium ─────────────────────────────────────────────────────
note "6/6 debezium (CDC via live Connect)"
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
        streams: [public.orders]
      sink: {format: kafka, topic: cdc.orders}
      debezium:
        deployment:
          mode: bring-your-own
          server_url: "http://connect:8083"
        snapshot_mode: initial
exposes:
  - exposeId: orders
    kind: stream
    binding: {platform: local, format: kafka, location: {topic: cdc.orders}}
    contract: {schema: [], schemaPolicy: evolve_safe}
YAML
( cd "$WORK/dbz" && timeout 30 python -m fluid_build.cli apply --build ingest contract.fluid.yaml >/repo/.verify-logs/dbz.out 2>&1 )
state=$(run_state "$WORK/dbz")
if [ "$state" = "succeeded" ]; then ok "state=succeeded against live Connect" debezium
else miss "state=$state err=$(run_error "$WORK/dbz")" debezium; fi

echo
echo "── Engine-matrix summary ──"
for k in duckdb dlt meltano airbyte kafka-connect debezium; do
  printf "  %-15s %s\n" "$k" "${RESULT[$k]:-MISSING}"
done
echo "PASS: $pass  FAIL: $fail"
exit "$fail"
