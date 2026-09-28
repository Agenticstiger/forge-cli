# `fluid verify` on a GCP BigQuery binding

An expose bound like this is provisioned by `fluid apply` as a BigQuery dataset
and table, and the build loads its rows into the table (an acquisition build
through the duckdb runner, an embedded-SQL build on DuckDB through the same
load):

```yaml
binding:
  platform: gcp
  format: bigquery_table
  location:
    project: northwind-demo
    dataset: demo_bronze
    table: customer_subscriptions
    region: europe-west1
```

The table verify reads is named the way the load names it
(`build_runners/_bigquery_load.py::bigquery_load_target`): `{{ env.X }}` in the
binding is resolved as `fluid apply` resolves it, and a binding with no
`project` uses `GOOGLE_PROJECT` / `GOOGLE_CLOUD_PROJECT` / `GCLOUD_PROJECT` /
`CLOUDSDK_CORE_PROJECT`, then Application Default Credentials' own.

| Check | How | Result when it fails |
|---|---|---|
| The table exists | `tables.get` | error (always exit 1); in a reference-only contract, INFO |
| Columns match the contract | the table's schema against `contract.schema`, types folded through the type map `fluid apply` declares them with | missing column / changed type: CRITICAL; extra column: INFO |
| Required / nullable | the table's field modes | WARNING |
| Location | the dataset's location against the binding's region | CRITICAL |
| It serves what the build loaded | `SELECT COUNT(*)` of the table, against the run records of the acquisition build that writes the expose (below) | CRITICAL |
| It is not empty | a count of 0 | CRITICAL; in a reference-only contract with no run of its own, INFO |
| Masked columns landed treated | for an expose with `policy.privacy.masking`, the same query counts each masked column's non-null values that lack their strategy's shape: `COUNTIF(col IS NOT NULL AND NOT REGEXP_CONTAINS(CAST(col AS STRING), @shape))`, the shape anchored `\A(?:...)\z` and bound as a query parameter | one such value: CRITICAL. Only counts leave the query, never a value |

The count query needs `bigquery.jobs.create` on the project
(`roles/bigquery.jobUser`) as well as read access to the table; a query that
cannot run is an error, not a pass. `metadata.num_rows` in the report is the
counted rows (the table's own `numRows`, which leaves out the streaming buffer
and which the goccy emulator leaves unset, is kept as `table_num_rows`).

### Which run the table is held to

The rules of the Athena verifier (`docs/verify-aws-athena.md`), with one
difference: a run is this table's when its record's `facets.bigquery_load`
names the table, which the duckdb runner writes after the load, with the rows
the load job (or, on an emulator, the count after it) says arrived. A run that
succeeded with no BigQuery load landed somewhere else (a local or aws run from
the same contract directory) and is passed over. The newest run that may be
this table's is compared: equal for `full_refresh`, at least the sum since the
last full load for `incremental_append`. A failed run, or one whose record
does not parse, is not a count to hold the table to, and the count is then
reported without a gate. An embedded-SQL build writes no run record, so its
table is counted and must not be empty, and is not compared.

### Emulators

With `BIGQUERY_EMULATOR_HOST` set, the client goes to that host with anonymous
credentials. The goccy bigquery-emulator runs the count query, the query
parameters and `REGEXP_CONTAINS`; it does not report `numRows`.
