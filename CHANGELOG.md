# Changelog

All notable changes to FLUID Forge CLI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.8.4] - 2026-05-26

### Added (DMM CLI surface + lineage UX)
- **`fluid dmm wipe`** — multi-pass FK-aware mass delete of every DataProduct
  in the tenant. Supersedes the ad-hoc helper scripts everyone was writing.
  `--yes` to skip confirmation; `--max-passes <n>` to bound retry attempts.
- **`fluid dmm list-contracts` / `get-contract` / `delete-contract`** —
  parity with the DataProduct surface for the long-overlooked DataContracts
  side of the DMM API.
- **`fluid dmm delete` enriched error** — on DMM's `422 Cannot delete
  because data product is in use` lock, the CLI now enumerates the
  consumer products holding the FK and prints them in the error so the
  operator knows what to delete first.
- **`fluid dmm publish` access-agreement visibility** — output now shows
  the count + approval status of Access agreements created from
  `consumes[]`. When any are left `pending` the CLI prints a yellow hint
  pointing at `--auto-approve-access` / `DMM_AUTO_APPROVE_ACCESS=true`.
  This closes the most common "I published but the DMM lineage graph is
  empty" footgun: DMM only renders lineage from APPROVED agreements.
- **`fluid dmm publish --auto-approve-access` / `--no-auto-approve-access`** —
  explicit flags overriding the env var, with help text that explains
  the lineage-rendering implications.

### Fixed (DMM + emitter bugs)
- **`OdpsStandardProvider.render()` no longer drops duplicate-exposeId
  consumes.** Previously, two `consumes[]` entries sharing an `exposeId`
  (e.g. both upstream products expose `data_analytics_platform`) collapsed
  to ONE InputPort via name-keyed dedup; the second was silently dropped.
  Now dedup is keyed by `(name, contract_id)` and collisions are
  disambiguated by tail-of-productId prefix (`b2__data_analytics_platform`),
  so every consume produces its own InputPort. Pinned by
  `tests/test_odps_cli_spec.py::TestInputPortDuplicateExposeIdHandling`.
- **`_publish_access_agreements` pre-flight upstream check.** Previously,
  if a `consumes[].productId` pointed at a product that didn't exist in
  DMM, the access PUT returned a generic `404` mid-publish. Now the
  publisher lists existing products first and surfaces a structured
  `missing_upstream_product` warning per skipped agreement, naming the
  upstream that needs publishing first.
- **`fluid dmm list --format json`** — Rich console line-wrapping injected
  literal newlines into JSON string values, breaking `json.loads()` for
  pipe consumers. Now bypasses Rich for `--format json` and writes
  directly to stdout.
- **`generator: "fluid-forge-opds-provider"` payload literal** in the LF
  v4.1 wrapper renamed to `fluid-forge-odps-provider` for naming
  consistency with the canonical spec acronym. Wire-visible field;
  downstream consumers that keyed on the old string should accept both.

### Removed (dead code)
- `_generate_uuid_from_id` + `_CANONICAL_UUID_NAMESPACE` +
  `_LEGACY_UUID_NAMESPACE` + the `FLUID_LEGACY_UUID_NAMESPACE` env var
  escape hatch (added in Phase 8.2 as a thoughtful migration path) —
  **deleted**: zero callers in the codebase, no production path depends on
  it. If a future caller needs deterministic UUIDs from a product id, the
  helper is a 3-liner; re-introduce it with explicit wiring at that time.

### Changed (BEHAVIOR — read carefully)
- **`fluid generate standard` defaults reordered: Bitol ODPS v1.0.0 is now
  the center-stage `--format odps`.** Previously `--format odps` routed to
  the LF/ODPI v4.1 emitter (which itself silently fell back to a
  5-field degenerate placeholder because the underlying `cli.opds.run`
  symbol didn't exist). The fix:
  - `--format odps` → emits **Bitol Open Data Product Standard v1.0.0**
    (bare product YAML via `BitolOdpsProvider().render()`).
  - `--format odps-bitol` → explicit alias of `--format odps` (kept for
    callers that want disambiguation in CI logs).
  - `--format odps-v4.1` (NEW) → emits **LF/ODPI Open Data Product
    Specification v4.1** (bare JSON via `OdpsProvider().render()` with
    the `artifacts:` wrapper unwrapped).
  - `--format opds` → deprecated letter-swap alias of `--format odps-v4.1`
    (NOT `--format odps`) — emits a WARNING + the LF/ODPI v4.1 JSON.
    Reasoning: in this codebase `--format opds` has always *meant* the
    LF/ODPI export (the historical default of that flag), so back-compat
    callers keep getting the spec they actually consume.
  - `fluid export-opds` → also fixed; now emits a real LF/ODPI v4.1 JSON
    (was: same degenerate fallback). Logs a DEPRECATION warning pointing at
    `fluid generate standard --format odps-v4.1`.
  - All previously-emitted degenerate shapes (`{specVersion: "4.1", id,
    name, domain, owner}` and `{specVersion: "1.0", id, title, owner,
    domain, exposes[]}`) are GONE — every path now produces a real spec
    doc.
- **`fluid generate standard --list` output reordered Bitol-first**
  (`odps` → `odps-bitol` → `odcs` → `odps-v4.1` → `opds`). The README
  "Which ODPS?" section, AGENTS.md pipeline row, and the rich help
  formatter table are all reordered to lead with Bitol.

### Changed
- **ODPS / OPDS naming alignment with upstream specifications.** Resolved a
  long-standing letter-swap that treated the canonical acronym **ODPS**
  (Open Data Product Specification — Linux Foundation / ODPI v4.1) and the
  letter-swap **OPDS** as if they were separate standards in user-facing
  help text and emit-set listings. Two distinct standards do share the
  ODPS acronym (Bitol's *Standard* v1.0.0 and the LF *Specification* v4.1)
  and both are now disambiguated by full name everywhere the names appear.
  - `fluid odps` is the canonical subcommand; `fluid opds` remains as a
    documented deprecated alias.
  - `--spec odps-4.1` is the canonical id for the LF/ODPI spec;
    `--spec odpi-4.1` (which swapped the *spec* name for the *org* name) is
    accepted with a WARNING that points at the correct id.
  - `--format opds`, `--emit opds`, and the `OPDS_*` env vars
    (`OPDS_VERSION`, `OPDS_INCLUDE_BUILD_INFO`, `OPDS_INCLUDE_EXECUTION_DETAILS`,
    `OPDS_TARGET_PLATFORM`, `OPDS_VALIDATE_OUTPUT`) are accepted with a
    one-time WARNING that names the canonical ODPS form.
  - `pyproject.toml`: added `[odps]` extra as the canonical name; `[opds]`
    is kept as a back-compat empty alias that depends on `[odps]`. The
    `[all]` umbrella now references `[odps]`.
  - Internals: `SPEC_ODPS_4_1` / `ODPS_4_1_*_URL` are the canonical names
    in `fluid_build.cli.opds`; `SPEC_ODPI_4_1` / `ODPI_4_1_*_URL` remain
    as module-level back-compat aliases.
  - The `OdpsProvider.name` property and the deterministic UUID namespace
    string (`"fluid-forge-opds"`) are intentionally **unchanged** to
    preserve wire-format keys and deterministic IDs across releases.
  - Test coverage: every existing OPDS test (`tests/test_opds_*`,
    `tests/test_contract_compatibility_matrix.py`,
    `tests/forge/test_artifact_fanout.py`,
    `tests/test_pipeline_templates_branches.py`,
    `tests/test_forge_seed_import_matrix.py`,
    `tests/test_forge_copilot_seed.py`,
    `tests/cli/test_subcommand_dispatch_smoke.py`) re-runs green after the
    rename + back-compat aliases.

### Added
- **Bitol ODPS v1.0.0 — bidirectional provider** (`BitolOdpsProvider`,
  registered as `odps_bitol`; back-compat alias `odps-standard`).
  Export emits the canonical fragments layout: 1 ODPS doc + N sibling
  `<contractId>.odcs.yaml` files; the linking invariant
  `port.contractId == odcs.id` is enforced and asserted in tests.
- **`ContractResolver`** — resolves port `contractId` references through
  local probes + http(s) fetch + cache; refuses HTML-with-200 and
  non-http(s) schemes. Remote fetch is **OFF by default** (SSRF defence);
  opt in with `--allow-remote`.
- **Three import entry points** for `fluid opds import`: single ODPS
  file, directory bundle (ODPS + sibling ODCS, or ODCS-only), or a lone
  ODCS file. All converge on one validated FLUID.
- **`fluid opds export --spec bitol-1.0.0|odpi-4.1`** — `--spec`
  dispatcher with Bitol ODPS v1.0.0 default; ODPI v4.1 opt-in for
  back-compat. `--out-dir`, `--validate-strict`, `--format` flags.
  Legacy `--version 4.1` survives as a hidden deprecated alias.
- **`fluid forge --seed-from <path>`** — copilot accepts an ODCS
  contract, a Bitol ODPS product file, or a directory bundle as a
  structural seed. The seed's schema/quality/qos are ground truth.
  Pre-processor at `fluid_build.cli.forge_copilot_seed.load_seed`.
  Remote fetch of `contractId` references is **OFF by default**
  (SSRF defence); opt in with `--seed-allow-remote`.
- **ODCS bidirectional provider — modular architecture.** `providers/odcs/`
  split into `provider.py` + `mappers/{metadata,team,schema,servers,
  sla,quality,types}.py` + `validation.py` + `io.py`. Pure-function
  mappers with paired `to_fluid()` / `to_odcs()`; per-level
  `odcs_passthrough` buckets preserve unmodeled ODCS fields for
  lossless round-trip. `OdcsProvider.roundtrip_check()` returns a
  structured diff used by tests and the forge ground-truth guard.

### Security
- **`fluid_build.util.safe_http` — shared SSRF guard for every HTTP fetch.**
  One factory (`safe_httpx_client`) that all outbound http(s) calls now
  route through: scheme allowlist + private/loopback/link-local/CGNAT/6to4
  /NAT64/ORCHIDv2/IPv6-SR/RFC-TEST-NET filter + IPv4-mapped IPv6 unwrap
  (closes a Python 3.10/3.11 bypass) + reject-all on mixed-public+private
  DNS + connection-layer DNS pin (via httpx's first-class `sni_hostname`
  extension) + `follow_redirects=False` default + streaming body cap.
  Migrated 7 fetch surfaces: `ContractResolver` (the original site),
  `KafkaConnectRestClient`, Kafka Connect schema-registry client, the
  Airbyte REST client, all five catalog registrars (Glue / DataHub /
  OpenMetadata / Unity / Snowflake Horizon / DataMesh Manager), the
  Databricks auth-provider's API check, and the schema-manager remote
  fetcher. Borrow-before-build receipts in `fluid_build/util/safe_http.py`
  header — CIDR list + IPv4-mapped unwrap from `requests-hardened`
  (Saleor, BSD-3); httpx DNS-pin recipe from the maintainer-blessed
  `sni_hostname` extension docs.
- **SSRF hardening on `ContractResolver` http(s) fetch path.**
  - Rejects non-http(s) schemes and any private / loopback / link-local
    / multicast / reserved / unspecified IP address.
  - Reject-all on mixed-public+private DNS answers (an attacker cannot
    mix a public A with a private AAAA).
  - Extended CIDR deny-list (borrowed from `requests-hardened` —
    Saleor, BSD-3): carrier-grade NAT `100.64.0.0/10`, 6to4
    `192.88.99.0/24` + `2002::/16`, NAT64 `64:ff9b::/96` +
    `64:ff9b:1::/48`, ORCHIDv2 `2001:20::/28`, IPv6 SR `5f00::/16`,
    RFC TEST-NETs, class-E reserved.
  - **IPv4-mapped IPv6 unwrap** (`::ffff:169.254.169.254`) — closes a
    real SSRF bypass on Python 3.10/3.11 (stdlib `is_private` recursion
    into IPv4-mapped only landed in 3.12).
  - **DNS-rebind defence** — pins the validated IP at the TCP-connect
    layer via custom `HTTPConnection` / `HTTPSConnection` subclasses;
    HTTPS preserves the original hostname for SNI + cert validation.
    Preserves stdlib's `sys.audit("http.client.connect")` hook.
  - **Redirect re-validation** — `_SafeRedirectHandler` re-runs the
    full guard on every `Location:` target and re-pins the new IP.
    Cap at 3 redirects (down from urllib's default 10).
  - **Body size cap** — 10 MiB; oversized responses are dropped before
    parsing.
  - **Error-message scrubbing** — `ContractValidationError` for remote
    fetches no longer carries the jsonschema body fragments; `__cause__`
    is cleared. `OdcsProvider.import_contract` warning likewise omits
    the exception text.
  - **No-op handlers for ftp/file/data schemes** so any future direct
    use of `_SAFE_OPENER` cannot inherit `urllib`'s default `FTPHandler`
    / `FileHandler` / `DataHandler`.
  - **Path-traversal guard** — refuses `contract_id` values that
    escape `base_path` (e.g. `/etc/hostname` via `Path / "/abs"`).
  - **Cache-poison defence** — local files whose `id` is URL-shaped
    are not indexed (otherwise they could pre-empt a later remote
    fetch when `allow_remote` is flipped on).
  - **Audit log** emitted on every block (`ssrf_guard_blocked`,
    `skip_url_shaped_local_id`).
- **BREAKING — `allow_remote` defaults to `False` across CLI + library.**
  `fluid opds import` and `fluid forge --seed-from` no longer fetch
  http(s) `contractId` references unless `--allow-remote` /
  `--seed-allow-remote` is passed explicitly. Python callers of
  `BitolOdpsProvider().import_contract(...)`,
  `BitolOdpsProvider().import_directory(...)`,
  `ContractResolver(...)`, and `forge_copilot_seed.load_seed(...)`
  must now pass `allow_remote=True` for the previous behaviour.
  `--no-remote` and `--seed-no-remote` are retained as hidden no-op
  aliases.

### Changed
- **ODCS schema validation default-on with warn-on-fail.** Vendored
  ODCS v3.1.0 JSON Schema now runs on every export by default
  (`ODCS_VALIDATE=true`); failures warn rather than raise. Hard fail
  via `ODCS_VALIDATE_STRICT=true` or by calling `validate_contract()`.
- **ODCS type table now exhaustive against the FLUID 0.7.3 column-type
  enum** (79 types). Drift guard in
  `tests/providers/test_odcs_type_mapping.py` fails CI if a new FLUID
  schema type is missing from `_FLUID_TYPE_TO_ODCS_LOGICAL`.
- **Bitol ODPS input ports — three-source merge.** `ports.py::to_odps()`
  walks `consumes[]` (FLUID 0.7.2 canonical), `builds[]` (SDP source
  streams), and `expects[]` (FLUID 0.7.1 legacy) — de-duped by name.
  Uses upstream's `util.contract.consumes_to_canonical_ports` +
  `builds_to_canonical_input_ports` for the normalization.

## [0.8.3rc1] — 2026-05-12

Pre-release candidate of the first stable line after `v0.8.0`. Stacks the
v0.7.3 acquisition-pattern engine work (graduated from the `v0.8.2b1`
TestPyPI beta) on top of the new plugin extension points and their
trust-model hardening. **Published to PyPI as a pre-release** (`pip
install --pre data-product-forge` or `pip install
data-product-forge==0.8.3rc1`). The follow-on `v0.8.3` stable tag will
be cut after a downstream validation window; no functional changes are
expected between rc1 and stable.

### Added

- **Three plugin extension points** discovered via Python entry-points
  (`importlib.metadata.entry_points()`):
  - `fluid_build.commands` — register additional `fluid <name>`
    subcommands at CLI bootstrap (`cli/bootstrap.py`).
  - `fluid_build.extension_validators` — validate sub-keys of
    `contract.extensions` during `fluid validate` (`cli/validate.py`).
    Errors fold into the `ValidationResult` namespaced under
    `extensions.<ep-name>`.
  - `fluid_build.apply_hooks` — apply-time invariant checks (e.g.
    scaffold bundle digest drift) during `fluid apply` (`cli/apply.py`).
  All three follow a uniform contract: plugin exceptions are trapped,
  pre-redacted, and reported as errors — they cannot crash the CLI.
  Companion packages `data-product-forge-sdk==0.9.0` (Beta) and
  `data-product-forge-custom-scaffold==0.1.0` (Beta) ship reference
  ABCs, conformance harnesses, and a custom-scaffold engine that plug
  in via these hooks.

- **`--force-pattern-drift` flag on `fluid apply`** — override
  apply-hook drift errors (e.g. when a scaffold bundle digest moved
  underneath a development scenario). Errors downgrade to WARNINGs;
  apply proceeds.

- **`contract.extensions` schema field** in `fluid-schema-0.7.3.json`
  — optional top-level object with `additionalProperties: true`,
  validated per-sub-key by extension-validator plugins.

### Changed (security)

- **Apply hooks receive `copy.deepcopy(contract)`** rather than the live
  reference. A buggy or malicious hook cannot mutate the contract the
  rest of apply or other hooks consume. Pinned by
  `tests/test_cli_plugin_hooks.py::test_apply_hook_receives_deep_copy_of_contract`.

- **Plugin exception text and plugin-supplied error strings are
  pre-scrubbed with `redact_secret_text`** before reaching logs or the
  errors list. The `SecretRedactingFilter` only scrubs args bound to
  `password=%s`-style template tokens; plugin exceptions are free-form
  text that can carry credential-shaped substrings anywhere. Applied
  uniformly across `cli/apply.py`, `cli/validate.py`,
  `cli/bootstrap.py`. Pinned by `TestPluginErrorRedaction` (5 tests).

- **`bootstrap.py` imports `redact_secret_text` at module top**
  (was: nested try/except inside the except branch, which could have
  fallen back to unredacted logging if the import statement failed).
  Closes a defense-in-depth gap surfaced by the security review.

### Added (docs)

- **SECURITY.md** gains a "Plugin Trust Model" section: tables the
  three entry-point groups, names the defenses (crash containment,
  contract deep-copy, pre-redaction, override gate), names the
  deliberate non-defenses (no sandboxing, no timeout, no resource
  limits), routes plugin-side vs CLI-side vulnerability reports.

- **AGENTS.md** gains a "Plugin extension points" section documenting
  the three entry-point groups with hook signatures, failure model,
  and trust statement — for the AI-coding-agent audience.

### Test coverage

- **`tests/test_cli_plugin_hooks.py`** — 19 new tests across four
  classes that pin every behavior on the plugin trust surface:
  extension validators (4), apply hooks (6), bootstrap commands (4),
  plugin error redaction (5). Uses a `FakeEntryPoint` helper so tests
  don't need real installed packages.

### Notes

- This release stacks on top of the v0.7.3 acquisition-pattern engine
  work documented in [0.8.2] below — all six ingestion engines remain
  GA (`duckdb`, `airbyte`, `meltano`, `dlt`, `kafka-connect`,
  `debezium`).
- Companion plugin ecosystem packages on PyPI:
  - `data-product-forge-sdk` (0.9.0, Beta) — import path `fluid_sdk`,
    zero runtime dependencies.
  - `data-product-forge-custom-scaffold` (0.1.0, Beta) — reference
    `CustomScaffold` engine; Jinja+YAML or Python-plugin bundles.

## [0.8.2] — 2026-05-10

Beta release of the v0.7.3 acquisition-pattern engine runners and the
EngineRuntime registry across all CI emitters. Schema is GA; the
package is staged through a beta tag (`v0.8.2b1` → TestPyPI) so
downstream pipelines can validate the EngineRuntime wiring before
graduating to a stable `v0.8.2` on PyPI.

### Added

- **Data-product type vocabulary** (`metadata.productType`). v0.7.3
  introduces a Data Mesh-aligned classification that runs alongside the
  existing medallion `metadata.layer` field. Both vocabularies are
  first-class and accepted side-by-side; existing contracts using only
  `Bronze`/`Silver`/`Gold` validate unchanged. Canonical mapping:

  | medallion (`metadata.layer`) | Data Mesh (`metadata.productType`) | expansion                          |
  |------------------------------|------------------------------------|------------------------------------|
  | `Bronze`                     | `SDP`                              | Source-Aligned Data Product        |
  | `Silver`                     | `ADP`                              | Aggregated Data Product            |
  | `Gold`                       | `CDP`                              | Consumption-Aligned Data Product   |
  | `Platinum`                   | (no analogue)                      | —                                  |

  When a contract sets both fields they MUST agree; the validator emits a
  clear error otherwise. The discover emitter (`fluid init --discover`)
  populates both fields automatically. Helpers in
  `fluid_build.forge_datamodel.product_type` (`infer_product_type`,
  `infer_layer`, `validate_layer_product_type_consistency`) let
  downstream tooling normalize between the two vocabularies.

- **FLUID schema v0.7.3 — Source-Aligned Data Products.** First-class
  support for source-aligned (Bronze / SDP) data products that ingest
  external systems into the mesh.
  - New `acquisition` build pattern with full `$defs/acquisitionPattern`
    sub-schema (`source`, `sink`, `delivery`, `schemaEvolution`, `quality`,
    `cost`, `catalog`, `concurrency`, `imageSignature`, `deployment`).
  - **All six ingestion engines GA**: `duckdb`, `airbyte`, `meltano`, `dlt`,
    `kafka-connect`, `debezium` — every engine has a working runner, full
    test matrix (sources × sinks × modes × failure modes × capabilities ×
    deployment modes), and conforms to the public Runner Protocol via the
    conformance suite.
  - Three deployment modes (`embedded`, `bring-your-own`, `managed`) with
    three managed back-ends (`docker`, `kubernetes`, `terraform`).
  - Capability-based negotiation between contract and runner
    (`capabilities` array on builds; `RunnerCapability` enum on the public
    API).
  - Schema evolution semantics (`schemaPolicy`) with concrete per-policy +
    per-change decision matrix.
  - Delivery guarantees (`at_most_once | at_least_once | exactly_once`) +
    DLQ semantics (`dlq.maxRecordsBeforeAbort`, `dlq.alertOn`).
  - Cosign image-signature verification + SLSA provenance fields.
  - Top-level `retention` block (`runState`, `runLogs`, `lineage`, `dlq`).
  - Sovereignty extension: `dataResidency.region`,
    `dataResidency.prohibitTransferTo`.
  - `metadata.classification` enum + `metadata.experimental` array
    (feature gate).
  - All schema additions are additive and backward-compatible with v0.7.2;
    existing 0.7.x contracts validate unchanged.

- **`fluid_build.api` v1.0 — public extension contract.** Stable surface
  third-party runners and providers target. SemVer-governed via
  `__api_version__ = "1.0"` with a 2-minor-version deprecation window.
  Conformance suite available at
  `fluid_build.api.conformance.RunnerConformance`.

- **Common acquisition runtime** under `fluid_build/build_runners/` —
  `_state` (FileStateStore + single-flight lock), `_dlq`, `_retry`,
  `_idempotency`, `_schema_evolution`, `_fingerprint`, `_anomaly`
  (EWMA/IQR/exact), `_signature` (Cosign), `_cost` (budget gate),
  `_catalog` (registrar dispatch), `_lineage` (Null/Buffered/HTTP OL
  emitters), `_retention` (sweeper), and four pre-land hooks
  (`dlp_scan`, `tokenize_pii`, `quality_gate`, `emit_lineage_input`).

- **Six ingestion runners** under `fluid_build/build_runners/<engine>/`:
  - `duckdb` — zero-infra file/JDBC ingestion (CSV/Parquet/JSON/Postgres/MySQL/SQLite/HTTP).
  - `dlt` — Python-native sources, including custom `@dlt.source` modules + verified sources (filesystem, sql_database).
  - `meltano` — Singer protocol over subprocess; one runner unlocks 600+ Singer taps.
  - `airbyte` — REST mode against Airbyte OSS / Cloud + Cosign image signature verification (5-path verification matrix: signed/unsigned/wrong-key/SLSA-required-missing/SLSA-required-present).
  - `kafka-connect` — full connector lifecycle (create/get/update/delete/status) against a Kafka Connect cluster; JDBC + S3 + Salesforce + MongoDB sources; JDBC + S3 + Snowflake + Iceberg + BigQuery sinks.
  - `debezium` — CDC for Postgres / MySQL / MongoDB / SQL Server / Oracle; both Kafka Connect mode (preferred) and Debezium Server (embedded) mode; all 5 snapshot modes (`initial`, `schema_only`, `never`, `when_needed`, `always`).

- **Infrastructure layer** (`fluid_build/infra/`): three managed-mode artifact
  generators (Docker Compose, Helm with Flux-style HelmRelease CRs, OpenTofu
  modules). Pinned upstream Helm chart references; ExternalSecret + NetworkPolicy
  emission; sovereignty propagation into values overlays; deterministic bundle
  digests. **Hyperscaler-agnostic by construction** — no `boto3`, `google.cloud`,
  or `azure` imports anywhere in the layer (test-asserted).

- **Five catalog registrars** (`fluid_build/build_runners/catalog_registrars/`):
  DataHub (GMS REST), OpenMetadata, Databricks Unity Catalog, AWS Glue Catalog
  (HTTP — no boto3 dependency), Snowflake Horizon. Classifications propagate as
  glossary terms / PII tags / table parameters / column tags depending on target.

- **Authoring workflows**:
  - `fluid init --discover <uri>` — introspect Postgres / MySQL / filesystem
    sources and emit deterministic Bronze contracts.
  - `fluid import meltano <project>` / `fluid import airbyte <workspace>` /
    `fluid import dlt <pipeline>` / `fluid import singer <tap-cfg>` — convert
    foreign configs to FLUID contracts. Secrets are auto-redacted to `${ENV_VAR}`
    placeholders.

- **Day-2 operations** (`fluid_build/cli/ops/`). Run-record introspection
  is grouped under a `fluid runs` umbrella so the new commands don't
  collide with the existing top-level `fluid status / doctor / auth`
  which serve different (workspace-overview / system-diagnostic / cloud-
  auth) purposes.
  - `fluid runs status <product-id>` — last-N runs, freshness,
    error-rate-24h, last state, per-stream record counts.
  - `fluid runs logs <product-id> --component build|infra|server|worker|dlq`
    — component-scoped log fetch with `--grep` and JSON parsing.
  - `fluid runs diff <product-id> --build <id> --run-a <a> --run-b <b>`
    — schema and row-count delta between two runs.
  - `fluid retention sweep` — periodic cleanup with structured summary.
  - `fluid doctor --scope authoring|pipeline|ingestion|infra|catalog|all`
    — extends the existing `fluid doctor` with the acquisition-stack
    health report covering schema version, dispatcher integrity, runner
    module imports, optional extras, infra binaries, registrar imports.
  - `fluid secrets login|verify|rotate <secretRef>` — pipeline credential
    ops with keychain backend and probe-before-rotate semantics. Lives
    under its own `secrets` umbrella so it doesn't collide with the
    legacy cloud-provider `fluid auth`.

- **Typed CLI error catalog** (`fluid_build/cli/_errors.py`) — every
  user-facing error renders the five-field shape (`what / where / why /
  fix / doc`). 14 typed classes covering schema validation, capability
  mismatch, secret resolution, sovereignty, connectivity, partial failure,
  DLQ overflow, schema drift, budget, lock contention, replay staleness,
  missing extras, infra drift, residency, supply chain.

- **`fluid validate --probe`** flag for live external probes (off by
  default; pure schema validation otherwise).

- **End-to-end example** `examples/source-aligned-postgres-duckdb/` with
  `docker-compose.yml`, `seed.sql`, `Makefile`, `verify.py`. `make all`
  brings up Postgres, runs `fluid validate → apply`, and asserts row count
  + schema in the output Parquet. Verified end-to-end on Postgres 16.

- **Test infrastructure**:
  - Testcontainers fixtures for Postgres / MySQL / MongoDB.
  - respx mock servers for Airbyte / Kafka Connect / DataHub / OpenMetadata /
    Unity / Glue / Snowflake Horizon / Marquez.
  - Cosign mock with 5 signature scenarios (signed/unsigned/wrong-key/SLSA missing/SLSA present).
  - Synthetic Singer tap (`tap-fluid-fake`) for protocol-level tests.
  - Hypothesis property tests for state machine, locks, and schema evolution.

- **UX acceptance bar**:
  - Error-catalog completeness (15 typed error classes, all with `for_*`
    factories and five-field shape).
  - Engine uniformity: every runner declares the same Protocol surface,
    returns the same exit-code shape, and emits the same run-record JSON.
  - Performance budgets: validate < 3s, fingerprint < 500ms for 100 calls,
    schema evolution < 500ms for 100 resolves, doctor (all scopes) < 3s.
  - JSON output stability: error catalog snapshot, contract emitter snapshot.
  - Exit-code contract: 0 success, 1 user error, 2 partial, 3 transient, 4 internal.

### Changed

- `FluidContractValidator.__init__` and `ConformanceAgent.__init__` now
  default `fluid_version` to `FluidSchemaManager.latest_bundled_version()`
  (was hardcoded `"0.7.2"`). `FluidContractValidator.validate` honors the
  contract's own `fluidVersion` when present, so contracts emitted at one
  version validate against that version regardless of the validator's
  default.
- Dropped Python 3.9 support, raised the package baseline to Python 3.10,
  and expanded CI/package classifiers through Python 3.14.

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
