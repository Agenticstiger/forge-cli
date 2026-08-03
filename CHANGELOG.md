# Changelog

All notable changes to FLUID Forge CLI will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Release image: two CPython `tarfile` CVEs suppressed with justification.**
  Both are fixed only in CPython pre-releases, so no stable base image clears
  them. A curated `.grype.yaml` records each with its reachability argument —
  the single `extractall` call is guarded by `_safe_tar_members`, which raises
  on symlink and hardlink members, and no streaming (`"r|"`) mode is used
  anywhere — so the release gate no longer fails on an unfixable finding.

## [0.14.1] — 2026-08-03

A correctness and security patch: the MCP output port works against both
generations of the MCP SDK, two authorisation bypasses in that port are closed,
and the project's legal entity is named correctly across the distributed files.

### Security

- **Two HIGH authorisation bypasses closed in the MCP output port.** Verified
  caller attributes (JWT/mTLS) and self-attested ones were flattened into a
  single dictionary, so the "cryptographic identity wins" rule held only for
  the keys a claim mapping happened to emit. A caller could therefore (a)
  self-attest any `rowFilter` the mapping did not cover — including another
  tenant's value, turning a fail-closed denial into an attacker-chosen
  row-level-security predicate — and (b) self-attest `model` and `useCase` past
  the `agentPolicy` gate whenever the mapping omitted them, which any custom
  `FLUID_MCP_JWT_CLAIM_MAPPING` does, since it *replaces* the defaults rather
  than extending them. The flattening predated the SDK port and was reachable
  on SDK 1.x through `clientInfo` extras. Fixed by reading the `fluid_auth_kind`
  stamped by the transport middleware but never consulted: when authentication
  is enforced, only verified claims bind, and dropped self-attestations are
  logged at WARNING naming each one. The no-authentication path is unchanged.
  Five regression tests pin all four exploit shapes plus a no-auth control.
  (#492)

### Changed

- **The MCP output port supports SDK 1.x and 2.x.** `mcp` 2.0.0 renamed
  `FastMCP` to `MCPServer`, inverted the lowlevel `Server` registration API from
  decorators to `on_*` constructor handlers, removed
  `create_connected_server_and_client_session`, and renamed model fields from
  camelCase to snake_case at the attribute level. All generation branching is
  confined to one new tier-0 leaf, `fluid_build/_mcp_compat.py`, which probes
  with `find_spec` rather than importing `mcp` at module scope so the
  `fluid --help` startup budget is preserved. Twelve `getattr`-with-default
  camelCase reads were failing **silently** under 2.x — errors reading as
  success, tool schemas as empty — and now route through a dual-name accessor.
  Verified green against both `mcp` 1.29.0 and 2.0.0, including a live
  end-to-end run of `fluid mcp output-port serve` over HTTP/SSE under 2.0.0.
  The dependency pin stays `mcp>=1.20,<2.0` and the drift canary stays
  warn-only until the 2.x leg has a track record. (#492)

### Fixed

- Corrected the legal entity name across `NOTICE`, `LICENSE`, `pyproject.toml`
  and repository docs: the project is maintained by **Agentics Transformation
  Limited** (Ireland); earlier files misnamed the entity as "Pty Ltd". The
  trademark notice now correctly asserts an unregistered mark ("FLUID Forge™ is
  a trademark of Agentics Transformation Limited") instead of claiming a
  registration.

## [0.14.0] — 2026-07-28

A live-verification hardening release: the dbt Iceberg loop reaches BigQuery
behind a new validate-time no-op gate, 104 defects fixed and proved against a
real Snowflake account, and DOT-injection, secret-redaction and
leak-prevention security work.

### Security

- **`fluid viz-graph` escapes contract-authored text before it reaches
  generated DOT source.** Mesh node/edge labels were interpolated verbatim, so
  a product label like `X" label="SPOOFED` injected a second `label` attribute
  and let a contract display a name of its choosing — including another
  product's — in the rendered mesh graph; node IDs were interpolated unquoted
  through a denylist that missed `"` and `\`, letting a `consumes[].ref`
  inject phantom nodes into the lineage graph; and an unescaped trailing
  backslash failed the entire render. Both shared helpers are fixed (allowlist
  IDs, backslash-then-quote label escaping), covering the legacy inline
  emitter and `viz_renderers/dot.py`. (#481)
- **Secret redaction rewritten to redact by exact known value.** The previous
  regex-based approach guessed a secret's extent from its shape and leaked 17
  known inputs; redaction is now delimiter-agnostic by construction, masking
  the literal values it knows. (#478)
- **Commits carrying values from the committer's own environment are
  refused.** A new pre-commit/CI hook flags any diff line byte-identical to a
  non-trivial value in the committing machine's environment (`SNOWFLAKE_*`,
  `AWS_*`, `*_TOKEN`, `*_PASSWORD`, …) or a supplied `--env-file` — the
  low-entropy identifiers (account locators, usernames) that secret-shape
  scanners cannot see — and never prints the matched value. `detect-secrets`
  now also actually runs in CI, on changed files. (#479)
- **Vulnerability reports route through GitHub Private Vulnerability
  Reporting.** `SECURITY.md` now points at PVR (email stays as the fallback)
  and the supported-versions table reflects the released 0.13.x line. (#473)

### Added

- **The dbt Iceberg loop extends to BigQuery.** An `iceberg` expose on a GCP
  binding — which previously fell through the emit dispatch and produced
  nothing, silently — now emits `catalogs.yml` with
  `catalog_type: biglake_metastore` and provisions the GCS bucket dbt refuses
  to create. The IaC bucket name is derived from the same warehouse URI dbt
  writes into `external_volume`, so the bucket dbt loads into is always the
  bucket the IaC creates and governs, and whole-bucket `force_destroy` is
  dropped when the product owns only a prefix of a shared warehouse root.
  (#474)
- **Validate-time anti-no-op gate for Iceberg prerequisite bindings.** The
  Snowflake and GCP IaC emitters skip an Iceberg expose missing a required
  input rather than emitting a broken resource — previously the user found out
  at `dbt run`. `fluid validate` now errors on exactly those cases, naming the
  missing field; each check mirrors one emitter skip-branch and the pairing is
  test-pinned in both directions. Note for CI: under `--strict`, Snowflake
  catalogs that authenticate with secrets (polaris / unity / rest / nessie)
  now fail, since the emitted module is credential-free and strict promotes
  that existing warning. (#475)

### Fixed

- **Snowflake builds actually run on Snowflake.** Embedded-SQL builds on a
  `platform: snowflake` contract executed against local DuckDB (exit 0 either
  way); `--model-contracts` flattened every parameterized/alias type to
  `VARCHAR(16777216)`, so contract enforcement passed vacuously; generated
  `profiles.yml` ignored the contract's schema and silently targeted `PUBLIC`;
  a freshly generated project failed its own contract on first `dbt run`; and
  a `freshness` dq rule made the generated project unparseable. Part of a
  104-defect wave found by exercising a month of shipped features against a
  real Snowflake account, every fix re-verified by a second agent re-running
  the original failing scenario. (#476, #478)
- **`fluid import dbt` → `apply` → `verify` round-trips cleanly.** Importing
  then applying no longer creates a duplicate lowercase shadow namespace
  (`+2 ~2 -0` → `+0 ~2 -0`), `verify` can read the real objects through
  `snowflake_view`, and a p90 metric no longer silently round-trips into a
  median. (#476)
- **`fluid publish` no longer silently skips contract-declared catalog
  targets** (an `ImportError` swallowed by a bare `except`), and apply hooks
  now run on the OpenTofu path, making `--env` plumbing reachable for
  Snowflake / AWS / GCP applies. (#476)
- **Governed access enforcement holds under aliasing.** The governed `query`
  path now enforces `policy.authz.columnRestrictions` (a denied column was
  readable as a measure), and PII redaction can no longer be bypassed by
  aliasing a restricted column — policy was previously checked on the output
  column name, which the semantic layer aliases away. (#478)
- **Data-quality test reporting is truthful.** A failing `severity: critical`
  rule no longer reports PASS with exit 0; `accuracy` rules evaluate upper
  bounds instead of only `MIN()`; `summary.checks_passed` no longer counts
  warnings and failed criticals as passed; `--no-data` no longer asserts
  checks it never performed; and `--engine soda` can now actually produce
  checks (the schemas never defined the `exposes[].quality.tests[]` key it
  reads). (#478)
- **ODCS export/import fidelity.** Export no longer flattens every
  parameterized type to `logicalType: string`, round-trips no longer lose 88
  of 114 leaf fields, and `odcs import` no longer rewrites a Snowflake binding
  to `bigquery` while reporting success — now validated against the official
  ODCS schema. (#478)
- **`--adopt-shared-container` respects per-exposure `binding.packaging`
  overrides.** Flipping one binding no longer absorbs a shared platform pool
  into the tenant's state (which erased the pool's COMMENTs); transition
  scoping now keys on which container is nested where, not on resource type.
  (#478)

## [0.13.1] — 2026-07-26

A security + interoperability release on top of 0.13.0: two path-confinement
security fixes, the dbt importer's product-boundary split, the Snowflake
Iceberg dbt loop, Bitol ODPS v1.1.0 support, and OpenLineage emission that
real consumers can actually ingest.

### Security

- **`binding.location.dbFile` is now confined to `--readable-paths`.** The
  DuckDB output-port driver gated `binding.location.path` and `.attach`
  against the operator's allowlist but passed `dbFile` raw to
  `duckdb.connect`, so a served contract could open and read any host
  database — a read-side sandbox escape. `dbFile` now flows through the same
  resolve-and-contain gate as its siblings (`:memory:` passes through;
  no-allowlist behaviour is unchanged). (#463)
- **Document-controlled ids are contained in exporter filenames.** A contract
  with a traversal-shaped `exposeId` run through
  `fluid generate artifacts --out dist` could write an attacker-influenced
  file outside `--out` — and `MANIFEST.json` would bless the escaped path.
  New `providers/_path_safety.py` (the filename sibling of `_sql_safety.py`)
  passes schema-valid FLUID ids through verbatim, cleans-plus-digests
  anything else, and re-checks the resolved path against the output root at
  all three write sites; manifest emitters derive names through the same
  helper. Verified against 204k+ separator/control-char cases and a full
  Unicode sweep with zero escapes and zero canonical-layout changes. (#472)

### Added

- **`fluid import dbt --split-by {project|folder|group}`** splits a dbt
  manifest into multiple data products along folder or dbt-group boundaries
  (`project` remains the byte-stable single-contract default). Cross-split
  `ref()`s become cross-product `consumes[]`, `--out` becomes the output
  directory for multi-contract imports, and dbt ≥1.10 manifests with
  `arguments:`-nested test params now import correctly alongside legacy flat
  kwargs. In passing, model-level expose descriptions are now scrubbed like
  their column/semantic siblings (closing a hostile-Jinja smuggling path into
  generated `schema.yml`), and no-columns exposes emit a schema-valid
  contract. (#465)
- **Snowflake Iceberg loop for dbt, both halves.** `fluid generate` emits
  dbt's `catalogs.yml` (v1 schema, Snowflake adapter) for Iceberg exposes,
  and `fluid apply` provisions the prerequisites dbt refuses to create — the
  EXTERNAL VOLUME for Snowflake-managed catalogs and the AWS Glue CATALOG
  INTEGRATION. A single deterministic naming helper is shared by both
  emitters, so the volume `fluid apply` creates carries exactly the name
  `catalogs.yml` references (explicit override honoured via
  `binding.icebergConfig.properties`). Live-verified with a real
  `tofu apply`. (#469, #470)
- **Bitol ODPS v1.1.0 top-level `type`** (approved RFC 0029):
  `sourceAligned` / `aggregate` / `consumerAligned` map 1:1 and
  bidirectionally to FLUID's SDP / ADP / CDP classification. The default emit
  target stays v1.0.0 until Bitol cuts the release; opt in via
  `--api-version` / `ODPS_API_VERSION`. Validation keys on the document's own
  `apiVersion`, and custom org types round-trip verbatim. (#471)
- **Declared `consumers:` block in the 0.7.6 preview schema.** Contracts can
  now declare their downstream consumers — dashboards, notebooks, ML systems,
  applications — with a shape borrowed from dbt exposures (`name` / `label` /
  `type` / `owner` / `url` / `maturity`) plus FLUID-native `exposeIds` tying
  a consumer to specific output ports. Additive and optional; preview schema
  only, no GA change. (#466)

### Changed

- **CI:** routine pinned-action bumps. (#454, #455, #456, #457, #458)

### Fixed

- **OpenLineage events are now spec-conformant — and actually emitted.** The
  emitter historically produced payloads no OpenLineage consumer would accept
  (missing required `producer`/`schemaURL`, flattened `run`/`job` structure,
  non-UUID `runId`) and was never wired up, so zero events were ever sent.
  Emission now routes through `openlineage-python` at the acquisition-runner
  chokepoints (all six engines, no per-runner wiring), honours the standard
  `OPENLINEAGE_URL` so an existing Marquez/DataHub deployment just works, and
  redacts run facets and stream names before anything leaves the machine.
  ODCS contract publishing also moves onto OpenMetadata's first-class Data
  Contracts entity. (#467)
- **`fluid-schema-0.7.5.json` no longer breaks YAML-based consumers.** Six
  emoji in the GA schema's description were encoded as UTF-16 surrogate-pair
  escapes — valid JSON, but rejected by libyaml, so tooling that reads JSON
  Schema through a YAML parser (e.g. `datamodel-code-generator`) could not
  process the schema at all. The escapes are now literal characters (a
  lossless, one-line change), and a new guard test asserts every bundled
  schema stays surrogate-free and YAML-round-trippable. (#451)
- **Safety-gate override audit events now log at WARNING, as documented.**
  `opentofu_destructive_gate_override` (`--allow-data-loss`) and
  `packaging_adoption_override` (`--adopt-shared-container`) were emitted at
  INFO — invisible to audit pipelines filtering at WARNING and above. Event
  names and payloads are unchanged. (#450)

## [0.13.0] — 2026-07-18

A packaging + autonomy + semantics-correctness release: declarative
`isolated`/`shared` infrastructure packaging, the new autonomous `fluid
mission` surface, the Apache Ossie resync, and a wave of fixes that make the
`semantics` block return right numbers through every consumer.

### Security

- **dbt manifest import hardened against Jinja exfiltration in recovered
  free-text fields.** A hostile `manifest.json` carrying e.g.
  `{{ env_var('AWS_SECRET_ACCESS_KEY') }}` in a tag or description could render
  the secret into the operator's own artifact after import → generate →
  `dbt parse`. Display/governance text is now stripped of Jinja spans (fixpoint
  loop, so no delimiter survives by reforming at a gap junction), with each
  redaction reported; SQL-bearing fields (`expr`, metric `filter`) are preserved
  verbatim, with any non-MetricFlow templating surfaced as a
  review-before-generate risk. (#449)

### Added

- **Declarative packaging modes: contracts can declare infrastructure containers
  `isolated` (product-owned, today's behavior) or `shared` (referenced from a
  platform pool).** The 0.7.6-preview schema gains a `packaging` block
  (contract-wide default, per-binding override, per-kind `containers` map for
  hybrid tiers); the AWS/GCP/Snowflake IaC emitters emit a data source instead
  of a managed resource for referenced pools, so a product can never own or
  destroy shared platform infrastructure; shared-bucket grants are always
  prefix-scoped and fail closed when `location.path` is missing; a pre-plan
  ownership-transition guard blocks `isolated`→`shared` flips that would plan a
  pool DESTROY and gates `shared`→`isolated` adoption behind the new
  `--adopt-shared-container` flag; and `plan.json` gains a digest-covered
  `packaging` summary that itemises dropped container-creation actions.
  Contracts without the block emit byte-identical output, enforced by a golden
  pin over every example contract. Two IAM-widening bugs and a CEL-condition
  injection introduced during implementation were caught and fixed by the
  series' own security pass before merge. (#432, #433, #435, #441)
- **Mission-based deep agents: `fluid mission check | trust | list | run`.**
  Declarative YAML mission specs (goal, success criteria, budgets, gates, tool
  allowlist) with direnv-style content-hash trust pinning for workspace specs;
  `fluid mission check` is a zero-LLM scorecard usable as a standalone CI gate;
  `fluid mission run` drives a VERIFY-anchored loop in which only code-owned
  checks — never the LLM — can declare success, with hard USD / wall-clock /
  iteration budgets, a fail-closed destructive-diff gate, and
  resume-by-re-verification (`--resume`). Ships `gdpr-clean` and
  `quality-coverage` built-in missions. A HIGH path traversal in run-manifest
  handling was found and fixed by the series' own security pass before merge.
  (#432, #434, #436)
- **Apache Ossie resync + conformant interchange sidecar.** The OSI
  implementation resyncs to the current Apache Ossie core-spec (`BIGQUERY` +
  `MAQL` dialects, free-form vendors, plain-string `ai_context`), and the
  `.semantics.osi.*` sidecar is now a valid Ossie document (root wrapper,
  internal-only fields relocated into `custom_extensions`) validated against the
  vendored upstream JSON Schema. New `--osi-sidecar-format json` emits the shape
  dbt Core v1.12+ reads natively, so forge output becomes queryable through the
  dbt semantic layer — and consumable by the upstream Ossie converters — with
  zero conversion. (#438)
- **The dbt importer recovers the semantic layer.** `fluid import dbt` now maps
  manifest `semantic_models` and project-level metrics (simple / ratio /
  derived, including where-filters) onto the owning expose's `semantics` block —
  entities, dimensions with normalized time grains, measures, and
  `defaultAggTimeDimension` — with a degrade-loudly posture: every unmappable
  feature lands in `report.unsupported` instead of vanishing. (#442)
- **Every template product ships a queryable semantics block.** All five
  templates (analytics / etl_pipeline / ml_pipeline / starter / streaming)
  derive a conservative, deterministic semantics block from their columns, so
  template-mode products get the governed MCP `query` tool and MetricFlow export
  out of the box instead of after hand-authoring; templates can still override
  with an explicit spec. (#443)

### Changed

- **Cross-account / cross-project access is now live-proven at the emulator
  tier.** New gated tests drive bilateral cross-account Lake Formation +
  prefix-scoped S3 bucket policies through `tofu apply` against a two-account
  LocalStack Pro instance (the emitted bucket policy proven to be the deciding
  access control under `ENFORCE_IAM=1`), and cross-project BigQuery
  `dataset.access[]` entries through the BQ emulator; what the emulators cannot
  prove is recorded explicitly in `HONESTLY_TESTED.md`. (#446, #448)
- **Tier-1 semantic-layer RFC published.** Design RFC at repo root covering the
  five audit-ranked gaps — cumulative metrics, structured filters, time spine +
  null filling, relationships, and SCD validity params — all additive and
  0.7.6-preview-gated. (#445)

### Fixed

- **Metric filters are honored on the governed query path, and `percentile`
  gains real parameters.** A metric filter like `status = 'completed'` was
  silently dropped by the MCP query compiler — unfiltered numbers, no error —
  while the dbt export honored it; the filter is now allowlist-validated and
  ANDed into the WHERE alongside caller filters and policy rowFilters, with
  hostile or unbalanced-paren filters failing closed. `agg: percentile` gains
  the 0.7.6-preview `measures[].aggParams` (`percentile`,
  `useDiscretePercentile`), rendered as `PERCENTILE_CONT/DISC(p) WITHIN GROUP`
  and failing closed on engines without grouped ordered-set percentiles. (#439)
- **Forge-emitted measures no longer double-aggregate.** The emitter copied
  whole aggregate calls (`SUM(amount)`) into `measures[].expr` next to an
  inferred `agg`, so the query compiler rendered invalid `SUM(SUM(amount))` and
  the dbt bridge exported the same double wrap. A shared semantics builder now
  splits single-aggregate expressions into `agg` + inner expr (including
  `COUNT(DISTINCT …)` → `count_distinct`), the time-grain vocabulary is
  single-sourced with all aliases normalized (interview input included), and
  `defaultAggTimeDimension` is populated by both producers. (#440)
- **Metric owner + tags/labels round-trip through dbt.** `metrics[].owner` and
  semanticModel `tags`/`labels` were dead schema surface — no producer, no
  consumer. They now emit into MetricFlow `config.meta` (namespaced
  `fluid_tags` / `fluid_labels`) and the manifest importer recovers them, so the
  governance surface survives contract → dbt → contract. (#444)
- **IaC access grants read from the schema-valid `accessPolicy` surface.** The
  GCP emitter read `metadata.policies` — a shape every shipped schema rejects —
  so a cross-project-access contract could emit but never validate. Grants now
  come from `accessPolicy` (the deprecated `metadata.policies` surface is still
  appended, so a mid-migration contract drops nothing), and typed principals fix
  a latent bug where every `group:` address was emitted as `user_by_email`.
  (#447)

## [0.12.0] — 2026-07-18

The dbt-core integration release: generated dbt projects gain source freshness,
enforced model contracts, auto-pinned packages, MetricFlow semantic models, and
dbt Fusion (v2) compatibility; a faithful `manifest.json` importer brings
brownfield dbt projects into fluid contracts; contract schema 0.7.5 goes GA;
and `fluid verify` learns to reconcile declared vs observed lineage.

### Security

- **Docker image: fixable OS-package CVEs are now patched at build time.** The
  `Dockerfile` runs `apt-get upgrade` during the build so the published image
  ships without known-fixable OS CVEs, and the Grype ignore list was re-curated
  down from 4 suppressions to 1 (a documented, unreachable `html.parser` DoS
  with a removal trigger) — un-blocking the release pipeline's HIGH/CRITICAL
  CVE gate. (#412)

### Added

- **`fluid import dbt` — faithful brownfield dbt importer.** A new
  manifest-based importer (`fluid import dbt <project-dir | manifest.json>`,
  manifest schema v9+) replaces the old 5-model regex scanner: every enabled
  model/seed/snapshot becomes an expose with real column types (`catalog.json`
  overlay when present), `ref()` lineage becomes `consumes[]` + a per-step
  transformation DAG, generic dbt tests map back to `dq.rules[]` via the shared
  reverse mapping table, source freshness becomes `qosExpectations`, and
  everything skipped is accounted for in the import report. Pure stdlib —
  no dbt-core dependency. (#424)
- **Generated dbt projects emit `sources.yml` freshness from contract SLOs.**
  `exposes[].qos.freshnessSLO` and consumer `qosExpectations.freshnessMax`
  now become `warn_after` / `error_after` blocks (with `loaded_at_field`
  derived from the acquisition cursor where resolvable), so `dbt source
  freshness` operationalizes the contract's freshness promise instead of
  silently dropping it. (#419)
- **Opt-in dbt model contracts.** `fluid generate transformation
  --model-contracts` emits `contract: {enforced: true}` with adapter-correct
  per-column `data_type` (BigQuery / Snowflake / Redshift / DuckDB matrices)
  and `not_null` / `primary_key` constraints on every expose model, so
  `dbt build` fails in producer CI whenever the model's output drifts from
  `exposes[].contract.schema`. Without the flag the generated project is
  byte-identical to before. (#422)
- **`packages.yml` is now emitted alongside generated tests.** Projects whose
  tests reference `dbt_utils.*` / `dbt_expectations.*` get a managed
  `packages.yml` with only the needed range-pins (folded into
  `dependencies.yml` under `--mesh-hub`; user-managed files are never
  overwritten), so generated projects pass their own `dbt parse` gate out of
  the box. Also fixes the emitted `dbt_utils.recency` test, which carried a
  non-dbt `_fluid_window` kwarg that failed `dbt compile` — recency windows
  now derive honestly from the dq rule's ISO-8601 window. (#425)
- **MetricFlow bridge: `semantic_models.yml` from the contract semantics
  block.** `fluid generate transformation` now emits `semantic_models:` +
  `metrics:` YAML (plus the required day-grain time-spine model) from
  `exposes[].semantics`, with parse-strictness defaulting for primary
  entities, agg time dimensions, and metric references — verified against a
  real `dbt parse`. Contracts without semantics produce byte-identical
  output. (#426)
- **dbt Fusion (dbt Core v2) compatibility.** The runner now detects the
  engine flavor from `dbt --version` (`fusion` vs `core`), so Fusion users run
  natively instead of being silently punted to the Docker pip-install
  fallback; `fluid doctor` reports the detected engine and the welcome scan
  annotates the dbt row; `$DBT_EXECUTABLE` is honoured by the parse gate. The
  generated YAML is engine-aware too: `--dbt-tests-key auto|tests|data_tests`
  (env `FLUID_DBT_TESTS_KEY`) auto-detects the user's dbt and emits
  `data_tests:` for Fusion / core ≥ 1.8 while keeping the legacy `tests:`
  spelling for older cores — Fusion's strict parser no longer rejects
  generated projects. (#423, #429)
- **dbt build results close the loop into run records and verify.**
  `fluid apply --mode amend-and-build` now parses `target/run_results.json`
  after `dbt build` into per-test run records — `fluid runs status` shows each
  dbt test with its status and failure counts, and `fluid verify` gains
  transformation checks (`dbt_tests_passed`, no error-severity failures) that
  gate the exit code under `--strict`. (#420)
- **`fluid verify --reconcile-lineage`.** A local-only cross-check (no
  network) that the contract's declared lineage (`consumes[]` / `exposes[]`)
  agrees with what was actually observed (run records + cursor state) and
  what would be published (the catalog registrar payload, rebuilt locally).
  Drift classes: `declared_but_never_read` (soft), `read_but_undeclared` and
  `publish_payload_mismatch` (critical, gate under `--strict`). (#430)

### Changed

- **Contract schema 0.7.5 is now stable (GA); 0.7.6 opens as the next
  preview.** Untagged contracts now validate against 0.7.5 (was 0.7.4),
  graduating the Redshift-Serverless/Kinesis `bindingLocation` fields, the
  vector/embeddings `vectorConfig` output port, and the streaming
  Kafka→Iceberg surface. Note: untagged contracts' plan/bundle digests churn
  once on upgrade; explicitly-tagged contracts are unaffected. (#431)
- **One shared contract→dbt-test mapping across all three generation paths.**
  The engine, exporter, and copilot generators now delegate to a single
  module, so they can no longer drift: `relationships` tests now derive from
  the engine and exporter paths (previously copilot-only), numeric ranges
  standardize on `dbt_expectations.expect_column_values_to_be_between`
  (retiring `dbt_utils.accepted_range`), and freshness/recency now surfaces
  in the engine path. A symmetric reverse table powers the manifest
  importer. (#421)
- **CI: the overloaded Python 3.12 test leg was split**, moving the coverage
  run and gates into a parallel `coverage-and-audit` job so no matrix leg
  exceeds ~8 minutes. (#428)

### Fixed

- **Copilot enrichment now writes schema-valid contracts.** `fluid forge`'s
  `--apply-enrichment` pass wrote five slots the schema rejects, so every
  enriched contract failed `fluid validate`; enrichment data now lands in
  schema-valid locations (`qos.freshnessSLO` + a `dq.rules[]` freshness rule,
  the `extensions.enrichment.*` namespace, `binding.properties.physical`),
  and re-applying migrates contracts enriched by older versions. The quality
  engine also now parses the ISO-8601 `dqRule.window` format the schema
  requires (`PT6H`, not just `6h`), and a declared-but-unparseable window
  fails the check loudly instead of silently disabling the gate. (#417)
- **Retired the intermittent test-leg hang and the last `datetime.utcnow()`
  calls.** All 18 remaining `utcnow()` sites are now timezone-aware
  (removing the 3.12+ deprecation warnings that fed an unbounded mcp 1.x
  warning-relogging loop — filed upstream with a minimal repro), the test
  suite isolates root-logger state, and hosted-MCP operations gained a
  wall-clock bound via `FLUID_HOSTED_MCP_TIMEOUT_SECONDS` (default 120s).
  (#418)

## [0.11.0] — 2026-07-13

A forge-UX + AI-tooling feature release: arrow-key menus, `--offline` and
`--watch` modes, multi-provider AI config with an LLM-provider plugin system,
a layered prompt-customization stack, nine new domain agents, opt-in agent
tools, a pgvector RAG output port, and contract↔dbt reconciliation — plus SQL-
safety and redaction hardening. No breaking changes (the bare `odps` emit key
becomes a deprecated alias for `opds`).

### Security

- **Four remaining SQL string-literal sites route through the central
  `quote_string_literal` helper** (local DuckDB provider, IAM policy compiler,
  Meltano runner, MCP DuckDB driver). The local provider used `repr()`, which
  emitted double quotes — so any CSV/Parquet path containing a single quote
  (e.g. `/tmp/o'brien/data.csv`) broke the `read_csv_auto` / `read_parquet`
  load outright. (#387)
- **Six redaction-symmetry gaps closed** between the global secret redactor and
  the Snowflake-local twin: bare `passphrase=` values, quoted-JSON
  `"credentials"` keys, `auth` / `conn_str` / `connection_url` dict keys,
  generic (non-`eyJ`) three-segment JWTs, and bare `private_key=` values are
  now masked by both layers. (#389)
- **Exception text no longer leaks.** The `read_logical_model` copilot tool
  returns typed errors instead of interpolating raw exception strings into the
  LLM context; the always-on logging redaction filter now scrubs exception
  *objects* (the pervasive `LOG.warning("… %s", exc)` shape); and litellm
  exception text in user-facing error messages is routed through the secret
  redactor. (#392, #394)

### Added

- **Arrow-key navigation for every interactive menu.** ↑/↓ (and vim `k`/`j`)
  plus Enter on a real TTY across all seven forge / ai-setup menus, degrading
  cleanly to the numbered prompt in CI, pipes, and non-TTY shells; opt out with
  `FLUID_FORGE_NO_ARROW_KEYS=1`. (#352)
- **`fluid forge --offline`** (env twin `FLUID_FORGE_OFFLINE=1`) — a
  first-class no-network guided authoring path: no LLM, no mode picker, no
  remote schema fetch, and `--non-interactive` support for air-gapped or
  scripted runs. (#355)
- **`fluid forge --watch`** — watches the discovery path and regenerates the
  contract on source change, debouncing save-storms and never retriggering on
  its own output; Ctrl-C exits cleanly. (#362)
- **Mid-run LLM failure recovery.** An API-key 401 now re-prompts for a fresh
  key and retries generation in place (interview answers preserved) instead of
  ending the run; a 429 prints a `Rate limited. Waiting Ns before retrying...`
  notice instead of a silent frozen spinner. (#353, #361)
- **Multi-provider AI config.** `fluid ai setup` saves each provider into a
  map — a second setup no longer clobbers the first — `fluid ai status` lists
  every saved provider, and `fluid forge --llm-provider <name>` resolves any
  saved provider's key even without its env var set. (#363)
- **Import API keys from other CLIs.** `fluid ai setup` detects existing keys
  in well-known credential files (Codex `auth.json`, OpenAI/Anthropic
  dotfiles, `gh` `hosts.yml` for GitHub Models) and offers to import them —
  explicit opt-in prompt, HOME-confined reads, key values never displayed or
  logged. (#366)
- **Custom LLM provider plugins.** Third-party packages register providers via
  the `fluid_build.llm_providers` entry-point group
  (`pip install` → `fluid forge --llm-provider <name>`, no core edit);
  built-ins always win over a name clash, and the operator plugin allow/block
  policy governs discovery. (#365)
- **Layered prompt customization.** `fluid forge --prompt-profile <name>`
  swaps the whole prompt-guidance set for a named profile (bundled
  `eu-gdpr-strict` and `ai-lab-permissive` exemplars); per-tenant home-directory
  shadows and per-domain fragments override individual guidance blocks; and
  stackable `--prompt-overlay a,b,c` patches compose on top with validator
  rules and optional ed25519 signing. The active profile is stamped into
  `metadata.provenance.prompt_profile`. (#359, #364, #383)
- **Nine new built-in domain agents.** Eight verticals — manufacturing,
  logistics, energy, government, insurance, pharma, education, media — each
  grounded in its industry's authoritative standards (ISA-95, GS1 EPCIS,
  IEC CIM, NIEM, ACORD, CDISC, Ed-Fi, EIDR, …), plus `ai_ready`, which
  deterministically enforces AI-readiness metadata: per-port `agentPolicy`
  ("reporting yes, training no" for sensitive data), PII/sensitivity flags,
  and `ai-embeddable` column labels for RAG consumers. (#371, #405)
- **Domain keyword learning.** Forge tracks which product domains you build
  across runs (locally, in `~/.fluid/ai_config.json`) and, once a domain
  repeats, proactively nudges its template using frecency ranking. (#367)
- **pgvector vector/embeddings output port.** `fluid generate vector
  <contract>` compiles pgvector-bound exposes into an embeddings table, ANN
  index DDL (`hnsw` default / `ivfflat`), and a RAG manifest — driven by
  `vectorConfig` and the `ai-embeddable` labels the `ai_ready` agent stamps. (#410)
- **Opt-in agent tools.** SSRF-safe `web_search` + `web_fetch` behind
  `FLUID_AGENT_WEB_TOOLS=1` (private/metadata address ranges and DNS-rebind
  blocked, typed errors, keys never logged); plus the AI-tools trio — a
  read-only, redacted `fetch_sample_rows` live-DB sampler
  (`FLUID_FORGE_DB_TOOLS=1`), tool-search deferred schema loading
  (`FLUID_FORGE_TOOL_SEARCH=1`), and a hosted-MCP registry for the GitHub and
  Snowflake MCP servers (`FLUID_GITHUB_MCP=1` / `FLUID_SNOWFLAKE_MCP=1`). All
  off by default. (#358, #407)
- **Semantic-drift guard (opt-in).** `FLUID_FORGE_DRIFT_GUARD=1` catches the
  LLM renaming, dropping, or silently retyping columns relative to the
  discovered source schema or the `--refine` prior contract, and feeds the
  drift into the existing self-healing repair loop. (#409)
- **`fluid verify --reconcile-dbt`.** Statically cross-checks
  `exposes[].contract.schema` against the dbt project's `schema.yml` columns
  and reports drift (missing columns, type mismatches, unmatched models);
  drift exits 1 for CI, `--warn-only` downgrades it. (#403)
- **One guided Quickstart starter picker + provider blueprints.** `fluid
  init`'s first-run menu collapses from five rows to Quickstart / AI / Empty,
  with Quickstart leading to a single starter picker; new bundled GCP
  (BigQuery) and Snowflake starter blueprints are reachable via
  `--quickstart --provider gcp|snowflake` or `--blueprint <id>`. Existing
  flags are unchanged. (#370, #402)
- **`fluid apply --env` reaches apply hooks.** Hooks accepting an env
  parameter receive the resolved environment via backward-compatible signature
  dispatch (3-arg hooks unchanged), removing the `DEPLOY_ENV` fail-open. (#368)
- **Opt-in UX telemetry: provider choice + run completion.** Two bounded,
  enum-like fields behind the existing default-OFF consent gate with unchanged
  `DO_NOT_TRACK` / `FLUID_TELEMETRY` precedence. (#357)
- **Emulator-friendly AWS IaC.** When a custom endpoint is configured
  (`AWS_ENDPOINT_URL*`), the emitted `provider "aws"` block enables path-style
  S3 addressing and the validation skips LocalStack needs; on real AWS the
  output is byte-for-byte unchanged. (#411)
- **Examples.** 5-minute quickstart READMEs for examples 01–06 with an index,
  plus three realistic AWS-first example contracts (S3/Glue/Athena Parquet
  lake, Iceberg lakehouse, medallion lake) — all pinned by e2e validate
  tests. (#356, #384)

### Changed

- **DataHub registrar self-heals transient GMS failures.** Exponential backoff
  with jitter on 429/5xx (mirroring DataHub's own emitter defaults), honours
  `Retry-After`, configurable via `FLUID_CATALOG_DATAHUB_MAX_RETRIES`; a
  terminal outage still degrades to a clean failure instead of blocking the
  publish. (#404)
- **Faster, lighter `fluid --help`.** `fluid_build.commands` plugin
  subcommands now load lazily (an installed plugin's heavy imports no longer
  land on the cold path — and plugin subcommands that previously crashed on
  dispatch now work), and the `cli.mcp` re-exports + OTLP exporter are
  deferred, cutting roughly 200 modules off the cold start. (#369, #390)
- **LLM runtime relocated to `fluid_build.llm`,** severing the `copilot ⇄ cli`
  import cycle; every legacy `fluid_build.cli.forge_copilot_llm_*` import path
  still resolves to the same module via aliases. (#391)
- **CI hardening.** An incremental `mypy --strict` gate now covers four
  security-hotspot modules (#360), alongside routine pinned-action bumps.

### Fixed

- **`fluid apply --mode replace-and-build` now hits the data-loss gate.** The
  build-mode early-return dispatched to the builders before the stage-7 safety
  gate ran, so a destructive replace-and-build in a protected env proceeded
  without `--allow-data-loss`; the gate now runs before engine dispatch on
  both the native and OpenTofu paths. (#386)
- **FLUID-emitted ODCS round-trips losslessly.** Export is now a fixed point
  (`export(import(export(x))) == export(x)`): a phantom top-level `name` and
  dropped explicit `required: false` no longer appear on re-export. (#373)
- **OPDS/ODPS naming untangled; `opds` restored to the default emit set.** The
  LF/ODPI Open Data Product Specification is `opds` (provider renamed to
  `providers/opds/`), Bitol's Open Data Product Standard stays `odps-bitol`,
  and the bare `odps` key is a deprecated alias that warns; OPDS artifacts now
  validate against the vendored v4.1 schema in `fluid validate artifacts`. (#381)
- **Redshift Serverless + Kinesis contracts validate.** `bindingLocation` in
  schema 0.7.5 now declares `stream`, `namespace`, `workgroup`,
  `iam_role_arn`, `external_schema`, and `glue_database` — fields the AWS IaC
  emitter already read — so those contracts no longer fail `fluid validate`
  while passing `fluid generate iac`. (#396)
- **AWS IaC declares `aws_caller_identity` whenever the account-derived
  warehouse bucket is referenced,** so bucket-less Glue table contracts no
  longer fail `tofu plan` with an undeclared-resource error. (#408)
- **Copilot project memory reads and writes the same file.** Load, save, and
  `--show-memory` / `--reset-memory` all resolve one canonical memory root, so
  scaffolding into a subdirectory no longer duplicates or orphans accumulated
  workspace memory. (#354)

## [0.10.2] — 2026-06-27

A polish + internal-quality release on top of 0.10.1. No breaking changes.

### Added

- **`fluid init --blueprint` accepts `--domain` / `--owner-team` /
  `--owner-email`** and now writes a `.fluid/forge-receipt.json`, reaching parity
  with the `--blank` and template scaffolds so `fluid status` and drift detection
  see the same shape. The metadata flags flow through into the rendered
  contract's `domain` + `metadata.owner`. (#324)

### Changed

- **Error catalog: diagnostic-only entries now carry an actionable next step.**
  Five high-traffic failures (`generate_ci_failed`, `product_new_failed`,
  `signing_bundle_not_file`, `schedule_sync_dags_dir_not_directory`,
  `loader_missing_functions`) now pair the "what's wrong" line with a concrete
  "what to do"; `missing_contract` shares the scaffold hint with
  `contract_required`. (#326)
- **Internal — `fluid forge`'s `run_ai_copilot_mode` decomposed.** The interview,
  domain-enrichment, and project-creation cores are extracted into named helpers
  behind a typed `ForgeRunContext` carrier (behaviour-preserving, guarded by the
  304-test characterization net; 691 → 478 LOC). (#329)

### Fixed

- **Thread-safe, resettable lazy `CopilotAgent` cache.** The lazily-built
  `CopilotAgent` class is memoised with `functools.lru_cache` (lock-guarded,
  stable identity, `cache_clear()` reset hook) instead of a hand-rolled module
  global — removing a double-checked-locking race. (#323)

## [0.10.1] — 2026-06-27

A bug-fix + security release on top of 0.10.0.

### Security

- **Mask credentials embedded in URL userinfo** (`scheme://user:password@host`) <!-- pragma: allowlist secret -->
  across both redaction layers — the global `secret_redactor` and the
  Snowflake-local twin. Such a password (an Iceberg REST catalog
  `binding.location.uri`, a JDBC / `connection_url`, or a redis/AMQP broker URL)
  could previously survive into a log line or a persisted run record. The shared
  pattern is length-bounded (ReDoS-safe) and masks only the password, preserving
  scheme / user / host. (#316)

### Fixed

- **Iceberg REST/GCP/Azure catalog profiles are now expressible in a
  schema-valid contract.** `$defs/bindingLocation` in `fluid-schema-0.7.5.json`
  now declares `catalog`, `uri`, `warehouse`, and `partitionBy` — the fields the
  resolver and the streaming-sink validator already read — so a catalog-profile
  contract no longer fails `fluid validate` with "Additional properties are not
  allowed". (#314)
- **`fluid market --blueprints --format json` now emits clean, machine-parseable
  JSON** instead of the rich human table plus status banners, so it can be piped
  into a script. (#317)
- **`fluid market --blueprints` no longer prints a spurious "'fluid marketplace'
  is deprecated" banner** — that notice now fires only for a direct (hidden)
  `fluid marketplace` invocation. (#319)

## [0.10.0] — 2026-06-27

A plugin-system + provider-taxonomy release: the unified plugin manager with all
four SDK roles wired in, a real plugin↔CLI version gate, the ODPS/ODCS
provider-vs-exporter fix, and the operator surfaces (`fluid plugins`,
`fluid exporters`) — plus first-run/UX and error-reporting improvements.

### Added

- **Unified plugin manager + all four SDK roles wired into the CLI.** One
  host-side discovery substrate (`fluid_build.plugin_manager`) walks the
  role-tagged entry-point groups with per-plugin fail-isolation and an operator
  allow/block policy (`FLUID_PLUGINS_ALLOWLIST` / `FLUID_PLUGINS_BLOCKLIST`). The
  `Validator` role is wired into `fluid validate`, the `CatalogAdapter` role into
  `fluid publish`, and IaC clouds are now entry-point-pluggable
  (`fluid_build.iac_providers`); the orphaned parallel `cli/plugins.py` framework
  was retired. (#292, #293, #294)
- **`fluid plugins`** — operator inspection of installed plugins by role with
  their allow/block status (name-only, never loads plugin code). **`--detailed`**
  loads *allowed* plugins to surface their declared `PluginMetadata`
  (version / author / license / url); blocked plugins are never loaded. (#296, #307)
- **`fluid exporters`** — a discoverable home for the spec exporters
  (`odps` / `odcs` / `odps-bitol`) with their spec name, URL, and
  `fluid generate standard --format` invocation, backed by a registry separate
  from the provider registry. (#308)
- **Stable error slugs + a central catalog** of suggestions / docs URLs, so CLI
  errors carry a stable identifier and actionable next steps. (#302)
- **`fluid forge` can delegate agent tool-calls to the dbt MCP server** (opt-in),
  letting the copilot drive dbt through MCP. (#305)

### Changed

- **ODPS and ODCS are no longer cloud providers — they are spec EXPORTERS.**
  ODPS (Open Data Product Standard) and ODCS (Open Data Contract Standard) are
  data-product / data-contract *spec / export formats*, not infrastructure
  providers (their `apply()` does not deploy). Both are removed from the provider
  registry and entry-points, so they no longer appear in `fluid providers`,
  `fluid plugins`, or as a `--provider` choice. The fix is **principled** — an
  invariant test rejects any registered provider whose `apply()` doesn't deploy,
  so a future sibling can't slip through. Export is unchanged via
  `fluid odps export` / `fluid odcs` / `fluid generate standard --format` and the
  new `fluid exporters`. **Breaking (CLI surface):** `--provider odps` / `opds` /
  `odcs` are no longer accepted. (#298, #300, #304)
- **Friendlier first-run.** `fluid init` defaults to a Quickstart when no LLM key
  is configured, and counts a keyless coding agent (Claude Code / Cursor / Kiro)
  as AI-available when picking the menu default. (#303, #309)

### Fixed

- **The plugin↔CLI version gate is now real** (was a dead handshake): the CLI
  version comes from `importlib.metadata`, reads the new `fluid_sdk`, and gates a
  plugin's `requires_cli` PEP 440 specifier via `packaging.SpecifierSet`. (#291)
- **`fluid skills` is registered** — it shipped invisible due to swapped
  `_try_register` arguments. (#287)
- **`fluid product-add` emits canonical, schema-valid contract shapes** (routes
  sources / exposures / dq to their canonical homes). (#288)
- Corrected stale title/description in `fluid-schema-0.7.5.json`. (#290)

### Security

- **Every code-executing entry-point group is governed by the unified
  allow/block policy** — providers, commands, apply-hooks, extension
  schemas/validators, modeling techniques, source adapters — closing the gap
  where a blocked plugin could still load and run. Provider discovery no longer
  leaks raw exception text / tracebacks into `DISCOVERY_ERRORS` (type-only). (#295, #297)

### Performance

- `requests` and `build_runners` are deferred off the cold `--help` path. (#301)

## [0.9.0] — 2026-06-25

The Kafka → Apache Iceberg streaming-sink release: `fluid` now derives Iceberg
sink configuration for Kafka Connect, Debezium Server, and Confluent Tableflow
behind an opt-in 0.7.5 schema — alongside Windows UTF-8 fixes, a deterministic
`planDigest`, and streaming-credential redaction hardening.

### Security

- **Run records are redacted before they reach disk.** Kafka Connect
  task-failure traces captured into run-record facets can echo the connector
  config — `database.password`, S3 keys, `sasl.jaas.config` — and were written
  to `.fluid/<product>/…/runs/<run_id>.json` in plaintext, bypassing the
  logging redactor entirely. Every runner's run record now passes through the
  recursive redactor at the single `write_run_record` chokepoint before the
  bytes hit disk. (#272)
- **`sasl.jaas.config` values are fully masked in both redaction layers.** The
  JAAS string's value embeds the SASL login secret but its key contains no
  sensitive substring, so the key matcher missed it and a non-`password=`
  secret (e.g. OAuthBearer `clientSecret=`) could leak into logs and failure
  traces. Both the global `SecretRedactingFilter` and the provider-local
  redactor now mask the whole value, on the dict-key and serialized-text
  paths. (#270)
- **Dotted streaming-credential keys redact symmetrically.**
  `s3.secret-access-key`, `session-token`, `gcs.oauth2.token`, and
  `jdbc.password` are masked across all redaction layers (text and dict
  paths), delegating the key decision to the single `is_sensitive_key_name`
  source of truth. (#286)

### Added

- **Opt-in `fluid-schema-0.7.5` with a `streamingSink` block.** The
  `kafka-connect` properties gain a closed `streamingSink` object (typos in
  the new surface are caught) with typed `iceberg_sink_enabled` /
  `sink_topics` / `iceberg_catalog_overrides`, and `iceberg_table` is accepted
  as an alias of the `iceberg` expose format. 0.7.5 is a preview version —
  bundled and validatable on explicit opt-in, never the silent default, so
  existing contracts' plan and bundle digests don't churn. (#266)
- **Derived Iceberg sink config for Kafka Connect.** With
  `sink.format: iceberg` and no hand-written `sink_connector_config`, the
  runner derives the full `iceberg.catalog.*` / `iceberg.tables.*` connector
  config: Glue and REST catalogs, a per-product control topic (avoiding the
  shared `control-iceberg` collision), JSON or Avro converters depending on
  Schema Registry presence, and an `iceberg_catalog_overrides` escape hatch
  where operator keys win. Contracts with a hand-written config are
  byte-for-byte unaffected. (#264)
- **REST + GCP/Azure Iceberg catalog profiles.** The catalog resolver picks
  the FileIO implementation from the warehouse scheme (`s3://` → S3FileIO,
  `gs://` → GCSFileIO, `abfss://` → ADLSFileIO) and resolves REST/GCP/Azure
  catalog kinds with a safe `rest` fallback. (#280)
- **Plan-time validation for the Iceberg streaming sink.** `fluid validate`
  now catches the connector's silent-fail-at-first-record traps: an Iceberg
  sink build with no matching expose, `upsertMode` (deferred in v1), dynamic
  routing without `routeField`, an incomplete REST catalog binding, and an
  `iceberg_catalog_overrides` warehouse that diverges from the binding
  warehouse. (#267)
- **Kafka Connect task-level failure detection.** The status poll previously
  checked only `connector.state`, so a connector reporting `RUNNING` over a
  `FAILED` task counted a broken sink as a successful run. Health now requires
  the connector RUNNING and no task FAILED — the Iceberg sink connector
  included — with each failed task's trace captured into the run record; the
  Iceberg late-events side table is named `<database.table>__late_events`
  beside the target. (#268)
- **Embedded Debezium-Server Iceberg sink.** The debezium runner derives the
  `io.debezium.server.iceberg` config surface (warehouse, `table-namespace`,
  catalog impl, FileIO, region, upsert/identifier fields, partitioning) — a
  different surface from the Kafka Connect keys. Off when a hand-written
  `config` block is present; opt in via `iceberg_sink_enabled: true`. (#284)
- **Confluent Tableflow IaC plugin — managed Kafka → Iceberg.** A `confluent`
  platform binding compiles into the `confluentinc/confluent` OpenTofu
  provider's managed control plane: a Tableflow topic (`byob_aws`), the AWS
  Glue catalog integration, and the storage provider integration, with a
  validate-time anti-no-op gate. It reuses the AWS warehouse writer so the
  managed and self-managed paths resolve to the same table identity, and the
  emitted module stays credential-free. (#285)

### Changed

- **`fluid --help` no longer loads the MCP server SDK.** The ~87 `mcp.*`
  modules that every non-MCP command eagerly imported are now built lazily on
  first `fluid mcp serve`, cutting ~245 modules (~15%) off the cold path, with
  the startup budget enforced as a hard CI gate; `cli/mcp.py` was subsequently
  split into a package with the lazy loading and all policy/permission gates
  preserved. (#265, #282)
- **`fluid_build/config.py` renamed to `config_defaults.py`.** Distinguishes
  the compile-time static defaults from the runtime hierarchical
  `config_manager.py`; update imports if you consume the package as a
  library. (#225)

### Fixed

- **`planDigest` is deterministic across identical plan runs.** The volatile
  `generated_at` timestamp leaked into the SHA-256 and topological action
  ordering depended on per-process hash randomization, so two identical
  `fluid plan` runs disagreed — breaking apply-time plan-binding verification
  and CI digest diffing. Timestamps are masked out of the digest (they remain
  in `plan.json` as audit metadata) and action levels are built in stable
  parse order. (#260)
- **The CLI no longer crashes on Windows cp1252.** stdout/stderr are
  reconfigured to UTF-8 at startup — previously the first emoji in the help
  banner raised `UnicodeEncodeError` whenever output was piped or captured —
  and all ~160 text file I/O sites now pass `encoding="utf-8"` explicitly, so
  contracts with accented names or emoji read and write correctly under a
  cp1252 locale. A static test gate rejects any new unencoded text-I/O site.
  (#263, #269)
- **Iceberg warehouse derivation unified into a single writer.** The S3
  warehouse location for an Iceberg/Glue exposure was derived at three drifted
  sites (native planner, OpenTofu Glue emitter, Lake Formation emitter) that
  could resolve the same binding to different warehouses; all three now call
  one canonical `get_iceberg_warehouse`, with contract-derived text kept out
  of OpenTofu interpolation. Explicit-bucket contracts emit byte-identical
  output. (#262)
- **`fluid bundle --out -` no longer leaks its logging redirect.** Bundling to
  stdout set `propagate = False` process-globally and never restored it,
  silently breaking log propagation for every later caller in the same
  interpreter (pytest `caplog`, the `forge_run` MCP tool, library embeddings);
  logging state is now snapshot on entry and restored on exit while stdout
  stays clean during the call. (#261)
- **Debezium `.properties` values escape backslashes.** `java.util.Properties`
  treats a bare backslash as an escape introducer, silently corrupting
  values; the emitter now escapes per the Java grammar. (#286)

## [0.8.11] - 2026-06-16

### Added

- **Pluggable metadata source adapters for `fluid forge data-model from-source`.**
  A new `fluid_build.source_adapters` entry-point group lets a third-party
  package register a custom `CatalogAdapter` (e.g. an internal/enterprise
  metadata catalog) that merges into the `--source` choices and dispatch — and
  the `forge_from_source` MCP tool — without forking the CLI. Built-in catalog +
  JDBC sources now resolve through one shared registry (the two previously
  duplicated dispatch tables are gone). (#247)
- **Pluggable modeling techniques, plus `flat` and `custom`.** A new
  `fluid_build.modeling_techniques` entry-point group makes
  `--modeling-technique` extensible. Two new built-ins: **`flat`** (source-aligned
  1:1 — one expose per source table, no reshaping) for bronze / source-aligned
  products, and **`custom`** (bring-your-own logical model used verbatim via
  `--logical-model <path>`, no reshaping). The closed
  `{data_vault_2, dimensional}` enum is now a registry the modeler and contract
  emitter read instead of branching on the technique name. (#248)

### Fixed

- **Contract schema now accepts dbt features the build runner already supports.**
  `build.engine` accepts the whole `dbt-<adapter>` family (`dbt-glue`,
  `dbt-snowflake`, …) generically, and the hybrid-reference build pattern accepts
  `properties.target`, `properties.select`, and `properties.models` — all of
  which the dbt runner already honoured but `fluid validate` rejected. Applied
  across every bundled schema (0.7.1–0.7.4). (#249)
- **Nightly live-LLM / TS-conformance gate** no longer false-fails when no LLM
  API key is wired; duplicate `CircuitBreakerOpenError` definitions consolidated
  into `fluid_build/errors.py`. (#223)
- **CI Docker base bumped to `python:3.13-slim`** with grype-ignores for the
  unfixable CPython CVEs. (#226)

### Security

- **`Dockerfile.verify` now runs as a non-root user** (`fluid`, UID/GID 1000,
  overridable via `--build-arg UID/GID` for Linux hosts), closing trivy
  DS-0002 and matching the production image's non-root posture. (#235)
- **`.gitleaks.toml` with triaged allowlists** so full-history `gitleaks
  detect` scans run clean. All 464 historical candidates were confirmed fake
  (`trufflehog --only-verified`: 0): the detect-secrets baseline's hashed
  digests, redaction-test fixtures, and the redactor pattern sources.
  Requires gitleaks ≥ 8.25.0. (#236)

### Changed

- Dependency / CI-action bumps via Dependabot: black 26.3.1→26.5.1 (#232),
  `actions/setup-node` (#231), `actions/setup-go` (#243),
  `actions/github-script` (#244), `github/codeql-action` (#246),
  `docker/metadata-action` (#230), `docker/login-action` (#227),
  `docker/setup-buildx-action` (#245),
  `aws-actions/configure-aws-credentials` (#229),
  `peter-evans/create-pull-request` (#228),
  `softprops/action-gh-release` (#242).

## [0.8.10] - 2026-06-08

### Added

- **`fluid describe --self` self-description for the Command Center.** Library-
  callable `fluid_build.describe.self_describe()` now returns `fluid_version`,
  `schema_version`, `providers`, `build_engines`, `templates`, `capabilities`,
  and the full **command tree** (every subcommand + its flags), consumed by the
  backend via `GET /api/v1/forge/capabilities` so the UI can render the CLI
  surface without lagging it. (#220)
- **Opt-in usage telemetry, default OFF.** Nothing is emitted unless explicitly
  opted in; honours `FLUID_TELEMETRY=0` and the `DO_NOT_TRACK` standard, and the
  resolved state is surfaced in `fluid doctor`. (#217)
- **Reuse an existing LLM API key during AI setup.** `fluid` AI setup now offers
  to persist a recognized built-in-provider key already present in the
  environment (read-only, explicit confirmation, never logged). (#217)
- **Contract diff before writing on refine/regenerate.** The refine/preview flow
  now shows what changed (reusing the changelog differ) before the contract is
  written. (#217)

### Changed

- Renamed `providers/snowflake/provider_enhanced.py` → `provider.py` (canonical
  `SnowflakeProvider`); class name and entry points are unchanged. (#217)
- Added a startup-budget perf gate (`fluid --help` module-count + cold wall-time
  in clean subprocesses) to catch CLI-startup regressions. (#217)

### Fixed

- **BigQuery rollback restore.** `fluid rollback` now restores BigQuery products
  (previously Snowflake only), replaying the snapshot's
  `CREATE OR REPLACE TABLE ... AS SELECT` via the BigQuery client with a `gcp`
  dispatch alias. (#217)
- **`fluid generate iac --provider auto` detection broadened.** The target cloud
  is now detected from top-level `binding.{provider,platform}`, `builds[].provider`,
  and the runtime platform — not just `exposes[].binding.platform` — fixing the
  common single-binding contract that previously errored as "could not detect a
  supported cloud" (surfaced as a 422 in the Command Center); `local`/DuckDB now
  raises an actionable `generate_iac_local_target` error. (#211)
- **odps export fails loud.** `fluid generate standard --format odps-v4.1` /
  `fluid odps export` now error instead of silently writing the literal `[]` to
  disk while exiting 0. (#211)
- **CI resilient to transient OpenTofu registry outages.** IaC tests convert
  `tofu init` 504 / network failures to skips while real `tofu validate` schema
  errors still fail. (#218)

### Security

- **Fixed injection / RCE / RLS findings across the platform.** (#215)
  - Code-injection (RCE) in generated Airflow DAGs — untrusted contract values are
    now routed through repr-escaped literals / sanitized identifiers.
  - Shell-RCE + SQL injection in the Redshift external-schema emit — closed.
  - Multi-tenant row-level-security **bypass** in the MCP `query` / `query_sql`
    tools — they now apply `policy.rowFilters[]` and fail closed on missing caller
    identity.
  - Path-traversal RCE in the dlt custom-source loader — now fails closed via
    workspace confinement.
  - Adds cross-provider AST regression tests.
- **Rollback no longer executes attacker-authorable DDL.** The Snowflake and
  BigQuery rollback restorers reconstruct the restore statement from validated
  identifiers instead of replaying `snapshot.ddl[]` verbatim, so a tampered
  `.fluid/rollback-state.json` cannot smuggle arbitrary SQL (DROP/DELETE/GRANT). (#217)
- **Hardened CI Security Scan.** Project dependencies are now audited before the
  SAST scanners are installed, unbreaking the scan job. (#212)

### Removed

- **Unused root `requirements.txt` / `requirements.lock.txt`.** Neither was
  referenced by CI, the Dockerfiles, packaging, or any install step —
  `pyproject.toml` is the single source of truth. Removing them clears spurious
  Dependabot churn and false security alerts. (#213)

### Dependencies

- Bumped runtime dependencies: typer 0.25.1→0.26.6 (#197), certifi (#201),
  openai 2.37.0→2.40.0 (#202), yarl (#203), more-itertools (#204), aiohttp (#207).
- Bumped CI actions: setup-python (#195), attest-build-provenance (#196),
  build-push-action (#199), google-github-actions/auth (#200), checkout (#198).
- CI also gained clean-skip of MCP e2e jobs when secrets are missing (#209) and
  SHA-pinned dev workflows with a refreshed `PINNED_ACTIONS` list (#206).

## [0.8.9] - 2026-06-01

### Added

- **Keyless contract authoring via local AI coding agents.** `fluid forge` can
  now author contracts without an LLM API key of its own by reusing an AI coding
  agent the user already has, across two topologies on the existing `LlmProvider`
  seam (selection is by provider name — no new backend switch):
  - **In-IDE (MCP sampling).** `--llm-provider mcp-sampling` is now accepted by
    the `forge` / `forge data-model` argparse `choices` allowlist and exempted
    from the api-key gate (added to `_KEYLESS_PROVIDERS`), so `MCPSamplingProvider`
    routes the LLM call back to the host IDE via `sampling/createMessage` with no
    key (previously rejected as `copilot_missing_llm_api_key`).
  - **Standalone terminal (new).** A single parametrized `CodingAgentProvider`
    (new module `fluid_build/cli/forge_copilot_coding_agent.py`) shells out to each
    agent's headless CLI — `claude -p`, `codex exec`, `cursor-agent -p`,
    `kiro-cli chat --no-interactive` — over one list-argv subprocess seam
    (`_run_agent`, never `shell=True`), with schema-constrained output and
    ANSI-stripped stdout before envelope extraction. Only Claude Code is truly
    zero-setup keyless (subscription OAuth); codex / cursor / kiro reuse their own
    stored login or key.
  - Adds welcome-scan detection of installed agent CLIs, an auto-offer in
    `ai_setup.run_ai_setup_inline`, a `--forge-agent-mode {envelope,agentic}`
    flag, `fluid doctor` reporting, and the `FLUID_FORGE_AGENT`,
    `FLUID_FORGE_AGENT_MODE`, `FLUID_FORGE_AGENT_TIMEOUT_SECONDS`, and
    `FLUID_FORGE_AGENT_CWD` env vars. (#189)
- **Native copilot support for ANY `contract.extensions.<key>` block.** A new
  `fluid_build.extension_schemas` entry-point group lets a plugin advertise the
  JSON-Schema for its extension sub-key
  (`get_extension_schema(fluid_version=None) -> dict`). The `fluid forge` copilot
  now (a) grounds the modeler on every installed extension schema so it can
  natively propose a valid block, and (b) schema-gates + validates the proposed
  block before emit. Adding a new extension needs **zero** forge-cli changes — the
  entry-point group is the entire contract. See `fluid_build/extension_schemas.py`. (#191)

### Fixed

- **Pre-emit extension validation.** The copilot's conformance pass now runs
  registered `fluid_build.extension_validators` (the same plugins `fluid validate`
  uses) before writing the contract, so a malformed `extensions.<key>` block
  surfaces as an error-severity finding and enters the repair loop instead of
  being emitted silently (the core schema treats `extensions` as
  `additionalProperties: true`). (#191)

### Changed

- Updated the `sdk` optional extra from the retired `fluid-provider-sdk` to
  `data-product-forge-sdk>=0.9,<1` (`fluid_sdk`). (#191)

## [0.8.8] - 2026-05-31

### Added

- **On-demand OpenTofu provisioner — `fluid apply --ensure-opentofu`.** A cloud
  apply shells out to `tofu` via the OpenTofu engine; the official standalone
  installer needs root (`/usr/local/bin`) and gpg/cosign, which a non-root CI
  runner (e.g. a locked-down Jenkins agent) can't provide. When `--ensure-opentofu`
  is set and `tofu` is missing, FLUID downloads a pinned, **SHA-256-verified**
  OpenTofu release and installs it using only the Python standard library — **no
  root, gpg, cosign, curl, or unzip** — extracting only the `tofu` entry (no
  zip-slip) and prepending it to the process `PATH`. Idempotent (a usable `tofu`
  at/above the engine floor wins untouched); override the pin with
  `FLUID_OPENTOFU_VERSION`. `fluid generate ci` bakes the flag into the apply
  stage of all seven CI runners. (#187)
- **`fluid market` real catalog discovery over MCP** — DataHub, OpenMetadata,
  and Data Mesh Manager, replacing the previous demo data. (#172)
- **`fluid market` metadata enrichment** — two-phase fetch of full product
  detail plus data-asset column schema; `--detailed` is now a true superset of
  the listing rather than a replacement. (#175, #183)
- **`fluid market` onboarding + trust/usage surfacing** — actionable next steps
  and trust/usage signals in the listing. (#173)
- **`fluid market --blueprints` works offline** via bundled blueprints. (#181)

### Changed

- `fluid market` no longer serves fabricated demo data for roadmap-only catalog
  connectors (datahub / glue / data-catalog / rest); they are skipped with a
  clear roadmap note. (#171, #174, #182)
- `fluid market --format json` emits clean, machine-parseable output. (#184)
- Faster CLI startup — validation providers are lazy-loaded. (#167)
- Internal: the two simple-mode reporting blocks were extracted out of
  `cli/apply.py::run()`. (#185)

### Fixed

- **generate-ci dev-source Jenkins bootstrap left `fluid` uncallable.** It ran
  `pip uninstall -y data-product-forge` — deleting the `fluid` console script
  (the package's `console_scripts` entry point) — then invoked `fluid` relying
  only on `PYTHONPATH`, so stage 0 died with `fluid: not found`. It now keeps the
  installed console script (a `PYTHONPATH` prepend already shadows its modules
  with the bind-mounted checkout) and adds an `import fluid_build` sanity check. (#187)
- **generate-ci stage-8 policy-apply emitted an empty `--mode`.** The Jenkins
  stage read `${POLICY_APPLY_MODE}` as a raw shell env var; on the first build
  after a Jenkinsfile change the param isn't injected yet, so it expanded to
  `--mode ` and `fluid policy-apply` rejected the empty choice. Now defaults to
  `enforce` (the param's own default). (#187)
- `fluid forge --from-source` sanitizes the contract id derived from a sqlite
  file path. (#166)
- build-runners now warn when an inline-SQL build declares `engine: dbt`
  (previously silently ignored). (#168)
- `fluid policy-apply` surfaces a no-op message when a provider has no policy
  applier instead of appearing to succeed silently. (#169)
- `fluid market` per-catalog MCP search-limit param corrected (OpenMetadata
  `size`, DataHub `num_results`). (#180)
- `fluid market` / marketplace raises a proper `CLIError` instead of crashing
  with a `TypeError`. (#170)

### Removed

- Dead module `fluid_build/validation.py` (test-only, never wired in). (#178)
- Dead SQL-allowlist helpers — `parse_and_allowlist_sql` and the type/language
  validators. (#179)

### Security

- Re-symmetrized the Snowflake provider-local secret redactor with the global
  logging filter so new secret shapes are masked in both layers. (#176)

## [0.8.7] - 2026-05-30

### Fixed

- **0.7.4 schema was not actually backward-compatible with 0.7.3 (shipped in
  0.8.6).** The MCP "schema minimize" refactor that introduced
  `fluid-schema-0.7.4.json` dropped fields that 0.7.4 still advertised as fully
  backward-compatible with 0.7.3, so any 0.7.3 contract using them failed 0.7.4
  validation. Restored: top-level and per-binding `governance` (AWS Lake
  Formation admins, LF-tag definitions, principal grants, row/column filters);
  the `snowflake_view`, `redshift_table`, `redshift_serverless`, and
  `redshift_external_schema` binding formats; the `athena`, `glue`, and
  `redshift` runtime platforms; `datamesh_manager` catalog registration; and the
  `opentofu` deployment target. 0.7.4 is now a strict superset of 0.7.3 again —
  every genuine 0.7.4 addition (`expose.mcp`, the `postgres` platform, and the
  `postgres_table` / `athena_table` / `glue_table` formats) is retained. (#162)
- **Stale `0.7.3` version fallbacks no longer freeze the default schema.** Three
  degenerate "schema-discovery-came-up-empty" fallbacks (`cli/validate.py`,
  `cli/version_cmd.py`, `forge/core/validation.py`) plus four hardcoded defaults
  in `cli/plan.py` still pointed at `0.7.3`. They now derive from
  `FluidSchemaManager.latest_bundled_version()` (or include `0.7.4`), so the
  bundled-schema default tracks the newest version instead of drifting each
  release. (#163)

### Changed

- **README polish + version sync.** Single Install section, refreshed table of
  contents, CLI-completeness pass, current version numbers, and removal of the
  legacy `opds` spelling from the backward-compatibility note. (#160, #161)

## [0.8.6] - 2026-05-29

### Fixed

- **Daemon-thread leak that OOM-hung CI on Python 3.13/3.14.**
  `forge.core.monitoring.MonitoringSystem` started four background daemon
  workers (metric / log / alert processors + aggregator) per instance and
  only stopped them on an explicit `shutdown()`. Code that constructed
  instances and dropped them (notably the test suite) leaked threads
  without bound; across a long run the per-thread virtual stacks exhausted
  address space and the OS OOM-killed the process. Workers now stop
  promptly via a `threading.Event`, live instances are tracked in a
  `WeakSet`, `shutdown()`/context-manager support is added, and a per-test
  fixture drains them.
- **`ConfigManager` could corrupt process-wide defaults.** `_load_defaults`
  shallow-copied the module-level `DEFAULT_CONFIG`, sharing its nested
  dicts — so a later `set("logging.level", ...)` or config-file merge
  mutated the shared defaults in place, affecting every other
  `ConfigManager` in the process. Now deep-copies the defaults.
- **MCP gateway rate limiter no longer leaks a background thread.**
  Replaced PyrateLimiter's `Limiter` (which spins a per-instance "leaker"
  thread) with an in-process monotonic-clock deque sliding window —
  functionally identical for the single-replica gateway, with no thread
  and no dependency. Drops the `pyrate-limiter` dependency.

### Added (closes the final 5 honest gaps from the prior audit)

- **JWT bearer + mTLS gateway-native identity**
  (`fluid_build/output_ports/mcp/auth.py`). New `AuthValidator`
  strategy with modes: `shared-token` (existing v0.7.4 default),
  `jwt` (RS256/ES256/EdDSA against an issuer's JWKS endpoint,
  validates iss/aud/exp/sig), `none` (operator opts out, gateway
  warns loud at startup). Maps configured JWT claims into
  `caller_attributes` so `policy.rowFilters` `${caller.<attr>}`
  placeholders resolve cryptographically rather than via
  self-attestation. Mirrors `X-Client-CN` + `X-Client-Fingerprint`
  proxy-forwarded mTLS metadata for combined identity attribution.
  Real RSA signing roundtrip + wrong-key rejection in the test suite.

- **BigQuery row-access policy compiler** (real impl, replaces the
  earlier stub). Emits `CREATE OR REPLACE ROW ACCESS POLICY ON
  <table> GRANT TO (<service_accounts>) FILTER USING (<predicate>)`
  with caller.user → SESSION_USER() mapping and
  `agentPolicy.allowedModels` → `serviceAccount:fluid-mcp-<MODEL>@<project>`
  GRANT clauses.

- **AWS Lake Formation compiler** (real impl, replaces the earlier
  stub). Emits a runnable boto3 Python script with
  `lakeformation.create_data_cells_filter` + `grant_permissions`
  calls bound to per-LLM IAM roles
  (`arn:aws:iam::<ACCOUNT>:role/fluid-mcp-<MODEL>`). Operators paste
  into CDK/Terraform or run directly.

- **Audit webhook forwarder** for multi-instance HA. Set
  `FLUID_MCP_AUDIT_WEBHOOK_URL` (and optionally
  `FLUID_MCP_AUDIT_WEBHOOK_HEADER_AUTH`) and every audit event is
  POST-ed on a daemon thread to a SIEM aggregator (Splunk HEC,
  Datadog, Elastic, Loki). Best-effort: webhook failures NEVER
  block the local-disk write — local copy is the source of truth.

- **Multi-language MCP-client conformance** harnesses + CI matrix.
  `scripts/conformance/conformance_test.{ts,go,rs}` exercise the
  same 3-step contract (initialize → tools/list → call with allow +
  deny) via the official TypeScript SDK
  (`@modelcontextprotocol/sdk`), the community Go SDK
  (`mark3labs/mcp-go`), and the community Rust SDK (`rmcp`). New
  `multi-lang-mcp-conformance` job in `integration.yml` runs all
  three on every nightly + workflow-dispatch trigger.

- **Provider-selection regression tests**
  (`tests/output_ports/test_provider_resolution.py`). Pins the
  `_resolve_provider()` contract: when both `ANTHROPIC_API_KEY` and
  `OPENAI_API_KEY` are set, the live-LLM tests prefer Anthropic
  Haiku 4.5 (the Anthropic preference is now a regression-tested
  invariant, not just a docstring).

### Added (final gap-closing pass — production-grade defence in depth)

- **PostgreSQL driver** — psycopg v3, read-only sessions,
  per-statement timeout via `SET LOCAL`, `:p_<idx>` →
  `%(p_<idx>)s` parameter rewrite. Live e2e via the dockerized
  setup in `examples/mcp-output-port-docker/`.
- **AWS Athena driver** — boto3 default credential chain,
  `StartQueryExecution` → poll → page-through with
  `ExecutionParameters`. Mocked unit tests; live AWS validation
  deferred to a follow-up.
- **Out-of-tree driver registration tested** — `register_driver()`
  contract pinned by 3 new tests so a future refactor can't
  silently break customers' private wheels.
- **HTTP / SSE transport with optional bearer-token auth.** Set
  `FLUID_MCP_AUTH_TOKEN` and the gateway returns 401 on every
  unauthenticated request BEFORE the SSE handshake. Pair with the
  Caddy / nginx mTLS templates in
  `examples/mcp-output-port-docker/proxy/` for production
  defence-in-depth.
- **Per-tenant row-level security** via `policy.rowFilters[]`. Each
  filter compiles to a parameterised `WHERE` clause bound to MCP
  `clientInfo` extra fields (`${caller.tenant_id}` etc.). Missing
  caller attributes raise `RowFilterIdentityMissing` (fail-closed
  deny — the gateway never serves rows under undefined identity).
- **Cloud-IAM compiler** (`fluid_build.output_ports.iam_compiler`).
  Emits Snowflake row-access policies + Postgres `CREATE POLICY`
  statements that honour the same `agentPolicy` + `rowFilters`
  contract — defence-in-depth so bypass-the-gateway scenarios
  (analyst querying the warehouse directly) are also gated.
  BigQuery + AWS Lake Formation targets emit clear `-- TODO`
  stubs with explicit warnings.
- **EngineDriver.close() hoisted to base class** — every driver
  now has a uniform close path. Walks `_connection` / `_client`
  attributes for cleanup; idempotent.
- **Backpressure tracking split into `_in_flight` (drain count) +
  `_actively_dispatching` (concurrency cap)** — the unit test
  caught a real bug where the previous instrumentation conflated
  the two. Operators sizing connection pools now have a clean
  metric.
- **CI integration jobs** for Postgres docker e2e + Snowflake
  live e2e. `mcp-output-port-postgres-e2e` and
  `mcp-output-port-snowflake-e2e` join `mcp-output-port-live-llm`
  on the integration workflow's nightly + workflow-dispatch
  triggers.
- **Hash-pinned lockfile recipe** — generated via
  `uv pip compile pyproject.toml --generate-hashes -o
  requirements.lock.hashed.txt` (uv is the canonical pip-tools
  replacement; ~100× faster than the previous custom script). The
  earlier `scripts/generate_hashed_lockfile.py` placeholder was
  removed in this release per /borrow-before-build (uv covers the
  full surface and is dev-time only — no runtime cost).

### Changed (security)

- **Reverse-proxy templates** for production HTTP deployments —
  Caddy + nginx with mTLS + bearer token + SSE buffering.

### Added (enterprise-grade hardening — closes the v0.7.4 gap list)

- **PostgreSQL driver** (`fluid_build/output_ports/mcp/drivers/postgres.py`).
  Mirrors the Snowflake / BigQuery shape; binds on
  `platform=postgres`, `format∈{postgres_table, table}`. Read-only
  sessions enforced at connect time; per-statement timeout via
  `SET LOCAL statement_timeout`. Borrowed-not-built: `psycopg` v3
  (parameter-binding native, SQL-injection-safe identifier
  quoting). Exercised end-to-end against a dockerized Postgres in
  the new `examples/mcp-output-port-docker/`.

- **AWS Athena driver** (`fluid_build/output_ports/mcp/drivers/athena.py`).
  Binds on `platform=aws`, `format∈{athena_table, glue_table}`.
  Uses the boto3 default credential chain (env / `~/.aws/credentials`
  / IAM role / OIDC) — no long-lived keys baked in. Polls
  `GetQueryExecution` with configurable timeout, pages through
  `GetQueryResults`, parameterised queries via `ExecutionParameters`.

- **Row-level PII / PHI redaction** at the driver boundary. Columns
  marked `sensitivity: pii`, `sensitivity: phi`, or
  `sensitivity: sensitive` in `expose.contract.schema` keep their
  KEY visible (so the agent knows the field exists and can write
  `COUNT(DISTINCT)` aggregates) but VALUES are replaced with
  `[REDACTED-PII]` before the row leaves the gateway. Distinct from
  `columnRestrictions`, which drops the column wholesale.

- **Audit-log rotation**
  (`audit_trail.rotate_audit_directory`). Bounded by
  `FLUID_AUDIT_MAX_AGE_DAYS` (default 30) AND
  `FLUID_AUDIT_MAX_TOTAL_MB` (default 256). Runs automatically on
  gateway startup; oldest files dropped first when over budget.
  Replaces unbounded growth of `~/.fluid/store/audit/`.

- **Backpressure** via `asyncio.Semaphore`. Bounds concurrent tool
  calls to `FLUID_MCP_MAX_CONCURRENCY` (default 8) so a runaway
  agent can't saturate the engine connection pool.

- **Circuit breaker** on the driver layer. Trips after
  `FLUID_MCP_CIRCUIT_THRESHOLD` (default 5) failures within
  `FLUID_MCP_CIRCUIT_WINDOW_SECONDS` (default 60); open for
  `FLUID_MCP_CIRCUIT_COOLDOWN_SECONDS` (default 30). Returns a
  `CircuitOpen` envelope fast instead of pinning event-loop slots
  on a downstream Snowflake / BigQuery / Postgres outage.

- **Token-budget enforcement** for `agentPolicy.maxTokensPerDay`
  and `agentPolicy.maxTokensPerRequest`. Per-day counter rolls on a
  sliding 24-hour window; per-request cap evaluated against the
  serialised response payload. Cap denials emit a typed
  `TokenBudgetExceeded` envelope and an audit event with
  `policySource: "token-budget"`.

- **`canStore` advisory + retention startup warning.** When the
  contract sets `agentPolicy.canStore=false` or
  `retentionPolicy.requireDeletion=true`, the gateway surfaces a
  loud stderr notice at startup explaining the gateway honours the
  hint advisorily but cannot prevent the receiving model from
  storing data once it crosses the wire (cloud-IAM ephemeral
  credentials are the only true guarantee).

- **HTTP / SSE transport** option:
  `fluid mcp output-port serve --transport http --host H --port N`.
  Borrows `mcp.server.sse.SseServerTransport` + Starlette + uvicorn
  (transitive deps of `mcp[cli]`). No built-in HTTP auth —
  documented loud at startup; pair with mTLS / OAuth proxy until
  the auth phase ships.

- **Local Docker e2e harness**
  (`examples/mcp-output-port-docker/`). One-command
  `docker compose up -d` brings up Postgres seeded with a small
  telco customer table. `run_e2e.py` drives the gateway with a
  real LLM (litellm + OpenAI / Anthropic) across 4 scenarios:
  Postgres allow + PII redaction, Postgres deny by model, Postgres
  deny by use-case, DuckDB allow on the same gate (proves
  engine-agnostic enforcement). Total LLM cost per run ~$0.0001.

- **Throughput + latency benchmark**
  (`scripts/mcp_output_port_bench.py`). Drives the gateway via the
  SDK in-memory transport (zero LLM cost). Recorded baseline on
  laptop: **1080 calls/s, p50=6.7 ms, p95=7.5 ms, p99=34 ms** at
  500 calls / 8 concurrency against the local DuckDB driver.

- **Reproducible-build lockfile** (`requirements.lock.txt`). Pins
  the resolved transitive closure of every dep so a fresh install
  on CI / a contributor laptop / a container build resolves to
  the exact versions the maintainers tested.

### Added (v0.7.4 base — from prior turns)

- **FLUID schema v0.7.4 — Runtime agentPolicy Enforcement at the MCP gateway.**
  Closes the legitimacy gap where `agentPolicy` was declarative metadata
  only. Adds the `expose.mcp` block: its presence declares an expose
  agent-consumable over MCP (sampling caps + classification) and opts it
  into the gateway. Backward-compatible with v0.7.3: existing contracts
  validate unchanged.

- **`fluid mcp output-port serve` runtime enforcement of `agentPolicy`.**
  When an expose carries an `expose.mcp` block,
  the gateway loads `policy.agentPolicy.allowedModels` / `deniedModels`
  and `allowedUseCases` / `deniedUseCases` and enforces them on every
  `tools/call`. Caller `model_id` and `useCase` come from the MCP
  `clientInfo` handshake. Built on Anthropic's official
  [`mcp` Python SDK](https://github.com/modelcontextprotocol/python-sdk)
  (`>=1.20,<2.0`); the prior custom JSON-RPC dispatcher and stdio
  transport are deleted. Driver registry (DuckDB / Snowflake / BigQuery)
  and audit-trail integration are reused unchanged.

- **CLI overrides for ops/incident response.** New `--allow-models`,
  `--deny-models`, `--allow-use-cases`, `--deny-use-cases` flags on
  `fluid mcp output-port serve`. CLI values replace the contract values
  entirely (not merged); the audit event records `policySource: "cli"`
  vs `"contract"` so an audit reader can distinguish ops overrides from
  declared policy.

- **Sliding-window rate limit on the gateway.** Defaults to 60 calls per
  60 seconds per session; tune with `FLUID_MCP_RATE_LIMIT` and
  `FLUID_MCP_RATE_WINDOW_SECONDS`. Set `FLUID_MCP_RATE_LIMIT=0` to
  disable. Rate-limit denials emit a `RateLimitExceeded` envelope AND an
  audit event with `policySource: "rate-limit"`.

- **OTel + run_id correlation on every tool call.** Each `tools/call` is
  wrapped in a `fluid.mcp.call_tool` span carrying `fluid.run_id`,
  `fluid.tool`, `fluid.expose_id`, `fluid.model_id`, `fluid.use_case`,
  `fluid.policy_source`, `fluid.decision`, and `fluid.reason`. Spans are
  no-op when `OTEL_EXPORTER_OTLP_ENDPOINT` is unset.

- **Graceful shutdown.** SIGTERM / SIGINT install handlers that drain
  in-flight tool calls (up to 5 s) before tearing down driver
  connections. Cloud SDK connections (snowflake-connector-python,
  google-cloud-bigquery, duckdb) are closed explicitly.

- **`FLUID_AUDIT_ROOT`** env var redirects every gateway audit event to
  a custom root (e.g. a SIEM-forwarded volume). Unset → default to
  `~/.fluid/store/audit/`.

### Changed

- **`write_audit_event` no longer collides on burst writes.** The audit
  writer was previously second-precision and overwrote concurrent
  decisions on disk — disqualifying for an enterprise audit trail. Now
  uses `<timestamp>_<microseconds>-<pid>-<tag>-<seq>_<event>.json` with
  atomic rename so no two writes can clash even at thousands of events
  per second. Stress-tested with 400 concurrent writes across 8 threads
  (zero collisions).

- **Audit-event payload carries `runId` + `policySource`** in addition
  to the previous fields. Consumers that read `~/.fluid/store/audit/`
  get a consistent correlation token across all forge-cli stages and
  can distinguish CLI-override decisions from contract-driven ones.

- **Argument-summary in audit events is now redacted** through
  `fluid_build.observability.secret_redactor` (closes a side-channel
  where caller-supplied filter literals matching JWT / Stripe / GitHub /
  bearer / `k=v` shapes landed raw in audit JSON).

- **Tool-error envelopes no longer leak engine binding info to the
  wire**: the calling LLM gets a generic "see audit trail" message
  while the full annotated error (database / schema / table hints)
  lands on the operator log + audit event only.

- **Self-attestation startup banner**: when an `agentPolicy` model or
  use-case gate is configured, the CLI now writes a clear warning to
  stderr that caller `model_id` is self-attested via MCP `clientInfo`
  and that the gateway must not be exposed over an untrusted network
  until the OAuth/mTLS phase ships.

- **Multi-expose `find_expose` error** lists agent-eligible exposes
  separately so operators don't have to grep the contract to figure out
  which `--expose-id` choices the gateway can serve.

### Notes

- The MCP gateway integrates with `litellm` (already a core dep) for
  the live-LLM regression tests; no new test deps were added.
- `mcp` Python SDK pin is `>=1.20,<2.0` to bracket the `lifespan` API
  and `mcp.shared.memory` test fixtures we depend on. Tested against
  mcp 1.27.x.

## [0.8.5] - 2026-05-28

### Added (agentic world-class uplift — #155)
- **LiteLLM Router with cross-cloud fallback** — opt-in fallback chain
  (`anthropic→bedrock→vertex`) via `FLUID_LLM_FALLBACK_CHAIN`, auto-injected
  Anthropic `cache_control` points, and 3-token cost tracking (1.25× creation /
  0.10× read multipliers) persisted to `cost.json`.
- **JudgeAgent + CI judge-gate** — 6-axis CoT rubric (correctness, completeness,
  security, governance, performance, documentation) with a toggleable Self-Refine
  self-critique pass, a 10-contract eval set, and a snapshot regression gate.
- **Wave 2 enrichment** — deterministic `dbt_test_generator`, `freshness_emitter`,
  and `physical_layout` (Snowflake/BQ/Athena/Redshift) tools, surfaced via
  `fluid forge --apply-enrichment` with diff preview that never overwrites a
  user-set field.
- **Pause/resume + `fluid agents` namespace** — LangGraph-shape
  `BaseCheckpointSaver` (JSON-only file backend, no pickle); Ctrl-C writes a
  `.paused` marker that the next `fluid forge` auto-detects, plus
  `fluid agents list/show/prune` with safe-by-default archiving.
- **15-class PII classifier** wired into both catalog and JDBC intake paths
  (pattern borrowed from Presidio / piicatcher / GCP DLP / AWS Glue).
- **Catalog-driven model registry + 3-tier memory** — static model tables
  retired in favour of `cli/llm_models.json` (weekly-refreshed); personal/team/project
  memory with a documented precedence ladder (`fluid forge --show-memory`);
  `fluid doctor --env` enumerates the `FLUID_*` kill switches.

### Added (CC alignment — describe surface — #156)
- **`FluidSchemaManager.latest_schema_path()`** — returns the absolute path to the
  newest bundled schema JSON without hardcoding a filename.
- **`fluid_build.describe.self_describe()`** — flat, JSON-serializable snapshot of
  the installed environment (version, schema, providers, build engines, templates,
  capability flags), importable in-process by the CC backend. Capability flags are
  derived from importable backing modules (à la `pulumi about`), never hardcoded.
- **`fluid describe --self [--json]`** — human-readable summary by default, JSON
  with `--json`.

### Fixed
- JDBC PK/FK/CHECK extraction routed through `postgres_query()` / `mysql_query()`
  pass-throughs (DuckDB's `information_schema` union drops FK rows).
- Snowflake DDL honours `osi.datasets[].fields[].data_type` with parameterised
  `NUMBER(p,s)`; DV2 emits one expose per hub/link/sat.
- Catalog adapter paths corrected (DMM `/api/dataproducts`; DataHub
  `graph.get_urns_by_filter`) with description preservation at the
  `_translate_catalog_table` chokepoint.

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
## [0.8.3] — 2026-05-25

First stable release on the `0.8.x` line after `v0.8.0`. Folds five
post-`v0.8.0` PRs — ODCS / Bitol ODPS bidirectional provider with
SSRF-hardened HTTP (#136), CLI dispatch + smoke hardening (#137),
unified catalog registry with world-class DataHub + Data Mesh Manager
lineage (#138), tier-0 import-hygiene SSRF gate with `import-linter`
contracts (#139), and the OpenTofu autogenerator that recasts
`fluid apply` for cloud providers as a contract compiler (#140) — and
ships the previously-pre-released `v0.8.3rc1` plugin extension points
+ v0.7.3 acquisition-pattern engine as stable. `pip install
data-product-forge` resolves to this line by default. Stacks on the
beta-tier v0.7.3 acquisition runners and the EngineRuntime registry
released in [0.8.2] below — no schema break vs `v0.8.0`.

### Added

- **OpenTofu autogenerator — `fluid apply` becomes a contract compiler
  for cloud providers** (#140). New `fluid_build/iac/` module: modular
  `IacProviderPlugin` per cloud (dbt-adapter pattern), built-in plugins
  for **AWS / GCP / Snowflake**. The cloud providers compile the
  contract to a deterministic OpenTofu `main.tf.json` and delegate
  apply / state / drift / idempotency to the `tofu` binary; `local`
  keeps its native apply. New CLI surface: `fluid generate iac
  <contract>` (review-only emit) and `fluid apply` auto-routes cloud
  providers through `iac.cutover.resolve_engine`. The plan-binding
  integrity gate from the native engine is replicated at
  `_apply_opentofu_engine.py::_verify_plan_binding_for_opentofu`. The
  brownfield `tofu import` path is wired for all three plugins via
  `discover_imports`. Architecture doc at `AUTOGEN_SPIKE.md`;
  three-tier coverage matrix at `HONESTLY_TESTED.md`.

- **Tier-0 SSRF gate + import-linter architecture contracts** (#139).
  The canonical post-DNS-resolution SSRF check
  (`_hostname_is_private` — RFC1918 + link-local 169.254.0.0/16 +
  loopback + reserved + IPv4-mapped IPv6 unwrap, fails closed on DNS
  errors) moved to the new `fluid_build/_net.py` tier-0 leaf so
  `observability/reporter.py` can use it without importing
  `build_runners` (closes the cycle that previously broke
  `cli/__init__.py` import). Two declarative `[tool.importlinter]`
  contracts in `pyproject.toml` gate the architecture in CI:
  (1) `observability ↛ build_runners`, (2) `_net` is tier-0 (no
  `fluid_build.*` upstreams). Wired into the pre-commit hook and a
  new `import-hygiene` CI job. Four subprocess-isolated regression
  tests in `tests/observability/test_import_hygiene.py`.

- **Unified catalog registry + world-class DataHub + DMM lineage**
  (#138). One registry (`fluid_build/build_runners/catalog_registrars/
  __init__.py::build_registrar`) instantiates every backend from a
  uniform `CatalogPublicationPayload`. DataHub emits canonical MCPs
  with full schema + ownership + tags + descriptions; Data Mesh
  Manager emits proper `SourceSystem` lineage links rather than the
  prior flat dataset list. The retired Glue + Snowflake Horizon
  publish registrars folded their metadata-enrichment into the IaC
  emit (PR #140) — single source of truth, full drift detection,
  zero out-of-band registrar writes.

- **ODCS + Bitol ODPS bidirectional provider + `fluid forge --seed-from`
  pre-processor** (#136). `BitolOdpsProvider` (registered as
  `odps_bitol`, alias `odps-standard`) emits the canonical Bitol
  fragments layout (1 ODPS doc + N sibling `<contractId>.odcs.yaml`
  files) and the linking invariant `port.contractId == odcs.id` is
  asserted in tests. `ContractResolver` resolves port `contractId`
  references through local probes + opt-in http(s) fetch with the
  full SSRF guard (see Security). `fluid opds import` accepts three
  entry shapes — single ODPS, directory bundle, lone ODCS — and
  `fluid opds export --spec bitol-1.0.0|odpi-4.1` dispatches between
  the two output specs. `fluid forge --seed-from <path>` accepts an
  ODCS contract, Bitol ODPS product, or a directory bundle as
  structural seed for the copilot. The ODCS provider was modularised
  under `providers/odcs/` with paired `to_fluid()` / `to_odcs()`
  mappers and per-level `odcs_passthrough` buckets for lossless
  round-trip; `roundtrip_check()` returns a structured diff used by
  tests and the forge ground-truth guard.

- **Plugin extension points** — three entry-point groups discovered
  via `importlib.metadata.entry_points()` (graduated from
  `v0.8.3rc1`):
  - `fluid_build.commands` — register `fluid <name>` subcommands at
    CLI bootstrap (`cli/bootstrap.py`).
  - `fluid_build.extension_validators` — validate `contract.extensions`
    sub-keys during `fluid validate`. Errors fold into the
    `ValidationResult` namespaced under `extensions.<ep-name>`.
  - `fluid_build.apply_hooks` — apply-time invariants during
    `fluid apply`. Plugins receive `copy.deepcopy(contract)` and
    cannot mutate the live reference.
  All three trap exceptions, pre-redact them via `redact_secret_text`,
  and report them as errors — they cannot crash the CLI. Companion
  packages `data-product-forge-sdk==0.9.0` (Beta) and
  `data-product-forge-custom-scaffold==0.1.0` (Beta) plug in via
  these hooks.

- **`--force-pattern-drift` flag on `fluid apply`** — override gate
  for the scaffold-pattern drift detector when an intentional
  re-scaffold needs to bypass it (graduated from `v0.8.3rc1`).

- **`contract.extensions` schema field** in `fluid-schema-0.7.3.json`
  — optional top-level object with `additionalProperties: true`,
  validated per-sub-key by extension-validator plugins.

- **`fluid stats` aggregator** with `--by provider/type/engine`,
  `--since <spec>`, and `--json` over `.fluid/agents/*/cost.json`
  (cross-run cost telemetry, useful for budget tracking).

### Security

- **`fluid_build.util.safe_http` — shared SSRF guard for every HTTP
  fetch** (#136). One factory (`safe_httpx_client`) routes all
  outbound http(s) calls through: scheme allowlist + private /
  loopback / link-local / CGNAT / 6to4 / NAT64 / ORCHIDv2 / IPv6-SR /
  RFC-TEST-NET filter + IPv4-mapped IPv6 unwrap (closes a Python
  3.10/3.11 bypass — stdlib `is_private` only recurses into
  IPv4-mapped in 3.12+) + reject-all on mixed-public+private DNS +
  connection-layer DNS pin (via httpx's `sni_hostname` extension) +
  `follow_redirects=False` default + streaming body cap (10 MiB).
  Migrated seven fetch surfaces in one pass: `ContractResolver`,
  `KafkaConnectRestClient` + its schema-registry client, the Airbyte
  REST client, all five catalog registrars (Glue / DataHub /
  OpenMetadata / Unity / Snowflake Horizon / DataMesh Manager), the
  Databricks auth-provider's API check, and the schema-manager remote
  fetcher. Borrow-before-build receipts (CIDR list from
  `requests-hardened` BSD-3; httpx DNS-pin from the maintainer-blessed
  `sni_hostname` extension docs) are in
  `fluid_build/util/safe_http.py`'s module header.

- **Plan-binding gate replicated in the OpenTofu engine** (#140).
  `_apply_opentofu_engine.py::_verify_plan_binding_for_opentofu`
  mirrors the native engine's stage-7 `bundleDigest` + `planDigest`
  verification — a tampered `plan.json` is rejected before any
  `tofu apply` for every cut-over cloud (AWS / GCP / Snowflake).
  `--no-verify-plan-binding` is the emergency escape hatch and logs
  at WARNING.

- **Operational hardening on the OpenTofu engine** (#140): per-tofu
  subprocess timeout (default 1800s, override via
  `FLUID_TOFU_TIMEOUT_SECONDS`); `require_tofu_version()` floor at
  1.6.0 (catches the silent `terraform`-on-PATH-as-`tofu` mixup);
  `--allow-data-loss` override now emits a WARNING log + a structured
  `opentofu_destructive_gate_override` event for CI log-scrapers.

- **Apply hooks receive `copy.deepcopy(contract)`** rather than the
  live reference (graduated from `v0.8.3rc1`). A buggy or malicious
  hook cannot mutate the contract the rest of apply or other hooks
  consume.

- **Plugin exception text and plugin-supplied error strings are
  pre-scrubbed with `redact_secret_text`** before reaching logs or
  the errors list (graduated from `v0.8.3rc1`). The
  `SecretRedactingFilter` only scrubs args bound to `password=%s`-style
  template tokens; plugin exceptions are free-form text that can
  carry credential-shaped substrings anywhere. Applied uniformly
  across `cli/apply.py`, `cli/validate.py`, `cli/bootstrap.py`.

- **`bootstrap.py` imports `redact_secret_text` at module top**
  rather than nested inside an except branch — closes a
  defense-in-depth gap surfaced by security review.

### Changed

- **BREAKING (caller API) — `allow_remote` defaults to `False`
  across CLI + library** (#136). `fluid opds import` and
  `fluid forge --seed-from` no longer fetch http(s) `contractId`
  references unless `--allow-remote` / `--seed-allow-remote` is
  passed explicitly. Python callers of
  `BitolOdpsProvider().import_contract(...)`,
  `BitolOdpsProvider().import_directory(...)`, `ContractResolver(...)`,
  and `forge_copilot_seed.load_seed(...)` must now pass
  `allow_remote=True` for the previous behaviour. `--no-remote` and
  `--seed-no-remote` remain as hidden no-op aliases.

- **CLI dispatch wired on `stats` and `generate-pipeline` subcommands**
  (#137). Both subparsers now carry `set_defaults(func=run)` so
  `fluid stats` and `fluid generate-pipeline` dispatch to the
  implementation instead of falling through to the
  no-subcommand-selected help guide. Pinned by a smoke test that
  parses every registered subparser and asserts the `func` attribute
  is set.

- **Catalog registrar retirement — Glue + Snowflake Horizon** (#140).
  `fluid_build/build_runners/catalog_registrars/{glue,snowflake_horizon}.py`
  (~514 LOC of boto3/HTTP push) deleted; the catalog metadata they
  used to write at publish time (table descriptions, per-column
  comments, FLUID classification tags, contract YAML) is folded into
  the IaC emit (`iac/providers/aws.py::_emit_glue` for Glue +
  `iac/providers/snowflake.py::_build_horizon_table_comment` for
  Horizon). One source of truth, full drift detection by `tofu plan`,
  no out-of-band registrar writes that fight IaC state. The
  `acquisitionCatalog.register` schema enum drops `glue` and
  `snowflake_horizon` accordingly; `datahub`, `openmetadata`, and
  `datamesh_manager` registrars remain (the first two have community
  Terraform providers we may adopt later; DMM has no provider).

- **ODCS schema validation default-on with warn-on-fail** (#136).
  Vendored ODCS v3.1.0 JSON Schema runs on every export by default
  (`ODCS_VALIDATE=true`); failures warn rather than raise. Hard fail
  via `ODCS_VALIDATE_STRICT=true` or by calling `validate_contract()`.

- **ODCS type table is now exhaustive against the FLUID 0.7.3 column-type
  enum** (79 types) (#136). Drift guard in
  `tests/providers/test_odcs_type_mapping.py` fails CI if a new FLUID
  schema type is missing from `_FLUID_TYPE_TO_ODCS_LOGICAL`.

- **Bitol ODPS input ports — three-source merge** (#136).
  `ports.py::to_odps()` walks `consumes[]` (FLUID 0.7.2 canonical),
  `builds[]` (SDP source streams), and `expects[]` (FLUID 0.7.1
  legacy) — de-duped by name. Uses upstream's
  `util.contract.consumes_to_canonical_ports` +
  `builds_to_canonical_input_ports` for the normalisation.

### Test coverage

- **40+ new IaC test files at `tests/iac/`** (#140) — three-tier
  ladder: Stage 1 unit + `tofu validate` (creds-free, every PR),
  Stage 2 Docker emulators (LocalStack for AWS + goccy/bigquery-emulator
  + fsouza/fake-gcs-server + gcloud pubsub for GCP, every PR), and
  Stage 3 real-cloud OIDC keyless (nightly + manual; ~30 tests on
  real AWS, ~19 on real GCP, ~40 on real Snowflake). New
  `.github/workflows/iac-tests.yml` runs the ladder; the existing
  `ci.yml` Stage 2 path skips LocalStack cleanly when the optional
  `LOCALSTACK_AUTH_TOKEN` secret isn't configured.

- **16 unit tests for the brownfield `discover_imports` shape**
  across AWS + GCP + Snowflake plugins, with documented hashicorp/{aws,
  google,snowflake} import-id formats; plus live brownfield tests for
  AWS + GCP (pre-create resources out-of-band, run apply, verify
  adoption).

- **4 subprocess-isolated import-hygiene regression tests** at
  `tests/observability/test_import_hygiene.py` (#139) — including the
  original failing import path, sys.modules introspection after
  `observability` load, and assertions that `fluid_build._net` does
  not pull any other `fluid_build.*` module.

- **`tests/test_cli_plugin_hooks.py`** — 19 tests pinning every
  behaviour on the plugin trust surface (graduated from
  `v0.8.3rc1`).

### Upstream issues filed / resolved

- [snowflakedb/terraform-provider-snowflake#4775](https://github.com/snowflakedb/terraform-provider-snowflake/issues/4775)
  — filed 2026-05-25, **resolved upstream same day** by maintainer
  @sfc-gh-kwasilewski. The `snowflake_tag_masking_policy_association`
  resource was deprecated in v0.99.0 and removed in v1.0.0; the
  binding moved INTO `snowflake_tag.masking_policies` (set of
  fully-qualified names). Trello card created for implementing
  this in the Snowflake IaC plugin.
- [goccy/bigquery-emulator#484](https://github.com/goccy/bigquery-emulator/issues/484)
  — filed 2026-05-25. `tofu apply` via the hashicorp/google provider
  crashes on dataset read-back ("Plugin did not respond"). Pinned as
  an xfailed test until upstream resolves.
- Two LocalStack Pro quirks (Lambda V2 docker-in-docker socket
  reachability + LF DataLakeAdmin GrantPermissions auth) — drafts at
  `docs/upstream-issues/localstack-*.md`. The public LocalStack repo
  was archived 2026-03-23 and the Pro repo is private; the drafts
  point operators at LocalStack's support portal and Slack.

### Notes

- Companion plugin ecosystem packages on PyPI:
  - `data-product-forge-sdk` (0.9.0, Beta) — reference ABCs +
    conformance harnesses for the three entry-point groups.
  - `data-product-forge-custom-scaffold` (0.1.0, Beta) — reference
    `CustomScaffold` engine; Jinja+YAML or Python-plugin bundles.
- All six ingestion engines (`duckdb`, `airbyte`, `meltano`, `dlt`,
  `kafka-connect`, `debezium`) remain GA — no schema break vs
  `v0.8.0`. The v0.7.3 acquisition-pattern engine documented in
  [0.8.2] graduates to stable in this line.
- Cloud-provider apply paths now route through the OpenTofu engine
  by default. Operationally this means: install `tofu` (≥ 1.6.0)
  alongside `data-product-forge`; otherwise `fluid apply` against
  AWS / GCP / Snowflake fails fast with a clear error. The `local`
  provider is unaffected.

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

Tooling release. No user-facing CLI behavior changes; ships a refactored
release pipeline and a few packaging fixes.

### Changed
- **Dynamic versioning via `setuptools-scm`.** The wheel's version is now derived from the git tag at build time (`v0.7.11a1` → wheel `0.7.11a1`). Removes the static `version = "..."` literal from `pyproject.toml`, the `__version__ = "..."` literal from `fluid_build/__init__.py`, and the `base_version` field from `fluid_build/build-manifest.yaml`. `fluid_build.__version__` now reads from `importlib.metadata.version("data-product-forge")`. Closes the version-drift bug that produced a `0.7.10` wheel for a `v0.7.10a1` tag.
- **Release pipeline restructured to sequential TestPyPI → PyPI promotion.** Every release tag is now published to TestPyPI first, install-verified in a fresh venv, and only promoted to real PyPI if the TestPyPI smoke test passes. Pre-release tags (`a*`/`b*`/`rc*`/`dev*`) stop after TestPyPI verify; stable tags continue to PyPI + a final PyPI install verify. Closes the gap where stable releases bypassed TestPyPI entirely.
- **Concurrency control** on the release workflow (`group: release, cancel-in-progress: false`) so two simultaneous tag pushes can't race PyPI uploads.
- **Pytest markers replaced inline `--deselect` flags.** Tests that fail in specific environments (GHA runner detection, Python 3.12 import-cycle issues) now carry explicit `@pytest.mark.skipif` / `@pytest.mark.xfail` markers in the test files themselves with a documented `reason=`. The release workflow's pytest invocation is back to a clean one-liner.
- **README image and LICENSE link rewritten to absolute GitHub URLs** so the PyPI / TestPyPI project pages render the Fluid Forge logo and the License badge links to the actual file (relative paths 404 on PyPI).

### Fixed
- **Version-mismatch verify-install failure** on pre-release tags (the root cause of the v0.7.10a1 incident) — fixed by the setuptools-scm migration above.

Patch release covering post-0.7.9 work that accumulated in the `Unreleased`
section. No breaking changes — everything here is additive, internal tidy-up,
or hardening.

**Heads-up for users: the PyPI distribution name is changing.** Starting with
0.7.10, the package publishes as **`data-product-forge`** instead of `fluid-
forge`. Update your install command:

```bash
# old
pip install fluid-forge

# new
pip install data-product-forge
```

The import path (`import fluid_build`), the CLI entry point (`fluid`), and all
internal provider identifiers (Snowflake query tags, Airflow DAG owners,
`managed-by` labels) stay as `fluid-forge` — those are runtime/audit
identifiers, not user-facing names. The CLI's Docker image on GHCR also aligns
with the GitHub repo name: `ghcr.io/agenticstiger/forge-cli`.

Also the first release published from the `Agenticstiger/forge-cli` repository
via the Trusted-Publishing release pipeline.

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

[Unreleased]: https://github.com/Agenticstiger/forge-cli/compare/v0.14.0...HEAD
[0.14.1]: https://github.com/Agenticstiger/forge-cli/compare/v0.14.0...v0.14.1
[0.14.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.13.1...v0.14.0
[0.13.1]: https://github.com/Agenticstiger/forge-cli/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.10.2...v0.11.0
[0.10.2]: https://github.com/Agenticstiger/forge-cli/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/Agenticstiger/forge-cli/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.11...v0.9.0
[0.8.11]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.10...v0.8.11
[0.8.10]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.9...v0.8.10
[0.8.9]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.8...v0.8.9
[0.8.7]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.6...v0.8.7
[0.8.6]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.5...v0.8.6
[0.8.5]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.4...v0.8.5
[0.8.4]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.3...v0.8.4
[0.8.3]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.0...v0.8.3
[0.8.8]: https://github.com/Agenticstiger/forge-cli/compare/v0.8.7...v0.8.8
[0.8.0]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.10...v0.8.0
[0.7.11]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.10...v0.7.11
[0.7.10]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.9...v0.7.10
[0.7.9]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.8...v0.7.9
[0.7.8]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.7...v0.7.8
[0.7.7]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.6...v0.7.7
[0.7.6]: https://github.com/Agenticstiger/forge-cli/compare/v0.7.1...v0.7.6
[0.7.1]: https://github.com/Agenticstiger/forge-cli/compare/v0.5.7...v0.7.1
[0.5.7]: https://github.com/Agenticstiger/forge-cli/releases/tag/v0.5.7
