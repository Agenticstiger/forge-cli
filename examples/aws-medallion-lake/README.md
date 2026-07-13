# AWS · Medallion Data Lake (Bronze + Silver)

> One data product, two medallion zones: raw device payloads land in a Bronze Glue table, cleaned/typed readings publish to a Silver Glue table — both on S3, both queryable in Athena.

Part of the **[FLUID examples](../README.md)**. · AWS provider · offline-reviewable IaC.

## What this shows

- A **multi-exposure** data product: a single `contract.fluid.yaml` declares two
  tables (two `exposes[]` entries) representing two zones of the medallion
  (Bronze → Silver) architecture.
- **Bronze** (`raw_readings`) — raw device payloads land as **CSV** in the
  `raw/` prefix, untyped (`value` and `reading_ts` are strings): preserve
  everything, transform nothing.
- **Silver** (`clean_readings`) — cleaned, typed, deduplicated readings as
  **Parquet** in the `curated/` prefix, with inline quality assertions.
- Both zones are cataloged in the Glue Data Catalog (separate `iot_bronze` /
  `iot_silver` databases) so Athena can query either.

The raw shape is illustrated by [`sample_raw_readings.csv`](sample_raw_readings.csv)
(the Bronze objects on S3 look like this — FLUID does not load it; it is here to
make the schema concrete).

## Prerequisites

- The `fluid` CLI installed — see the [installation guide](../../README.md#installation).
- **Run the commands below from the repository root.**
- No AWS account or credentials are needed to validate and review the IaC.

## Run it

```bash
# from the repository root

fluid validate examples/aws-medallion-lake/contract.fluid.yaml

fluid generate iac examples/aws-medallion-lake/contract.fluid.yaml --out /tmp/aws-medallion
cat /tmp/aws-medallion/main.tf.json
```

## What gets generated

| Resource | AWS service | Purpose |
|----------|-------------|---------|
| `aws_glue_catalog_database.…iot_bronze` | Glue Data Catalog | Bronze database |
| `aws_glue_catalog_table.…raw_sensor_readings` | Glue Data Catalog | Bronze raw table (CSV) |
| `aws_glue_catalog_database.…iot_silver` | Glue Data Catalog | Silver database |
| `aws_glue_catalog_table.…sensor_readings` | Glue Data Catalog | Silver curated table (Parquet) |
| `aws_s3_bucket.…acme_iot_lake` | Amazon S3 | Shared object store (raw/ + curated/ prefixes) |

## Architecture

```
devices ──▶  S3  raw/iot/sensor_readings/  (CSV, schema-on-read)
                     │
                     ├─▶ Glue  iot_bronze.raw_sensor_readings   ──▶ Athena (audit / reprocess)
                     │
            (ETL: parse, type, dedupe)
                     │
                     ▼
             S3  curated/iot/sensor_readings/  (Parquet)
                     │
                     └─▶ Glue  iot_silver.sensor_readings       ──▶ Athena (analytics / BI)
```

## Next steps

- ⬅️ **[aws-s3-glue-athena](../aws-s3-glue-athena/)** — the single-table
  starting point.
- ➡️ **[aws-iceberg-lakehouse](../aws-iceberg-lakehouse/)** — make the Silver
  (Gold) zone an ACID Iceberg table.
- ⬅️ Back to the **[examples index](../README.md)**.
