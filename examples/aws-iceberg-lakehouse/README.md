# AWS · Apache Iceberg Lakehouse

> An ACID orders table on S3 using Apache Iceberg, cataloged in Glue and queried by Athena — updates, deletes, schema evolution, and time-travel over plain Parquet files.

Part of the **[FLUID examples](../README.md)**. · AWS provider · offline-reviewable IaC.

## What this shows

- The `format: iceberg` binding, which makes FLUID emit a Glue table tagged
  `table_type = ICEBERG`. That single parameter turns flat S3 object storage
  into a table that behaves like a database:
  - **ACID** — safe concurrent `INSERT` / `UPDATE` / `DELETE` / `MERGE`.
  - **Time-travel** — `SELECT … FOR TIMESTAMP AS OF …` / `FOR VERSION AS OF …`.
  - **Schema evolution** — add / drop / rename columns without rewriting data.
- An `iceberg:` management block — snapshot retention (bounded time-travel
  history) and compaction (keeps the small-files problem in check). Values
  follow community norms: 5-day retention, 256 MB target file size.

## Prerequisites

- The `fluid` CLI installed — see the [installation guide](../../README.md#installation).
- **Run the commands below from the repository root.**
- No AWS account or credentials are needed to validate and review the IaC.

## Run it

```bash
# from the repository root

fluid validate examples/aws-iceberg-lakehouse/contract.fluid.yaml

fluid generate iac examples/aws-iceberg-lakehouse/contract.fluid.yaml --out /tmp/aws-iceberg
cat /tmp/aws-iceberg/main.tf.json
```

## What gets generated

| Resource | AWS service | Purpose |
|----------|-------------|---------|
| `aws_glue_catalog_database.…sales` | Glue Data Catalog | The `sales` database |
| `aws_glue_catalog_table.…orders` | Glue Data Catalog | Iceberg `orders` table (`table_type = ICEBERG`) |
| `aws_s3_bucket.…acme_sales_lakehouse` | Amazon S3 | Backing object store |

Confirm the Iceberg wiring in the emitted module:

```bash
python -c "import json; t=json.load(open('/tmp/aws-iceberg/main.tf.json'))['resource']['aws_glue_catalog_table']; k=list(t)[0]; print(t[k]['parameters']['table_type'])"
# -> ICEBERG
```

## Time-travel, once applied

```sql
-- current state
SELECT * FROM sales.orders WHERE order_id = 'A-1001';

-- the same row as it looked yesterday
SELECT * FROM sales.orders FOR TIMESTAMP AS OF (current_timestamp - interval '1' day)
WHERE order_id = 'A-1001';

-- inspect the snapshot history Athena maintains
SELECT * FROM "sales"."orders$history";
```

## Architecture

```
writers ─(MERGE/UPDATE/DELETE)─▶  Iceberg table  s3://acme-sales-lakehouse/iceberg/sales/orders/
                                        │  (Parquet data files + Iceberg metadata/manifests)
                                        ▼
                               Glue Data Catalog   database: sales · table: orders (ICEBERG)
                                        ▼
                                    Athena / Redshift Spectrum / EMR
```

## Next steps

- ⬅️ **[aws-s3-glue-athena](../aws-s3-glue-athena/)** — the plain-Parquet
  version of this pattern.
- ➡️ **[aws-medallion-lake](../aws-medallion-lake/)** — Bronze + Silver zones in
  one data product.
- ⬅️ Back to the **[examples index](../README.md)**.
