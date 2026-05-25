# Honestly Tested

Status of `forge-cli`'s IaC test coverage on `feat/opentofu-iac-autogen`.
Written so a reviewer can pick up cold: what's covered, what's NOT
covered, and where the upstream limitations live.

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
| Stage 3 cross-account proxy | `tests/iac/test_iac_aws_real_cross_account_e2e.py` | ✅ 2/2 — consumer role assumes + LF + S3 bucket policy + Athena SELECT; un-granted principal correctly denied. **Zero new schema fields** — uses the existing `binding.governance.lakeFormation.grants[]` block; bucket policy is paired automatically with any IAM-principal grant. |
| Stage 1 Glue catalog enrichment | `tests/iac/test_iac_aws.py::TestAwsGlueCatalogEnrichment` (6) + `test_iac_tofu_validate.py[aws]` | ✅ — `aws_glue_catalog_table.description` + per-column comments + fluid_layer/fluid_product_type/fluid_domain/fluid_version/fluid_contract/forge.pii.<col> parameters, absorbed from the retired `GlueCatalogRegistrar`. **Zero new schema fields** — reads existing `description`/`metadata.description`/`metadata.layer`/`metadata.productType`/`domain`/`fluidVersion`/`column.tags[]`/`column.description`. |
| Stage 3 Glue catalog enrichment | `tests/iac/test_iac_aws_real_e2e.py::test_real_iceberg_on_glue_round_trip` | ✅ live — Description + Parameters + per-column Comments + `forge.pii.amount` verified via boto3 GetTable |

### GCP

| Tier | Tests | Status |
|---|---|---|
| Stage 1 unit | `tests/iac/test_iac_gcp.py` + `test_iac_tofu_validate.py[gcp]` | ✅ 24 green in 7.7s |
| Stage 2 docker emulator | `tests/iac/test_iac_gcp_emulator_e2e.py` | ✅ 8 hybrid (emit via plan + emulator via SDK) + 1 documented xfail — see [Upstream limitations](#upstream-limitations) |
| Stage 3 base | `tests/iac/test_iac_gcp_real_e2e.py` | ✅ 7/7 (BQ dataset+table, BQ view, GCS, Pub/Sub, multi-exposure, dbt-bigquery via CLI, full mesh) |
| Stage 3 CLI matrix | `tests/iac/test_iac_gcp_real_cli_matrix_e2e.py` | ✅ 6/6 |
| Stage 3 idempotency | `tests/iac/test_iac_gcp_real_idempotency_e2e.py` | ✅ (see Stage 3 §) |
| Stage 3 replace mode | `tests/iac/test_iac_gcp_real_replace_e2e.py` | ✅ |
| Stage 3 brownfield | _file claimed but absent — see "deferred" §_ | ❌ deferred (`discover_imports()` returns `[]` so any test would be a no-op) |
| Stage 3 cross-project proxy | `tests/iac/test_iac_gcp_real_cross_project_e2e.py` | ⚠️ written; user runs (needs `gcloud auth application-default login` + `tofu apply` of bootstrap update to materialize the `fluid-iactest-consumer` SA). **Zero new schema fields** — uses the existing `metadata.policies` surface; cross-project SAs ride BQ's `user_by_email` access entry. |

### Snowflake

| Tier | Tests | Status |
|---|---|---|
| Stage 1 unit | `tests/iac/test_iac_snowflake.py` + `test_iac_tofu_validate.py[snowflake]` | ✅ pre-existing |
| Stage 3 live | `tests/iac/test_iac_snowflake_live.py` | ✅ pre-existing |
| Stage 3 CLI matrix | `tests/iac/test_iac_snowflake_real_cli_matrix_e2e.py` | ✅ symmetry with AWS+GCP |
| Stage 1 catalog enrichment | `tests/iac/test_iac_snowflake.py::TestSnowflakeCatalogEnrichment` (3) | ✅ — Horizon markdown table COMMENT + per-column comments on `snowflake_table`, absorbed from the retired `SnowflakeHorizonRegistrar`. **Zero new schema fields** — reads existing `description`/`metadata.description`/`metadata.layer`/`metadata.productType`/`column.description`. Declarative tag governance deferred (see "deferred" §). |
| Stage 3 catalog enrichment live | `tests/iac/test_iac_snowflake_live.py::test_live_table_carries_horizon_markdown_comment` | ✅ — apply against real Snowflake (snowflake-biz-lab creds), `SHOW TABLES` exposes the markdown comment, `DESC TABLE` exposes per-column comments. Confirms the Stage 1 emit lands on the live wire. |

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
    block. Any IAM-principal grant automatically pairs with an
    `aws_s3_bucket_policy` for the same principal — no opt-in flag.
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
  **What's still deferred**: bilateral apply (consumer in account/
  project B actually running a SELECT against producer in A). That
  needs creds for the second account/project — when those are
  provisioned, flip the env vars and the existing tests run.
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
- **Snowflake CLI-surface matrix** — AWS + GCP each have 6 CLI-matrix
  tests (validate / plan / dry-run / amend / tamper-reject / bypass).
  Snowflake's Stage 3 suite (`test_iac_snowflake_live.py`) predates
  the CLI matrix push and goes through `runner.tofu_apply` directly,
  same shape as the AWS+GCP base Stage 3 files. A Snowflake CLI
  matrix would be a pure copy with `bigquery_table` swapped for
  `snowflake_table`; deferred for symmetry with the AWS+GCP work
  shipping first.
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

