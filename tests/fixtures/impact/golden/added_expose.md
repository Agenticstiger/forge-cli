# Contract changelog

##  Non-breaking (1)

- **expose_added** `exposes[1]` — `expose 'orders_eu' added`

**Summary:** 0 breaking · 1 non-breaking · 0 info

## Impact

### Consumer interface

**Status:** changed

- non_breaking: `exposes[1]`: `expose 'orders_eu' added`
- Consumers (1; `readers outside the workspace, or in files not named contract.fluid.yaml, are not listed`):
  - `finance.revenue` (`workspace`, reads `orders`): not broken

### Governance and target

**Status:** changed

- Target of `orders_eu`: `format` (unset) to `bigquery_table`
- Target of `orders_eu`: `location.dataset` (unset) to `sales`
- Target of `orders_eu`: `location.project` (unset) to `shop-eu`
- Target of `orders_eu`: `location.table` (unset) to `orders_eu`
- Target of `orders_eu`: `platform` (unset) to `gcp`
- Partitioning of `orders_eu`: `location.partitionBy` (unset) to `["order_date"]`; provider impact unknown: `no plan given: pass --plan with the output of fluid plan --tofu`

### Resource impact

**Status:** unknown: `no plan given: pass --plan with the output of fluid plan --tofu`
