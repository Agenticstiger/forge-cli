# Old generators against the one renderer

`schedulers/airflow` replaced `providers/{aws,gcp,snowflake}/codegen/airflow.py`, the Snowflake
`orchestration/airflow_generator.py` and `runtimes/airflow_provider_actions.py`. The snapshots in
this folder are the new output for `tests/schedulers/airflow_fixtures.py`. The old output for the
same contracts was captured before the old modules were deleted and diffed line by line. Every
difference follows, with why it is right.

## Every provider

| Old | New | Why |
|---|---|---|
| Header `FLUID Generated DAG: <name>` with provider lines in the module's own order | `FLUID Generated Pipeline: <name>`, `Provider:`, then the provider's lines | One header builder (`codegen_utils.generate_file_header`, escaped). The GCP generator already used it. |
| Imports every operator the provider has | Imports only the operators a task uses | An import that cannot resolve stops the whole DAG from loading. The GCP generator already gated Dataflow on this ground (it needs the Beam provider); this applies the same rule to all of them. `GCSDeleteBucketOperator` and `SnowflakeHook` were imported and never used. |
| A task variable is `sanitize_identifier(taskId)` (GCP, Snowflake) | Collision-free and never a name the module binds | `a-b` and `a.b` both became `a_b` and one task vanished; `taskId: task` or `dag` rebound a name. AWS had this guard; GCP and Snowflake did not. |
| A dependency on a task outside the DAG emitted `gone >> x` (GCP, Snowflake) | A comment, no edge | The name was never defined, so Airflow failed the whole file at import. AWS had this guard. |
| Two or more dependencies emitted one edge each (AWS), a list (GCP, Snowflake) | `[a, b] >> c` | One form. |
| `dag_id` kept `.` and `-` (AWS), turned `.` into `_` (Snowflake), `sanitize_identifier` (GCP) | `sanitize_identifier(contract id)` | `fluid generate schedule` already used it, so `generate schedule` and `export` now name the DAG alike. A contract id `sales.orders` is DAG `sales_orders` for AWS now. The file name `fluid export` writes is unchanged. |
| Tags had the provider for GCP and Snowflake only | Tag `aws` too | One tag rule. |

## AWS

| Old | New | Why |
|---|---|---|
| `aws.glue.<other op>` (for example `run_job`) became a task that raised "provider-action tasks are retired" at run time | `GlueJobOperator` | The scheduler already rendered it that way, and `aws.glue.run_job` is what a Glue build synthesises to. |
| An action with no operator raised at run time through `_execute_provider_action` | A task that logs the action and params | What the GCP and Snowflake generators and the scheduler always did. This is a loss of loudness: a task for an action nothing renders now succeeds. |
| `aws.athena.execute_query` only; `outputLocation` only | Any `aws.athena.*` op; `output_location` or `outputLocation` | The scheduler took any op; both spellings are in contracts. With no location, the account bucket when the account is known, else `s3://results/`. |
| Schedule `hourly`/`daily`/`weekly`/`monthly` mapped to cron (`0 * * * *`, `0 2 * * *`, `0 2 * * 0`, `0 2 1 * *`); any other word passed through | The Airflow keyword (`@hourly`, `@daily`, `@weekly`, `@monthly`); any other word is `@daily` | The shared converter, as GCP and Snowflake already had. `daily` runs at 00:00, not 02:00. Cron strings are unchanged. |
| `_execute_provider_action` helper in every file | Gone | It only raised. The Glue Data Catalog helpers are emitted only when a task calls them. |
| TaskFlow variant `generate_airflow_dag_taskflow` | Gone | Nothing but its own tests called it. |

## GCP

| Old | New | Why |
|---|---|---|
| Task `task` was assigned as `task` | `task_2` | `task` is the Airflow decorator name. |

## Snowflake

| Old | New | Why |
|---|---|---|
| `SNOWFLAKE_CONN_ID = 'snowflake_default'` constant | `conn_id=` from `provider_config["connection_id"]` | `fluid generate schedule` already passed it; `export` could not set it. |
| `hook_params` always had warehouse and database | Only the ones in `provider_config` | `fluid generate schedule` has neither, so it renders none. `export` passes both, as before. |
| Only services `database`, `schema`, `table`, `query` ran SQL | The operation decides (`create_*`, `query`, `run_query`, `execute_sql`) | `snowflake.sql.execute_sql` logged instead of running before. |
| An unknown operation of those services ran `SELECT 'Operation: x'` | Logs it, unless the task has `params.sql` | A made-up statement is worse than none. |

## Removed with no replacement

* `runtimes/airflow_provider_actions.py` (`generate-airflow` for a contract with `builds` and no
  `orchestration`): a DAG of `echo` placeholders for most action types. A scheduled build renders
  through `fluid_apply`; a contract with nothing scheduled has nothing to render.
* `providers/snowflake/orchestration/`: no command called it. It read the `sf.*` action
  vocabulary with `parameters`, not `params`.
