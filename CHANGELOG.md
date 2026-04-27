# Changelog

All notable changes to FLUID Forge CLI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`CHANGELOG required` CI job** — fails non-`chore`/non-`docs`/etc. PRs
  that don't touch `CHANGELOG.md`. Exempt prefixes: `chore`, `docs`,
  `test`, `ci`, `build`, `style`, `perf`, `refactor`, `revert`. Manual
  override available via the `no-changelog` label. Closes the gap that
  let 10/12 PRs in the v0.7.11 → v0.8.0 sprint ship without entries.

### Changed

- Dropped Python 3.9 support, raised the package baseline to Python 3.10,
  and expanded CI/package classifiers through Python 3.14.
- Branch protection on `main` tightened: 14 required status checks
  (full Python matrix + CodeQL + analyzers + lint + license + docs),
  CODEOWNERS reviews required, last-push approval required, stale
  reviews dismissed on new push, conversation resolution required.
  Admin override remains enabled per repo policy.

### Fixed

## [0.8.0] — 2026-04-25

### Changed

- **`fluid compile` renamed to `fluid bundle --format yaml`.** The
  legacy `fluid compile` top-level command was removed when the
  11-stage pipeline landed (`cli/compile.py` → `cli/bundle.py`). The
  canonical bundle command is `fluid bundle --format tgz` (signed,
  content-addressable archive used as the root of trust by every
  downstream pipeline stage); `fluid bundle --format yaml` is the
  drop-in replacement for the old single-document compile behaviour.
  Any CI scripts, docs, or muscle memory targeting `fluid compile`
  needs a one-line update. See `examples/0.7.1/bitcoin-multifile/`
  for the updated usage pattern.
- **`policy-check` / `policy-compile` / `policy-apply` → `fluid policy
  {check,compile,apply}` subcommand group.** The three top-level
  hyphenated forms are easily confused (all start with `policy-`,
  all do different things). They're now grouped under a single
  `fluid policy` umbrella mirroring `fluid auth` / `fluid generate`.
  Legacy hyphenated forms remain registered for one release; plan
  to migrate before the next minor.
- **`fluid rollback --list`** — new read-only discovery flag for
  enumerating available snapshots before committing to a restore.
  Mirrors `terraform state list` / `git reflog`.

## [0.7.11] — 2026-04-16

Tooling release. No user-facing CLI behavior changes; ships a refactored release pipeline and a few packaging fixes.

### Changed
- **Dynamic versioning via `setuptools-scm`.** The wheel's version is now derived from the git tag at build time (`v0.7.11a1` → wheel `0.7.11a1`). Removes the static `version = "..."` literal from `pyproject.toml`, the `__version__ = "..."` literal from `fluid_build/__init__.py`, and the `base_version` field from `fluid_build/build-manifest.yaml`. `fluid_build.__version__` now reads from `importlib.metadata.version("data-product-forge")`. Closes the version-drift bug that produced a `0.7.10` wheel for a `v0.7.10a1` tag.
- **Release pipeline restructured to sequential TestPyPI → PyPI promotion.** Every release tag is now published to TestPyPI first, install-verified in a fresh venv, and only promoted to real PyPI if the TestPyPI smoke test passes. Pre-release tags (`a*`/`b*`/`rc*`/`dev*`) stop after TestPyPI verify; stable tags continue to PyPI + a final PyPI install verify. Closes the gap where stable releases bypassed TestPyPI entirely.
- **Concurrency control** on the release workflow (`group: release, cancel-in-progress: false`) so two simultaneous tag pushes can't race PyPI uploads.
- **Pytest markers replaced inline `--deselect` flags.** Tests that fail in specific environments (GHA runner detection, Python 3.12 import-cycle issues) now carry explicit `@pytest.mark.skipif` / `@pytest.mark.xfail` markers in the test files themselves with a documented `reason=`. The release workflow's pytest invocation is back to a clean one-liner.
- **README image and LICENSE link rewritten to absolute GitHub URLs** so the PyPI / TestPyPI project pages render the Fluid Forge logo and the License badge links to the actual file (relative paths 404 on PyPI).

### Fixed
- **Version-mismatch verify-install failure** on pre-release tags (the root cause of the v0.7.10a1 incident) — fixed by the setuptools-scm migration above.

Patch release covering post-0.7.9 work that accumulated in the `Unreleased` section. No breaking changes — everything here is additive, internal tidy-up, or hardening.

**Heads-up for users: the PyPI distribution name is changing.** Starting with 0.7.10, the package publishes as **`data-product-forge`** instead of `fluid-forge`. Update your install command:

```bash
# old
pip install fluid-forge

# new
pip install data-product-forge
```

