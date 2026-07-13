# AWS · S3 + Glue + Athena Data Lake

> Catalog a Parquet dataset on S3 in the Glue Data Catalog and query it with Athena — the serverless AWS data-lake happy path.

Part of the **[FLUID examples](../README.md)**. · AWS provider · offline-reviewable IaC.

## What this shows

- A single `platform: aws` data product that FLUID compiles into real AWS
  infrastructure: an **S3 bucket** (storage plane), a **Glue Data Catalog
  database + table** (metadata plane), and — because Athena reads the Glue
  catalog natively — an **Athena-queryable** table with no extra resource.
- **Parquet** as the on-disk format, the single biggest lever on Athena scan
  cost (columnar + compressed ≈ an order of magnitude cheaper than raw text).
- An `accessPolicy` block that downstream compiles into IAM / Lake Formation
  grants, and a `contract.quality[]` block of inline SQL assertions.

## Prerequisites

- The `fluid` CLI installed — see the [installation guide](../../README.md#installation).
- **Run the commands below from the repository root.**
- No AWS account or credentials are needed to validate and to review the
  generated IaC — the emit is a pure function of the contract.

## Run it

```bash
# from the repository root

# 1. Validate the contract against the FLUID JSON schema
fluid validate examples/aws-s3-glue-athena/contract.fluid.yaml

# 2. Compile to an OpenTofu module (no cloud calls) and review it
fluid generate iac examples/aws-s3-glue-athena/contract.fluid.yaml --out /tmp/aws-lake
cat /tmp/aws-lake/main.tf.json
```

## What gets generated

`fluid generate iac` emits a credential-free `main.tf.json` with three resources:

| Resource | AWS service | Purpose |
|----------|-------------|---------|
| `aws_glue_catalog_database.…web_analytics` | Glue Data Catalog | The `web_analytics` database |
| `aws_glue_catalog_table.…pageviews` | Glue Data Catalog | The `pageviews` table + column schema |
| `aws_s3_bucket.…acme_web_analytics_lake` | Amazon S3 | Backing object store (curated zone) |

Apply it with OpenTofu (`tofu init && tofu apply`) once you have AWS
credentials — or apply through FLUID with `fluid apply`.

## Architecture

```
producers ──▶  S3  s3://acme-web-analytics-lake/curated/web_analytics/pageviews/  (Parquet)
                │
                ▼
        Glue Data Catalog   database: web_analytics · table: pageviews
                │
                ▼
             Athena   SELECT * FROM web_analytics.pageviews WHERE country_code = 'US'
```

## Customizing

- **Bucket name** — S3 bucket names are globally unique; change
  `binding.location.bucket` before applying.
- **Region** — `binding.location.region` defaults to `us-east-1`.

## Next steps

- ➡️ **[aws-iceberg-lakehouse](../aws-iceberg-lakehouse/)** — upgrade the table
  to Apache Iceberg for ACID updates and time-travel.
- ➡️ **[aws-medallion-lake](../aws-medallion-lake/)** — Bronze + Silver zones in
  one data product.
- ⬅️ Back to the **[examples index](../README.md)**.
