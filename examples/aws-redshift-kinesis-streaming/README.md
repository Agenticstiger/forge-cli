# AWS Redshift Serverless + Kinesis Streaming Example

Real-time event ingest on **Amazon Kinesis Data Streams**, served from
**Amazon Redshift Serverless**, with a Glue-backed **Spectrum external schema**.

## What This Example Covers

| Exposure | `binding.location` fields | Emits |
|----------|---------------------------|-------|
| `events-stream` | `stream`, `region` | `aws_kinesis_stream` |
| `events-warehouse` | `namespace`, `workgroup`, `iam_role_arn`, `database`, `schema`, `table` | `aws_redshiftserverless_namespace` + `aws_redshiftserverless_workgroup` |
| `events-external` | `external_schema`, `glue_database`, `workgroup`, `iam_role_arn` | redshift-data `CREATE EXTERNAL SCHEMA` bridge |

These six `bindingLocation` fields are **new in fluid-schema v0.7.5** — the AWS
IaC emitter already read them (`iac/providers/aws.py`), but the schema's
`additionalProperties: false` rejected them at `fluid validate` time until this
example's supporting schema change landed.

## Prerequisites

- `fluid` CLI installed (`pip install data-product-forge`)
- For a real apply: an AWS account with Kinesis / Redshift Serverless / Glue / IAM permissions, and AWS credentials configured

## Quick Start

```bash
# 1. Validate (schema v0.7.5)
fluid validate contract.fluid.yaml

# 2. Review the generated OpenTofu (credential-free, deterministic)
fluid generate iac contract.fluid.yaml -o out/
cat out/main.tf.json

# 3. Validate the emitted IaC against the real hashicorp/aws provider (offline)
fluid generate iac contract.fluid.yaml --validate -o out/
```

## Verified

- `fluid validate` → passes (v0.7.5)
- `fluid generate iac --validate` → **OpenTofu validation passed** against the
  real `hashicorp/aws` provider schema (no credentials)
- The Kinesis slice **applies live on LocalStack** — the `acme-realtime-events`
  stream is created and confirmed via boto3. Redshift Serverless / redshift-data
  return `501` on LocalStack (a known emulator gap), so their live coverage is
  the real-AWS nightly (`FLUID_IAC_LIVE_AWS=1`); the offline `tofu validate`
  proves the module shape.
