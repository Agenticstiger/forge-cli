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

- Grant added: `domain:example.com` `read`, `manage` **broad**: every account of a domain, admin or manage permission

### Resource impact

**Status:** unknown: `no plan given: pass --plan with the output of fluid plan --tofu`
