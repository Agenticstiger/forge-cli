# Contract changelog

##  Info (2)

- **metadata_description_changed** `description` — `description updated`
- **column_description_changed** `exposes[0].contract.schema.amount.description` — `column 'amount' description updated`

**Summary:** 0 breaking · 0 non-breaking · 2 info

## Impact

Documentation only: no interface, grant, target or partition changed.

### Consumer interface

**Status:** unchanged

- No column, field, expose or stream changed.
- Consumers (1; `readers outside the workspace, or in files not named contract.fluid.yaml, are not listed`):
  - `finance.revenue` (`workspace`, reads `orders`): not broken

### Governance and target

**Status:** unchanged

- No grant, target, partitioning, policy or input changed.

### Resource impact

**Status:** known (1 no-op)
