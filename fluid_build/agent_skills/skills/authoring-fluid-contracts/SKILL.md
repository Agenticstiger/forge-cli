---
name: authoring-fluid-contracts
description: Creates or changes a FLUID data product contract (contract.fluid.yaml) with the fluid CLI, then runs fluid validate until it is clean. Use when asked to build, scaffold, define, edit, refine or fix a data product, its exposes, column schema, bindings (local files, DuckDB, Snowflake, BigQuery or GCP, AWS), schedule, data quality rules or owner, or its SDP, ADP or CDP (Bronze, Silver or Gold) classification.
license: Apache-2.0
compatibility: Needs the fluid CLI on PATH (pip install data-product-forge).
---

# Authoring FLUID contracts

`fluid validate` decides what is valid. Do not guess a field or a value: look it up in
[references/contract-schema.md](references/contract-schema.md), edit, validate, repeat.

## Workflow

Copy this checklist and tick it off as you go:

- [ ] Read the inputs: the request, the first lines of each sample data file and any
      existing contract. Column names and types come from them, never from imagination.
- [ ] New product: scaffold a valid skeleton (offline, no LLM), then edit it. Existing
      product: edit only the lines the request changes and leave every other field as it is.
- [ ] Replace every starter value (table below).
- [ ] Validate. Fix the first reported path, validate again, until it prints
      `Valid FLUID contract`.
- [ ] Report what you built, each assumption you made, and the final validate line.

```bash
fluid forge --template starter --non-interactive --yes --target-dir . --no-memory
fluid validate contract.fluid.yaml --offline
```

Never run a bare `fluid forge`: it opens an interactive menu you cannot answer. Do not ask
`fluid forge` to write the contract for you with an LLM: you are the author.

## Starter values to replace

| Field | Starter writes | Write instead |
|---|---|---|
| `id` | `starter-product.starter` | `<domain>.<name>`, for example `sales.orders_raw` |
| `name`, `description`, `domain` | starter text | what the request says |
| `metadata.owner.team` | `data-team` | the team the request names |
| `metadata.owner.email` | `data-team@company.com` | the email the request gives, otherwise delete the key |
| `builds[0]` | `SELECT 1 AS id, ...` | the real build, or delete `builds` |
| `exposes[0]` | `starter_table` | the real output: exposeId, binding, columns |
| `exposes[0].semantics` | starter metrics | delete it unless a semantic model is asked for |

Never invent an owner, team, email, column, source or business rule. When the request
leaves something open, pick the smallest valid option and list it as an assumption.

## Classification

Set both `metadata.productType` and `metadata.layer`; they must agree:

- `SDP` with `Bronze`: raw data from one source, no joins or business logic.
- `ADP` with `Silver`: joined, cleaned or conformed data built on other products.
- `CDP` with `Gold`: shaped for a consumer: marts, metrics, dashboards, features.

A product that reads other data products declares them in `consumes[]`; see the
composing-fluid-data-products skill.

## Bindings

`exposes[].binding` says where the output lives. Use the block for the target platform:

```yaml
# exposes[].binding
platform: local
format: parquet
location:
  path: runtime/out/orders_raw.parquet
```

```yaml
# exposes[].binding
platform: snowflake
format: snowflake_table
location:
  database: SALES_RAW
  schema: PUBLIC
  table: ORDERS
```

```yaml
# exposes[].binding
platform: gcp
format: bigquery_table
location:
  project: acme-analytics
  dataset: sales_raw
  table: orders
```

There is no DuckDB format. A local DuckDB build writes a file, so bind it as `parquet`
(or `csv`). Use `format: other` only when no listed format fits.

## Columns

Columns live in `exposes[].contract.schema[]`. Mark personal data with `sensitivity`.

```yaml
# exposes[].contract.schema[]
- name: order_id
  type: string
  required: true
  description: Order identifier from the source system.
- name: customer_email
  type: string
  required: false
  sensitivity: pii
- name: amount
  type: decimal(12,2)
  required: true
```

## Quality rules

Checks that `fluid` runs go in `exposes[].contract.dq.rules[]`. This is "order_id is
unique and customer_id is never null":

```yaml
# exposes[].contract.dq
rules:
- id: order_id_unique
  type: uniqueness
  selector: order_id
  threshold: 1.0
  operator: '>='
  severity: error
- id: customer_id_not_null
  type: completeness
  selector: customer_id
  threshold: 1.0
  operator: '>='
  severity: error
```

## Builds and schedules

A local build is SQL that DuckDB runs from the contract's directory. A schedule is a
five-field cron on the build's trigger:

```yaml
# builds[]
- id: ingest_orders
  pattern: embedded-logic
  engine: sql
  properties:
    sql: SELECT * FROM read_csv_auto('data/orders.csv', header=true)
  execution:
    trigger:
      type: schedule
      cron: 0 2 * * *
    runtime:
      platform: local
```

## A complete example

[references/example.contract.fluid.yaml](references/example.contract.fluid.yaml) is a
valid source-aligned product with all of the parts above.
