# 05 · Data Quality Validation

> Drop invalid rows at the source using SQL guard clauses, so only clean data is published.

Part of the **[FLUID 5-minute quickstart series](../README.md)**. · ~5 min · builds on [04 · External SQL Files](../04-external-sql-files/).

## What this shows

- Enforcing data quality inside the transformation with a `WHERE` clause:
  not-null keys, positive amounts, and allow-listed categorical values.
- Adding a `validated_at` audit timestamp to every row that passes.

## Prerequisites

- The `fluid` CLI installed — see the [installation guide](../../README.md#installation).
- **Run the commands below from the repository root.**

## Run it

```bash
# from the repository root
fluid validate examples/05-data-quality-validation/contract.fluid.yaml
fluid apply    examples/05-data-quality-validation/contract.fluid.yaml --provider local --mode amend-and-build --yes
```

The input [`sales_data.csv`](sales_data.csv) deliberately contains 8 rows, 5 of which are
bad (negative amount, null key, null date, zero amount, invalid region):

```csv
order_id,customer_id,order_date,amount,region,status
1001,C001,2024-01-15,299.99,North,completed
1002,C002,2024-01-16,-50.00,South,completed
1003,C003,2024-01-17,1499.99,East,completed
1004,,2024-01-18,89.99,West,completed
1005,C005,2024-01-19,0.00,North,pending
1006,C006,,199.99,South,completed
1007,C007,2024-01-21,75.00,InvalidRegion,completed
1008,C008,2024-01-22,299.99,North,completed
```

## Expected output

```text
🔷 Build 'clean_sales' (embedded-SQL / local DuckDB)
   ✅ Completed in 0.1s — 1 action(s) executed
   📁 runtime/out/validated-sales-v1.csv
```

```bash
cat runtime/out/validated-sales-v1.csv
```

```csv
order_id,customer_id,order_date,amount,region,status,validated_at
1001,C001,2024-01-15,299.99,North,completed,2026-07-12 19:40:23.420659+02
1003,C003,2024-01-17,1499.99,East,completed,2026-07-12 19:40:23.420659+02
1008,C008,2024-01-22,299.99,North,completed,2026-07-12 19:40:23.420659+02
```

**Only 3 rows survive** (`1001`, `1003`, `1008`). The five rejects and why:

| order | reason dropped |
|-------|----------------|
| 1002 | `amount` is negative (`-50.00`) |
| 1004 | `customer_id` is null |
| 1005 | `amount` is `0.00` (must be `> 0`) |
| 1006 | `order_date` is null |
| 1007 | `region` is not in `('North','South','East','West')` |

## How it works

In [`contract.fluid.yaml`](contract.fluid.yaml), the `WHERE` clause is the quality gate:

```sql
WHERE customer_id IS NOT NULL
  AND order_date  IS NOT NULL
  AND amount > 0
  AND region IN ('North', 'South', 'East', 'West')
  AND status IN ('pending', 'completed', 'cancelled')
```

Because the input schema marks `customer_id` and `order_date` as `required: false`, the
raw CSV loads cleanly and the SQL — not the loader — decides what is publishable. The
survivors are written to `runtime/out/validated-sales-v1.csv`.

## Next steps

- ➡️ **[06 · Time Windows](../06-time-windows/)** — rolling averages and period-over-period math.
- ⬅️ Back to the **[examples index](../README.md)**.
