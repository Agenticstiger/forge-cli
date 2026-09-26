# Scheduled builds: the generated Airflow DAG and `schedule-sync`

A build that declares a cron trigger is run on that schedule by Airflow,
through fluid itself. Stage 3 (`fluid generate artifacts`) writes the DAG,
stage 11 (`fluid schedule-sync`) delivers it to the scheduler.

```yaml
builds:
  - id: ingest_subscriptions
    execution:
      trigger:
        type: schedule          # or no type at all
        schedule: "0 */4 * * *" # `cron:` is accepted too; five fields or a preset
        timezone: Europe/Paris  # default UTC
      retries:
        maxAttempts: 4          # Airflow retries = maxAttempts - 1 (default 3, max 10)
```

---

## When a DAG is generated

Stage 3 emits `schedule` artifacts when either:

- `orchestration.engine` is set (anything but `none`, which opts out), or
- there is no `orchestration.engine` and at least one build declares
  `execution.trigger.schedule` (or `cron`) with trigger `type: schedule` or
  no type.

With no engine declared, the engine defaults to **Airflow**. The file is
inert until `schedule-sync` pushes it, and stage 11 names the scheduler
(`airflow`, `mwaa`, `composer`, `astronomer` all run Airflow DAGs), so the
default decides only the file format. Declare `orchestration.engine` to pick
another one.

Each such build gets its own DAG that runs `fluid apply`, unless the contract
hand-declares `orchestration.tasks` (those keep their declared operators). A
build is left out when its own `execution.orchestration.engine` is `none` or
not Airflow, or when its trigger is `event`, `dataset`,
`schedule_and_dataset` or `timetable`, which a plain cron DAG cannot express.

Layout, one directory per product:

```
dist/artifacts/schedule/<contract id>/<build id>_dag.py
```

The DAG is hashed into `dist/artifacts/MANIFEST.json` like every other
artifact, and a rerun of stage 3 replaces it.

Stage 3 fails (exit 1, `generate_artifacts_failed`, `emit_key: schedule`)
rather than skip the schedule when the contract reads but does not load,
for example a malformed overlay for the `--env` given. Only a contract file
that cannot be read at all skips it (`generate_artifacts_skip_schedule_unreadable`).

### The schedule

`schedule` is an Airflow preset (`@once @hourly @daily @weekly @monthly
@yearly @annually @midnight`) or a five-field cron, checked to the grammar
Airflow parses it with (croniter), so a schedule Airflow would refuse fails
generation (exit 2) instead of the DAG import:

| Field | Values |
|---|---|
| minute | 0-59 |
| hour | 0-23 |
| day of month | 1-31, `L` (last day), `?` |
| month | 1-12, `JAN`-`DEC` |
| day of week | 0-7 (0 and 7 are Sunday), `SUN`-`SAT`, `DAY#1`..`DAY#5`, `?` |

Each field takes `*`, lists, ranges (a range may wrap: `22-2`) and `/step`.
`DAY#n` needs `*` or `?` as the day of month: croniter parses the pair and
then never finds a next run. A six-field cron is refused, because croniter
reads the sixth field as seconds while Quartz puts seconds first. `W`, `LW`,
`L` in the day of week and `H` are not supported.

## What each run executes

```bash
cd "$FLUID_PROJECT_DIR"
fluid apply "$FLUID_PROJECT_DIR/<contract path>" --env <env> \
    --mode amend-and-build --build-id <build id> --yes
```

This is the same apply stage 7 runs, so a cloud target works on the same
OpenTofu state as CI when the worker carries the same state-backend settings
(`FLUID_STATE_BACKEND`).

Fixed when the DAG is generated:

| Value | Stage 3 flag | Default |
|---|---|---|
| `--env` | `--env <env>` | `$FLUID_ENV`, the variable the generated pipelines apply with (`--env "${FLUID_ENV:-dev}"`); when that is unset or empty, no `--env`, with the warning `generate_artifacts_schedule_env_defaulted` if the input is a bundle or the contract has overlays. `--env ''` means no `--env`, without the warning |
| contract path | `--contract-path <path>` | the input's path relative to the current directory; `contract.fluid.yaml` (with a warning) for a bundle |
| schedule, timezone, retries | from the build's trigger | |

For a raw contract, stage 3 renders the schedule with the `--env` overlay
applied. A bundle was overlaid in stage 1, so build it with the same `--env`.

```bash
fluid bundle contracts/orders/contract.fluid.yaml --env aws --format tgz --out runtime/bundle.tgz
fluid generate artifacts runtime/bundle.tgz --out dist/artifacts/ \
    --env aws --contract-path contracts/orders/contract.fluid.yaml
```

