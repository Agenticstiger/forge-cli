---
name: composing-fluid-data-products
description: Wires FLUID data products together through consumes[] so an ADP or CDP reads the outputs of upstream SDP, ADP or CDP contracts, binds those reads in the build, and validates the chain. Use when a data product joins, aggregates or builds on other data products, for source-aligned to aggregate to consumer-aligned (SDP to ADP to CDP, Bronze to Silver to Gold) pipelines, or when fluid validate reports a composition rule error.
license: Apache-2.0
compatibility: Needs the fluid CLI on PATH (pip install data-product-forge).
---

# Composing FLUID data products

A downstream product does two things for each upstream output it reads: it declares the
output in `consumes[]`, and its build reads that output's data.

## What may consume what

`fluid validate` enforces this table on `consumes[]`:

<!-- BEGIN GENERATED composition-rules -->
| productType | layer | may consume |
|---|---|---|
| `SDP` | `Bronze` | nothing (no `consumes[]`) |
| `ADP` | `Silver` | `ADP`, `SDP` |
| `CDP` | `Gold` | `ADP`, `CDP`, `SDP` |
<!-- END GENERATED composition-rules -->

## Workflow

- [ ] Open every upstream contract. Note its top-level `id`, the `exposeId` you need,
      that expose's columns, and its `binding.location`.
- [ ] Give each product its own directory under one repository, with
      `fluid.workspace.yaml` at the repository root:
      `products/orders_raw/contract.fluid.yaml`, `products/orders_clean/...`.
      `fluid validate` finds upstream contracts by scanning that tree.
- [ ] Write the downstream contract (authoring-fluid-contracts skill). Pick its
      `productType` and `layer` from the table above.
- [ ] Add one `consumes[]` entry per upstream output.
- [ ] Bind each read in the build (below) and reference only upstream columns that exist.
- [ ] Validate every contract, upstream first.

## consumes[]

```yaml
# consumes[]
- productId: sales.orders_raw
  exposeId: orders_raw
  purpose: Orders to clean and convert to cents.
```

`productId` is the upstream contract's `id`, character for character. `exposeId` must
be one of that contract's `exposes[].exposeId`. `fluid validate` checks the product
types but not the `exposeId`, so open the upstream contract and check it yourself.

## Reading upstream data in a local build

How a local SQL build reads an upstream output depends on the apply mode:

- `--mode amend`, the mode the deploying-fluid-contracts-safely skill runs, never binds
  `consumes[]`. The build reads the upstream file through
  `builds[].properties.parameters.inputs`, which registers it as a table for the SQL. In
  this mode an input without a `format` is read as CSV.
- `--mode amend-and-build` registers each `consumes[]` entry that the SQL reads by its
  `exposeId` as a view, finding the upstream contract through `fluid.workspace.yaml`. An
  explicit input still wins.

An input with a `format` works in both modes, so always write one:

```yaml
# builds[]
- id: clean_orders
  pattern: embedded-logic
  engine: sql
  properties:
    sql: SELECT order_id, customer_id, CAST(amount * 100 AS BIGINT) AS amount_cents FROM orders_raw
    parameters:
      inputs:
      - name: orders_raw
        path: ../orders_raw/runtime/out/orders_raw.parquet
        format: parquet
  execution:
    trigger:
      type: schedule
      cron: 0 3 * * *
    runtime:
      platform: local
```

The path is relative to the downstream contract's directory and points at the upstream
binding's `location.path`. Apply each product from its own directory, upstream first.

On Snowflake or BigQuery, the build reads the upstream table by the name its binding
gives (database.schema.table, or project.dataset.table).

## Fixing composition errors

| `fluid validate` says | Fix |
|---|---|
| `SDP (...) does not accept upstream products` | An SDP reads a raw source only: remove `consumes[]`, or make the product an ADP |
| `ADP accepts upstreams of type ['ADP', 'SDP'] but 'x' is CDP` | Consume the ADP that feeds that CDP, or make this product a CDP |
| `metadata.layer='Gold' and metadata.productType='SDP' are inconsistent` | Set the pair from the table above |
