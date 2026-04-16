# FLUID Forge CLI — Trello Card Status Tracker

> **Last audited:** 2026-03-06 (automated codebase scan + implementation session)
>
> **Legend:** ✅ Done | 🟡 Partial | ❌ Not Started | 🚫 Cannot Verify (requires external service)

---

## Dashboard

| Epic | Done | Partial | Not Done | Completion |
|------|:----:|:-------:|:--------:|:----------:|
| 1 — Legal & Compliance | 4 | 0 | 0 | 100% |
| 2 — Security & Secrets | 3 | 0 | 0 | 100% |
| 3 — Code Quality | 1 | 0 | 2 | 33% |
| 4 — CLI Consolidation | 0 | 1 | 4 | ~5% |
| 5 — Provider Modularization | 1 | 1 | 0 | 75% |
| 6 — Airflow & Glue | 0 | 1 | 4 | ~10% |
| 7 — Testing & Quality | 0 | 2 | 2 | ~30% |
| 8 — Documentation | 2 | 1 | 1 | 60% |
| 9 — Community Setup | 2 | 1 | 1 | 55% |
| 10 — Release Preparation | 0 | 3 | 3 | ~25% |
| **Totals** | **13** | **10** | **17** | **~45%** |

---

## Epic 1: Legal & Compliance

| Card | Priority | Status | Evidence |
|------|----------|:------:|----------|
| **1.1** Add License Headers | P0 | ✅ | `scripts/add_license_headers.py` exists. CI checks tracked Python files repo-wide except illustrative `examples/**`, and maintained source files carry the Apache 2.0 header. |
| **1.2** Audit Third-Party Dependencies | P0 | ✅ | `THIRD_PARTY_LICENSES.md` generated (46 packages). No GPL-only or AGPL deps. `text-unidecode` is dual-licensed Artistic/GPL — using under Artistic License. |
| **1.3** Create Community Documents | P0 | ✅ | `CONTRIBUTING.md` (121 lines), `CODE_OF_CONDUCT.md` (133 lines), `SECURITY.md` (63 lines) all exist and are linked from `README.md`. |
| **1.4** Remove Proprietary References | P0 | ✅ | No "agentics internal", "confidential" (as IP), or "proprietary" references found. One benign reference to Bitol.io's "proprietary ODPS variant" in `odps_standard.py:21` — describes a third-party spec, not Agentics IP. |

### Remaining work — Epic 1
- [x] `pip install pip-licenses && pip-licenses --format=markdown > THIRD_PARTY_LICENSES.md`
- [x] Review output for GPL/AGPL deps — **1 flagged** (`text-unidecode` dual Artistic/GPL — safe under Artistic)
- [x] Commit `THIRD_PARTY_LICENSES.md`
- [ ] *(Optional)* Reword `odps_standard.py:21` from "proprietary" to "custom"

---

## Epic 2: Security & Secrets

| Card | Priority | Status | Evidence |
|------|----------|:------:|----------|
| **2.1** Scan for Hardcoded Secrets | P0 | ✅ | `.secrets.baseline` generated (25 findings, all reviewed as false positives). `.pre-commit-config.yaml` created with detect-secrets hook. Hardcoded `'admin'` default password in `market.py` replaced with env var lookup (`ATLAS_USERNAME`/`ATLAS_PASSWORD`). |
| **2.2** Sanitize Account IDs | P0 | ✅ | All 17 instances of `123456789012` replaced. Source code uses `self.account_id` for dynamic ARNs. Docs/examples use `YOUR_AWS_ACCOUNT_ID` placeholder. `grep` returns 0 matches. |
| **2.3** Remove Internal Tools | P1 | ✅ | `admin.py` deleted and un-registered from `bootstrap.py` + `_STABLE_COMMANDS`. `analytics.py` deleted (was dead code). `scripts/internal/` and `tests/internal/` already absent. All 264 tests pass. |

