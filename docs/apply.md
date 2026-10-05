# `fluid apply` — the mode matrix

`fluid apply --help` links here, and `--mode`'s help text points here for
"the full matrix". This page is that reference.

`fluid apply` is the platform's mutation command: it provisions the
contract's infrastructure and, in the build-augmented modes, runs the
contract's transformations. `--mode` selects the DDL/DML strategy.

```bash
fluid apply contract.fluid.yaml --mode amend --yes
```

---

## The six modes

| Mode | DDL | Builds | Existing data | Destructive |
|---|---|---|---|---|
| `dry-run` | rendered, never executed | no | untouched | no |
| `create-only` | `CREATE IF NOT EXISTS`, fails if the target exists | no | untouched | no |
| `amend` *(default)* | additive — `ALTER … ADD COLUMN IF NOT EXISTS`; views `CREATE OR REPLACE` | no | preserved; new columns `NULL` | no |
| `amend-and-build` | same as `amend` | yes | preserved; transforms re-run | no |
| `replace` | drop + recreate the target | no | **dropped** | **yes** |
| `replace-and-build` | same as `replace` | yes (`dbt --full-refresh`) | **dropped**, then rebuilt | **yes** |

`--mode` defaults to `amend`. `--dry-run` is an ergonomic alias for
`--mode dry-run`.

### Which modes run builds

`amend-and-build` and `replace-and-build` run the contract's `builds[]`
after the DDL phase. Every other mode provisions infrastructure only — a
plain `fluid apply` on a contract with a `builds:` block does **not** run
the SQL in it.

`--build-id <id>` filters the run to one build; without it every build in
the contract runs.

> `--build` was retired when the mode matrix replaced it. `fluid apply`
> rejects it with a pointer to `--mode amend-and-build` / `--build-id`
> rather than guessing what you meant.

### Which modes are destructive

`replace` and `replace-and-build`. Both require `--allow-data-loss` unless
the environment is `dev` **and** the target is provably empty. An unknown
row count is treated as populated (fail-safe).

```
$ fluid apply contract.fluid.yaml --mode replace --yes
ERROR  --mode replace is destructive (env not set; target row count unknown
       (treating as populated)). Pass --allow-data-loss to confirm the drop. …
```

---

## Apply engines, and what that changes

The engine is resolved per provider — there is no user-facing switch.

| Provider | Engine |
|---|---|
| `aws`, `gcp`, `snowflake`, `confluent` | OpenTofu (`.tf.json` + `tofu init/plan/apply`) |
| `local` | native in-process apply |

Two mode behaviours differ by engine. Both are reported at run time, but
know them before you plan a destructive change:

**1. No pre-replace snapshot on the OpenTofu engine.** The native path
plans a pre-flight zero-copy snapshot (`<target>__backup_<ts>`) and records
it in `.fluid/rollback-state.json` so `fluid rollback` can restore it.
`tofu` has no CTAS/CLONE step, so on every cloud provider **no backup table
is created and `fluid rollback` has no restore point**. The data-loss gate
says so explicitly, and so does the `--allow-data-loss` override warning.
Back the target up yourself first if you need one.

**2. Column changes are not reconciled by `tofu`.** The Snowflake emitter
pins `lifecycle.ignore_changes = ["column"]` on every table, because the
build engine owns the materialized column types and Snowflake rejects most
in-place scale changes. A contract whose declared column type no longer
matches the live table will therefore plan clean — including under
`--mode replace`. The apply prints the suppressed drift:

```
⚠️  column drift NOT reconciled on DB.SCHEMA.TABLE (mode=replace):
    CREATED_AT: contract=STRING live=TIMESTAMP
    The emitted module pins lifecycle.ignore_changes=["column"] …
    Run `fluid verify --strict` to gate on it.
```

`fluid verify --strict` is the gate — it exits non-zero on a type mismatch.

