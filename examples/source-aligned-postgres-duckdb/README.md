# Source-aligned Postgres → DuckDB → Parquet

A minimal source-aligned data product (`Bronze` / `SDP`) that acquires
`public.orders` from a Postgres source via DuckDB's `postgres_scan` and
lands it as Parquet.

**Vocabulary note.** v0.7.3 carries two interchangeable classifications:
the medallion `metadata.layer` (`Bronze`/`Silver`/`Gold`) and the Data
Mesh `metadata.productType` (`SDP`/`ADP`/`CDP`). The contract below
declares both for clarity; either alone is also valid.

## Layout

| File | Purpose |
|---|---|
| `contract.fluid.yaml` | The data product contract |
| `docker-compose.yml`  | Postgres + seeded data |
| `seed.sql`            | Initial rows in `public.orders` |
| `Makefile`            | `make all` runs end-to-end |
| `verify.py`           | Asserts row count + schema after apply |

## Run

```bash
make all          # up + run + verify
# or
make up           # start Postgres
make run          # fluid validate → apply
make verify       # assert output
make down         # stop Postgres
```

The pipeline:

```
fluid validate contract.fluid.yaml
fluid apply --build ingest_orders contract.fluid.yaml
```

`apply` detects `pattern: acquisition` + `engine: duckdb`, dispatches to
the DuckDB runner, loads the `postgres` extension on demand, copies the
table to `out/orders.parquet`, and persists a run record under
`.fluid/runs/<product>/<build>/runs/<run-id>.json`.

## What this exercises

- v0.7.3 `acquisition` build pattern + `duckdb` engine
- Postgres source via `postgres_scan` (no Airbyte server needed)
- Parquet sink via `COPY (...) TO ... (FORMAT 'parquet')`
- Run-record persistence + lock acquisition
- Pre-land hook chain (`dlp_scan`, `quality_gate`) — non-blocking on this
  fixture but exercises the wiring
