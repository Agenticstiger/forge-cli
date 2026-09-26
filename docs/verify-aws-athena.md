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
| The table exists | `glue:GetTable` | error (always exit 1); in a reference-only contract, INFO and not counted (see below) |
| Columns match the contract | Glue columns + partition keys against `contract.schema`, types folded through the same Hive type map `fluid apply` declares them with | missing column / changed type: CRITICAL; extra column: INFO |
| The table reads the binding's prefix | Glue `StorageDescriptor.Location` against `s3://<bucket>/<path>` | CRITICAL |
| Athena can read it | `SELECT COUNT(*)` through Athena, polled until it finishes or the timeout passes | query FAILED, CANCELLED or timed out: error (always exit 1); a timed-out query is stopped, and the error says so only when the stop succeeded |
| It serves what the build landed | the count against the run records of the acquisition build that writes the expose (see below) | CRITICAL |
| It is not empty | a count of 0, with or without a run record | CRITICAL; in a reference-only contract with no run of its own to compare with, INFO (see below) |

CRITICAL fails `fluid verify --strict`; INFO fails only with `--fail-on-warning`.
An error fails `fluid verify` with or without `--strict`. Glue columns declare
no nullability, so the constraints dimension has nothing to compare and says so
in the JSON report.

A reference-only contract (a `builds[].pattern` of `reference`,
`hybrid-reference` or `external-reference`) leaves the rows to a pipeline
outside forge, which on the first run has not written yet. `fluid apply` still
creates the Glue table, so the table exists and is empty. That is reported
with `dimensions.row_count.status: info` and severity INFO, and it does not
fail the run under `--strict` or `--fail-on-warning`, the same as a missing
table in such a contract. The schema and location are still checked. When a
run of the contract's own acquisition build says it landed rows in the table,
an empty table fails as usual.

### Which run the table is held to

The build writes a run record into the contract directory's
`.fluid/runs/<contract.id>/<build.id>/runs/<run id>.json`. A CI pipeline that
runs verify in a separate stage has to carry that directory over for the
comparison to happen. The duckdb runner records, in `facets.landed`, what the
comparison needs:

```json
"facets": {
  "landed": {
    "mode": "full_refresh",
    "rows_from": "write",
    "destinations": {"public.product_subscription": "s3://northwind-demo-lake/bronze/customer_subscriptions/customer_subscriptions.parquet"}
  }
}
```

`destinations` maps each stream to the URI it wrote (a local file is recorded
relative to the contract directory). `rows_from: write` says `records_total` is
the row count DuckDB's `COPY ... TO` returned for the write; `source_count` is
a second read of the source, and `write_before_late_arrival_split` is the write's
count before the late-arrival split moved rows out of the file. Neither of the
last two is a count of what the destination holds.

The contract directory serves every target an overlay selects, so its records
mix local and cloud runs of the same build. Verify walks them newest first,
passes over runs whose destinations all lie outside this table's
`s3://<bucket>/<path>`, and takes the newest one inside it. The count is then
held to it by the mode that run recorded:

| Recorded mode | Rule (`compared_with.rule`) |
|---|---|
| `full_refresh` | `equal`: the count equals that run's `records_total` |
| `incremental_append` | `at_least_cumulative`: the count is at least the sum of `records_total` over the runs back to and including the last `full_refresh` run (`compared_with.runs` lists them). The walk also stops at a run that failed, did not count at the write, recorded another mode or does not say where it landed; the sum is still a floor, because an append removes no row. `compared_with.reached_full_load` says whether the walk reached a `full_refresh` run; when it did not (a CI stage has only the records it carried over), the message says earlier runs may be missing from the floor |
| anything else | `reported`: a merge, dedup, CDC or streaming load can update or delete rows, so the count bounds nothing |

Reported with the reason and never gated: a build that is not
`pattern: acquisition` (the dbt runner's `records_total` counts dbt nodes, not
rows); no run record; no run that landed in this table; a newest candidate run
that does not record its destinations (a run from before they were recorded, or
another engine); a run that did not succeed; and a run that did not count at the
write. In each case only an empty table fails.

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
silent fallback, and so is each of these, found before any AWS call: no region;
a region that is not an AWS region name; a database, table or bucket that is
still a `{{ env.* }}` template after resolution (the error names the variable);
a database or table name with anything but letters, digits and underscores (a
name may start with a digit: the query double-quotes both).

`{{ env.* }}` templates in the binding are resolved the way `fluid apply`
resolves them before it creates the table (`resolve_env_templates_in_contract`),
so verify looks for the table apply created. That resolver leaves a
placeholder whose name looks like a credential (`{{ env.X_PASSWORD }}`,
`_TOKEN`, `_SECRET`, `_API_KEY` and the rest of the redactor's list) literal,
and so does verify: these values go into the report, the console and
Athena's query history.

The region is the first of: `binding.location.region`, then `binding.region`
(a template still unresolved is passed over, as `fluid apply` passes over a
region value that is not a region code), `AWS_REGION`, `AWS_DEFAULT_REGION`,
and the region boto3 resolves from the AWS config profile. The target line names the source when it is not the binding,
for example `demo_bronze.customer_subscriptions (Glue + Athena, eu-north-1 from
the AWS config profile)`, and the JSON report records it as
`athena.region_source`.

Where Athena writes the query result, first match wins:

1. The workgroup has managed query results, or enforces its own output
   location: the workgroup's. Athena ignores the client's choice there.
2. `--athena-output-location` / `FLUID_ATHENA_OUTPUT_LOCATION`.
3. The workgroup's configured output location.
4. `s3://<binding bucket>/.fluid/athena-results/`. The leading dot keeps the
   files out of any Hive table listing over the bucket.

A location inside the table's own prefix (the Glue table's location or the
binding's) would put Athena's result files among the table's data. Whoever chose
it: an override or default there is refused; a workgroup that enforces one is
refused before the query starts, because Athena would not honour another; a
workgroup location there that is not enforced is replaced by the binding-bucket
default, with `athena.output_location_note` saying why. Each run leaves Athena's result objects (a
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