### Remaining work — Epic 2
- [x] `pip install detect-secrets && detect-secrets scan > .secrets.baseline`
- [x] Fix `market.py:880-881` — replaced hardcoded `username='admin'/password='admin'` with env var lookup
- [x] Create `.pre-commit-config.yaml` with detect-secrets, black, and ruff hooks
- [x] Find-and-replace all `123456789012` → `YOUR_AWS_ACCOUNT_ID` (17 files, 0 remaining)
- [x] Remove `fluid_build/cli/admin.py`; un-registered from `bootstrap.py`
- [x] Remove `fluid_build/cli/analytics.py` (dead code)
- [x] Full test suite confirmed: 264 passed, 0 failed

---

## Epic 3: Code Quality

| Card | Priority | Status | Evidence |
|------|----------|:------:|----------|
| **3.1** Fix Bare Except Clauses | P0 | ✅ | **0 bare `except:` clauses** remain. All 681 except statements in `fluid_build/` specify an exception type. |
| **3.2** Complete Critical TODOs | P0 | ❌ | **15 TODOs** remain. All 5 target files still have unresolved TODOs: `credentials/resolver.py:277`, `cli/provider_action_executor.py:227`, `cli/marketplace.py:455`, `providers/aws/codegen/airflow.py:412,420`, `providers/aws/plan/schedule.py:523,549,570,589,610`. |
| **3.3** Replace `print()` with Logging | P1 | ❌ | Logging infra exists (`logging_utils.py`, `structured_logging.py`). 867 `logger.*()` calls in use. But **738 plain `print()` calls remain** across production code. Top offenders: `pipeline_generator.py` (53), `market.py` (52), `verify.py` (47). |

### Remaining work — Epic 3
- [ ] Triage 15 TODOs: implement critical ones, convert the rest to GitHub Issues
- [ ] Priority TODOs: `credentials/resolver.py` config file support, `provider_action_executor.py` Snowflake handler
- [ ] Create a `print()` → `logger` migration script or do in batches (738 calls)
- [ ] Start with top 5 offender files

---

## Epic 4: CLI Consolidation

| Card | Priority | Status | Evidence |
|------|----------|:------:|----------|
| **4.1** Merge Validation Commands | P0 | 🟡 | `--strict` flag added to `validate.py`. But `--compatibility` and `--check-resources` missing. `contract_validation.py` and `contract_tests.py` still exist as separate commands with no deprecation warnings. |
| **4.2** Consolidate Export Commands | P0 | ❌ | `export.py` has no `--format opds` option. `export_opds.py` still exists as separate file. No deprecation warnings. |
| **4.3** Add Command Groups | P1 | ❌ | All commands registered flat: `policy-check`, `viz-graph`, `generate-airflow`. No sub-subcommand grouping (`fluid policy compile`). |
| **4.4** Rename Marketplace Commands | P1 | ❌ | No `catalog.py`. Both `market.py` (127 KB) and `marketplace.py` (22 KB) still exist with original names. |
| **4.5** Remove Duplicate Commands | P1 | ❌ | All 4 duplicate files still exist: `product_new.py`, `contract_tests.py`, `contract_validation.py`, `export_opds.py`. |

### Remaining work — Epic 4
- [ ] **4.1:** Add `--compatibility` + `--check-resources` to `validate.py`. Add deprecation warnings to `contract_validation.py` and `contract_tests.py`. Then remove them in next release.
- [ ] **4.2:** Add `--format opds` to `export.py`, add deprecation warning to `export_opds.py`
- [ ] **4.3:** Refactor `bootstrap.py` to create argparse sub-parsers for `policy`, `viz`, `generate` command groups
- [ ] **4.4:** Rename `market.py` → `catalog.py`, update registration
- [ ] **4.5:** Delete merged duplicates after 4.1/4.2 are done — must update `bootstrap.py` registration and tests

---

## Epic 5: Provider Modularization

