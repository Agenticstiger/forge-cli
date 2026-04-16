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

- The contract points to `./models/billing_history` as a repository placeholder. Replace that with your real dbt project path or repo reference.
- FLUID reads the Snowflake provider from `binding.platform`, so the normal plan/apply flow does not need `--provider snowflake`.
- Keep warehouse, database, schema, and role explicit for each environment. Avoid hard-coding production object names into a one-size-fits-all contract.

If you want the smallest first deployment instead of the recommended production path, start with [`../smoke`](../smoke/README.md).