**Drift against the apply's own state.** `fluid diff` (and `fluid verify
--state-drift`) run the refresh `fluid apply` runs, without applying: from
the same workdir and backend (`--workspace-dir`, `--state-backend`,
`FLUID_STATE_BACKEND`), they emit the apply's module, run `tofu plan
-detailed-exitcode` and read the saved plan back. A resource the refresh
found changed outside the apply, in an attribute the plan would put back,
is drift and fails `--exit-on-drift`; a change the plan makes with nothing
changed outside behind it is pending (the contract moved). What the refresh
sees in attributes the module does not declare (computed read-backs,
settings the contract does not manage) is reported, not gated: the exit
code alone is not used, because `-refresh-only` reports those on a clean
apply. State cannot see what the build writes into the containers (object
contents, object-level encryption) or a shared pool the contract only
references; the per-expose SDK checks stay for those. With no state
reachable (no apply ran here, `local` contracts, no `tofu`), the pass says
so and the SDK checks are the whole answer, as before.

---

## Build execution details

### Where an embedded-SQL build runs

A build with `engine: sql` (or `pattern: embedded-logic` + `properties.sql`)
runs on the platform its `execution.runtime.platform` declares:

```yaml
builds:
  - id: seed_table
    pattern: embedded-logic
    engine: sql
    properties:
      sql: |
        CREATE OR REPLACE TABLE "{{ env.SNOWFLAKE_DATABASE }}"."{{ env.SNOWFLAKE_SCHEMA }}"."T" …
    execution:
      runtime:
        platform: snowflake          # ← selects the executor
        resources:
          warehouse: "{{ env.SNOWFLAKE_WAREHOUSE }}"
          database:  "{{ env.SNOWFLAKE_DATABASE }}"
          schema:    "{{ env.SNOWFLAKE_SCHEMA }}"
          role:      "{{ env.SNOWFLAKE_ROLE }}"
```

* `snowflake` — executes on the declared warehouse.
* `local` / `duckdb` / unset — the local provider's DuckDB engine.
* anything else — a hard error. forge-cli will not downgrade a declared
  platform to the local engine.

`{{ env.X }}` placeholders are resolved before any engine sees the SQL, for
both apply inputs (a `.fluid.yaml` path and a `plan.json`).

### What a DuckDB embedded-SQL build reads, and where it lands

On the DuckDB engine (`local` / `duckdb` / unset), each `consumes[]` entry the
SQL reads is a view, named by its `exposeId`:

```yaml
consumes:
  - productId: bronze.customer_subscriptions
    exposeId: subscriptions          # ← the view name
builds:
  - id: summarize
    pattern: embedded-logic
    engine: duckdb
    properties:
      sql: SELECT product_id, status, COUNT(*) AS n FROM subscriptions GROUP BY 1, 2
