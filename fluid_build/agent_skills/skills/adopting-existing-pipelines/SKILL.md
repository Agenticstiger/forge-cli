---
name: adopting-existing-pipelines
description: Proposes contracts from running PySpark jobs and Airflow DAGs with fluid import, and takes existing Glue tables over with --adopt-tables. Use when asked to adopt, migrate, onboard or import an existing Spark job, Airflow DAG or data platform, to write a conventions file or an importer plugin, or when a plan or apply fails with table_adoption_requires_flag, table_adoption_mismatch, table_adoption_unavailable or plan_adopt_tables_needs_tofu.
license: Apache-2.0
compatibility: Needs the fluid CLI on PATH (pip install data-product-forge). Taking tables over also needs OpenTofu and AWS credentials.
---

# Adopting existing pipelines

`fluid import` proposes contracts from code that already runs. It parses the files and never
imports or runs them: no job starts and Airflow is never loaded. A person completes and
reviews each contract before anything is applied.

`fluid describe --commands` prints every command and flag as JSON, the importers installed
here included. Check a command or flag there before you use one this skill does not show.

## 1. Propose, then write

Run the dry run first. It prints each proposed product (its writer, outputs and upstreams),
the report, and what would happen to each file: new, unchanged, replaced or refused.

```bash
fluid import pyspark jobs/ --dags dags/ --dry-run
fluid import airflow dags/ --dry-run
```

- `pyspark` reads the jobs; `--dags` joins each DAG task to the script it submits, which adds
  the schedule, the owner and the dependencies. `airflow` reads a DAGs directory and finds
  the scripts through the conventions file's `script_roots`.
- The default is one product per output table or topic, with one writer. `--split-by` also
  takes `job`, `dag`, `folder` and `project`.
- Show the user the proposal and the report, and write only when they agree:

```bash
fluid import pyspark jobs/ --dags dags/ --conventions conventions.yaml --out contracts/
```

`--out` is a directory when the import writes several products. A file that exists and
differs from what the import would write is refused. `--force` replaces it, so ask first:
a person may have completed it by hand.

## 2. Complete what the code cannot say

The report has four parts: Mapped 1:1, Used defaults, Unsupported and Notes. Each line under
Used defaults or Unsupported is something to complete. Never guess it:

- A name that is not a constant (a table name built at run time) is reported with the reason.
  Ask the user, or add a rule to the conventions file and import again.
- Columns, a Databricks schema and a Snowflake warehouse are not in the code. Ask for them
  and add them to the contract.
- A table in a Spark catalog gets `binding.location.catalog` only when the catalog's kind is
  known. When the report says to set it, ask which catalog it is.
- A DAG's schedule lands under the build's `execution.orchestration` with `mode: manual`, and
  the trigger is `manual` (`continuous` for a stream). Keep it while the DAG still runs: a
  `schedule` trigger makes `fluid generate schedule` render a second DAG for the same job.

Then validate each contract and fix what it reports (see the troubleshooting-fluid-errors
skill):

```bash
fluid validate contracts/<file>.fluid.yaml --offline
```

## 3. The conventions file

The organisation's layout is data in one YAML file: where scripts and data live, the helper
functions that wrap Spark reads and writes, the operator classes that submit Spark work, the
values of environment variables, layers, owners and what to ignore. Start from
[references/conventions.example.yaml](references/conventions.example.yaml) and keep only the
keys that describe the project.

- No code in it runs. A key fluid does not know is refused, so a typo fails instead of being
  ignored.
- A path in it must stay inside the file's own folder or a directory named on the command
  line (the jobs directory, `--dags`).
- An `environments` value may be empty (`TABLE_PREFIX: ""`); a variable the file does not
  name stays unresolved and is reported.

## 4. Importer plugins

A Python package adds a tool to `fluid import` through the `fluid_build.importers`
entry-point group:

```toml
[project.entry-points."fluid_build.importers"]
my-tool = "my_package.forge_importer:MyImporter"
```

The object needs no forge import. It has `import_to_contract(source, *, options)`, which
returns `(contract, report)`, or `import_to_contracts(source, *, options)` for several
products. The report has four lists: `mapped_one_to_one`, `required_defaults`,
`unsupported` and `notes`. Set `guards_overwrite = True` when a person completes what it
writes. A plugin cannot take a built-in tool's name, two packages that ship one name are both
refused, and `FLUID_PLUGINS_ALLOWLIST` and `FLUID_PLUGINS_BLOCKLIST` apply before it loads.

## 5. Take over the tables that already exist

An existing Glue table the contract names, which the OpenTofu state does not hold, is refused
(`table_adoption_requires_flag`) unless fluid made it for this product. Take it over only
when the user says yes, through a reviewed plan, in the order the
deploying-fluid-contracts-safely skill gives, with `--tofu --adopt-tables` on the plan:

```bash
fluid plan runtime/bundle.tgz --env dev --mode amend --tofu --adopt-tables --out runtime/plan.json
fluid apply runtime/plan.json --bundle runtime/bundle.tgz --env dev --mode amend
```

- The plan checks each table against the contract and imports it through an OpenTofu `import`
  block: a table that matches is taken over unreplaced, with its Lake Formation permissions.
  The saved plan applies what it planned; the apply needs no flag.
- What a table holds that the contract does not declare (a description, an owner, column
  comments, a column name's case) is printed. An Iceberg table keeps it; a Hive table is
  updated in place. Tell the user which.
- A partitioned Hive table cannot be taken over yet: the run is refused and says so.
- `fluid ship --adopt-tables` passes the flag to its apply stage.

| Event | What it means | Do |
|---|---|---|
| `table_adoption_requires_flag` | an existing table fluid did not make for this product | ask the user, then plan the take-over as above |
| `table_adoption_mismatch` | a table differs from its contract; nothing was imported or applied | ask whether the contract or the table is right, align `exposes[].contract.schema` and `binding.location`, plan again |
| `table_adoption_unavailable` | the run cannot look the tables up (no AWS account id) | set `AWS_ACCOUNT_ID`, or use credentials `sts:GetCallerIdentity` accepts |
| `plan_adopt_tables_needs_tofu` | `--adopt-tables` on a plan without `--tofu` | add `--tofu` |
| `opentofu_adoption_failed` | a look-up failed (credentials, network) | fix the access and run again; never create over an object that may exist |

`table_adoption_accepted`, logged at WARNING, is the audit record of a take-over, not a
failure.
