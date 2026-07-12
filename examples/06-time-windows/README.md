# 06 · Time Windows

> Turn raw line items into a daily time series with rolling averages and period-over-period metrics.

Part of the **[FLUID 5-minute quickstart series](../README.md)**. · ~5 min · builds on [05 · Data Quality Validation](../05-data-quality-validation/).

## What this shows

- SQL **window functions**: moving averages with `AVG(…) OVER (ORDER BY … ROWS BETWEEN …)`,
  a running total, and `LAG(…)` for day-over-day / week-over-week comparisons.
- Extracting calendar parts (`EXTRACT`, `CASE` on day-of-week) for reporting.
- A trend indicator derived from the 7-day average.

## Prerequisites

- The `fluid` CLI installed — see the [installation guide](../../README.md#installation).
- **Run the commands below from the repository root.**

## Run it

```bash
# from the repository root
fluid validate examples/06-time-windows/contract.fluid.yaml
fluid apply    examples/06-time-windows/contract.fluid.yaml --provider local --mode amend-and-build --yes
```

The input [`daily_sales.csv`](daily_sales.csv) holds 18 line items spanning 14 days
(2024-10-01 → 2024-10-14).

## Expected output

```text
🔷 Build 'sales_time_series' (embedded-SQL / local DuckDB)
   ✅ Completed in 0.1s — 1 action(s) executed
   📁 runtime/out/sales-time-series-v1.csv
```

```bash
cat runtime/out/sales-time-series-v1.csv
```

```csv
sale_date,year,month,day,day_of_week,day_name,daily_revenue,daily_units,distinct_products,revenue_3day_avg,revenue_7day_avg,cumulative_revenue,day_over_day_pct,week_over_week_pct,trend_indicator
2024-10-01,2024,10,1,2,Tuesday,2149.93,7,2,2149.93,2149.93,2149.93,,,At Average
2024-10-02,2024,10,2,3,Wednesday,1239.96,4,2,1694.95,1694.95,3389.89,-42.33,,Below Average
2024-10-03,2024,10,3,4,Thursday,839.9,10,2,1409.93,1409.93,4229.79,-32.26,,Below Average
2024-10-04,2024,10,4,5,Friday,2999.97,3,1,1693.28,1807.44,7229.76,257.18,,Above Average
```

**14 rows**, one per day. The first day has empty `day_over_day_pct` and
`week_over_week_pct` because there is no earlier day to compare against — `LAG` returns
`NULL` at the window's edge, and `week_over_week_pct` stays empty until day 8.
`cumulative_revenue` grows monotonically as the running total.

## How it works

In [`contract.fluid.yaml`](contract.fluid.yaml):

- A `daily_metrics` CTE aggregates the raw line items to one row per `sale_date`.
- A `rolling_metrics` CTE layers on the window functions (3-day and 7-day moving averages,
  running total, and `LAG(…, 1)` / `LAG(…, 7)` for the comparisons).
- The final `SELECT` adds calendar fields and the `trend_indicator`, then publishes to
  `runtime/out/sales-time-series-v1.csv`.

## Next steps

You've finished the quickstart series. 🎉

- ⬅️ Back to the **[examples index](../README.md)** — see **Beyond the quickstart** for
  cloud (AWS Glue), MCP output ports, and end-to-end pipeline examples.
