# Honestly Tested

Status of `forge-cli`'s IaC test coverage on `feat/opentofu-iac-autogen`.
Written so a reviewer can pick up cold: what's covered, what's NOT
covered, and where the upstream limitations live.

**Suite size, pinned at the release tag.** `v0.14.1` (`f41b57bd`) collects
**17,434** tests:

```
$ git checkout v0.14.1 && python -m pytest --collect-only -q | tail -1
17434/17443 tests collected (9 deselected) in 14.90s
```

Reproduce it before quoting it — the figure moves with every release, and a
count taken off `main` will not match the tag.

## Stage ladder

| Stage | Definition | Where it runs |
|---|---|---|
| **Stage 1 — unit** | Pure-function emit + `tofu validate` against generated `.tf.json`. No creds, no docker, no cloud. | Every developer laptop + every PR. |
| **Stage 2 — emulator** | Docker emulators of the cloud APIs (LocalStack for AWS, goccy/bigquery-emulator + fsouza/fake-gcs-server + gcloud pubsub for GCP). `tofu apply` round-trips where the emulator supports the provider; SDK-verified resource shape where it doesn't. | Local with docker; CI integration job. |
| **Stage 3 — real cloud** | Real AWS / GCP / Snowflake accounts. Triple-gated (binary on PATH + opt-in env + bootstrap IAM). Per-test resource isolation via UUID prefix + session sweeper. | Local with auth; CI nightly with OIDC/WIF. |

Every test stage rolls up to a single pytest invocation —
`pytest -m unit` / `-m integration` / `-m aws and integration` etc.
Markers declared in `pyproject.toml`.

## Coverage matrix

### AWS

| Tier | Tests | Status |
|---|---|---|
| Stage 1 unit | `tests/iac/test_iac_aws.py` + `test_iac_tofu_validate.py[aws]` | ✅ all green |
| Stage 2 LocalStack Ultimate | `tests/iac/test_iac_aws_localstack_e2e.py` | ✅ 13 round-trips |
| Stage 3 base | `tests/iac/test_iac_aws_real_e2e.py` | ✅ 11/11 — S3, Iceberg-on-Glue, Kinesis, Lambda, SFN, Redshift Serverless, Athena, dbt-athena CTAS, mesh dual-port, dry-run, idempotency |
| Stage 3 dbt-via-CLI | `tests/iac/test_iac_aws_real_dbt_mesh_cli_e2e.py` | ✅ 3 active (athena / redshift-VPC / mesh) + 1 documented skip (dbt-glue) |
| Stage 3 Lake Formation | `tests/iac/test_iac_aws_real_lakeformation_e2e.py` | ✅ 3/3 (location reg + grant, LF-TBAC, row+col filter) |
| Stage 3 CLI matrix | `tests/iac/test_iac_aws_real_cli_matrix_e2e.py` | ✅ 6/6 (validate, plan, dry-run, amend, tamper-reject, bypass) |
| Stage 3 idempotency | `tests/iac/test_iac_aws_real_idempotency_e2e.py` | ✅ (see Stage 3 §) |
| Stage 3 replace mode | `tests/iac/test_iac_aws_real_replace_e2e.py` | ✅ data-loss gate verified |
| Stage 3 brownfield | _file claimed but absent — see "deferred" §_ | ❌ deferred (`discover_imports()` returns `[]` so any test would be a no-op) |
| Stage 3 cross-account proxy | `tests/iac/test_iac_aws_real_cross_account_e2e.py` | ✅ 2/2 before `bucketPolicy` existed — consumer role assumes + LF + S3 bucket policy + Athena SELECT; un-granted principal correctly denied. The consumer is in the deployer's account, so under the new default (`bucketPolicy: cross-account`) it gets **no** bucket-policy statement and the test now asserts that; that version has **not yet been re-run live**. Uses the existing `binding.governance.lakeFormation.grants[]` block. |
| Stage 1 LF bucket-policy modes | `tests/iac/test_iac_lakeformation_bucket_policy.py`, `tests/iac/test_iac_lakeformation_bucket_policy_plan.py`, `tests/iac/test_iac_tofu_validate.py::test_lakeformation_bucket_policy_modes_pass_tofu_validate` | ✅ — the three `bucketPolicy` modes rendered (all-grantees byte-identical to the previous emit), `tofu validate` of each, and a real `tofu plan` against moto's STS proving the default keeps a cross-account grantee's statements and plans no bucket policy for a same-account one. |
| Stage 1 LF column grants | `tests/iac/test_iac_lakeformation_column_grants.py`, `tests/iac/test_iac_lakeformation_column_permissions.py`, `tests/iac/test_iac_lakeformation_column_grants_plan.py` | ✅ — the three grant shapes rendered (no columns: `table`; `columns`: `table_with_columns.column_names`, no wildcard; `excludedColumns`: `wildcard = true` + `excluded_column_names`; refused at emit: both together, a column name the expose's schema does not declare, an exclusion of every column, and a column limit on a binding with no `location.table`), and a real `tofu plan` against moto of all three. Plan, not validate: the excluded shape without the wildcard passes `tofu validate` and fails `tofu plan` with "Missing required argument", and the file pins that too. A column-limited grant is emitted and planned with `SELECT` only: Lake Formation takes nothing else on `table_with_columns` ("Permissions modification is invalid" on the demo lab's apply, 28 Sept 2026), a `DESCRIBE` beside it is dropped because the column-limited `SELECT` already lets the principal see the table and Lake Formation refuses `DESCRIBE` to a principal holding a partial `SELECT`, and `ALTER`, `DROP`, `DELETE`, `INSERT` or `ALL` beside a column limit is refused at emit, as is a second grant for the same principal on the same table (Lake Formation refuses the table-level permissions to it, a table-level `SELECT` would read the withheld columns, and the provider reads the pair back as one). `DESCRIBE`'s grant option beside a column limit is refused (no resource can carry it). `SELECT`'s grant option is kept beside `columns` and refused beside `excludedColumns`: the guide's pages disagree (the permissions reference: "you can't include the grant option if column filtering is applied"; the console page offers it for simple column-based access), and *Data filtering limitations* is the specific rule followed: "To grant SELECT with the grant option and column filtering, you must use an include list, not an exclude list." **Not proven here**: moto stores any permission on any resource and `tofu plan` checks none, so the permission rules rest on the AWS LF developer guide and that failed apply; the fixed grant has **not yet been applied live**, and `SELECT` with the grant option on an include list has **not been tried live** (a live `GrantPermissions` of it settles the conflict). |
| Stage 1 LF tags | `tests/iac/test_iac_lakeformation_tags.py`, `tests/iac/test_iac_lakeformation_tags_moto.py` | ✅ — a tag the contract defines is named in its association by reference (`key = aws_lakeformation_lf_tag.<name>.key`, the provider's documented pattern) with no `depends_on`, and a tag the platform owns by its literal key; the refusals at emit and at `fluid validate`; `tofu validate` of a platform-owned association. A real `tofu apply` / `plan` / `destroy` against moto: an association applied as 0.19.0 emitted it (literal keys and `depends_on`) plans no change under the references, and the state then records the tags as its dependencies; a tag the platform created beforehand is associated beside one the contract creates, reads back through `GetResourceLFTags`, plans clean and survives the destroy; a platform-owned key that does not exist fails the apply with `EntityNotFoundException`. **Not proven here**: moto checks that an associated key exists but not its value, so a wrong value on a platform-owned tag, which Lake Formation refuses, is not proven to fail; moto enforces no `ASSOCIATE` permission; and the platform-owned path has **not been applied live**. |
| Stage 1 retention + encryption at rest | `tests/iac/test_iac_aws_retention_encryption.py`, `tests/iac/test_iac_aws_retention_encryption_moto.py`, `tests/iac/test_iac_tofu_validate.py::test_retention_and_kms_pass_tofu_validate`, `tests/cli/test_verify_athena.py` (storage section) | ✅ — `exposes[].lifecycle {retention, expire: true}` and `binding.encryption.kms` (fluid-schema 0.7.6) rendered (prefix-scoped rules, never a whole-bucket rule; key, alias, default SSE-KMS with a bucket key; key policy), `tofu validate` of each shape, and a real `tofu plan` / `apply` / `destroy` against moto: the key policy evaluated against the applying account, an object written with no encryption header landing SSE-KMS under the product key, `fluid verify`'s storage checks passing and then failing on a changed rule and an SSE-S3 object, and dropping the fields deleting the lifecycle configuration and scheduling the key's deletion 7 days out. An existing key is planned against moto too: named by alias, S3 is handed its key ARN; a key pending deletion and an RSA key fail the plan on the SSE configuration's preconditions; the AWS managed key named by its key ARN (which passes the name check) fails it with registerLocation (moto records every key as customer managed, so the test sets its record of alias/aws/s3 to `AWS`). A brownfield bucket carrying an operator's lifecycle rule and SSE-S3 is adopted through `fluid apply`'s own `_adopt_existing`, and the plan shows both configurations as in-place updates. `fluid verify` is covered with botocore stubs, including a key that is not `Enabled` and lifecycle rules filtered by tag, size or a narrower prefix that expire objects sooner. **Not proven here**: moto enforces neither key policies nor Lake Formation, so Athena reading an SSE-KMS location through Lake Formation-vended credentials (the service-linked role's key-policy statement) rests on the AWS documentation and has **not been run live**. |
| Stage 1 GCP governance parity (retention, CMEK, column restrictions, mapped principals) | `tests/iac/test_iac_gcp_governance.py`, `tests/iac/test_iac_gcp_governance_plan.py`, `tests/iac/test_iac_aws_column_restrictions.py`, `tests/cli/test_verify_bigquery_governance.py` | ✅ — rendered and `tofu validate`d: partition expiration (never a table TTL), a Cloud KMS key ring, key, service-agent grant and the dataset and table keys, a Data Catalog taxonomy, policy tags and fine-grained readers, and non-authoritative dataset members with the contract's logical principals mapped by `binding.principals` (placeholders and unmapped principals refused). A real `tofu plan` / `apply` with terraform-provider-google against an in-process stand-in for the BigQuery REST API (`tests/iac/_fake_bigquery.py`; the goccy emulator crashes the provider on apply): adding retention or a key to a live table plans its replacement, which the data-loss gate refuses without `--allow-data-loss`; a new retention is in place; the governed table plans clean; the dataset keeps its own access entries. The same stand-in shows a revoked reader is a member destroy the data-loss gate lets through, and that the first apply after the authoritative access list of 0.16.6 and earlier revokes a grant removed in the same change and then plans clean. AWS: the restriction becomes each Lake Formation grant's excluded columns, a real `tofu plan` against moto accepts them, and `fluid verify`'s Lake Formation check runs against moto's stored grants (and against a listed `TableWildcard` grant). `fluid verify` on BigQuery is covered with real `google.cloud.bigquery` Table/Dataset objects and a fake Data Catalog session. **Not proven here**: nothing ran against real BigQuery, Cloud KMS, Data Catalog or Lake Formation; no emulator enforces policy tags, keys or IAM, so a denied principal's refusal, the service agent's use of the key and a load into a tagged column rest on the providers' documentation. |
| Stage 2 cross-account (two-account LocalStack) | `tests/integration/test_iac_cross_account_localstack.py` | ✅ 10 tests, gated on `FLUID_IAC_LIVE_XACCT=1` + a second LocalStack Pro carrying `lakeformation`. Producer stack applies in account A (`000000000000`); every grant names a role in account B (`222222222222`) — LocalStack derives the account from the access-key id. Verified live: LF permission + LF prefix-registration + `aws_s3_bucket_policy` all land and read back; the shared-**pool** bucket is referenced via `data.aws_s3_bucket`, its `GetObject` grant is scoped to `location.path` and its `ListBucket` carries the `s3:prefix` condition. Under `ENFORCE_IAM=1` the emitted bucket policy is proven to be the **deciding** access control: with a deliberately broad identity policy on the account-B role, the granted prefix reads and the sibling tenant's prefix is denied — and widening only the bucket policy flips that denial (causal control). **Still NOT proven here**: LF cross-account *authorization* (LocalStack rejects `GrantPermissions` under IAM enforcement even for a registered `DataLakeAdmin` — `docs/upstream-issues/localstack-lakeformation-grant-auth.md`), and cross-account Glue catalog sharing (no AWS RAM in LocalStack; account B gets `EntityNotFoundException`, which is state isolation, not a denial). |
| Stage 1 Glue catalog enrichment | `tests/iac/test_iac_aws.py::TestAwsGlueCatalogEnrichment` (6) + `test_iac_tofu_validate.py[aws]` | ✅ — `aws_glue_catalog_table.description` + per-column comments + fluid_layer/fluid_product_type/fluid_domain/fluid_version/fluid_contract/forge.pii.<col> parameters, absorbed from the retired `GlueCatalogRegistrar`. **Zero new schema fields** — reads existing `description`/`metadata.description`/`metadata.layer`/`metadata.productType`/`domain`/`fluidVersion`/`column.tags[]`/`column.description`. |
| Stage 3 Glue catalog enrichment | `tests/iac/test_iac_aws_real_e2e.py::test_real_iceberg_on_glue_round_trip` | ✅ live — Description + Parameters + per-column Comments + `forge.pii.amount` verified via boto3 GetTable |

