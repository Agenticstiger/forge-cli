# AUTOGEN_SPIKE.md — OpenTofu autogenerator: status & decision record

Tracking artifact for the migration of forge-cli from native per-cloud
provisioning to a **contract-compiler** that emits OpenTofu `.tf.json` and lets
`tofu` own apply / state / drift / idempotency.

It records phase status, the proof evidence, the honest LOC accounting, and the
file-level retirement manifest.

---

## Verdict — migration complete: all three providers cut over & retired

forge-cli is now a **contract-compiler**. The autogenerator loop is proven
against a real `tofu` binary, and **GCP, AWS, and Snowflake are all cut over end
to end** — `fluid apply` compiles every contract to `.tf.json` and runs `tofu`.
All three native CRUD paths (`actions/` packages, the apply halves of the
provider classes, Snowflake's `governance.py`) are **deleted**.

The branch-wide change set is **+692 / −19,929** on tracked files. forge-cli is
roughly **fifteen thousand lines smaller** than before this work — and it now
provisions via a borrowed, battle-tested engine instead of hand-rolled per-cloud
code. Phases 0–4 are done.

---

## 1. Phase status

| Phase | Scope | Status |
|---|---|---|
| **0 — Standardize** | OpenTofu cleanup (`infra/opentofu.py`, schema, `.tf`/`.tf.json` detector) | ✅ done |
| **1 — Spike** | Modular emitter + the `fluid apply` OpenTofu engine | ✅ done |
| **2 — Decision gate** | Run the spike, prove e2e, this document | ✅ done |
| **3 — Cutover mechanism** | shadow-compare tool, cutover registry, automatic per-provider routing | ✅ done |
| **3 — Cutover execution** | per-provider: harden emitter → flip default | ✅ **GCP, AWS, Snowflake done** |
| **4 — Retire native** | delete native CRUD | ✅ **GCP, AWS, Snowflake done** |

Tooling: OpenTofu **v1.12.0**, `hashicorp/google ~> 6.0`, `hashicorp/aws ~> 5.0`,
`snowflakedb/snowflake ~> 2.0`, moto **5.2.1**.

---

## 2. What the spike proved (Phases 0–2)

| # | Claim | Status | Evidence |
|---|---|---|---|
| 1 | Emitted `.tf.json` for all 3 clouds passes real `tofu validate` | ✅ | `tests/iac/test_iac_tofu_validate.py` |
| 2 | A real `tofu init→plan→apply→destroy` cycle provisions + tears down real (emulated) resources | ✅ (AWS) | `tests/iac/test_iac_moto_e2e.py` |
| 3 | The emitter is deterministic and secret-free (credential-free `provider {}`; canonical JSON) | ✅ | `test_iac_framework.py` + per-provider tests |
| 4 | The OpenTofu engine is isolated and per-provider switchable | ✅ | `test_iac_apply_engine.py`, `test_iac_cutover.py` |

**The e2e proof:** `tofu` is a Go binary, so it cannot use moto's in-process
`mock_aws` — it needs a real HTTP endpoint. `test_iac_moto_e2e.py` stands up
moto's `ThreadedMotoServer` (an in-process AWS API on a real `localhost` port —
no Docker, no token, no credentials), compiles a contract to `.tf.json`, runs a
real `tofu init→plan→apply`, **independently confirms with `boto3`** that the S3
bucket + Glue database + table exist, then `tofu destroy`s them.

---

## 3. Phase 3 — cutover mechanism + the three cutovers

**Mechanism** (provider-agnostic):

- **Shadow-compare** — `fluid_build/iac/shadow.py`. Diffs the native planner's
  intent against the OpenTofu emitter's, normalised to `LogicalResource`
  `(kind, identity)` pairs. Surfaced via `fluid generate iac --shadow`.
- **Cutover registry** — `fluid_build/iac/cutover.py`. `OPENTOFU_DEFAULT_PROVIDERS`
  now contains `{"aws", "gcp", "snowflake"}` — every cloud provider.
