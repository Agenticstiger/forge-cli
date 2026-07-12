# 03 · Multi-Source Join

> Join two CSV sources and aggregate them into one per-customer analytics table.

Part of the **[FLUID 5-minute quickstart series](../README.md)**. · ~5 min · builds on [02 · CSV to Data Product](../02-csv-to-data-product/).

## What this shows

- Declaring **two CSV inputs** (`customers` and `orders`) in a single build.
- A `LEFT JOIN` with `GROUP BY` to roll orders up per customer.
- Conditional aggregation (`COUNT(CASE WHEN …)`, `SUM(CASE WHEN …)`) to split completed
  vs. pending orders and compute revenue.

## Prerequisites

- The `fluid` CLI installed — see the [installation guide](../../README.md#installation).
- **Run the commands below from the repository root.**

## Run it

```bash
# from the repository root
fluid validate examples/03-multi-source-join/contract.fluid.yaml
fluid apply    examples/03-multi-source-join/contract.fluid.yaml --provider local --mode amend-and-build --yes
```

Inputs: [`customers.csv`](customers.csv) (5 customers) and [`orders.csv`](orders.csv)
(8 orders, one still `pending`).

## Expected output

```text
🔷 Build 'customer_summary' (embedded-SQL / local DuckDB)
   ✅ Completed in 0.1s — 1 action(s) executed
   📁 runtime/out/customer-analytics-v1.csv
```

```bash
cat runtime/out/customer-analytics-v1.csv
```

```csv
customer_id,customer_name,email,segment,total_orders,completed_orders,pending_orders,total_revenue,avg_order_value,first_order_date,last_order_date
3,Charlie Brown,charlie@example.com,VIP,2,2,0,1650.0,825.0,2024-03-10,2024-03-25
5,Eve Davis,eve@example.com,Premium,1,1,0,750.0,750.0,2024-05-12,2024-05-12
2,Bob Smith,bob@example.com,Standard,1,1,0,599.0,599.0,2024-02-15,2024-02-15
1,Alice Johnson,alice@example.com,Premium,3,2,1,449.49,224.745,2024-01-15,2024-06-01
```

**5 rows**, one per customer, ordered by `total_revenue`. Note Alice: 3 total orders but
only the 2 `completed` ones count toward `total_revenue` — her `pending` order is tracked
separately in `pending_orders`.

## How it works

In [`contract.fluid.yaml`](contract.fluid.yaml):

- **`parameters.inputs[]`** declares both CSVs; FLUID registers them as the `customers`
  and `orders` tables in DuckDB.
- **`properties.sql`** `LEFT JOIN`s orders onto customers, `GROUP BY`s per customer, and
  uses `CASE` expressions so a customer with no orders still appears (with zeros).
- **`exposes[]`** publishes the joined result to `runtime/out/customer-analytics-v1.csv`.

## Next steps

- ➡️ **[04 · External SQL Files](../04-external-sql-files/)** — move the SQL into its own file.
- ⬅️ Back to the **[examples index](../README.md)**.
