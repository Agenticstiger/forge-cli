# FLUID Examples

Runnable, copy-paste examples for the `fluid` CLI. Every folder is **self-contained**:
open it, read its `README.md`, and run the exact commands shown — no cloud account
required. Examples 01–06 are the **5-minute quickstart series** and run entirely on
your machine using the embedded DuckDB engine (`--provider local`).

> **Run all commands from the repository root** (the folder that contains this
> `examples/` directory). The contracts reference their sample data with paths like
> `examples/02-csv-to-data-product/customers.csv`, which are resolved relative to
> where you invoke `fluid`.

## Prerequisites

- The `fluid` CLI installed with the local provider — see the
  [installation guide](../README.md#installation). Verify with:

  ```bash
  fluid --version
  ```

## The 5-minute quickstart series

Work through these in order. Each one builds on the concepts before it, and each
produces a real CSV you can open.

| # | Example | Teaches | Time |
|---|---------|---------|------|
| 01 | [Hello World](01-hello-world/) | The smallest possible contract: one SQL `SELECT` → a CSV | ~2 min |
| 02 | [CSV to Data Product](02-csv-to-data-product/) | Ingest a CSV, clean and normalize it, publish the result | ~5 min |
| 03 | [Multi-Source Join](03-multi-source-join/) | Join two CSV sources and aggregate per customer | ~5 min |
| 04 | [External SQL Files](04-external-sql-files/) | Keep transformation SQL in a standalone `.sql` file | ~5 min |
| 05 | [Data Quality Validation](05-data-quality-validation/) | Drop bad rows with SQL guard clauses | ~5 min |
| 06 | [Time Windows](06-time-windows/) | Window functions: rolling averages and period-over-period | ~5 min |

### Run any example

Every quickstart example runs with the same two commands (swap in the folder you want):

```bash
# from the repository root
fluid validate examples/02-csv-to-data-product/contract.fluid.yaml
fluid apply    examples/02-csv-to-data-product/contract.fluid.yaml --provider local --mode amend-and-build --yes
```

`validate` checks the contract against the FLUID schema and provider rules; `apply`
with `--mode amend-and-build` runs the SQL locally on DuckDB and writes the output
CSV declared in the contract's `exposes[].binding.location.path` (under
`runtime/out/`).

## Beyond the quickstart

Deeper, real-world examples — each with its own README:

- [`aws-s3-glue-athena/`](aws-s3-glue-athena/) — S3 + Glue Data Catalog + Athena serverless data lake (Parquet).
- [`aws-iceberg-lakehouse/`](aws-iceberg-lakehouse/) — Apache Iceberg ACID table on Glue + S3, with time-travel.
- [`aws-medallion-lake/`](aws-medallion-lake/) — Bronze + Silver medallion zones in one AWS data product.
- [`aws-glue-data-lake/`](aws-glue-data-lake/) — Glue Data Catalog, Iceberg tables, ETL jobs on AWS.
- [`aws-redshift-kinesis-streaming/`](aws-redshift-kinesis-streaming/) — Kinesis Data Streams + Redshift Serverless + Spectrum external schema (v0.7.5 binding fields).
- [`mcp-output-port/`](mcp-output-port/) — expose a data product as a Model Context Protocol server.
- [`source-aligned-postgres-duckdb/`](source-aligned-postgres-duckdb/) — a source-aligned product reading from Postgres via DuckDB.
- [`bitcoin-price-api-declarative-part-b/`](bitcoin-price-api-declarative-part-b/) · [`part-c/`](bitcoin-price-api-declarative-part-c/) — an end-to-end declarative pipeline with observability and policy tags.
- [`0.7.1/`](0.7.1/) — schema-feature reference contracts (GDPR, AI-restricted data, backward compatibility, provider-action workflows).

---

New to FLUID? Start with **[01 · Hello World](01-hello-world/)**.