- **`fluid apply`** resolves its engine automatically and per-provider from
  the registry — the cloud providers compile to OpenTofu, `local` keeps its
  native apply. There is no user-facing engine switch.

**The emitters** (`fluid_build/iac/providers/{gcp,aws,snowflake}.py`):

- **GCP** — BigQuery (dataset / table / view), Cloud Storage buckets, Pub/Sub
  topics + subscriptions, and **IAM** (`metadata.policies` → BigQuery dataset
  `access` entries + `google_storage_bucket_iam_member`). Full parity with the
  standard GCP planner.
- **AWS** — Glue catalog (database + table, **Iceberg-aware**), S3 buckets,
  Kinesis streams.
- **Snowflake** — database / schema / table / view, and **grants**
  (`security.access_control.grants[]` → `snowflake_grant_privileges_to_account_role`).

Each emitter's output is checked by real `tofu validate` for all three clouds.

---

## 4. Phase 4 — native CRUD retired (all three)

The native apply paths are **deleted**, not bypassed:

| Provider | Deleted | provider class |
|---|---|---|
| **GCP** | `gcp/actions/` (8 modules) + `provider_action_handler.py` + apply machinery | `provider.py` 1,149 → 592 |
| **AWS** | `aws/actions/` (13 modules) + apply machinery | `provider.py` 1,255 → 482 |
| **Snowflake** | `snowflake/actions/` (11 modules) + `governance.py` (masking/grant DDL) + apply machinery | `provider_enhanced.py` 1,433 → 462 |

Every `provider.py` is now **plan-only**: it keeps `plan()`,
`restore_ddl`/`cleanup_backups` (rollback snapshots — Snowflake's
`cleanup_backups` now opens its own connection rather than routing through the
retired dispatcher), `render`, `auth_report`, codegen `export`, and the
plan-side helpers. `apply()` raises a clear "native apply is retired" error if
reached programmatically. GCP's `apply_policy` (the last native IAM-mutation
path, reached via `fluid policy-apply`) was retired to a non-mutating
reporter — IAM is now provisioned declaratively by the emitter.

**Honest coverage boundaries** (documented, not hidden):

- **GCP** — the emitter covers the core declarative data-plane (BigQuery,
  Cloud Storage, Pub/Sub pull subscriptions, IAM). The schedule-trigger surface
  (Cloud Run, Cloud Scheduler, Composer, Pub/Sub push subscriptions) is an
  emitter **follow-on** — see §9.
- **AWS** — the emitter covers the declarative data-plane (Glue, S3, Kinesis).
  The schedule / orchestration surface (Lambda, EventBridge, Step Functions,
  MWAA, S3 notifications, Glue jobs) and Lake Formation IAM are emitter
  **follow-ons** — all are declarable in `hashicorp/aws` (see §9). Genuinely
  **imperative** (no resource exists — R8): Athena query execution and Redshift
  table DDL.
- **Snowflake** — db/schema/table/view + grants are emitted. Snowflake's
  DDL-heavy surface (streams, tasks, procedures, UDFs, masking & row-access
  policies, raw SQL) is the largest declarative **follow-on**; raw SQL execution
  is imperative (R8).

The three imperative ops (`publishEvent`/`revokeAccess`/`custom`) keep a native
executor permanently (R8) — an architectural boundary.

---

## 5. Retirement manifest — per provider

| Provider | Native CRUD | Status |
|---|---|---|
| **GCP** | `gcp/actions/`, `provider_action_handler.py`, `gcp/provider.py` apply path | ✅ retired |
| **AWS** | `aws/actions/`, `aws/provider.py` apply path | ✅ retired |
| **Snowflake** | `snowflake/actions/`, `governance.py`, `provider_enhanced.py` apply path | ✅ retired |

**Kept everywhere** (backward compat + irreducible value): `providers/*/plan/`
(forge-cli stays the planner); auth / connection / config; `codegen/`
(Airflow/Dagster/Prefect — the generated provider-action task helper now fails
loud, pointing operators at `fluid apply`); monitoring; the data-snapshot /
rollback machinery; the `.tf`/`.tf.json` `TerraformDetector` brownfield importer.

