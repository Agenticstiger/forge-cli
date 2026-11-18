# Contract changelog

##  Non-breaking (1)

- **column_added** `exposes[0].contract.schema.coupon_code` — `column 'coupon_code' added to expose 'orders'`

**Summary:** 0 breaking · 1 non-breaking · 0 info

## Impact

### Consumer interface

**Status:** changed

- non_breaking: `exposes[0].contract.schema.coupon_code`: `column 'coupon_code' added to expose 'orders'`
- Consumers: **unknown**: `no fluid.workspace.yaml above ws/orders: products that read this one are not searched`

### Governance and target

**Status:** unchanged

- No grant, target, partitioning, policy or input changed.

### Resource impact

**Status:** unknown: `no plan given: pass --plan with the output of fluid plan --tofu`
