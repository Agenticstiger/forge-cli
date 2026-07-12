# 04 · External SQL Files

> Keep complex transformation SQL in a standalone, reviewable `.sql` file next to the contract.

Part of the **[FLUID 5-minute quickstart series](../README.md)**. · ~5 min · builds on [03 · Multi-Source Join](../03-multi-source-join/).

## What this shows

- Organizing non-trivial logic as a readable, version-controlled SQL file —
  [`transformations/customer_revenue_analysis.sql`](transformations/customer_revenue_analysis.sql).
- A multi-step query using **CTEs** (`WITH … AS`) for monthly revenue and lifetime value.
- Deriving business fields with `CASE` (`engagement_level`, `value_segment`).

> The runnable [`contract.fluid.yaml`](contract.fluid.yaml) embeds the same query so the
> example works with a single `apply`. The `.sql` file is the human-friendly source of
> truth you edit and review — keep the two in sync when you change the logic.

## Prerequisites

- The `fluid` CLI installed — see the [installation guide](../../README.md#installation).
- **Run the commands below from the repository root.**

## Run it

```bash
# from the repository root
fluid validate examples/04-external-sql-files/contract.fluid.yaml
fluid apply    examples/04-external-sql-files/contract.fluid.yaml --provider local --mode amend-and-build --yes
```

Inputs: [`customers.csv`](customers.csv) (6 customers) and
[`transactions.csv`](transactions.csv) (10 completed transactions).

## Expected output

```text
🔷 Build 'customer_revenue_metrics' (embedded-SQL / local DuckDB)
   ✅ Completed in 0.1s — 1 action(s) executed
   📁 runtime/out/customer-revenue-analysis-v1.csv
```

```bash
cat runtime/out/customer-revenue-analysis-v1.csv
```

```csv
customer_id,customer_name,email,country,signup_date,subscription_tier,lifetime_value,avg_monthly_revenue,active_months,engagement_level,value_segment,first_revenue_month,last_revenue_month
4,Diana Prince,diana@example.com,Canada,2024-04-05,enterprise,599.98,299.99,2,Engaged,High Value,2024-04-01 00:00:00,2024-05-01 00:00:00
1,Alice Johnson,alice@example.com,USA,2024-01-15,premium,224.98,74.99333333333333,3,Engaged,Medium Value,2024-01-01 00:00:00,2024-03-01 00:00:00
3,Charlie Brown,charlie@example.com,USA,2024-03-10,premium,99.99,99.99,1,New,Low Value,2024-03-01 00:00:00,2024-03-01 00:00:00
6,Frank Miller,frank@example.com,UK,2024-06-01,premium,99.99,99.99,1,New,Low Value,2024-06-01 00:00:00,2024-06-01 00:00:00
```

**6 rows**, ordered by `lifetime_value`. Each customer gets an `engagement_level` (from
distinct active months) and a `value_segment` (from total revenue). Rows 5–6 (Bob and Eve)
follow below the head shown above.

## How it works

- [`transformations/customer_revenue_analysis.sql`](transformations/customer_revenue_analysis.sql)
  holds the query: a `monthly_revenue` CTE, a `customer_lifetime_value` CTE, and a final
  `SELECT` that `LEFT JOIN`s the metrics back onto every customer.
- [`contract.fluid.yaml`](contract.fluid.yaml) declares the two CSV inputs and embeds that
  query under `properties.sql`, then publishes to
  `runtime/out/customer-revenue-analysis-v1.csv`.

## Next steps

- ➡️ **[05 · Data Quality Validation](../05-data-quality-validation/)** — reject bad rows before they land.
- ⬅️ Back to the **[examples index](../README.md)**.