The import path (`import fluid_build`), the CLI entry point (`fluid`), and all internal provider identifiers (Snowflake query tags, Airflow DAG owners, `managed-by` labels) stay as `fluid-forge` — those are runtime/audit identifiers, not user-facing names. The CLI's Docker image on GHCR also aligns with the GitHub repo name: `ghcr.io/agenticstiger/forge-cli`.

Also the first release published from the `Agenticstiger/forge-cli` repository via the Trusted-Publishing release pipeline.

### Added
- **Master-schema validation on DMM publish (opt-in enforcement; backward-compatible across every bundled FLUID version).** `fluid datamesh-manager publish` (alias `fluid dmm publish`) now validates the loaded FLUID contract before constructing any provider payload, enforcing the CLI's role as master coordinator. **Validation honors the contract's own declared `fluidVersion`**: a 0.5.7 contract is validated against `fluid-schema-0.5.7.json`, a 0.7.1 contract against `fluid-schema-0.7.1.json`, a 0.7.2 contract against `fluid-schema-0.7.2.json`, and so on. Upgrading the CLI never invalidates a contract that was valid against its own version — the CLI coordinates publishes across the whole FLUID version range, not just the latest. **The default mode is `warn` — existing workflows are NOT affected: a contract that previously published will still publish, schema errors are logged, and the publish proceeds.** Users who want hard enforcement opt in via `--validation-mode strict`, which aborts the publish with a detailed error summary on any schema violation. Tests cover strict/warn modes, the valid-contract-in-strict-mode happy path, an end-to-end integration test that walks the full pipeline without mocking the provider, and a parametrized backward-compat suite that exercises strict-mode publish on every bundled FLUID version (0.5.7 / 0.7.1 / 0.7.2).
- **Migrated all 13 bundled templates to FLUID 0.7.2.** Mechanical migration of `builds[*].pattern` (`single-stage` → `embedded-logic`), `consumes[*]` legacy file-reference entries (removed — templates consume files via build SQL, not as upstream data products), `metadata` strict fields (dropped `sla`/`orchestration`/`environment`/`pattern`), DQ rule types (`validity` → `valid_values`, `consistency` → `accuracy`), DQ severities (`warning` → `warn`), operators (`=` → `==`), and trigger types (`scheduled` → `schedule`). All 13 templates now pass strict validation against `fluid-schema-0.7.2.json`.
- **`init --blank` scaffold now emits a valid 0.7.2 contract** (with a placeholder expose so the generated document passes the `exposes.minItems: 1` constraint out of the box).
- **ODPS input-port lineage** — `OdpsStandardProvider` now maps FLUID `consumes[]` entries to ODPS-Bitol `inputPorts`, so upstream data-product lineage is preserved when publishing to Entropy Data (datamesh-manager `provider_hint="odps"`).
- **Cross-version compatibility matrix** — new `tests/test_contract_compatibility_matrix.py` parameterizes a small set of golden fixture contracts (minimal 0.5.7/0.7.1/0.7.2, lineage 0.7.1/0.7.2) across schema validation and every export path (ODCS, official OPDS, ODPS-Bitol, DMM DPS dry-run, DMM ODPS dry-run). Guards against silent regressions across FLUID schema bumps.
- **`consumes_to_canonical_ports` / `get_owner` / `slugify_identifier` helpers** in `fluid_build/util/contract.py` — shared normalization used by both ODPS providers and the CLI init path.
- **FLUID 0.7.2 bundled schema** added to the fallback set in `FluidSchemaManager._discover_bundled_versions`, plus a new `latest_bundled_version()` classmethod so the "latest version" is resolved once and centrally.

