# Contract changelog

_No changes detected._

**Summary:** 0 breaking · 0 non-breaking · 0 info

## Impact

### Consumer interface

**Status:** unchanged

- No column, field, expose or stream changed.
- Consumers (1; `readers outside the workspace, or in files not named contract.fluid.yaml, are not listed`):
  - `finance.revenue` (`workspace`, reads `orders`): not broken

### Governance and target

**Status:** changed

- Partitioning of `orders`: `location.partitionBy` `["order_date"]` to `["order_id"]`; provider impact in_place: `the plan: update google_bigquery_table.orders`

### Resource impact

**Status:** known (1 update)

- update: `google_bigquery_table.orders`
