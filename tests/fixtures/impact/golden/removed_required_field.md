# Contract changelog

##  Breaking (1)

- **column_removed** `exposes[0].contract.schema.amount` — `column 'amount' removed from expose 'orders'`

**Summary:** 1 breaking · 0 non-breaking · 0 info

## Impact

### Consumer interface

**Status:** breaking

- breaking: `exposes[0].contract.schema.amount`: `column 'amount' removed from expose 'orders'`
- Consumers (1; `readers outside the workspace, or in files not named contract.fluid.yaml, are not listed`):
  - `finance.revenue` (`workspace`, reads `orders`): breaks on `exposes[0].contract.schema.amount`

### Governance and target

**Status:** unchanged

- No grant, target, partitioning, policy or input changed.

### Resource impact

**Status:** unknown: `no plan given: pass --plan with the output of fluid plan --tofu`