### Changed
- **`fluid_build.cli.validate` exposes a public `run_on_contract_dict(...)` helper** (plus a public `output_text_results` alias) that other CLI commands can use to validate an already-loaded contract dict with identical UX to `fluid validate`. The DMM publish path is the first caller; the private `_output_text_results` name is no longer reached into.
- **`get_owner` precedence flipped** to the 0.7.2-canonical order: `metadata.owner` first, with top-level `owner` kept as the legacy fallback. Matches the master schema (which forbids top-level `owner` under `additionalProperties: false`).
- **`consumes_to_canonical_ports` now forwards the complete 0.7.2 `consumeRef` field set** (`versionConstraint`, `qosExpectations`, `requiredPolicies`, `tags`, `labels`) in addition to the legacy extension fields. Providers can forward any subset without re-parsing the raw contract. Non-mapping/non-list values on typed fields degrade to `None` for predictable downstream checks.
- **`slugify_identifier` leading-digit guard now applies to the fallback too**, so numeric or punctuation-only fallbacks still produce valid FLUID identifiers. Both input and fallback are slug-cleaned before the guard runs; if both collapse to empty, a single-character sentinel (`"x"`) is returned rather than an invalid identifier.
- **Scan-mode helper extraction and cleanup.** `generate_contracts_from_scan`, `apply_governance_policies`, and `show_migration_summary` now live in `fluid_build.cli.init_scan`, with `fluid_build.cli.init` re-exporting the public helpers for import stability. The temporary scan-mode `produces[]` fallback has been removed now that repo- and issue-level checks no longer show a live dependency.
- **Removed dead helpers** from `fluid_build/util/contract.py`: `get_expose_schema`, `get_expose_format`, `normalize_expose`, `normalize_contract`. These were silently wrong for 0.7.2 (reading fields that 0.7.2 `additionalProperties: false` rejects or producing outputs that don't satisfy the 0.7.2 `binding` shape), had zero production callers, and only existed to keep their own tests green. Associated test classes removed.
- **End-to-end publish integration test added** (`tests/cli/test_datamesh_manager.py::TestCmdPublishEndToEnd`) that walks through the full pipeline — on-disk 0.7.2 fixture → loader → master-schema validation → `DataMeshManagerProvider.apply(dry_run=True)` → payload assertions — without mocking the provider. Catches any wiring regression along the publish chain that unit tests with mocks would paper over.
- **ODPS input ports no longer fabricate `contractId` or default `required: True`.** Fields that were not explicitly declared on a `consumes[]` entry are omitted from the output rather than filled with synthetic defaults. Downstream consumers see only fields that point to real upstream identifiers, and pipelines are no longer implicitly marked as requiring every upstream.
- **Unified input-port extraction.** The official `OdpsProvider` and `OdpsStandardProvider` now share a single `consumes_to_canonical_ports` traversal (in `util/contract.py`) instead of reimplementing it side-by-side.
- **`OdpsStandardProvider` 0.7.x field tolerance** — reads both `expose.id`/`exposeId`, `expose.contract.schema`/`expose.schema`, `binding.platform`/`expose.provider`, `binding.location`/`expose.location`, and top-level `owner`/`description`/`domain`/`fluidVersion` as fallbacks for their `metadata.*` counterparts.
- **ODPS output ports now emit an `id` field** (previously only `name`). `contractId` on output ports is only populated when explicitly set on the expose — the DMM layer still overlays a deterministic `contractId` when publishing companion contracts.
- **`OdpsStandardProvider` raises `ProviderError`** (instead of `KeyError`) when an expose is missing both `id` and `exposeId`.
- **Malformed `consumes[]` entries are now skipped with a logged warning** rather than silently dropped.
- **CLI `init` blank mode** now uses `slugify_identifier()` to produce valid contract IDs from arbitrary project names (handles punctuation, leading digits, and non-ASCII).
- **Latest bundled FLUID version is resolved lazily** in `fluid_build/cli/init.py` and `fluid_build/cli/provider_init.py` via `_latest_fluid_version()` helpers instead of module-level constants computed at import time.
- **Templates bumped to FLUID 0.7.2.**

## [0.7.9] — 2026-04-06

### Added
- **Smart Forge first-run recovery** — bare `fluid forge` now distinguishes between an implicit entry and an explicit `--mode`, auto-starts copilot only when LLM prerequisites are already satisfied, and otherwise offers session-only AI setup or alternate creation-mode fallback instead of crashing.
- **Explicit extended doctor diagnostics** — `fluid doctor --extended` now runs optional workspace diagnostics via `scripts/diagnose.sh`, with `--comprehensive` supported as a compatibility alias.

### Changed
- **Doctor default behavior** — `fluid doctor` is now self-contained by default, showing a built-in summary, copilot readiness, and actionable next steps without probing workspace-only scripts unless `--extended` is requested.
- **Tooling and help alignment** — Make targets, bootstrap helpers, pipeline templates, and help text now reference the explicit extended-diagnostics flow so the CLI contract is consistent across entry points.
- **Release version bump** — promoted the CLI and build manifest version to `0.7.9`.

### Fixed
- **Forge missing-LLM onboarding** — missing LLM configuration now surfaces friendly recovery guidance and suggestions instead of raw error keys or a traceback during Forge startup.
- **Doctor missing-script messaging** — default doctor runs no longer end with `Diagnostic script not found or invalid: scripts/diagnose.sh`; that message is now replaced by a clear `Extended diagnostics are not installed in this checkout.` error only when `--extended` is explicitly requested.

## [0.7.8] — 2026-04-03

### Changed
- **Release version consistency** — aligned the runtime CLI version with package metadata so `fluid --version` and the published package both report `0.7.8`.
- **Release notes continuity** — added the `0.7.8` changelog section and compare link so the release history matches the version bump.

### Fixed
- **Windows timeout enforcement** — corrected the non-`SIGALRM` security timeout path so operations time out promptly instead of waiting for the worker to finish.
- **Regression coverage** — added a targeted test for the Windows timeout path to keep the fallback behavior stable.

## [0.7.7] — 2026-04-01

### Added
- **Forge copilot architecture refresh** — modularized the `fluid forge` copilot flow into focused runtime, context, UI, mode, and agent layers for easier iteration and maintenance.
- **Declarative domain-agent specs** — built-in YAML-backed agent specs now power domain guidance without hard-coding every interview path in Python.
- **Project-memory-aware copilot flow** — copilot generation now supports project-scoped memory and post-generation clarification loops to refine outputs with more context.

### Changed
- **Release version bump** — promoted the Forge CLI and companion Claude plugin assets to `0.7.7` to reflect the sizable copilot feature set landing in this release.

### Fixed
- **Test suite alignment with PR #8 merge** — updated tests to match new `_extract_sla_properties()` list-of-dicts return format, `_publish_odcs_per_expose()` keyword argument signature, and `dataContractId` format (`{product_id}.{expose_id}`)
- **Result URL domain** — test assertions updated to match `api.entropy-data.com` (from `app.entropy-data.com`)
- **License headers** — added Apache 2.0 headers to `tests/test_datamesh_manager_publish_spec.py` and `tests/test_odcs_sla_properties.py`

## [0.7.6] — 2026-03-31

### Fixed
- **Data Product payload conformance** — `PUT /api/dataproducts/{id}` now sends `dataProductSpecification: "0.0.1"`, root-level `id`, `info.title` (not `info.name`), and `info.owner` per Data Product Specification v0.0.1 schema
- **Output port server object** — output ports now send structured `server` objects (`account`, `database`, `table`) instead of flat `location` strings, matching the DPS schema
- **Result URLs** — publish result URLs now use the configured `api_url` instead of hardcoded `app.entropy-data.com`
- **`_cmd_publish()` signature bug** — all five `_cmd_*` functions in `datamesh_manager.py` now accept `(args, logger=None)` to match the CLI dispatcher in `__init__.py:432`

### Added
- **ODCS v3.1.0 data contract support** — `_build_data_contract_odcs()` generates Open Data Contract Standard v3.1.0 payloads (`apiVersion`, `kind: DataContract`, `team.name`, `description.purpose`, array-based `schema` with `logicalType` mapping)
- **`--contract-format {odcs,dcs}` CLI flag** — choose between ODCS v3.1.0 (default) and deprecated DCS 0.9.3 when publishing companion data contracts
- **`dataContractId` wiring** — output ports automatically include `dataContractId` linking to the companion contract when `--with-contract` is used
- **Archetype inference** — `info.archetype` auto-inferred from `metadata.layer` (Bronze→source-aligned, Silver→consumer-aligned, Gold→aggregate) when not explicitly set
- **SQL-to-ODCS type mapping** — `_odcs_logical_type()` maps 25+ SQL/FLUID types to ODCS logical types
- **86 tests** for `fluid dmm` subcommand (up from 40), covering DPS conformance, ODCS/DCS builders, server objects, format dispatch, and dataContractId wiring

### Deprecated
- DCS 0.9.3 data contract format — use `--contract-format dcs` for backward compatibility. Entropy Data removing DCS support after 2026-12-31.

## [0.7.1] — 2026-01-30

### Added
- Multi-provider Airflow DAG generation (`fluid generate-airflow`)
- GCP code generators: Airflow, Dagster, Prefect
- Snowflake code generators: Airflow, Dagster, Prefect
- AWS code generators: Airflow, Dagster, Prefect
- Circular dependency detection in contract validation
- Provider SDK extraction (`fluid-provider-sdk` v0.1.0)
- Plugin-based provider discovery via entry points
- Local provider with DuckDB backend
- Policy engine (check, compile, apply)
- Blueprint system for data product templates
- ODPS/ODCS standard export support
- Marketplace and catalog connectors
- Comprehensive GitHub Actions CI (Python 3.9–3.12, ruff, black, bandit, coverage)
- Apache 2.0 license headers on all source files
- CONTRIBUTING.md, CODE_OF_CONDUCT.md, SECURITY.md
- Issue templates (bug report, feature request, provider request)
- PR template

### Performance
- Contract generation: 0.29–2.54ms per contract
- 828 tests passing at release

## [0.5.7] — 2025-08-15

### Added
- Initial public release
- Core CLI: `validate`, `plan`, `apply`
- GCP BigQuery provider
- Contract schema v0.5.7
- Basic Airflow DAG export

[Unreleased]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.10...v0.8.0
[0.7.11]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.10...v0.7.11
[0.7.10]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.9...v0.7.10
[0.7.9]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.8...v0.7.9
[0.7.8]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.7...v0.7.8
[0.7.7]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.6...v0.7.7
[0.7.6]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.1...v0.7.6
[0.7.1]: https://github.com/Agenticstiger/forge-cli/compare/v0.5.7...v0.7.1
[0.5.7]: https://github.com/Agenticstiger/forge-cli/releases/tag/v0.5.7
