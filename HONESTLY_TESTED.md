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
| Stage 3 brownfield | `tests/iac/test_iac_aws_real_import_e2e.py` | ✅ `--import-existing` adopts |

### GCP

| Tier | Tests | Status |
|---|---|---|
| Stage 1 unit | `tests/iac/test_iac_gcp.py` + `test_iac_tofu_validate.py[gcp]` | ✅ 24 green in 7.7s |
| Stage 2 docker emulator | `tests/iac/test_iac_gcp_emulator_e2e.py` | ✅ 8 hybrid (emit via plan + emulator via SDK) + 1 documented xfail — see [Upstream limitations](#upstream-limitations) |
| Stage 3 base | `tests/iac/test_iac_gcp_real_e2e.py` | ✅ 7/7 (BQ dataset+table, BQ view, GCS, Pub/Sub, multi-exposure, dbt-bigquery via CLI, full mesh) |
| Stage 3 CLI matrix | `tests/iac/test_iac_gcp_real_cli_matrix_e2e.py` | ✅ 6/6 |
| Stage 3 idempotency | `tests/iac/test_iac_gcp_real_idempotency_e2e.py` | ✅ (see Stage 3 §) |
| Stage 3 replace mode | `tests/iac/test_iac_gcp_real_replace_e2e.py` | ✅ |
| Stage 3 brownfield | `tests/iac/test_iac_gcp_real_import_e2e.py` | ✅ |

### Snowflake

| Tier | Tests | Status |
|---|---|---|
| Stage 1 unit | `tests/iac/test_iac_snowflake.py` + `test_iac_tofu_validate.py[snowflake]` | ✅ pre-existing |
| Stage 3 live | `tests/iac/test_iac_snowflake_live.py` | ✅ pre-existing |
| Stage 3 CLI matrix | `tests/iac/test_iac_snowflake_real_cli_matrix_e2e.py` | ✅ symmetry with AWS+GCP |

## What's NOT tested (deliberately deferred)

These are real gaps. Each has a one-line rationale and a tentative
follow-up scope. None block the current branch from shipping.

- **Workload Identity Federation (WIF) for local dev** — bootstraps
  still need `gcloud auth application-default login` (interactive
  one-time) and `AWS_PROFILE` for local. CI uses OIDC keyless
  (`aws-actions/configure-aws-credentials@v4` +
  `google-github-actions/auth@v2`) — see `.github/workflows/iac-tests.yml`.
  Local-dev keyless is a multi-week org-coordination change; deferred.
- **Cross-account / cross-project orchestration** — every test runs
  against ONE AWS account and ONE GCP project. Real production data
  meshes span accounts (AWS Org / GCP Folder). Multi-account testing
  needs a second sandbox + AssumeRole / impersonation chains; deferred.
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
- **`--import-existing` brownfield path** — the CLI flag exists, but
  the per-provider `discover_imports()` methods in
  `fluid_build/iac/providers/{aws,gcp}.py` currently return `[]`
  (per their docstrings: "Brownfield ``tofu import`` candidates —
  not yet wired"). Testing this path today would just exercise a
  no-op. The forge-cli enhancement to populate `discover_imports`
  (probably via the existing `cli/diff.py` introspection) is its own
  scoped PR; the test will land alongside that work.
- **Snowflake CLI-surface matrix** — AWS + GCP each have 6 CLI-matrix
  tests (validate / plan / dry-run / amend / tamper-reject / bypass).
  Snowflake's Stage 3 suite (`test_iac_snowflake_live.py`) predates
  the CLI matrix push and goes through `runner.tofu_apply` directly,
  same shape as the AWS+GCP base Stage 3 files. A Snowflake CLI
  matrix would be a pure copy with `bigquery_table` swapped for
  `snowflake_table`; deferred for symmetry with the AWS+GCP work
  shipping first.
- **AWS Lake Formation enforcement-side tests** — IaC-side LF is
  verified (location registered, grants land, tags attached, filters
  exist) but a non-admin principal trying to SELECT and being
  allowed/denied per the grants is its own follow-up. Needs STS
  AssumeRole plumbing per-test.
- **Snowflake Lake Formation analogue** — Snowflake masking policies
  + row-access policies. The schema's `governance` block was designed
  to extend cleanly (per-provider sub-keys); Snowflake's branch is
  deferred until there's a customer ask.

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

## Security findings surfaced during this work

- **Plan-binding bypass in the OpenTofu apply engine** — fixed in
  `4c9163f fix(security): wire plan-binding verification into the
  OpenTofu apply engine`. Surfaced by
  `test_real_cli_apply_plan_binding_tamper_rejected` failing on the
  first run. Pre-fix: a tampered `plan.json` would apply against real
  cloud infra without the digest mismatch being checked, for every
  provider that cut over to OpenTofu (aws / gcp / snowflake).
  Post-fix: rejected with `apply_plan_digest_plan_tamper` event.

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