| Card | Priority | Status | Evidence |
|------|----------|:------:|----------|
| **5.1** Extract Provider SDK | P1 | ✅ | `forge-cli-sdk/` exists with `pyproject.toml` (v0.1.0), `src/fluid_provider_sdk/`. Main CLI imports it and lists it as a dependency. |
| **5.2** Separate Providers into SDK Packages | P1 | 🟡 | `entry_points` declared in `pyproject.toml` for plugin discovery (local, aws, gcp, snowflake, odps). But providers **still live inside** `fluid_build/providers/` — not yet extracted to separate packages. |

### Remaining work — Epic 5
- [ ] Extract `fluid_build/providers/gcp/` to `fluid-provider-gcp` package
- [ ] Extract `fluid_build/providers/aws/` to `fluid-provider-aws` package
- [ ] Extract `fluid_build/providers/snowflake/` to `fluid-provider-snowflake` package
- [ ] Each needs: own `pyproject.toml`, entry point registration, tests, docs
- [ ] Keep `local` provider bundled with CLI

---

## Epic 6: Airflow & Glue Completion

| Card | Priority | Status | Evidence |
|------|----------|:------:|----------|
| **6.1** Complete Airflow DAG Generation | P0 | ❌ | 4 TODOs remain: `airflow.py:412,420` (table creation, action execution), `airflow_generator.py:166,198` (unsupported task type, SQL generation). |
| **6.2** Implement TaskFlow API | P1 | ❌ | 0 references to TaskFlow in codebase. Only Airflow 1.x style operators generated. |
| **6.3** Complete AWS Glue Job Support | P1 | 🟡 | `glue.py` has `ensure_database`, `ensure_table`, `ensure_crawler`, `ensure_iceberg_table`. But **`glue.job.ensure`** (ETL job create/manage) is missing. |
| **6.4** Add Glue Examples | P1 | ❌ | No `examples/aws-glue-data-lake/` directory. No Glue examples anywhere. |
| **6.5** Add Airflow Tests | P0 | ❌ | 0 Airflow/DAG test files. 0 test functions for Airflow generation. |

### Remaining work — Epic 6
- [ ] Implement the 4 TODOs in Airflow codegen
- [ ] Add `glue.job.ensure` action for Glue ETL job creation
- [ ] Create `examples/aws-glue-data-lake/` with database+table, crawler, Iceberg, and job examples
- [ ] Write 20+ Airflow generation tests (DAG syntax, task deps, operators, schedule parsing)
- [ ] TaskFlow API is P1 — defer until Airflow 1.x generation is solid

---

## Epic 7: Testing & Quality

| Card | Priority | Status | Evidence |
|------|----------|:------:|----------|
| **7.1** Increase Test Coverage to 95% | P1 | ❌ | **264 tests pass** (191 provider + 28 CLI smoke + 45 config manager). No `fail_under` threshold enforced in CI. Coverage not measured against 95% target. |
| **7.2** Add Property-Based Tests | P2 | ❌ | Hypothesis not installed. 0 property-based tests. |
| **7.3** Add Performance Benchmarks | P2 | ❌ | `pytest-benchmark` not installed. 0 benchmark tests. |
| **7.4** Setup GitHub Actions CI | P0 | 🟡 | `.github/workflows/ci.yml` exists. Tests on Py 3.9–3.12, runs `ruff`, `black`, `pytest`, uploads coverage to Codecov. CI status badge added to README. Bandit now **blocks on failures** (removed `|| true`). **Remaining gap:** no `fail_under` coverage threshold. |

### Remaining work — Epic 7
- [ ] Run `pytest --cov=fluid_build --cov-report=html` to measure current coverage %
- [ ] Add `--cov-fail-under=80` to CI (stepping stone to 95%)
- [ ] Add tests for AWS provider, ODPS provider, Forge module, remaining CLI commands
- [x] Remove `|| true` from Bandit step in CI
- [x] Add CI status badge to README
- [ ] *(P2)* Install `hypothesis` and add property-based tests for contract validation
- [ ] *(P2)* Install `pytest-benchmark` and add benchmarks for plan generation

