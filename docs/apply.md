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
