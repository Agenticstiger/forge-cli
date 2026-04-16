# AWS Glue Data Lake Example

End-to-end data lake on AWS using Glue for cataloging, ETL, and Iceberg tables.

## What This Example Covers

| Contract | Description |
|----------|-------------|
| `contract-database.fluid.yaml` | Glue database + standard table with schema |
| `contract-crawler.fluid.yaml` | Glue crawler scanning an S3 prefix |
| `contract-iceberg.fluid.yaml` | Iceberg table with ACID & time-travel |
| `contract-etl-job.fluid.yaml` | Spark ETL job reading CSV → writing Parquet |

## Prerequisites

- AWS account with Glue, S3, and IAM permissions
- `fluid` CLI installed (`pip install fluid-forge`)
- AWS credentials configured (`aws configure` or env vars)

## Quick Start

```bash
# Validate all contracts
fluid validate contract-database.fluid.yaml
fluid validate contract-crawler.fluid.yaml
fluid validate contract-iceberg.fluid.yaml
fluid validate contract-etl-job.fluid.yaml

# Plan (dry-run)
fluid plan contract-database.fluid.yaml

# Apply
fluid apply contract-database.fluid.yaml
```

## Architecture

```
S3 Bucket (raw/)
    ↓  Glue Crawler
Glue Catalog (database + tables)
    ↓  Spark ETL Job
S3 Bucket (curated/)  →  Iceberg Table
    ↓
Athena Queries
```