---

## Epic 8: Documentation

| Card | Priority | Status | Evidence |
|------|----------|:------:|----------|
| **8.1** Enhance README.md | P0 | 🟡 | 440+ lines, has quickstart, installation, features, badges (including CI badge), contributing section, "What is FLUID?" section with one-liner diagram. **Remaining:** architecture diagram (mermaid/ASCII). |
| **8.2** Create Getting Started Guide | P0 | 🟡 | Getting-started content exists in the docs sites (`docs/docs/getting-started/`). Not present as standalone guide in the CLI repo. |
| **8.3** Create Provider Documentation | P1 | ✅ | Comprehensive per-provider docs exist: `aws.md` (49K), `gcp.md` (44K), `snowflake.md` (49K), `local.md`, `odps.md`, `sdk.md` in docs site. Plus `docs/CREATING_PROVIDERS.md`. |
| **8.4** Create Video Tutorials | P2 | ❌ | 0 video links in README or docs. |

### Remaining work — Epic 8
- [x] Add "What is FLUID?" section to top of README
- [ ] Add ASCII/mermaid architecture diagram to README
- [ ] Add `QUICKSTART.md` to CLI repo root (or link prominently to docs site guide)
- [ ] *(P2)* Create and link video tutorials

---

## Epic 9: Community Setup

| Card | Priority | Status | Evidence |
|------|----------|:------:|----------|
| **9.1** Configure GitHub Repository | P0 | ✅ | `.github/ISSUE_TEMPLATE/` exists (3 templates). `.github/CODEOWNERS` created with team ownership rules. Branch protection requires GitHub UI. |
| **9.2** Create Issue Templates | P0 | ✅ | `bug_report.yml`, `feature_request.yml`, `provider_request.yml`, and `pull_request_template.md` all exist. |
| **9.3** Setup Discussion Categories | P1 | 🚫 | README links to GitHub Discussions. Actual categories require GitHub settings — cannot verify from code. |
| **9.4** Create Discord/Slack Community | P1 | ❌ | No Discord/Slack links in README or docs. |
| **9.5** Rollout the Doc Site | P0 | ✅ | Two VuePress-based doc sites: `docs/` (with Dockerfile) and `fluid-forge-docs/` (with built `dist/`). Build commands configured in `package.json`. |

### Remaining work — Epic 9
- [x] Create `.github/CODEOWNERS` file
- [ ] *(GitHub UI)* Verify branch protection rules are active
- [ ] *(GitHub UI)* Verify discussion categories are configured
- [ ] Set up Discord or Slack workspace; add invite link to README
- [ ] *(Optional)* Pin welcome message in Discussions

---

## Epic 10: Release Preparation

| Card | Priority | Status | Evidence |
|------|----------|:------:|----------|
| **10.1** Version Bump to 1.0.0 | P0 | 🟡 | Versions consistent (`pyproject.toml` = `__init__.py` = `0.7.7`). Still at 0.7.7, not bumped to 1.0.0. **`CHANGELOG.md` created** with full history (0.5.7 → 0.7.1 → 0.7.6 → 0.7.7 → Unreleased). |
| **10.3** Publish to Test PyPI | P0 | ❌ | No Test PyPI workflow or evidence of test publish. |
| **10.4** Publish to PyPI | P0 | 🟡 | `pyproject.toml` has build config + PyPI badge suggests it's already published at 0.7.7. But **no automated publish workflow** in CI. |
| **10.5** Create GitHub Release | P0 | ❌ | No release notes in CLI repo (docs still carry the historical `fluid-forge-docs/docs/RELEASE_NOTES_0.7.1.md`). No tag-triggered release workflow. |
| **10.6** Launch Announcement | P0 | ❌ | No announcement or blog content in repo. |

