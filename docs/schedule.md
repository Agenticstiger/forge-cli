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
        schedule: "0 */4 * * *" # `cron:` is accepted too
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
| `--env` | `--env <env>` | none: the flag is left out |
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
path, the env name and the *names* of the `{{ env.NAME }}` variables the
contract reads. fluid is started through `env -i` with only:

- `PATH HOME USER LOGNAME LANG LANGUAGE LC_ALL LC_CTYPE TZ TMPDIR`, the CA
  bundle variables and the proxy variables;
- the `FLUID_* AWS_* GOOGLE_* GCP_* GCLOUD_* CLOUDSDK_* AZURE_* ARM_*
  SNOWFLAKE_* DATABRICKS_* DBT_* TF_* OPENLINEAGE_* OTEL_*` families;
- the variables the contract reads through `{{ env.NAME }}`;
- the names in `FLUID_DAG_ENV_PASSTHROUGH`.

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
