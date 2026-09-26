# `fluid verify` on an AWS S3 + Glue binding

An expose bound like this is provisioned by `fluid apply` as an S3 prefix plus a
Glue catalog table that points at it, and the build writes its rows into the
prefix:

```yaml
binding:
  platform: aws
  format: parquet
  location:
    database: demo_bronze
    table: customer_subscriptions
    bucket: northwind-demo-lake
    path: bronze/customer_subscriptions/
    region: eu-north-1
```

`fluid verify` (stage 9) checks it against the live account, in the binding's
region:

| Check | How | Result when it fails |
|---|---|---|
| The table exists | `glue:GetTable` | error (always exit 1) |
| Columns match the contract | Glue columns + partition keys against `contract.schema`, types folded through the same Hive type map `fluid apply` declares them with | missing column / changed type: CRITICAL; extra column: INFO |
| The table reads the binding's prefix | Glue `StorageDescriptor.Location` against `s3://<bucket>/<path>` | CRITICAL |
| Athena can read it | `SELECT COUNT(*)` through Athena, polled until it finishes or the timeout passes | query FAILED, CANCELLED or timed out: error (always exit 1); a timed-out query is stopped |
| It serves what the build landed | the count against `records_total` of the last successful run of the build that writes the expose (`.fluid/runs/<product>/<build>/runs/*.json` next to the contract), by the build's `properties.source.mode`: equal for `full_refresh`, at least as many for `incremental_append`, reported only for a merge, dedup, CDC or streaming build | CRITICAL |
| It is not empty | a count of 0, with or without a run record | CRITICAL |

CRITICAL fails `fluid verify --strict`; INFO fails only with `--fail-on-warning`.
An error fails `fluid verify` with or without `--strict`.
With no run record, or when the last run did not succeed, the count is reported
and only an empty table fails. The run record is written by the build into the
contract directory's `.fluid/`, so a CI pipeline that runs verify in a separate
stage has to carry that directory over for the comparison to happen. Glue
columns declare no nullability, so the constraints dimension has nothing to
compare and says so in the JSON report.

Only formats the Glue table is emitted with Hive storage classes for are checked
this way (today `parquet`, the format the builds write). Other formats keep the
"no verifier" answer, which is reported and never fails the run.

## Options

| Flag | Env var | Default |
|---|---|---|
| `--athena-output-location S3_URI` | `FLUID_ATHENA_OUTPUT_LOCATION` | see below |
| `--athena-workgroup NAME` | `FLUID_ATHENA_WORKGROUP` | `primary` |
| `--athena-timeout SECONDS` | `FLUID_ATHENA_TIMEOUT_SECONDS` | `300` |

The flag wins over the env var. An invalid value is a verification error, not a
silent fallback, and so is each of these, found before any AWS call: no region
(`binding.location.region`, else `binding.region`, else `AWS_REGION` /
`AWS_DEFAULT_REGION`); a region that is not an AWS region name; a database or
table name that is not a plain SQL identifier; a bucket that is an unresolved
`{{ env.* }}` template.

Where Athena writes the query result, first match wins:

1. The workgroup has managed query results, or enforces its own output
   location: the workgroup's. Athena ignores the client's choice there.
2. `--athena-output-location` / `FLUID_ATHENA_OUTPUT_LOCATION`.
3. The workgroup's configured output location.
4. `s3://<binding bucket>/.fluid/athena-results/`. The leading dot keeps the
   files out of any Hive table listing over the bucket.

A location inside the table's own prefix is refused, because the result files
would be read back as table data. Each run leaves Athena's result objects (a
one-row CSV and its `.metadata`) at the location used; an S3 lifecycle rule on
the prefix expires them. The JSON report records the location used and
why (`results.<expose>.athena.output_location_source`: `workgroup-managed`,
`workgroup-enforced`, `override`, `workgroup` or `binding-bucket`).

boto3 comes from the `aws` extra: `pip install 'data-product-forge[aws]'`.
Without it the check is an error that names the extra.

## IAM

The identity `fluid verify` runs as needs, with the region, account, bucket,
database and table replaced by the binding's:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AthenaCountQuery",
      "Effect": "Allow",
      "Action": [
        "athena:GetWorkGroup",
        "athena:StartQueryExecution",
        "athena:GetQueryExecution",
        "athena:GetQueryResults",
        "athena:StopQueryExecution"
      ],
      "Resource": "arn:aws:athena:eu-north-1:123456789012:workgroup/primary"
    },
    {
      "Sid": "GlueTable",
      "Effect": "Allow",
      "Action": ["glue:GetTable", "glue:GetDatabase"],
      "Resource": [
        "arn:aws:glue:eu-north-1:123456789012:catalog",
        "arn:aws:glue:eu-north-1:123456789012:database/demo_bronze",
        "arn:aws:glue:eu-north-1:123456789012:table/demo_bronze/customer_subscriptions"
      ]
    },
    {
      "Sid": "ReadTheData",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::northwind-demo-lake/bronze/customer_subscriptions/*"
    },
    {
      "Sid": "WriteTheResult",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": "arn:aws:s3:::northwind-demo-lake/.fluid/athena-results/*"
    },
    {
      "Sid": "ListBothPrefixes",
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketMultipartUploads"],
      "Resource": "arn:aws:s3:::northwind-demo-lake"
    }
  ]
}
```

* `athena:GetWorkGroup` is optional: without it the workgroup's configuration
  is not read and the result goes to the override or the binding's bucket (a
  workgroup that enforces its own location still wins on Athena's side).
* `athena:StopQueryExecution` is used only when the timeout passes or the run
  is interrupted.
* A partitioned table also needs `glue:GetPartitions` on the same resources.
* When the prefix is registered with Lake Formation, add
  `lakeformation:GetDataAccess` and a Lake Formation `SELECT` grant on the
  table.
* When the result goes to a workgroup or override location, grant the
  `WriteTheResult` actions there instead of on `.fluid/athena-results/`.