### Remaining work — Epic 10
- [x] Create `CHANGELOG.md` from git history + release notes
- [ ] When ready for 1.0.0: bump version in `pyproject.toml` + `__init__.py`
- [ ] Create `.github/workflows/publish.yml` for automated PyPI publishing on tag
- [ ] Test on Test PyPI first
- [ ] Create GitHub Release from tag with proper release notes
- [ ] Prepare launch announcement for HN, Reddit, Twitter/X, LinkedIn

---

## Morning Pickup Plan

### Step 1: Quick Wins (< 1 hour each)

| Task | Card | Command / Action |
|------|------|------------------|
| Generate third-party licenses | 1.2 | `pip install pip-licenses && pip-licenses --format=markdown > THIRD_PARTY_LICENSES.md` |
| Fix hardcoded password | 2.1 | Edit `market.py:880-881` — require config/env var instead of `'admin'` default |
| Replace `123456789012` | 2.2 | `grep -rn "123456789012" . \| sed` across 17 files → `YOUR_AWS_ACCOUNT_ID` |
| Create CODEOWNERS | 9.1 | Create `.github/CODEOWNERS` with team ownership rules |
| Add CI badge to README | 7.4 | Add `![CI](https://github.com/Agenticstiger/forge-cli/actions/workflows/ci.yml/badge.svg)` |
| Create CHANGELOG.md | 10.1 | Generate from git log + release notes |
| Add "What is FLUID?" to README | 8.1 | Add 2-paragraph section at top of README |

### Step 2: Half-Day Tasks

| Task | Card | Notes |
|------|------|-------|
| Setup detect-secrets + pre-commit | 2.1 | Install, scan, create baseline + `.pre-commit-config.yaml` |
| Remove internal commands | 2.3 | Remove `admin.py`, `analytics.py`; update `bootstrap.py` |
| Add `--compatibility` + `--check-resources` to validate | 4.1 | Merge logic from `contract_validation.py` + `contract_tests.py` |
| Add `--format opds` to export | 4.2 | Merge logic from `export_opds.py` |
| Make Bandit blocking in CI | 7.4 | Remove `\|\| true` from `ci.yml` Bandit step |

### Step 3: Multi-Day Work (schedule for the week)

| Task | Card | Effort |
|------|------|--------|
| Complete 15 critical TODOs | 3.2 | 2 weeks (or triage to issues) |
| Replace 738 `print()` calls | 3.3 | 2 days |
| Implement command groups (`policy`, `viz`, `generate`) | 4.3 | 3 days |
| Rename `market` → `catalog` + remove duplicates | 4.4, 4.5 | 2 days |
| Complete Airflow codegen TODOs + write tests | 6.1, 6.5 | 1 week |
| Implement `glue.job.ensure` + examples | 6.3, 6.4 | 1 week |
| Coverage push toward 95% | 7.1 | 2 weeks |
| Extract cloud providers to SDK packages | 5.2 | 3 days |

### Step 4: Deferred / P2

| Task | Card | Notes |
|------|------|-------|
| TaskFlow API | 6.2 | After Airflow 1.x codegen is solid |
| Property-based tests | 7.2 | Nice-to-have after coverage target met |
| Performance benchmarks | 7.3 | Nice-to-have |
| Video tutorials | 8.4 | Marketing-dependent |
| Discord/Slack community | 9.4 | Requires team decision |
| Launch announcement | 10.6 | After 1.0.0 release |

---

## Copilot Implementation — Discussion Needed

> *"We need to discuss the Copilot implementation"*

The codebase has a `fluid_build/cli/copilot.py` command and an `fluid_build/cli/ide.py` module already registered. Before proceeding, we should discuss:

1. **Scope** — What does "Copilot implementation" mean? AI-assisted contract authoring? IDE integration? Agent-driven pipeline generation?
2. **Architecture** — Does it use the existing SDK hooks system, or is it a standalone module?
3. **Priority** — Where does it fit vs. the P0 items above?

Add this as a card-planning discussion item for your morning sync.