```

* **Which contract.** The one declaring `id: <productId>` under the nearest
  directory above this contract that holds `fluid.workspace.yaml`, plus any
  root in `FLUID_UPSTREAM_CONTRACTS` (four levels deep, `contract.fluid.yaml`
  or `.json`; VCS, venv and output directories such as `dist/`, `runtime/`,
  `.fluid/` are skipped). An id declared by two files fails the build naming
  both.
* **Which entries.** An entry is read when the SQL names a relation equal to
  its `exposeId` (case-insensitive; a CTE of that name does not count), as
  DuckDB's own parser reads the query. An entry the SQL does not read is
  lineage only, as every entry was before: it is listed in the build output,
  not resolved, and the provider still logs `local_consumes_not_bound` when no
  explicit input is declared. So a contract that binds its inputs under other
  names, or reads its upstream by path inside the SQL, builds as it did. When
  the parser cannot tell (a statement other than SELECT, a syntax error),
  every entry is treated as read. An entry naming this contract's own id is
  refused: it would read the file this build is about to overwrite.
* **Which binding.** The upstream is loaded with the same `--env` overlay as
  this run, so `--env aws` reads the upstream's aws binding. Without `--env`:
  for a `plan.json`, the env `fluid plan --env` recorded in
  `contract_metadata.env` (covered by `planDigest`; a plan made from a bundle
  that records none falls back to the env the bundle's MANIFEST records); for
  a bundle input, the env its MANIFEST records. `fluid apply plan.json --env X`
  with an `X` other than the one the plan records is refused
  (`plan_env_mismatch`) before any build runs. Overlays keep patching only
  `exposes[].binding`; the SQL is the same on every target.
* **Which relation.** A local binding reads its `location.path`, relative to
  the UPSTREAM contract's directory; when an embedded-SQL build of the
  upstream writes it, under the local provider's file name and format (a
  `format: parquet` path without the suffix is read at `<path>.parquet`, and a
  non-parquet format is CSV, which is what that provider writes). An AWS
  binding naming a `location.bucket` and `location.path` reads
  `s3://<bucket>/<path>/*.<ext>`, the prefix the duckdb acquisition runner
  writes into and the Glue table `fluid apply` declares for it. A GCP
  `bigquery_table` binding reads that table (`<project>.<dataset>.<table>`, the
  one `fluid apply` created, whatever `gs://` path the binding also carries):
  when the build runs, the table is read through the BigQuery API
  (`tabledata.list` pages, streamed) into a Parquet file under the build's
  `.fluid/staging/<build>/inputs/`, the view reads that file, and the file is
  removed after the build. A BigQuery `TIMESTAMP` reads as a DuckDB `TIMESTAMP`
  holding the UTC wall clock, which is what the same SQL reads on the local and
  aws targets. Another warehouse's table, a stream, or a GCS/Azure prefix is an
  `UnreadableBindingError` naming the platform. A `{{ env.X }}` in the upstream
  binding with `X` unset is an error, not an empty string.
* **Explicit inputs win.** A `properties.parameters.inputs` entry whose `name`
  equals the `exposeId` binds that view by hand, and the entry is not resolved
  at all: that is how a federated upstream, or one this engine cannot read, is
  still bound.
* **Nothing silent.** An entry the SQL reads that is neither resolved nor
  covered fails the build before the SQL runs, naming the productId and where
  it looked. The resolved `productId` / `exposeId` / `uri` are printed and
  kept in the provider's `runtime/out/local_apply_log.jsonl`; this path writes
  no run record and emits no OpenLineage event. Why a provider action failed
  is printed too, with the value of every credential-named `{{ env.X }}` the
  build uses redacted, as it is in the apply log and the log lines.

When the first expose's binding is an AWS object-store binding
(`platform: aws`, `location.bucket` + `location.path`, `format` parquet, csv
or json), the result is written to the object the acquisition runner writes
for that binding: `s3://<bucket>/<path>/<location.table or exposeId>.<ext>`,
inside the Glue table's location, so `fluid verify --env aws` counts it.
S3 access uses the ambient AWS credential chain and the binding's region;
`AWS_ENDPOINT_URL_S3` / `AWS_ENDPOINT_URL` point it (and the acquisition
runner) at an S3-compatible store such as MinIO. With either variable set, a
DuckDB secret that cannot be created (for example the `aws` extension cannot
be loaded) fails the build (`ObjectStoreEndpointError`) instead of reading
and writing AWS itself. A local binding is
unchanged. An expose declaring `policy.privacy.masking` is refused
(`MaskingNotAppliedError`): this path does not apply masking, and cleartext
must not land silently.

