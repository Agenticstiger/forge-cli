# Snowflake Billing History Example

This example shows the recommended **dbt-snowflake** contract shape for production teams:

- `builds[]` describes the transformation workload
- the Snowflake runtime is configured explicitly with warehouse, database, schema, and role
- the exposed table is still declared in the contract so `plan`, `apply`, `verify`, and governance tooling know what the data product should look like

## When To Use This Example

Use this pattern when your team already manages transformations in dbt and wants FLUID to be the deployment and contract layer around that workflow.

It is a better fit than the smoke example when you need:

- environment-specific Snowflake databases and schemas
- least-privilege roles for build vs. read access
- CI gates around `validate`, `plan`, and `verify`
- a production-oriented contract that stays aligned with a dbt project

## Notes

- **The dbt project is not shipped with this example.** `./models/billing_history`
  is a placeholder path; there is no `dbt_project.yml` there. Until you point
  the build at your real dbt project, `--mode amend-and-build` provisions the
  DDL, skips the build, and exits **1**:

  ```
  ⚠️  Build 'billing_history_aggregation' - dbt project not found: …/models/billing_history/dbt_project.yml
  ❌ Every build was skipped (1/1) — nothing was transformed …
  ```

  That non-zero exit is deliberate: a deployment that provisioned an empty
  table and ran no transformation is not a success. Use
  `--allow-skipped-builds` only if you genuinely intend to deploy DDL alone.
- Every Snowflake object name resolves from the environment
  (`SNOWFLAKE_ACCOUNT` / `SNOWFLAKE_DATABASE` / `SNOWFLAKE_SCHEMA` /
  `SNOWFLAKE_WAREHOUSE` / `SNOWFLAKE_ROLE`), the same way [`../smoke`](../smoke/README.md)
  does. Applying this contract creates objects in **your** database, not a
  new top-level one.
- FLUID reads the Snowflake provider from `binding.platform`, so the normal plan/apply flow does not need `--provider snowflake`.
- Keep warehouse, database, schema, and role explicit for each environment. Avoid hard-coding production object names into a one-size-fits-all contract.

If you want the smallest first deployment instead of the recommended production path, start with [`../smoke`](../smoke/README.md).
