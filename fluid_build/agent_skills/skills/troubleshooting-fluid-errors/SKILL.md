---
name: troubleshooting-fluid-errors
description: Diagnoses and fixes a failing fluid command from its output, its stable ERR_ error code and the path-level messages of fluid validate. Use when a fluid command exits non-zero or prints an ERR_ code, when validation reports an error such as is not one of, is a required property, Additional properties are not allowed or does not match, or for composition, layer and productType, or plan-binding failures.
license: Apache-2.0
compatibility: Needs the fluid CLI on PATH (pip install data-product-forge).
---

# Troubleshooting fluid errors

## Steps

1. Read the whole output of the failing command, not only its last line.
2. If it prints an `ERR_<EVENT>` code, look the code up in
   [references/error-codes.md](references/error-codes.md) and do what the first
   suggestion says.
3. If `fluid validate` lists numbered errors, fix them one at a time, in order, using the
   table below. Each error starts with the path of the field it is about.
4. Run the same command again. Repeat until it passes.
5. Report the cause and the fix. Never silence a check to make an error go away.

```bash
fluid validate contract.fluid.yaml --offline
```

## Validation messages

| Message | Cause | Fix |
|---|---|---|
| `<path>: 'x' is not one of [...]` | a value outside its list | use one of the listed values; for a local DuckDB output use `format: parquet` |
| `<path>: 'x' is a required property` | a missing key | add the key at that path |
| `Additional properties are not allowed ('x' was unexpected)` | a key the schema does not have, or one at the wrong level | remove it, or move it to where the schema defines it |
| `<path>: 'x' does not match '^[A-Za-z0-9_]...'` | an id with a space or a leading or trailing `-` or `.` | use letters, digits, `_`, `.` and `-`, for example `sales.orders_raw` |
| `metadata consistency: metadata.layer=... and metadata.productType=... are inconsistent` | the pair disagrees | Bronze with SDP, Silver with ADP, Gold with CDP |
| `composition rule: ...` | `consumes[]` breaks the product-type rules | see the composing-fluid-data-products skill |

## Other failures

- `Cannot access file ... file system operations are disabled by configuration` during a
  local apply: the SQL reads a file outside what the sandbox grants. Run `fluid apply` from
  the contract's directory. To read another product's output, put `fluid.workspace.yaml` at
  the repository root and bind the file under `builds[].properties.parameters.inputs`.
- A plan or apply gate (`apply_plan_mode_mismatch`, `apply_plan_digest_*`): plan again;
  see the deploying-fluid-contracts-safely skill. Never bypass the gate with a flag.
- `fluid` not found: install it with `pip install data-product-forge`, then re-run.
