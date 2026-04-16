# Snowflake Smoke Example

This is the smallest native-SQL Snowflake contract in the repo that exercises the end-to-end FLUID happy path:

- `fluid auth status snowflake`
- `fluid validate contract.fluid.yaml`
- `fluid plan contract.fluid.yaml --out runtime/plan.json`
- `fluid apply contract.fluid.yaml --yes`
- `fluid verify contract.fluid.yaml --strict`

## Required Environment

Export these values before running the contract:

```bash
export SNOWFLAKE_ACCOUNT="your-account"
export SNOWFLAKE_USER="your-user"
export SNOWFLAKE_PASSWORD="your-password"
export SNOWFLAKE_WAREHOUSE="TRANSFORM_WH"
export SNOWFLAKE_DATABASE="DEV_ANALYTICS"
export SNOWFLAKE_SCHEMA="COMMUNITY_SMOKE"
export SNOWFLAKE_ROLE="TRANSFORMER"
```

`SNOWFLAKE_ROLE` is strongly recommended for team environments even if your user has a default role.

## Why This Example Exists

Use this contract when you want the first successful Snowflake deployment with the fewest moving pieces:

- one embedded SQL build
- one exposed Snowflake table
- env-driven account, warehouse, database, schema, and role settings
- a verification step you can reuse in CI

For the recommended production pattern with a dbt-style transformation workflow, see [`../billing_history`](../billing_history/README.md).