When the first expose's binding is a GCP `bigquery_table`, the result is
written as Parquet under `.fluid/staging/<build>/` and one load job moves it
into that table (`WRITE_TRUNCATE_DATA`, which keeps the table's row access policies,
`CREATE_NEVER`, the table's own schema),
the load the duckdb acquisition runner performs; a `gs://` `location.path` on
the binding is never written to. A failed or short load fails the build. Any
other landing this path cannot write, a `gs://` or other non-S3 URI, a GCS
bucket, an Azure, Snowflake or Databricks binding, is refused
(`EmbeddedSqlLandingError`) before the SQL runs, instead of being written to a
local file of that name. The load is recorded as a run under
`.fluid/runs/<product>/<build>/runs/`, the way the acquisition load is, so
`fluid verify` holds the table's count to the rows it landed.

Also refused before anything is read (`EmbeddedSqlLandingError`):

* a landing that resolves to one of the build's own inputs: a BigQuery table
  the build reads (names compared case-insensitively; a project left to the
  client matches any), or an S3 object inside a prefix it reads. The load
  replaces the table, so it would overwrite another product's rows, and no
  `--allow-data-loss` is ever asked for a data write;
* a further expose named in the build's `outputs` and bound to a cloud store
  or a warehouse (an aws, gcp, azure, snowflake or databricks binding, or any
  remote URI): this path lands only the first expose. A further local expose
  or output port is not written either, and the build prints a warning.

When the contract declares `sovereignty` and the build reads or loads a
BigQuery table, the locations those reads and the load actually use are held
to it by `fluid validate`'s rules (`EmbeddedSqlSovereigntyError`): every
BigQuery binding must name its region (without one it is `US`, the IaC's
default), the landing must be outside `deniedRegions`, inside
`allowedRegions` and in the declared `jurisdiction`, and, with `dataResidency`
and no `crossBorderTransfer` (the schema's defaults), every input must be in
the landing's jurisdiction. BigQuery's `EU` and `US` multi-regions count as
EU and US. `enforcementMode: strict` (the default) refuses, `advisory` warns,
`audit` logs.

BigQuery reads and loads authenticate with Application Default Credentials
(gcloud ADC, an attached service account, or a Workload Identity Federation
`external_account` file in `GOOGLE_APPLICATION_CREDENTIALS`). With
`BIGQUERY_EMULATOR_HOST` set they go to that emulator with anonymous
credentials, so no real token is sent to it. A load job that reports no row
count (the goccy emulator's never do) is checked by counting the table after
the load, never assumed.

### Skipped builds are not success

If every build in a build-augmented mode was skipped — a missing dbt
project or driver script — `fluid apply` exits **1**. DDL-only success on
an empty table is a broken deployment reported green. Pass
`--allow-skipped-builds` when the skip is expected (build artifacts living
outside the checkout). A partial skip, where at least one build ran, still
exits 0.

---

## Plan binding

`fluid apply plan.json` re-verifies the plan's `planDigest` (and
`bundleDigest`, when the plan carries one) before any DDL, so the apply
provably matches the plan that was reviewed. A plan generated with
`fluid plan --mode X` records that mode; applying it under a different
`--mode` is refused.

`--no-verify-plan-binding` waives the gate for emergencies and logs at
WARNING.

---

## Safety flags

| Flag | Effect |
|---|---|
| `--yes` | skip the confirmation prompt |
| `--allow-data-loss` | confirm a destructive mode / a plan that destroys resources |
| `--allow-skipped-builds` | exit 0 even when every build was skipped |
| `--force-pattern-drift` | override apply-time plugin hooks that report drift |
| `--no-verify-plan-binding` | skip plan/bundle digest verification |
| `--no-verify-federation` | skip the federated-consumes upstream-digest gate |
| `--ensure-opentofu` | provision a pinned, SHA-256-verified `tofu` if missing |

`fluid apply` sets `allow_abbrev=False`: flags are matched exactly, never by
unambiguous prefix. On a command with destructive modes, a mistyped or
retired flag must be an error rather than a silent reinterpretation of its
value.

---

## Apply-time plugin hooks

Plugins registered under the `fluid_build.apply_hooks` entry-point group run
before any infrastructure change, on **every** engine — cloud and local
alike. A hook appends to its `errors` list to abort the apply
(scaffold-bundle digest drift, lockfile freshness, env-aware deploy
guards). `--force-pattern-drift` downgrades reported drift to a warning.

The resolved `--env` is forwarded to hooks that opt in via their signature;
legacy three-parameter hooks are called unchanged. See
`fluid_build/cli/apply.py::_dispatch_apply_hook`.

---

## See also

* `fluid plan` — generates the reviewable, digest-bound `plan.json`.
* `fluid verify --strict` — gates the live object against the contract.
* `fluid rollback --list` — lists restore points (native engine only).
* `docs/HOW_IT_WORKS.md` — the 11-stage pipeline this command is stage 7 of.