Read on the worker at run time:

| Variable | Meaning |
|---|---|
| `FLUID_PROJECT_DIR` | **Required.** The directory holding the product checkout, the one CI ran the pipeline from. The run fails if it is unset or the contract is not under it. |
| `FLUID_BIN` | The fluid executable (default `fluid` on `PATH`). |
| `FLUID_DAG_ENV_PASSTHROUGH` | Extra variable names, space separated, to pass to fluid. |

### The environment fluid sees

Nothing secret is written into the DAG file. It holds ids, the contract
path, the env name and the *names* of the variables the contract reads.
fluid is started through `env -i` with only:

- `PATH HOME USER LOGNAME LANG LANGUAGE LC_ALL LC_CTYPE TZ TMPDIR`, the CA
  bundle variables, the proxy variables, and
  `GLUE_ROLE_ARN S3_STAGING_DIR S3_DATA_DIR GOOG_SERVICE_ACCOUNT_NAME
  TESTCONTAINERS_HOST_OVERRIDE`;
- every name matching `FLUID_* AWS_* GOOGLE_* GCP_* GCLOUD_* CLOUDSDK_*
  AZURE_* ARM_* SNOWFLAKE_* DATABRICKS_* PG* POSTGRES_* REDSHIFT_* ATHENA_*
  VAULT_* DBT_* DLT_* DATAHUB_* DMM_* ODCS_* ODPS_* TF_* OPENLINEAGE_*
  OTEL_*` (`PG*` is libpq's `PGHOST`, `PGPASSWORD`, `PGSSLMODE` and the rest);
- the variables the contract names through `{{ env.NAME }}`, `${NAME}` or a
  `secretRef: env://NAME`;
- the names in `FLUID_DAG_ENV_PASSTHROUGH`.

That list is what fluid itself reads while it applies and builds: a test
scans every module `fluid apply` imports (the build runners and providers
included) and fails when one reads a variable the list would drop. A dbt
project's own `env_var()` calls, or dlt's `SOURCES__*` / `DESTINATION__*`
settings, are not fluid's to know: name those in `FLUID_DAG_ENV_PASSTHROUGH`.
`VIRTUAL_ENV` is not passed: on a worker it names Airflow's environment (the
apache/airflow image sets it), and fluid's python runner would run builds
with that interpreter instead of its own.

A name starting `AIRFLOW` never passes, even when the contract or
`FLUID_DAG_ENV_PASSTHROUGH` names it, so the Airflow worker's own
configuration (`AIRFLOW__*`, `AIRFLOW_CONN_*`, the Fernet key) never reaches
the apply, and neither does an entry whose name is not a shell identifier
(`db.password`). Put the credentials fluid needs in
the worker's environment.

### Airflow versions

Written for Airflow 3: `airflow.sdk.DAG`, `BashOperator` from
`apache-airflow-providers-standard`, `schedule=`. Guarded imports fall back
to `airflow.DAG` and `airflow.operators.bash` on Airflow 2.6 and later. The
DAG sets `catchup=False` and `max_active_runs=1` (two applies of one build
never overlap), `skip_on_exit_code=None` (a fluid exit code of 99 fails the
task rather than skipping it) and a three hour `execution_timeout`.

## Delivering it: `schedule-sync --delete-scope`

The file, ssh, git+ssh, s3 and gs transports (and MWAA's s3) mirror with
deletion, so a DAG removed from the contract disappears from the scheduler.
`--delete-scope` bounds what that deletion may reach:

| `--delete-scope` | Behaviour |
|---|---|
| `product` *(default)* | Each top-level directory of `--dags-dir` is one product's DAGs, mirrored into the same-named directory of the destination. Stale DAGs are deleted there and nowhere else, so a shared DAG root keeps every other product's files. Loose files at the top of `--dags-dir` are refused (exit 2), as are symlinks and names that are not plain identifiers. |
| `destination` | Mirror `--dags-dir` onto the whole destination and delete everything else in it. Only for a destination this product owns alone. |
| `none` | Copy; delete nothing. |

```bash
# dist/artifacts/schedule/<contract id>/ lands in /opt/airflow/dags/<contract id>/
fluid schedule-sync --scheduler airflow --dags-dir dist/artifacts/schedule/ \
    --destination /opt/airflow/dags/
```

`az`, `scp`, `composer`, `astronomer`, `prefect` and `dagster` never delete
at the destination and ignore the flag.