### GCP

| Tier | Tests | Status |
|---|---|---|
| Stage 1 unit | `tests/iac/test_iac_gcp.py` + `test_iac_tofu_validate.py[gcp]` | ✅ 24 green in 7.7s |
| Stage 2 docker emulator | `tests/iac/test_iac_gcp_emulator_e2e.py` | ✅ 8 hybrid (emit via plan + emulator via SDK) + 1 documented xfail — see [Upstream limitations](#upstream-limitations) |
| Stage 2 cross-project `access[]` | `tests/iac/test_iac_cross_project_gcp_emulator.py` | ✅ 5/5 — the **emitted** cross-project `user_by_email` entry (incl. two distinct consumer projects) round-trips through goccy/bigquery-emulator intact. **Storage fidelity only**: the emulator performs no validation (accepts `role: NOT_A_ROLE` + a non-email principal, HTTP 200) and no IAM evaluation (unauthenticated query succeeds) — both pinned executably by `test_emulator_neither_validates_nor_enforces_access`. Real IAM/authorization still needs a second GCP project (Stage 3 row below). |
| Stage 3 base | `tests/iac/test_iac_gcp_real_e2e.py` | ✅ 7/7 (BQ dataset+table, BQ view, GCS, Pub/Sub, multi-exposure, dbt-bigquery via CLI, full mesh) |
| Stage 3 CLI matrix | `tests/iac/test_iac_gcp_real_cli_matrix_e2e.py` | ✅ 8/8 — the 6 above, plus `fluid verify` in both directions: passes against a matching live table, and **fails** `--strict` when the contract declares a column that was never deployed |
| Stage 3 idempotency | `tests/iac/test_iac_gcp_real_idempotency_e2e.py` | ✅ (see Stage 3 §) |
| Stage 3 replace mode | `tests/iac/test_iac_gcp_real_replace_e2e.py` | ✅ |
| Stage 3 brownfield | _file claimed but absent — see "deferred" §_ | ❌ deferred (`discover_imports()` returns `[]` so any test would be a no-op) |
| Stage 3 cross-project proxy | `tests/iac/test_iac_gcp_real_cross_project_e2e.py` | ⚠️ written; user runs (needs `gcloud auth application-default login` + `tofu apply` of bootstrap update to materialize the `fluid-iactest-consumer` SA). **Zero new schema fields** — uses the existing `metadata.policies` surface; cross-project SAs ride BQ's `user_by_email` access entry. |

### Snowflake

| Tier | Tests | Status |
|---|---|---|
| Stage 1 unit | `tests/iac/test_iac_snowflake.py` + `test_iac_tofu_validate.py[snowflake]` | ✅ pre-existing |
| Stage 1 emulated (in-process) | `tests/providers/test_snowflake_emulated_happy_path.py` | ✅ 1 — `SnowflakeConnection` connect → DDL → DML → select under `fakesnow.patch()`. **Proves forge-cli calls the connector correctly and nothing below that**: the patch replaces snowflake-connector-python in-process, so the connector's own code never executes. |
| Stage 1 emulated (over the wire) | `tests/providers/test_snowflake_server_emulated_happy_path.py` | ✅ 2 — the **unpatched** connector against fakesnow's Starlette server speaking the Snowflake HTTP wire protocol: real login request, real query submission, real result-set decoding, and `_initialize_session`'s `USE` statements actually reaching an endpoint. Only `host`/`port`/`protocol` are injected, because the connector derives its URL from the account name and offers no override. Keyless, free, runs on every PR including forks. **Fidelity ceiling unchanged**: fakesnow executes on DuckDB and accepts a more liberal dialect than Snowflake, so Snowflake SQL correctness is still Stage 3's job. Considered and rejected: LocalStack for Snowflake, a separate licence at $29–35/user/month, for what this covers at $0. |
| Stage 3 live | `tests/iac/test_iac_snowflake_live.py` | ✅ pre-existing |
| Stage 3 CLI matrix | `tests/iac/test_iac_snowflake_real_cli_matrix_e2e.py` | ⚠️ 2/6 — plan-binding tamper-reject + bypass only; validate / plan / dry-run / amend not ported (see "deferred") |
| Stage 1 catalog enrichment | `tests/iac/test_iac_snowflake.py::TestSnowflakeCatalogEnrichment` (3) | ✅ — Horizon markdown table COMMENT + per-column comments on `snowflake_table`, absorbed from the retired `SnowflakeHorizonRegistrar`. **Zero new schema fields** — reads existing `description`/`metadata.description`/`metadata.layer`/`metadata.productType`/`column.description`. Declarative tag governance deferred (see "deferred" §). |
| Stage 3 catalog enrichment live | `tests/iac/test_iac_snowflake_live.py::test_live_table_carries_horizon_markdown_comment` | ✅ — apply against real Snowflake (snowflake-biz-lab creds), `SHOW TABLES` exposes the markdown comment, `DESC TABLE` exposes per-column comments. Confirms the Stage 1 emit lands on the live wire. |

### Databricks

| Tier | Tests | Proven | Not proven here |
|---|---|---|---|
| Stage 1 unit | `tests/iac/test_iac_databricks.py`, `test_iac_databricks_jobs.py`, `test_iac_databricks_auth.py`, `test_iac_databricks_engine_phase2.py`, `tests/test_util_cron.py`, `tests/build_runners/test_databricks_job_builds.py`, `tests/engines/test_dbt_databricks.py`, `tests/cli/test_verify_databricks.py`, `tests/test_doctor_databricks.py`, `tests/test_welcome_scan_databricks.py` | ✅ 521 tests in the Databricks files above (139 tables, 149 jobs, 56 credentials, 47 engine hooks, 45 cron, 31 runner, 40 verify, 14 doctor and welcome scan), plus the dbt profile and type tests. The emitted module: a `databricks_catalog` data source, the owned schema, a managed Delta table, per-principal grants with best-effort imports, the provider's `retry_timeout_seconds`. A declared `binding.location.region` (validated, lower-cased; `databricks-region-invalid` and `databricks-region-conflict` refused) adds `data.databricks_current_metastore` and a precondition on each owned schema; with none the module is unchanged. A contract's governance labels (`iac/governance_labels.py`, the AWS and GCP source) are one `databricks_entity_tag_assignment` per tag on each owned schema (the contract's own) and each table (the expose's too), `-` read as `_` in a key; two labels meeting at one key are `databricks-tag-collision`, more than 50 tags on a securable `databricks-tags-too-many`; a contract without labels gets the module it always did; each assignment has a best-effort import (`entity_type,entity_name,tag_key`); removing one is not data loss. Databricks is a region-placed platform for the `sovereignty` checks (`tests/policy/test_sovereignty_enforcement_mode.py`). Each dbt build on Databricks becomes a serverless job with its workspace files, mapped from the build: Quartz cron, triggers, retries, notifications, timeout and run-as. The project upload's allowlist and refusals; the escaped file names. Every refusal `fluid validate` and the emitter share. The credential policy (host, profile, auth type, blanked variables) and the SDK kwargs. The in-place data-loss and protocol-upgrade readings of a saved plan. Job and file removals left ungated; the `USE CATALOG` notice; the drift report for dbt-written tables. `fluid verify`'s Databricks inspector against a stubbed client (residency: pass, mismatch, skipped; governance tags: pass, a missing tag, a wrong value, the paginated list read to its last page with an empty page in the middle, skipped); the doctor rows and welcome-scan detection; the dbt runner leaving a Databricks job build to its job. | What Databricks accepts (the manual live row). The real databricks-sdk is neither in the dev install nor in CI, so a stubbed client stands in for it; one test answers as the live workspace did, with the privilege under the group that conveys it. |
| Stage 1 `tofu validate` | `tests/iac/test_iac_tofu_validate.py` `[databricks]`, `test_a_databricks_dbt_job_passes_tofu_validate[git\|workspace]` | ✅ Both job modules (GIT with schedule, retries, notifications and run-as; WORKSPACE with a continuous trigger and files) and the dbt-written table's `ignore_changes` list validate against provider 1.140.0's schema. | Anything the provider checks only at plan or apply. |
| Stage 1 real tofu, stand-in workspace: tables | `tests/iac/test_iac_databricks_fake_e2e.py` (`tests/iac/_fake_databricks.py`) | ✅ 28. The real `fluid` CLI, `tofu` and databricks/databricks 1.140.0 against an in-process stand-in for the Unity Catalog, SQL statement and permissions APIs. validate / plan / `generate iac --validate` / dry-run route to OpenTofu, and a second plan is empty. A partition change (a replace), a dropped column and a positional rename are refused without `--allow-data-loss`; a grant revoke is not. A column reorder is refused even with the flag. An added column applies in place. A table that exists with column mapping is adopted, not replaced; without it the apply is refused (with or without the flag) until its owner runs the `ALTER`, and the refused run, like a dry-run, leaves nothing it imported in state. Existing grants are adopted with their table: the adoption plan is `+0 ~1 -0` (the provider imports neither the table's properties nor its warehouse id, so the first apply writes back the values it already has), and the plan after it is empty. A privilege the contract does not name shows as an update that removes it. A declared `location.region` plans and applies when the stand-in's metastore is in it (case-insensitively) and fails the plan, naming both regions and `DATABRICKS_HOST`, before anything is written when it is not. A type change and a missing catalog fail at plan or import, and a missing host or credential fails before `tofu` starts; each names the fix before any change. `fluid diff` initialises with the committed lock. A contract's governance labels are created as tag assignments on the schema and the table in the stand-in and a second plan is empty; a changed value updates the tag in place; a removed label destroys only its assignment, without `--allow-data-loss`; an assignment already in the workspace (state lost, or set by hand with another value) is adopted and its value corrected, never a failed create, and a tag someone else set is left alone. | The stand-in models the requests provider 1.140.0 sends: SQL dialect, warehouse state and privilege names are not checked. Its metastore region is whatever the test sets; whether a real workspace's `metastore_summary` reports the region in the spelling a contract declares (`eu-central-1`, `westeurope`) is not proven: the residency check and the verify dimension were not run against a live workspace; the governance tags and their verify dimension were not run live either. The tag endpoints are modelled on the SDK's source (databricks-sdk-go v0.190.0, the one provider 1.140.0 builds on), not on a live workspace: the live tier was not run for tags, so what real Databricks accepts as a tag key or value, whether it keeps a table's tags across a dbt CREATE OR REPLACE (undocumented), and whether the import id `entity_type,entity_name,tag_key` resolves a live assignment are unproven; `fluid verify`'s governance-tags dimension is the gate for the last. It stores the table properties it was sent, so the adoption check reads `properties` / `effective_properties` as a real workspace may not fill them. |
| Stage 1 unit: Databricks column masks and row filters | `tests/iac/test_iac_databricks_access.py` (the SQL), `tests/iac/test_iac_databricks_enforce.py` (the hooks and the engine's gate), `tests/cli/test_verify_databricks.py` (21 of its 61), `tests/iac/_databricks_governed.py`, `tests/iac/_fake_databricks.py` | ✅ 97 + 101 + the verify dimension's. The job pause is decided once, in `emit`, and the notice reads that decision; `fluid verify` fails the jobs dimension for a job paused while its table's masks or row filter are not in place; a plan that fails the residency check prints one line with the declared region, the live region and the fix (`databricks-residency-region`). A Customer-Profile-360-shaped contract (six denied columns, a `msisdn` mask, a consent row filter, principals mapped to `fluid-demo-lab-*` groups) compiles to the exact `CREATE FUNCTION IF NOT EXISTS` / `ALTER TABLE ... SET ROW FILTER ... ON (...)` / `ALTER COLUMN ... SET MASK` statements. The function bodies are evaluated in DuckDB (transpiled from the Databricks dialect, `is_account_group_member` stubbed): a restricted group reads only the rows its predicate keeps, a member of several filtered principals reads the union, everyone else every row, a NULL predicate hides the row from a member; `last_four` / `first_four` / `nullify` and a deny give the BigQuery-matching values, a value of four characters or fewer is its SHA-256, a NULL stays NULL, a deny beats a mask, and a mask rule belongs to the principal that names it (nullify for one group and last_four for another on one column: strictest branch first, a member of both reads NULL). Every identity is tested as a group and as a user, so a `group:` principal mapped to a service principal's application ID or a user's email is still restricted (DuckDB's own session user stands in for a service principal that is in no group). Names carry a hash of the definition: the same contract names the same functions, a changed predicate or rule names a new one. Every refusal kind, 30 predicate injection attempts (`;`, comments, subqueries, session functions, unknown or qualified columns, a backslash-escaped quote) and a principal name with a quote or a backslash. The hooks against the stand-in (statements, Tables API reads): pre_apply binds existing tables and leaves a missing table to post_apply, a dry run prints and executes nothing, a second run executes nothing, a changed predicate binds a new function and, after an apply that succeeds, drops the old one (only when no table in the schema uses it and this run's identity owns it; never after a failed apply or bind, nor in a schema whose tables cannot all be read; a drop that fails is named and the apply still succeeds), a removal drops only what this module bound, a statement needing a column the apply adds waits for post_apply, a failed pre_apply stops the run and revokes nothing, a failed post_apply binds a deny-all row filter for the restricted identities (SELECT untouched), and when that cannot be bound, or would replace a filter not made here, revokes SELECT and ALL PRIVILEGES from every grantee but the owner and this run's identity (naming an owner it could not read, an owner the contract restricts as a path still open, and a permission listing that shows no `inherited_from_*` field as undetermined), lists the schema and catalog grants still open with the statement to run, and reports a revoke that failed or a grant listing that failed; a function pre-created under any hashed name (row filter, mask, deny-all) by another owner, with another body, parameters or return type, is never bound; a row filter or mask set by hand is never replaced, with nothing changed on any table; a Tables API read that fails for one table (503, 429) is a failed bind for it and the next table is still tried; a filter needing a column the apply adds is held by the deny-all filter in pre_apply; a dbt job (cron or continuous) writing a table that does not exist, is unbound, has a wrong function or cannot be read is created PAUSED, and a manual job or a job writing no restricted table is left alone, after a `tofu apply` that failed part way, post_apply binds what exists, drops nothing, skips a table never created without a NOT REVOKED message, and the engine prints an enforcement failure there without raising it; the SQL statement poller (running, failed, cancelled, timed out, credentials scrubbed). The engine calls the hooks in order around `tofu apply`. `fluid generate iac` refuses a module with masks or filters (emit stays pure). `fluid verify`'s row-filter-and-masks dimension, including the owner, body, parameters and return type of every bound function (pass, no filter, another function, other input columns, missing and wrong mask, case and backticks ignored, skipped, the CLI output and `--strict`). | What Databricks accepts. The stand-in executes the statements as this module sends them and models the Unity Catalog refusal of `CREATE OR REPLACE FUNCTION` on a bound function, the preserved bindings across `CREATE OR REPLACE TABLE`, and the Tables API fields `row_filter.function_name` / `input_column_names` and `columns[].mask.function_name` (databricks-sdk-go v0.190.0), all from the docs and the SDK source, not from a live workspace. DuckDB evaluates the bodies, not Databricks: `sha2`, `right`, `left` and `is_account_group_member` are Databricks' own. |
| Stage 1 real tofu, stand-in workspace: masks and row filters | `tests/iac/test_iac_databricks_fake_e2e.py` (17 of its 45; needs databricks-sdk, the `[databricks]` extra, and skips without it) | ✅ 17. The real `fluid` CLI and `tofu` apply bind the filter and masks (after the apply, on a table it created); a second apply executes no statement; a table that already exists is bound before the apply's first grant on it; `--mode dry-run` prints the statements and executes none; a changed predicate binds a new function and the apply drops the old one; a removed restriction is dropped and said to widen access without `--allow-data-loss`; a binding that fails revokes SELECT from the restricted principal only, fails the run (`opentofu_access_enforcement_failed`), and the next apply binds before it grants again; a failure before the apply stops it with nothing applied; an invalid predicate is refused before anything runs; an apply that fails after creating the table (the grant on it refused) still binds the filter and masks and the run exits with `opentofu_apply_failed`, an apply that fails before the table exists binds nothing and revokes nothing, and a failed apply drops no binding the contract no longer asks for (it is dropped by the next apply that succeeds); a function taken under the hashed name stops the apply before it runs; a row filter set by hand stops it and is left in place; a binding that fails with no deny-all possible revokes every grantee but the owner and this run's identity and the next apply binds first and grants again; a failed mask leaves the deny-all filter and every grant until the next apply; a filter needing a column the apply adds is held by the deny-all filter, then replaced, in that order around the added column; a dbt job created with its restricted table is paused and the next apply unpauses it in place; `generate iac` refuses the module. | Live behaviour: that `CREATE FUNCTION IF NOT EXISTS`, `SET ROW FILTER` and `SET MASK` run on a real warehouse as modelled, that a second `SET` replaces a binding already on the table (if it does not, a changed predicate fails closed with the old binding in place), that `current_user()` of a service principal is its application ID, that a Free Edition or classic warehouse can run them, and whether the SDK's statement API answers as the stand-in does; whether `SET ROW FILTER ... ON ()` (a zero-column function) is accepted; whether FunctionInfo's `routine_definition`, `input_params` and `full_data_type` come back in the form compared (whitespace-normalised, lower-case types), whose mismatch would refuse a good function; whether `scim/v2/Me` `userName` is the owner `CREATE FUNCTION` records for a service principal; that `SET` replaces a bound filter or mask (the stand-in does; none of the fixes uses a DROP first, which would leave a window with no filter); the permissions and effective-permissions fields used to lock a table; and the pause behaviour of a job on a live workspace. Unproven for ALL_PRIVILEGES grantees on a live workspace: a revoke of ALL PRIVILEGES removes more than read access, by design of the lock. A principal that is a metastore admin or the table's owner is never restricted by these bindings. |
| Stage 1: Databricks bronze landing and masking at landing (live: the 2026-11-04 row) | `tests/build_runners/test_duckdb_databricks_load.py` (the runner and the loader), `tests/iac/test_iac_databricks_masking_at_landing.py` (the emitter), `tests/iac/test_iac_databricks_landing_fake_e2e.py` (real `fluid` + `tofu` + databricks-sdk; skips without the `[databricks]` extra), `tests/iac/_fake_databricks.py` | ✅ A `platform: databricks` binding no longer writes a local file: the run stages Parquet under `.fluid/staging`, uploads it to `DATABRICKS_STAGING_VOLUME` through the Files API, runs one `INSERT OVERWRITE ... read_files` over the one file it uploaded (a file someone else puts in the run's directory is not loaded) and deletes the staged files (also after a failed upload, a failed or short load and a Ctrl-C, which cancels the statement first; one that cannot be deleted fails the run naming it first), then removes the build's and the product's staging directories when each is listed empty (never one another run or build still uses; a failure to remove one is logged, not fatal), and deletes the local masked copy under `.fluid/staging/<build>/` after the load attempt, success or failure (`facets.databricks_load.local_staging_deleted`; one that cannot be deleted fails the run naming it); each load first deletes an earlier run's staging of the same build once all of it is over an hour old, and leaves a recent one. The statement's text, quoting and column list are pinned, as are the volume's validation, the staging path (no contract id climbs out), the refusals before any work (unset volume or warehouse, mode, sink, stream count), a missing table (names `fluid apply`, uploads nothing), credential scrubbing and the matrix of when the emitter accepts `policy.privacy.masking` (every writer a DuckDB acquisition build) against when it refuses it, through both `validate` and `emit`. End to end against the stand-in: `fluid apply` creates the table with its mask and row filter, `--mode amend-and-build` loads the rows with the masked column hashed and the cleartext absent, the volume is empty, nothing but the load runs on the warehouse, the table is not replaced, a second plan is empty, and a second run overwrites the rows. | **Not run live.** The stand-in is not Databricks: it reads the uploaded Parquet with DuckDB, so what `read_files` accepts, that `INSERT OVERWRITE` keeps a managed table's row filter and column masks, implicit casts from the Parquet types to the table's, the Files API's limits and a real warehouse's permissions are unproven. The run-time schema gate reads a declared `string` as a type change against DuckDB's `VARCHAR`, so a contract declares `varchar` for a text column. |
| Stage 1 real tofu, stand-in workspace: dbt jobs | `tests/iac/test_iac_databricks_jobs_fake_e2e.py` | ✅ 6. A local dbt build applies as one serverless job plus the workspace files dbt reads, each in a folder created before it (no upload meets a missing parent) (never `profiles.yml`, `target/` or a hidden file), and a second plan is empty. A changed model updates its file in place. A GIT build runs its tag on a continuous trigger and uploads nothing. Turning a job build local removes the job, its files and their folders without `--allow-data-loss`, and says so; a folder holding a file the apply did not put there is not deleted. The table a dbt build writes keeps what dbt's CREATE OR REPLACE made of it, and the apply reports the column drift instead of failing on a type change. | Whether Databricks accepts and runs the job: serverless environment version 2, the `dbt-databricks>=1.8.0,<2.0.0` range, the commands, a WORKSPACE `project_directory` under `/Workspace`, `run_as` permissions, a Git credential for a private repository, `dbt deps` with a local package (not uploaded). The manual live row proves the first four. Whether the jobs API echoes a server default the stand-in does not (the fake's builder found `performance_target` and an all-false `notification_settings` would each plan an update), which would make a second plan non-empty (live, it was empty). What dbt's CREATE OR REPLACE does to a table with column mapping (the module sets none on a dbt-written table). Not bound by a plan: an apply uploads the dbt project as it is when it runs (`planDigest` and `bundleDigest` cover the contract, not the project's files, as for a local dbt run), and a bundle does not carry the project. Known gap, by design: a job is not adopted. Its import id is a job id, which neither the contract nor `tofu` can look up by name, so an apply that lost its state creates a second job. `fluid verify` flags the duplicate (a stubbed client proves it). Workspace files need no import: the provider's create overwrites. |
| Stage 1 unit: Databricks retention (`lifecycle.expire`) | `tests/iac/test_iac_databricks_retention.py`, `tests/cli/test_verify_databricks_retention.py` | ✅ 195 + 18. ISO 8601 durations (years and months at their shortest, P1Y = 365 and P1M = 28 days; a fraction, a sign, a lower-case letter, `PT`, `P1DT`, out-of-order and over-long numbers refused), Delta `interval` properties (rounded up, months refused) and the history as the larger of the two properties with Delta's defaults. A dbt model's `materialized` resolved from `config()` in the SQL, the model's YAML `config:` and `dbt_project.yml` (the `+` prefix or none, root, project, folder and model-name levels, the most specific wins, SQL over YAML over project), the default `view`, the model found by alias or file name, commented-out and `raw` configs ignored, a `config()` inside a Jinja block, `var()`, `**`, a custom schema or database, a Python model, a versioned or twin model, a linked file and a Git repository refused as undeterminable. The build's `--select` must include the resolved model (name, `fqn`, `path:`, `*`, unions and intersections; a graph operator on another node, a tag, a config or run state is refused as not evaluable). Accepted: a dbt `table` model and a DuckDB acquisition with P30D, P365D, P6Y; refused: P7D, P29D, P4W, P1M, a history set longer than the retention, `incremental`, `snapshot`, `view`, another acquisition engine, no writer, a mixed set of writers. `fluid validate` and the emitter agree on kind and message. `fluid verify`'s retention dimension against a stubbed client and, over HTTP, the stand-in workspace (pass, fail with the `ALTER TABLE`, the runtime property over the declared one, skip, predictive optimization off as a warning). | Live behaviour: no real Databricks endpoint was called. Whether Unity Catalog's GetTable serves a table's `delta.logRetentionDuration` and `delta.deletedFileRetentionDuration` in `properties` or `delta_runtime_properties_kvpairs` (read from databricks-sdk-go v0.190.0 `model.go`, not observed), whether `effective_predictive_optimization_flag` is filled for a managed table, that predictive optimization runs `VACUUM` within the retention (a Databricks guarantee, not tested), and that dbt-databricks `table` replaces the table each run. Delta history is only half the promise: the acquisition acceptance rests on the landing being an `INSERT OVERWRITE` of the whole table in the one mode it runs (`full_refresh`, read from the landing's own `LOAD_MODES`; another source mode is refused as `incremental`, none as `unmapped`), which the bronze landing row covers against the stand-in, not live. A GetTable answer with no properties is a verify warning (Delta's defaults assumed). Embedded-logic DuckDB builds, `dlt`, `airbyte` and the other acquisition engines are refused. Selection is matched on names and paths only, not on dbt's graph: `+other+` is refused even if `other` is upstream of the model. |
| Stage 1: Databricks Unity Catalog metadata (comments, product tags, column tags; live: the 2026-11-04 row) | `tests/iac/test_iac_databricks_metadata.py` (the emitter), `tests/iac/test_iac_databricks_metadata_fake_e2e.py` (real `fluid` + `tofu` + databricks/databricks 1.140.0 against `tests/iac/_fake_databricks.py`), `tests/cli/test_verify_databricks.py` (the comment and column-tag cases) | ✅ 43 + 5 + 7. The table's `comment` is the expose's description, else the product's (its `purpose` when structured), each described column's `comment` its schema description, stripped; none on a table a dbt build writes (dbt's CREATE OR REPLACE owns them; `fluid validate` warns). The product's identity and owner team are tags on the table only (`fluid_product_id`, `fluid_product_name`, `fluid_domain`, `fluid_layer`, `fluid_product_type`, `fluid_product_version`, `fluid_version`, `fluid_owner_team`), an unstated field makes none, and they count with the governance labels toward 50; `lifecycle.state: deprecated` (the expose's, else the product's) sets `system.certification_status = deprecated`, else the label `certification: certified` (the expose's, else the product's) sets `certified` and is no plain tag (a product-level label certifies every expose's table, as intended; an expose-level one only its own); another label value is `databricks-certification-invalid`; a plan notice says the identity needs ASSIGN on that governed tag. Against the stand-in: certified, then deprecated in place, then removed. A classified column (`sensitivity`) carries `fluid_sensitivity` as a `columns` tag named `catalog.schema.table.column`, imported best-effort by the provider's id. Refused through `validate` and `emit` alike: a backslash in any comment (`databricks-comment-unsafe`; the provider's `parseComment` escapes only quotes) and a `${` (Spark variable substitution), a label meeting a product key (`databricks-tag-collision`), a value over 256 characters (`databricks-tag-value-too-long`), over 1,000 column tags (`databricks-tags-too-many`); a `${...}` in a tag value is rendered as a literal. Against the stand-in: apply writes the comments (an apostrophe included, through the provider's escaping), the product tags and the column tag, never an `OWNER TO`; a second plan is empty; a changed description is an in-place `COMMENT ON TABLE`, never a replace; removing a classification destroys only its column tag; a table made before this is adopted and gains its comments in place (`+5 ~1` for the product tags and the table update). `fluid verify` passes after the apply and fails a comment or column tag changed by hand; against a stubbed client it reads each column's tag list to its last page, ignores a tag or comment someone else added, and checks no comment on a dbt-written table. | **Not run live.** What real Unity Catalog stores and returns for a table or column comment (whitespace, newlines, length limits, which the docs do not state), whether it accepts a tag value with `, . : / = -` (the docs restrict only keys), whether `entity-tag-assignments/columns/{catalog.schema.table.column}` resolves as the SDK documents, and whether a dbt CREATE OR REPLACE keeps column tags (undocumented; `fluid verify` is the gate), and whether the Entity Tag Assignments API sets the system tag `system.certification_status` (its docs show the UI and `SET TAG`; the SDK documents governed tags through the API, needing ASSIGN; the stand-in checks no permission). A contract that adds a column while comments change for the first time hits the provider's mixed-change refusal (its fix-it says to apply in two steps). |
| Manual live, Free Edition (founder-approved) | the real `fluid` CLI, no test file: run 2026-11-01 from main dfd6f145, databricks-sdk 0.158.0, every `fluid_iactest_*` object swept after (1 schema, 1 table, 1 folder; 0 left) | ✅ validate, `generate iac --validate`, plan, dry-run and apply `+9`; a second plan is EMPTY against the real Jobs API; Databricks accepts the Quartz schedule, serverless environment 2 with `dbt-databricks>=1.8.0,<2.0.0` (dbt 1.12.6, adapter 1.13.0), the WORKSPACE `project_directory` and the commands as emitted; one `run-now` ends SUCCESS and dbt builds the table; `fluid verify` with the real SDK (columns, types, one job); `fluid diff`'s state check (its live check has no Databricks inspector); a model change `~1`; the build made local removes job, files and folders `-5` without `--allow-data-loss` (the empty `.fluid/<product>/<build>` folders above `files/` stay: only `files/` and below are managed). It found the USE CATALOG check reading a group-conveyed privilege as none. After the fix, `fluid verify` on a fresh one-table product (`+4`) passes every dimension, catalog access included (1 schema and 1 table swept, 0 left). | A GIT source, profile or OAuth auth, a run-as service principal, table adoption and a larger project, live; the region residency check (the metastore precondition and `fluid verify`'s residency dimension), which was added after this run and not run live; dbt's CREATE OR REPLACE drops a contract's NOT NULL, so verify flags a `required` column a dbt model does not constrain itself. |
| Manual live, Free Edition (founder-approved), masks and row filters | the real `fluid` CLI, no test file: run 2026-11-03 at the tip of #785, a real workspace with an AWS us-west-2 metastore; every object swept after (0 left) | ✅ 10 steps passed. Functions are created and read back (owner is the user's email; `routine_definition` is the body without `RETURN`, backticks kept; `full_data_type` is STRING or BOOLEAN; a zero-parameter function's FunctionInfo has no `input_params`). The row filter and masks are bound and enforced on the restricted user's own SELECT: only the consented rows, `msisdn` as `XXXXX4567`, a 3-character value hashed, the denied column NULL. `SET ROW FILTER ... ON ()` is accepted and a `SET` replaces a live filter. Tags are present, `fluid verify` passes every dimension, a second apply is `+0 ~0 -0`, the residency precondition fails the plan and names both regions, `generate iac` refuses, and the sweep left 0. | Still open, as this run showed: superseded functions were left behind (orphans; the follow-up that drops them after a successful apply is not run live); `tofu destroy` of the module fails at a schema that still holds functions, so a teardown must drop them first; and the residency failure text was forge-cli's 800-character tail of the tofu JSON, which cut off the sentence naming the live region (the one-line explanation is not run live). A metastore in another cloud or region, a service principal as the run identity, and a group-conveyed grant on the live workspace are still not run. |
| Manual live, Free Edition (founder-approved), 2026-11-04 | the real `fluid` CLI, no test file: #801 + #831 at a7169202, run by the demo session, every object swept back to the baseline after | ✅ Unity Catalog metadata read back live: the table and column comments, the product tags (`fluid_product_id` and the rest), the owner-team tag, the `fluid_sensitivity` column tags and `system.certification_status = certified` (accepted for an admin personal access token). The bronze landing: 5 rows loaded, the email column hashed, the staged files deleted from the volume; a second run overwrote the table (the Delta history shows `numRemovedFiles=1`). The column masks, the row filter and a scoped grant were enforced after the `INSERT OVERWRITE`. `lifecycle.retention: P30D` with `expire: true` was accepted, `fluid verify` passed, and predictive optimization was on. A second apply planned `+0 ~0 -0`. A changed row filter bound the new `fluid_rf_` function and dropped the superseded one. Findings fixed in the follow-up PR: the landing left the empty `<volume>/<product>/<build>/` directories and the local masked copy under `.fluid/staging/<build>/`, and a dry run did not list the superseded function drops. | Not tested live: `system.certification_status` set by a non-admin identity holding only ASSIGN on the governed tag (only an admin token was used). Open: `facets.databricks_load.rows` counts the rows loaded, which a row-filtered `SELECT COUNT(*)` by a restricted reader does not match (by design of the filter, not reconciled in the run record). |
| Stage 2 emulator (nightly, non-gating) | `tests/iac/test_iac_databricks_minilake.py` (job `nightly-databricks-minilake`) | ⚠️ first run 2026-10-30 (manual dispatch, run 41111425504, minilake v1.9.0): 1/4. Schema, table and grant creates work, and the replace gate holds. | A second plan is not empty and every in-place table update fails: minilake strips `TBLPROPERTIES` at create and its SQL parser rejects `ALTER TABLE ... SET TBLPROPERTIES`, which the provider sends before each column change. An upstream issue is drafted, not filed. Jobs and workspace files are not in this flow yet. |
| Stage 3 live | — | ❌ not automated: manual maintainer runs only, never CI. Two have run: the manual rows above (2026-11-01 and 2026-11-03). | Everything the rows above leave to the live tier. |

## What's NOT tested (deliberately deferred)

These are real gaps. Each has a one-line rationale and a tentative
follow-up scope. None block the current branch from shipping.

- **Workload Identity Federation (WIF) for local dev** — bootstraps
  still need `gcloud auth application-default login` (interactive
  one-time) and `AWS_PROFILE` for local. CI uses OIDC keyless
  (`aws-actions/configure-aws-credentials@v4` +
  `google-github-actions/auth@v2`) — see `.github/workflows/iac-tests.yml`.
  Local-dev keyless is a multi-week org-coordination change; deferred.
- **Cross-account / cross-project ORG-BOUNDARY crossing** — the
  IAM-grant LOGIC is covered end-to-end via Stage 3 same-account /
  same-project proxies — with **zero new contract-schema fields**:
  - AWS uses the existing `binding.governance.lakeFormation.grants[]`
    block. An IAM-principal grant pairs with an `aws_s3_bucket_policy`
    statement, by default only when the principal is in another account
    than the one applying (`bucketPolicy`, fluid-schema 0.7.6).
  - GCP uses the existing `metadata.policies` surface. SA emails from
    other projects are accepted verbatim via BQ's `user_by_email`
    field on the dataset's embedded `access[]` block.
  - Live tests: `test_iac_aws_real_cross_account_e2e.py` (2/2 green),
    `test_iac_gcp_real_cross_project_e2e.py` (3 written; same-project
    consumer SELECT + same-project deny verified live).
  - **Env-var-gated true-cross-account tests**: each cross-account /
    cross-project test file carries a NEW `test_real_cross_*_grant_
    carries_external_arn` / `_carries_external_sa` test that runs
    against a real *second-sandbox* identifier when set:
    - `FLUID_AWS_LIVE_CONSUMER_ACCOUNT_ID=<12-digit-id>` — applies a
      contract with an external-account ARN; verifies the LF + bucket
      policy land with that ARN. Skips cleanly when unset.
    - `FLUID_GCP_LIVE_CONSUMER_PROJECT=<project-id>` — applies a
      contract with an external-project SA email; verifies the
      dataset's access[] block carries it. Skips cleanly when unset.
  **Partially closed on the emulator** by
  `tests/integration/test_iac_cross_account_localstack.py`: a second,
  disposable LocalStack Pro (with `lakeformation`) hosts two real
  accounts, the producer stack applies in A with grants naming a role
  in B, and both accounts' APIs read the artifacts back. Under
  `ENFORCE_IAM=1` the **S3** half is genuinely authorised across the
  boundary — with a causal control proving the forge-emitted bucket
  policy, not the identity policy, is what narrows access.
  **What's still deferred**: (a) Lake Formation cross-account
  *authorization* — LocalStack rejects `GrantPermissions` under IAM
  enforcement even for a registered `DataLakeAdmin`, so the LF grant
  is only ever verified as landed, never as enforced; (b) cross-account
  Glue catalog sharing, which on real AWS is brokered by AWS RAM +
  a catalog resource policy that LocalStack does not implement;
  (c) org-boundary semantics (SCPs, `aws:PrincipalOrgID`,
  trust-policy external-ids). All three need a real second AWS
  account — when creds are provisioned, flip the env vars above and
  the existing Stage 3 tests run.
- **Latency / performance SLOs** — no SLO on `tofu plan` duration,
  emit-path CPU, etc. No baseline measurements yet. Deferred until
  we have a target.
- **Cost runaway alarms** — sweepers are best-effort. No external
  monitor / Slack alert if a `fluid-iactest-*` resource lingers past
  N hours. Deferred to observability work.
- **`fluid apply --mode replace-and-build`** — `--mode replace`
  destructive path IS covered; the `replace-and-build` variant (drop
  + recreate + dbt rebuild) shares 95% of code paths with the two
  already-tested modes; explicit test deferred.
- **`--import-existing` brownfield path** — **CLOSED.** Both AWS +
  GCP plugins now implement `discover_imports` mirroring the
  Snowflake pattern (contract → ImportBlock per declared resource).
  Import IDs follow the documented hashicorp/aws + hashicorp/google
  formats:
  - `aws_glue_catalog_database` — `{catalog_id}:{name}` (catalog_id
    via `AWS_ACCOUNT_ID` env or `sts:GetCallerIdentity`); without it
    the Glue blocks are SUPPRESSED so a malformed import doesn't
    confuse the operator.
  - `aws_glue_catalog_table` — `{catalog_id}:{database}:{name}`.
  - `aws_s3_bucket` / `aws_kinesis_stream` / `aws_redshiftserverless_
    namespace` — bare resource name.
  - `google_bigquery_dataset` — `projects/{project}/datasets/{ds}`
    (project from `GOOGLE_PROJECT`/`GOOGLE_CLOUD_PROJECT`).
  - `google_bigquery_table` — `projects/{p}/datasets/{ds}/tables/{t}`.
  - `google_storage_bucket` — bare bucket name.
  - `google_pubsub_topic` — `projects/{p}/topics/{name}`.
  Coverage: 16 unit tests in `tests/iac/test_iac_importer.py` +
  2 live brownfield Stage 3 tests
  (`tests/iac/test_iac_aws_real_brownfield_e2e.py`,
  `tests/iac/test_iac_gcp_real_brownfield_e2e.py`) — pre-create
  resources out-of-band, run apply, verify adoption.
  Real bug found+fixed during closure: the AWS Glue import id needs
  the catalog_id prefix; without it `tofu import` returns "Invalid
  import id" and the subsequent apply fails `AlreadyExistsException`.
- **Snowflake CLI-surface matrix — 4 of 6 still missing.** AWS has all
  six CLI-matrix tests (validate / plan / dry-run / amend /
  tamper-reject / bypass); Snowflake's file carries **only the last
  two**, the plan-binding tamper-reject and the `--no-verify-plan-binding`
  bypass. Its `validate`, `plan`, `apply --mode dry-run` and
  `apply --mode amend` equivalents were never ported. The coverage
  table above says `2/6` for this reason.
  Snowflake's other Stage 3 suite (`test_iac_snowflake_live.py`) does
  exercise apply end-to-end, but through `runner.tofu_apply` directly
  rather than the CLI dispatch chain, so it does not close this gap.
  The remaining four would be a near-copy of the GCP file with
  `bigquery_table` swapped for `snowflake_table`.
- **AWS Lake Formation enforcement-side tests** — *partially* covered
  now by `tests/iac/test_iac_aws_real_cross_account_e2e.py` which
  STS-assumes a non-deployer role and runs an Athena SELECT through
  its credentials, proving the LF + bucket-policy + IAM grants
  actually authorise a read. The original "deferred" framing
  contemplated arbitrary principals; the cross-account proxy now
  exercises the canonical case end-to-end. What's NOT covered:
  multi-tag-filter interaction matrices (does LF correctly compose
  row-filter ∩ column-filter ∩ tag-allowlist for a given principal
  carrying tag X but not Y?). That's a much bigger combinatorial
  surface; tracked as future work.
- **Snowflake Horizon declarative tag governance** — scoped OUT
  in the "minimum schema changes" pass. The canonical Horizon flow
  (define tag → bind masking policy to tag → attach tag to column)
  needs either (a) new contract-schema fields the user explicitly
  asked us to avoid, OR (b) the `snowflake_tag_masking_policy_association`
  resource which `snowflakedb/snowflake` v2 dropped (upstream issue
  drafted at `docs/upstream-issues/snowflake-tag-masking-policy-v2.md`).
  Catalog-style enrichment — table COMMENT + per-column comments
  on `snowflake_table` — IS emitted, using only the existing
  `description` / `metadata.description` / `column.description`
  fields (covered by
  `tests/iac/test_iac_snowflake.py::TestSnowflakeCatalogEnrichment`).
  Tag taxonomy stays a security-team-managed Snowsight setup until
  the upstream provider gap is closed.
- **dbt-glue live** — Glue Interactive Sessions cost ~$2-5 per
  cold start and take 3-5 min to provision. The
  `_build_generated_dbt_profile` logic for `platform: glue` /
  `aws-glue` is pinned in `tests/build_runners/test_dbt_glue_profile.py`
  (8 tests covering: alias acceptance, 240 s session-timeout
  default, worker defaults + overrides, optional `glue_version`/
  `location`, `glue_database`/`schema` aliasing). The live test
  stays gated on `FLUID_AWS_LIVE_DBT_GLUE=1` so anyone with the
  AWS sandbox can run it on demand.
  **Cannot be moved to the emulator tier.** dbt-glue drives the Glue
  Interactive Sessions API, which LocalStack Pro does not implement —
  verified directly against Pro 2026.6.0:
  `CreateSession` → `InternalFailure: Sorry, the CreateSession
  operation on the glue service is not currently supported by
  LocalStack` (`ListSessions` returns the same). Real AWS is the only
  target for a live dbt-glue run; there is no emulator shortcut.

## Upstream limitations

These are documented in xfails / skips in the test code. The
project's policy (per `CLAUDE.md` memory rule) is to **discuss and
file upstream**, not silently work around. Issue links:

| Limitation | Lives in test | Upstream issue |
|---|---|---|
| `tofu apply` against goccy/bigquery-emulator crashes the hashicorp/google provider plugin on read-back ("Plugin did not respond") | `test_iac_gcp_emulator_e2e.py::test_emu_tofu_apply_round_trip_xfail` | [goccy/bigquery-emulator#484](https://github.com/goccy/bigquery-emulator/issues/484) (filed 2026-05-25; draft preserved at `docs/upstream-issues/gcp-emulator-tofu-apply.md`) |
| dbt-bigquery has no native emulator endpoint support — connection layer doesn't honour `BIGQUERY_EMULATOR_HOST` or accept `api_endpoint` via profile | Documented in `test_iac_gcp_emulator_e2e.py` module docstring (no test attempts the path) | Tracking dbt-labs/dbt-bigquery#358 (open, no committed timeline) |
| dbt-glue requires Glue interactive sessions (~3-5 min cold start, billed per DPU-hour) and `fluid generate iac` does not yet emit the matching Glue Job + GlueInteractiveSession config | `test_iac_aws_real_dbt_mesh_cli_e2e.py::test_real_cli_dbt_glue_amend_and_build` (skip) | Tracked internally as a forge-cli enhancement |
| AWS Redshift Serverless workgroup hostname does NOT publish to in-VPC private DNS until an `aws_redshiftserverless_endpoint_access` resource is explicitly created | Worked around with the `private_endpoint_subnets` contract field — no upstream issue (the workaround is the documented AWS path) | n/a |
| AWS org policy `constraints/iam.disableServiceAccountKeyCreation` (and GCP's equivalent) blocks SA-key creation. We use impersonation + ADC instead | Documented in `tests/iac/_gcp_stage3_bootstrap/README.md` | n/a — intended security design |
| ~~`snowflake_tag_masking_policy_association` was removed from `snowflakedb/snowflake` v2~~ — **resolved upstream**: the binding moved INTO the `snowflake_tag` resource as a `masking_policies` field (set of fully-qualified names) in v1.0.0. Documented in [MIGRATION_GUIDE.md → snowflake_tag_masking_policy_association deprecation](https://github.com/snowflakedb/terraform-provider-snowflake/blob/main/MIGRATION_GUIDE.md#snowflake_tag_masking_policy_association-deprecation) and [removed-deprecated-objects](https://github.com/snowflakedb/terraform-provider-snowflake/blob/main/MIGRATION_GUIDE.md#removed-deprecated-objects). | The Snowflake plugin currently has no contract surface for tag-based masking (we reverted the experimental `governance.snowflake.tagMaskingPolicies[]` block per the minimum-schema-changes pass). Adding it is a clean follow-on — emit `snowflake_tag.masking_policies = [snowflake_masking_policy.X.fully_qualified_name]`; the v2.16+ provider needs a default warehouse in the connection per the [tag_references](https://docs.snowflake.com/en/sql-reference/functions/tag_references) requirement. Tracked at [Trello — implement tag-based masking via snowflake_tag.masking_policies](https://trello.com/c/y4hwfVBH). | [snowflakedb/terraform-provider-snowflake#4775](https://github.com/snowflakedb/terraform-provider-snowflake/issues/4775) (filed + resolved 2026-05-25 by upstream maintainer @sfc-gh-kwasilewski; draft preserved at `docs/upstream-issues/snowflake-tag-masking-policy-v2.md`) |
| LocalStack Pro Lambda V2 cannot reach the host docker socket from inside docker-in-docker → invocations fail with "docker daemon unreachable" while apply succeeds | `tests/iac/test_iac_aws_localstack_e2e.py` Lambda tests self-skip on the docker-in-docker topology | `docs/upstream-issues/localstack-lambda-docker-in-docker.md` (drafted; **public LocalStack repo was archived on 2026-03-23 and the Pro repo is private — file via <https://app.localstack.cloud/community> or LocalStack Slack**) |
| LocalStack Pro Lake Formation `GrantPermissions` rejects calls from principals listed in `DataLakeAdmins` (real AWS authorises them) | LF LocalStack test self-skips; tier-3 real-AWS coverage in `test_iac_aws_real_lakeformation_e2e.py` is the canonical pin (9 tests, green) | `docs/upstream-issues/localstack-lakeformation-grant-auth.md` (drafted; same routing as the Lambda issue above — public repo archived, file via support portal / Slack) |

## Schema footprint (intentionally minimal)

This branch's only contract-schema additions are the Lake Formation
block (`governance.lakeFormation` + `bindingGovernance.lakeFormation`)
which has no analog in the pre-existing surface. Cross-account /
cross-project / Snowflake catalog enrichment all ride **existing**
fields:

| Capability | Reads from |
|---|---|
| Cross-account S3 access | `binding.governance.lakeFormation.grants[].principal` (LF block); a bucket-policy statement is paired with each grantee in another account (`bucketPolicy: cross-account`, the default; `none` / `all-grantees` in fluid-schema 0.7.6) |
| Cross-project BQ access | `metadata.policies` (existing) → `google_bigquery_dataset_iam_member` (was the dataset's authoritative `access[]`) — ⚠️ **but see the note below: `metadata.policies` does not pass `fluid validate`** |
| Glue catalog enrichment | `description` / `metadata.{description,layer,productType}` / `domain` / `fluidVersion` / `column.{description,tags}` |
| Snowflake catalog enrichment | same set as Glue |

Result: every new capability landed without growing the surface a
contract author has to learn. Reverted this session were three
schema fields (`crossAccountS3Access` boolean, `bigQuery.crossProjectGrants[]`,
`governance.snowflake.*`) that all turned out to be redundant with
existing fields once we tested the cleanest emit path.

> ⚠️ **Known gap — `metadata.policies` is read but not schema-valid.**
> `iac/providers/gcp.py` reads `contract["metadata"]["policies"]`, but
> `metadata` sets `additionalProperties: false` and does **not** list
> `policies` in **any** shipped schema (0.7.3 / 0.7.5 / 0.7.6). A
> contract carrying it therefore fails `fluid validate`:
>
> ```
> $ fluid validate contract.fluid.yaml
> ❌ Invalid FLUID contract (1 error(s)) (schema v0.7.6)
>  1. metadata: Additional properties are not allowed ('policies' was unexpected)
> ```
>
> `fluid generate iac` does not run schema validation, so the emit path
> works and every test above passes — but the cross-project grant is
> **not expressible in a contract that passes `fluid validate` today**.
> The validated access-control surface (`accessPolicy`, consumed by
> `cli/policy_compile.py` and the ODPS provider) is a *different* field
> that the IaC GCP plugin does not read. Closing this means either
> adding `policies` to the metadata schema or teaching the plugin to
> read `accessPolicy`. Discovered 2026-07-18 while closing the Stage 2
> cross-project row.

## Code that was retired (no more maintenance burden)

Folding catalog work into the IaC plugins removed ~514 LOC of
boto3 / HTTP-driven publish-side code:

| Retired module | Replaced by |
|---|---|
| `fluid_build/build_runners/catalog_registrars/glue.py` (270 LOC) | `aws_glue_catalog_table.description` + `parameters` (fluid_layer / fluid_product_type / fluid_domain / fluid_version / fluid_contract / forge.pii.\<col\>) + per-column `comment` on `storage_descriptor.columns[]` — all emitted by `iac/providers/aws.py::_emit_glue`. |
| `fluid_build/build_runners/catalog_registrars/snowflake_horizon.py` (244 LOC) | `snowflake_table.comment` (FLUID classification markdown + contract YAML) + per-column `comment` — emitted by `iac/providers/snowflake.py::_emit_snowflake` + `_build_horizon_table_comment`. The governance / tag-masking emit was reverted after the schema-revert session because `snowflake_tag_masking_policy_association` was removed in provider v2 with no replacement (see Upstream limitations row). |

What we kept (these are NOT redundant with IaC):

| Registrar | Why kept |
|---|---|
| `datahub.py` | DataHub MCP (metadata change proposals) + lineage updates run per-`fluid` invocation, not just at IaC apply. The community Terraform provider exists but switching loses the MCP path. |
| `openmetadata.py` | OpenMetadata REST + continuous lineage. The community provider is recent enough that we want the python registrar as the proven path; revisit when the provider matures. |
| `datamesh_manager.py` | No Terraform provider exists; DMM is REST-only by design. |

Net effect: one source of truth (the contract) → one `tofu plan` →
one `tofu apply` covering both provisioning AND publishing for AWS
Glue + Snowflake Horizon. Drift detection across both layers. The
publish stage's `--target glue` / `--target snowflake_horizon` names
were retired from `register_catalog_backend()` so the orchestrator
reports a clean "not configured" instead of running a stale push that
would fight IaC state.

## Security findings surfaced during this work

- **Plan-binding bypass in the OpenTofu apply engine** — fixed in
  `4c9163f fix(security): wire plan-binding verification into the
  OpenTofu apply engine`. Surfaced by
  `test_real_cli_apply_plan_binding_tamper_rejected` failing on the
  first run. Pre-fix: a tampered `plan.json` would apply against real
  cloud infra without the digest mismatch being checked, for every
  provider that cut over to OpenTofu (aws / gcp / snowflake).
  Post-fix: rejected with `apply_plan_digest_plan_tamper` event.
- **Audit gap on `--allow-data-loss` destructive bypass** — the
  OpenTofu engine accepted the override but emitted no structured
  event, so CI log-scrapers could not detect a destructive apply
  that bypassed the gate. Fixed in this session: every override
  now logs a WARNING (`--allow-data-loss: bypassing the data-loss
  gate; N resource(s) will be destroyed`) and emits a structured
  `opentofu_destructive_gate_override` event with the change counts.
- **Independent code review verdict** — `Security review of branch`
  (Explore agent, 2026-05-25) returned **zero exploitable
  vulnerabilities** at confidence ≥ 0.8. Specifically verified:
  subprocess invocation passes args as list (no shell, no command
  injection); `_escape_tofu_literals` neutralises `${…}` /
  `%{…}` in contract-derived strings; credentials are never embedded
  in `.tf.json` (env-only); plan-binding gate runs before any tofu
  apply; brownfield import addresses are sanitised via `safe_ident`.

## Operational hardening surfaced during this work

The `Operational maturity` review (Explore agent, 2026-05-25) flagged
three runner-level risks that were closed in this session:

| Risk | Fix | Pinned by |
|---|---|---|
| No per-command timeout on `tofu` calls — a hung process hangs the CLI indefinitely | `iac/runner.py::_run` now passes `timeout=_resolve_timeout()` and surfaces a `returncode=124` (coreutils-`timeout` convention) `TofuResult` on expiry; default 1800s, override via `FLUID_TOFU_TIMEOUT_SECONDS` | `tests/iac/test_iac_runner.py::TestTofuTimeout` (5 tests) |
| No `tofu` version check at startup — a stale binary discovers the mismatch only mid-apply, after partial state has been mutated | `iac/runner.py::require_tofu_version` is called by `_apply_opentofu_engine.py` before any `tofu init`; floor is `1.6.0` | `tests/iac/test_iac_runner.py::TestTofuVersionGate` (5 tests) |
| `--allow-data-loss` override had no audit trail | WARNING log + `opentofu_destructive_gate_override` structured event — see "Security findings" above | covered by the same code path |

## Branch-modified CLI surfaces (unit-test coverage)

The branch touched a few CLI surfaces beyond `iac/`. These pins guard
against silent regression of the migration-detector / doctor surface /
policy-apply noop fast-path:

| Surface | Branch change | Pin |
|---|---|---|
| `cli/import_cmd.py::TerraformDetector` | Recognises `.tf.json` (the format `fluid generate iac` emits); structurally parses the resource map to set `target_platform` for gcp / snowflake | `tests/test_import_cmd.py::TestTerraformDetector` (10 tests, 5 new) |
| `cli/doctor.py::_check_fluid_features` | AWS check probes `fluid_build.iac.get_iac_plugin("aws")` instead of the retired action-modules import | `tests/test_cli_doctor.py::TestCheckFluidFeatures` (7 tests, 3 new) |
| `cli/policy_apply.py::run` | Empty-bindings noop fast-path (acquisition-only contracts compile to zero grants and used to fail `provider_not_specified`) | `tests/cli/test_policy_apply_empty_bindings.py` (4 tests, new file) |

## How to run

```bash
# Stage 1 — every laptop, no creds.
.venv/bin/python -m pytest -m "unit"

# Stage 2 — needs docker.
docker compose -f tests/iac/_gcp_emulator/docker-compose.yml up -d  # for GCP
# LocalStack: see tests/iac/test_iac_aws_localstack_e2e.py docstring.
export FLUID_IAC_LIVE_LOCALSTACK=1 FLUID_IAC_LIVE_GCP_EMULATOR=1
.venv/bin/python -m pytest -m "integration and not slow"

# Stage 3 — real cloud, opt-in gated.
# AWS:
export AWS_PROFILE=fluid-stage3-tester AWS_REGION=eu-west-1
export FLUID_IAC_LIVE_AWS=1
# + the four FLUID_AWS_*_ROLE_ARN env vars from the bootstrap output.
.venv/bin/python -m pytest tests/iac/test_iac_aws_real_*.py -v

# GCP:
gcloud auth application-default login   # one-time
export FLUID_GCP_PROJECT=<project> FLUID_GCP_TEST_SA=<sa-email>
export FLUID_GCP_REGION=<region> FLUID_IAC_LIVE_GCP=1
.venv/bin/python -m pytest tests/iac/test_iac_gcp_real_*.py -v

# Snowflake (uses snowflake-biz-lab credentials):
set -a; . ../snowflake-biz-lab/.env; set +a
export FLUID_IAC_LIVE_SNOWFLAKE=1
.venv/bin/python -m pytest tests/iac/test_iac_snowflake_live.py -v
```

## Bookkeeping

- The bootstrap modules (`tests/iac/_aws_stage3_bootstrap/`,
  `tests/iac/_gcp_stage3_bootstrap/`) are themselves tofu — apply once
  per cloud account and source the outputs into the env vars listed
  above.
- Per-test resources are tagged `managed_by=fluid` + named
  `fluid-iactest-<uuid>` so the session sweeper picks up any leaks
  even from crashed runs.
- The `feat/opentofu-iac-autogen` branch summary lives in
  `AUTOGEN_SPIKE.md` at the repo root.
