# 02 · CSV to Data Product

> Ingest a CSV, clean and normalize it with SQL, and publish a typed data product.

Part of the **[FLUID 5-minute quickstart series](../README.md)**. · ~5 min · builds on [01 · Hello World](../01-hello-world/).

## What this shows

- Declaring a **CSV input** with a typed schema under `builds[].properties.parameters.inputs`.
- Cleaning data in SQL: `UPPER(name)`, `LOWER(email)`, and a `WHERE status = 'active'` filter.
- Adding a derived `processed_at` timestamp and publishing the result as a `local` CSV.

## Prerequisites

- The `fluid` CLI installed — see the [installation guide](../../README.md#installation).
- **Run the commands below from the repository root.**

## Run it

```bash
# from the repository root
fluid validate examples/02-csv-to-data-product/contract.fluid.yaml
fluid apply    examples/02-csv-to-data-product/contract.fluid.yaml --provider local --mode amend-and-build --yes
```

The input is [`customers.csv`](customers.csv) — 5 customers, one of them `inactive`:

```csv
customer_id,name,email,signup_date,status
1,Alice Johnson,alice@example.com,2024-01-15,active
2,Bob Smith,bob@example.com,2024-02-20,active
3,Charlie Brown,charlie@example.com,2024-03-10,inactive
4,Diana Prince,diana@example.com,2024-04-05,active
5,Eve Davis,eve@example.com,2024-05-12,active
```

## Expected output

```text
🔷 Build 'clean_customers' (embedded-SQL / local DuckDB)
   ✅ Completed in 0.1s — 1 action(s) executed
   📁 runtime/out/customer-clean-v1.csv
```

```bash
cat runtime/out/customer-clean-v1.csv
```

```csv
customer_id,customer_name,email,signup_date,status,processed_at
1,ALICE JOHNSON,alice@example.com,2024-01-15,active,2026-07-12 19:37:51.064811+02
2,BOB SMITH,bob@example.com,2024-02-20,active,2026-07-12 19:37:51.064811+02
4,DIANA PRINCE,diana@example.com,2024-04-05,active,2026-07-12 19:37:51.064811+02
5,EVE DAVIS,eve@example.com,2024-05-12,active,2026-07-12 19:37:51.064811+02
```

**4 rows** — inactive Charlie is filtered out — names are upper-cased, emails lower-cased,
and each row gets a run-time `processed_at` (yours will differ).

## How it works

In [`contract.fluid.yaml`](contract.fluid.yaml):

- **`parameters.inputs[]`** points at `customers.csv` and declares its columns and types.
  FLUID loads it into DuckDB as the `customers_raw` table.
- **`properties.sql`** normalizes the fields and keeps only `status = 'active'`.
- **`exposes[]`** publishes the cleaned table to `runtime/out/customer-clean-v1.csv`
  with its own output schema.

## Next steps

- ➡️ **[03 · Multi-Source Join](../03-multi-source-join/)** — join two CSVs together.
- ⬅️ Back to the **[examples index](../README.md)**.
