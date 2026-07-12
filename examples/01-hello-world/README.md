# 01 · Hello World

> The smallest possible FLUID contract: one SQL `SELECT`, no inputs, one CSV out.

Part of the **[FLUID 5-minute quickstart series](../README.md)**. · ~2 min · no prior example needed.

## What this shows

- The three parts of every FLUID contract: **identity** (`id`, `metadata`),
  **logic** (`builds`), and **interface** (`exposes`).
- How `fluid apply --provider local` runs your SQL on the embedded DuckDB engine and
  writes a real file — no database or cloud account to set up.

## Prerequisites

- The `fluid` CLI installed — see the [installation guide](../../README.md#installation).
- **Run the commands below from the repository root.**

## Run it

```bash
# from the repository root
fluid validate examples/01-hello-world/contract.fluid.yaml
fluid apply    examples/01-hello-world/contract.fluid.yaml --provider local --mode amend-and-build --yes
```

## Expected output

`validate` confirms the contract is well-formed:

```text
✅ Valid FLUID contract (schema v0.7.2)
```

`apply` runs the SQL and reports the file it wrote:

```text
🔷 Build 'hello_transformation' (embedded-SQL / local DuckDB)
   ✅ Completed in 0.1s — 1 action(s) executed
   📁 runtime/out/hello-world-v1.csv
```

Open the result:

```bash
cat runtime/out/hello-world-v1.csv
```

```csv
message,created_at
"Hello, FLUID!",2026-07-12 19:42:17.346813+02
```

> The `created_at` timestamp is generated at run time, so yours will differ.

## How it works

The whole contract is [`contract.fluid.yaml`](contract.fluid.yaml):

- **`builds[]`** — a single `embedded-logic` build whose `engine: sql` holds an inline
  `SELECT`. There are no inputs, so it just emits one row.
- **`exposes[]`** — declares the output port: a `local` CSV at
  `runtime/out/hello-world-v1.csv` with a typed `schema`. `apply` writes exactly that
  path.

## Next steps

- ➡️ **[02 · CSV to Data Product](../02-csv-to-data-product/)** — feed a real CSV in and clean it up.
- ⬅️ Back to the **[examples index](../README.md)**.