## Upstream limitations

These are documented in xfails / skips in the test code. The
project's policy (per `CLAUDE.md` memory rule) is to **discuss and
file upstream**, not silently work around. Issue links:

| Limitation | Lives in test | Upstream issue |
|---|---|---|
| `tofu apply` against goccy/bigquery-emulator crashes the hashicorp/google provider plugin on read-back ("Plugin did not respond") | `test_iac_gcp_emulator_e2e.py::test_emu_tofu_apply_round_trip_xfail` | **TODO** — see `docs/upstream-issues/gcp-emulator-tofu-apply.md` for the issue draft + `gh` CLI command. As of the date this doc was written, the search of `goccy/bigquery-emulator/issues` returned no existing report. |
| dbt-bigquery has no native emulator endpoint support — connection layer doesn't honour `BIGQUERY_EMULATOR_HOST` or accept `api_endpoint` via profile | Documented in `test_iac_gcp_emulator_e2e.py` module docstring (no test attempts the path) | Tracking dbt-labs/dbt-bigquery#358 (open, no committed timeline) |
| dbt-glue requires Glue interactive sessions (~3-5 min cold start, billed per DPU-hour) and `fluid generate iac` does not yet emit the matching Glue Job + GlueInteractiveSession config | `test_iac_aws_real_dbt_mesh_cli_e2e.py::test_real_cli_dbt_glue_amend_and_build` (skip) | Tracked internally as a forge-cli enhancement |
| AWS Redshift Serverless workgroup hostname does NOT publish to in-VPC private DNS until an `aws_redshiftserverless_endpoint_access` resource is explicitly created | Worked around with the `private_endpoint_subnets` contract field — no upstream issue (the workaround is the documented AWS path) | n/a |
| AWS org policy `constraints/iam.disableServiceAccountKeyCreation` (and GCP's equivalent) blocks SA-key creation. We use impersonation + ADC instead | Documented in `tests/iac/_gcp_stage3_bootstrap/README.md` | n/a — intended security design |
| `snowflake_tag_masking_policy_association` was removed from `snowflakedb/snowflake` v2 with no documented replacement — blocks declarative tag-based masking binding | Discussed in `iac/providers/snowflake.py` — the Snowflake plugin does **not** expose a `governance.snowflake.*` schema surface (the experimental block was reverted); tag-based masking will land once a declarative resource is restored | [snowflakedb/terraform-provider-snowflake#4775](https://github.com/snowflakedb/terraform-provider-snowflake/issues/4775) (filed 2026-05-25; draft preserved at `docs/upstream-issues/snowflake-tag-masking-policy-v2.md`) |
| LocalStack Pro Lambda V2 cannot reach the host docker socket from inside docker-in-docker → invocations fail with "docker daemon unreachable" while apply succeeds | `tests/iac/test_iac_aws_localstack_e2e.py` Lambda tests self-skip on the docker-in-docker topology | `docs/upstream-issues/localstack-lambda-docker-in-docker.md` (drafted) |
| LocalStack Pro Lake Formation `GrantPermissions` rejects calls from principals listed in `DataLakeAdmins` (real AWS authorises them) | LF LocalStack test self-skips; tier-3 real-AWS coverage in `test_iac_aws_real_lakeformation_e2e.py` is the canonical pin (9 tests, green) | `docs/upstream-issues/localstack-lakeformation-grant-auth.md` (drafted) |

## Schema footprint (intentionally minimal)

This branch's only contract-schema additions are the Lake Formation
block (`governance.lakeFormation` + `bindingGovernance.lakeFormation`)
which has no analog in the pre-existing surface. Cross-account /
cross-project / Snowflake catalog enrichment all ride **existing**
fields:

| Capability | Reads from |
|---|---|
| Cross-account S3 access | `binding.governance.lakeFormation.grants[].principal` (LF block); bucket policy is paired automatically with any IAM-principal grant — no opt-in flag |
| Cross-project BQ access | `metadata.policies` (existing) → dataset `access[]` via `_bq_access_entries` |
| Glue catalog enrichment | `description` / `metadata.{description,layer,productType}` / `domain` / `fluidVersion` / `column.{description,tags}` |
| Snowflake catalog enrichment | same set as Glue |

Result: every new capability landed without growing the surface a
contract author has to learn. Reverted this session were three
schema fields (`crossAccountS3Access` boolean, `bigQuery.crossProjectGrants[]`,
`governance.snowflake.*`) that all turned out to be redundant with
existing fields once we tested the cleanest emit path.

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