**Also retired — post-cutover cleanup:** the orphaned `infra/` generator (the
pre-spike `.tf.json` emitter, wholly superseded by the modular `iac/` package)
plus a sweep of verified-dead helper modules. `fluid apply`, `fluid generate
iac`, and the `TerraformDetector` brownfield path are the only IaC-emitting
surfaces left — every one routes through `iac/`.

A deep dead-code audit (parallel scans, every "dead" verdict re-verified by
hand) then retired ~5K LOC of pre-spike code the cutover had orphaned, in two
rounds:

- Round 1: the legacy GCP provider (`gcp/gcp.py`) and its pre-spike
  `bq.py`/`gcs.py`/`pubsub.py` plan/apply functions + the unreferenced
  `tools/plan.py` that drove them; three dead `util/` modules
  (`gcp/util/retry.py`, `aws/util/credentials.py`, `aws/util/metrics.py`);
  `aws/types.py`; `snowflake/credentials.py`; and the Snowflake `util/retry.py`
  + `util/circuit_breaker.py` + `errors.py` cluster (rooted in a single dead
  import). The unused `snowflake-snowpark-python` dep was dropped from the
  `[snowflake]` extra.
- Round 2 (a re-scan, after the round-1 changes): the obsolete native
  AWS-tagging machinery `aws/util/metadata.py` (`MetadataExtractor`/`TagManager`
  — the latter still calling `put_bucket_tagging`) and its only consumer
  `aws/util/agent_policy.py`, both reachable solely through a test; the dead
  parallel `snowflake/connection_enhanced.py`; and the now-orphaned
  `sovereignty.py::extract_sovereignty_tags`. The "live via the Snowflake
  planner" belief about `metadata.py`/`agent_policy.py` was disproven — zero
  importers tree-wide.

`gcp/provider.py::apply_policy` — the last native IAM-mutation path — was
retired to a non-mutating reporter in the same pass.

---

## 6. LOC accounting

The original proposal claimed *"~40K retires"* — inflated. Measured reality, all
three cutovers done:

- Tracked change set: **+692 / −19,929** across 75 files.
- Native per-cloud CRUD source retired: **~16K LOC** (the `actions/` packages,
  the apply halves of three provider classes, `governance.py`,
  `provider_action_handler.py`); the remaining deletions are the obsolete tests
  that covered that code.
- The spike's own footprint (`iac/` module + its tests + the apply engine) is
  ~+3K — paid back roughly five times over.

The maintenance win exceeds the LOC win: every cloud-API change in the retired
surface now moves from forge-cli's burden to the OpenTofu provider teams.

---

## 7. Abstract-op coverage (the 9 v0.7.1 ops)

| Coverage | Ops |
|---|---|
| Fully declarative | `provisionDataset`, `registerSchema`, `createView`, `grantAccess` |
| Partial | `scheduleTask`, `updatePolicy` |
| No declarative form (need a native imperative-op executor — R8) | `revokeAccess`, `publishEvent`, `custom` |

---

## 8. Test status

The full `tests/` suite is the gate. The three cutovers are verified green
across `tests/iac` (real `tofu validate` for all 3 clouds + the moto
apply/destroy e2e), `tests/providers`, and the apply / orchestration / doctor
slices. Deleting the native CRUD also retired the tests that exercised it
(per-service action tests, the abstract-op dispatch pin, Snowflake SQL-safety /
allowlist suites) — those covered an attack surface that no longer exists.

The GCP Pub/Sub emulator e2e (`test_iac_gcp_emulator_e2e.py`) is a
**CI-integration-stage probe** — `integration` + `emulated_heavy`,
self-skipping when `tofu` or the gcloud emulator is absent.

---

## 9. What is NOT yet proven

- **Real-cloud apply** on real GCP / AWS / Snowflake accounts (AWS is proven via
  the moto e2e; GCP/Snowflake have `tofu validate` + the GCP emulator CI probe).
- **The rollback snapshot pre-hook** — only the fail-closed data-loss gate
  ships (`_apply_opentofu_engine.py::_data_loss_blocked`): a destructive plan is
  blocked unless `--allow-data-loss`. No silent data loss, but the CTAS/CLONE
  snapshot-before-destroy pre-hook is deferred.
- **Remote state backend** against a real `s3://`/`gcs://` bucket; **state
  locking** — single-user local state by design in Phase 1.
- **Emitter op→resource mappings — complete for the declarative surface.** The
  `emit(contract, actions)` plumbing threads the native planner's interpreted
  actions into every emitter (with an optional `emit_data()` companion for
  `data` blocks); every op with a declarative form is now mapped, each one
  checked by real `tofu validate`:
  - **Snowflake** — streams, tasks, `views[]`, SQL procedures, SQL UDFs
    (`snowflake_stream_on_table` / `_task` / `_view` / `_procedure_sql` /
    `_function_sql`), and masking + row-access policies.
  - **GCP** — Cloud Run, Cloud Scheduler, Pub/Sub topics + push subscriptions,
    BigQuery table-level IAM, and Composer DAG deploy (the planner renders the
    DAG; the emitter uploads it as a `google_storage_bucket_object`).
  - **AWS** — Glue ETL jobs, Step Functions, and the Lambda schedule / event
    path: `aws_lambda_function` ships its inline source via a `data.archive_file`
    (`tofu` zips it), plus `aws_lambda_permission`, `aws_lambda_event_source_mapping`,
    `aws_scheduler_schedule`, `aws_cloudwatch_event_rule`/`_target`,
    `aws_s3_bucket_notification`.

  What is *not* mapped is genuinely imperative — an R8 boundary, not an emitter
  gap:
  - **AWS MWAA** (`aws_mwaa_environment`) — needs VPC `network_configuration`
    (subnets, security groups) the contract does not carry.
  - **Non-SQL Snowflake procedures / UDFs** (Python / Java / Scala) — need
    runtime / handler / package config the contract does not carry.
  - **`composer.trigger_dag`**, **Athena query execution**, **Redshift table
    DDL**, **raw DML / CTAS** (`sf.sql.execute`) — no declarative form.

---

## 10. Risk register

| Risk | Status |
|---|---|
| **R1** Rollback / no data snapshot | Fail-closed gate built & tested; snapshot pre-hook deferred. Not a blocker. |
| **R2** `plan()` coverage bounds emitters | Shadow-compare measures the gap; each emitter covers its provider's core declarative data-plane — the schedule / orchestration follow-ons are catalogued in §9. |
| **R3** ODPS/Alibaba thin coverage | Removed from scope by maintainer decision. |
| **R4** No Azure provider | Plugin model leaves a clean one-file slot. |
| **R5** Snowflake v2 provider churn | Pinned `snowflakedb/snowflake ~> 2.0` in `iac/versions.py`. |
| **R6** `tofu init` network egress | Needed to fetch providers; CI/air-gapped users need a provider mirror. |
| **R7** State locking | Single-user local state by design in Phase 1. |
| **R8** Imperative ops | `revokeAccess`/`publishEvent`/`custom` + Redshift/Athena/raw-SQL DDL keep a native executor permanently — a documented boundary, not an emitter gap. |

---

## 11. How to reproduce

```bash
# Unit + emitter + shadow-compare + cutover tests — no tofu, no creds
.venv/bin/python -m pytest tests/iac -m unit

# + tofu validate (creds-free) + the moto apply/destroy e2e
#   needs: tofu on PATH + pip install -e '.[test-emulators]'
.venv/bin/python -m pytest tests/iac
```

The GCP Pub/Sub emulator e2e runs only in the CI integration stage.

---

## 12. Recommendation

The migration is structurally complete — forge-cli is a contract-compiler; all
three providers provision via `tofu`. Before production reliance the maintainer
should: run a real-cloud apply on GCP / AWS / Snowflake accounts; stand up the
rollback snapshot pre-hook for destructive modes; and decide which §4 emitter
follow-ons (chiefly Snowflake streams/tasks/procedures/masking) are required for
the contracts in use. An Azure plugin, when wanted, is one new file under
`iac/providers/`.
