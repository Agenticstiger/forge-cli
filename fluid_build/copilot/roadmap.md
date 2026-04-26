# forge-cli roadmap

forge-cli v1.0 shipped 2026-04-23. The Lean v1 scope previewed several v1.1+
milestones early; they are listed under "Shipped in v1.0" below and have been
retargeted out of the upcoming-milestone track so the banner and `fluid
roadmap` point at the next truly unshipped milestone.

## Shipped in v1.0 (previewed from v1.1+ roadmap)

The following milestones were pulled forward into v1.0 and need no further
work:

- **v1.1 — Team Collaboration** · shared cache backends (`SqliteBackend`,
  `PostgresBackend`, `VectorBackend`), `audit_trail.write_audit_event()` wired
  into `fluid forge data-model`, and `history.archive_snapshot()` for
  per-artifact versioning. Typed exception hierarchy is tracked as a separate
  v1.1.x follow-up.
- **v1.3 — Fine-Grained Agents** · five-agent split actively used by
  `StageCoordinator` (`LogicalAgent`, `BuilderAgent`, `ReadmeAgent`,
  `TransformationAgent`, `ValidatorAgent`). `ModelerAgent` / `ConceptualAgent`
  remain as internal composition helpers — direct use is deprecated in
  favour of `LogicalAgent`.
- **v1.4 — Richer CLI Surface** · `fluid forge data-model diff`,
  `--emit-dimensional-variants`, `--emit-ddl-dir`, `--emit-osi-sidecar`
  (default on), and `--deterministic` all ship in v1.0.

### v1.0.1 follow-up (landed 2026-04-24)

Sprint-local hardening that shipped the day after v1.0, driven by live
Gemini biz-lab Phase-4 findings:

- **Retry envelope on every staged LLM call** — `BaseStageAgent.call()`
  now wraps provider dispatch in `retry_with_backoff` (3 attempts,
  exponential backoff, jitter). Closes plan gap A3.
- **Provider-agnostic "thinking" UX** — new `cli/progress.py`
  `AgentStatus` context manager renders a `rich.Live` panel while the
  provider call is in flight. Self-disables on non-TTY, `FLUID_QUIET=1`,
  `FLUID_NO_TUI=1`, or `FLUID_NONINTERACTIVE=1`.
- **OpenAI tier ordering reconciled** — `cli/llm_models.json` now pairs
  `balanced=gpt-4.1-mini` with `fast=gpt-4.1-nano` for the OpenAI
  provider, aligning the `tiers.openai` map with the `providers.openai`
  routing defaults. Closes plan gap A6.
- **`--quiet` flag honoured at every banner surface** — `forge.py`,
  `generate_speed_transformation.py`, and `ai_setup.py` all pass
  `quiet=getattr(args, "quiet", False)` through to `print_v2_banner()`.
- **Minimum-coverage non-negotiable on `dimensional.yaml`** — the prompt
  now requires `facts[] ≥ 1` and `dimensions[] ≥ 2`; vacuous output is
  rejected, and the prompt tells the LLM to infer a skeleton from
  canonical models (NRF ARTS, ISO 20022, TMF SID, HL7 FHIR) when the
  intent is thin.
- **Defense-in-depth skeleton seeding** — `ModelerAgent` post-processes
  every LLM result and transplants `seed_dimensional_skeleton` /
  `seed_dv2_skeleton` from the active `IndustryPack` when the returned
  `DimensionalModel` / `DV2Model` is still vacuous (Gemini's OpenAPI-3.0
  mode occasionally returns empty arrays even with the strengthened
  prompt). A warning is logged so operators can see when the backfill
  fires.
- **`--deterministic` regression-pinned** — new
  `tests/test_deterministic_flag.py` pins that the flag forces
  `tiered=False` + `no_cache=True`, that the heuristic path is
  byte-stable across runs, and that the audit payload records the flag.
- **`emit/ddl.py` field-name bug fixed** — `.logical_type` →
  `.data_type`; surfaced by the new end-to-end emit tests.
- **Phase-4 biz-lab scenario runner hardened** — incremental flush after
  each scenario (atomic `.tmp` + `os.replace`) plus a 1200s timeout so
  partial results survive a mid-run stall.
- **Phase 6 (dbt parse gate) shipped + bug fix** — previously blocked
  pending cloud-warehouse access; unblocked by targeting the local
  DuckDB profile that `engines/dbt/profiles.py` emits by default.
  Manual validation: retail dimensional + telco DV2 intents both forge
  → generate → `dbt parse` clean. Inline fix to
  `generate_speed_transformation._run_dbt_parse_gate`: conditionally
  inject `--profiles-dir <output_dir>` when the generator emitted a
  project-local `profiles.yml`, so fresh users without `~/.dbt/`
  don't see the gate fail out of the box. Pinned by two new unit
  tests (branch: profiles.yml present vs absent) plus a real
  end-to-end integration test (`tests/test_speed_transformation_dbt_e2e.py`)
  that auto-skips when `dbt` is not on `PATH`.

### v2-gap close-out (landed 2026-04-24)

After the v1.0.1 hardening above, the branch closed eight additional
items from the "True v1 gaps" + early-v2 list so nothing carries
forward as tech debt. The roadmap marker for v1.1 still names "typed
exceptions as a v1.1.x follow-up"; that specific follow-up is closed
below. Directive from the user: "nothing left behind."

- **F1 — Typed exception hierarchy (plan names)** · `FluidGenerationError`,
  `DDLGenerationError`, `AgentExecutionError` exported from
  `fluid_build.copilot.agents.errors`. Pinned by
  `tests/copilot/test_agent_errors.py`. Closes the v1.1.x follow-up
  called out in the v1.0 section above.
- **F2 — Handoff doc correction** · `docs/v1.0.1-handoff.md` no longer
  claims the legacy memory migration print is silent — the shim has
  printed a one-shot stderr notice since v1.0.
- **F3 — Four cross-technique industry skeletons** · `telco/dimensional`,
  `retail/data_vault_2`, `healthcare/dimensional`,
  `finance/one_big_table`. Users who force a non-default technique
  via `--technique` now get a seeded skeleton instead of cold
  invention. Pinned by `tests/copilot/test_industry_cross_technique_skeletons.py`.
- **M1 — Parallel physical-stage fanout** ·
  `StageCoordinator._run_physical_stages` runs builder ∥ readme ∥
  transformation on a 3-worker `ThreadPoolExecutor`, preserving OTEL
  span parenthood via per-submission `contextvars.copy_context()`.
  Escape hatch: `FLUID_COPILOT_PARALLEL_PHYSICAL=0`. Pinned by 18
  tests (`tests/copilot/test_coordinator_parallel_readme.py`) that
  use a `threading.Barrier(3, 5.0)` to prove actual concurrency.
- **M2 — OSI child-level fields** · per-entity `ai_context` and
  `custom_extensions` on Dataset, `unique_keys` on Dataset, `label`
  on Field. Fully backward-compatible (all new fields default to
  empty / None). Pinned by
  `tests/copilot/test_osi_child_level_fields.py`.
- **M3 — Targeted repair loop** · when the validator rejects a
  physical draft, `_diagnose_failing_stage` maps
  `ValidationFinding.field` back to `"builder"` /
  `"transformation"` / `"logical"` / `"readme"`, and
  `_maybe_repair_physical` re-runs only the blamed physical stage
  with `session.no_cache=True`. Bounded at one retry; logical-scope
  failures are diagnosed (for telemetry) but not repaired in v1.0.
  Pinned by `tests/copilot/test_coordinator_targeted_repair.py`
  (38 tests: 27 diagnosis-table rows + 9 coordinator integration +
  2 module-constant pins).
- **B1 — Semantic retrieval on cache-miss prompts** ·
  `ModelerAgent` injects the top-3 `memory/semantic` matches into
  every cache-miss LLM user_prompt. Read-only (no auto-write), and
  all three graceful-degradation paths (empty store, search failure,
  no-store session) fall through with no crash and no empty-list key.
  Pinned by `tests/copilot/test_modeler_semantic_retrieval.py`.
- **B2 — Optional embedding-backed VectorBackend** · `use_embeddings=True`
  on the opt-in path; gated on `pip install "data-product-forge[vector]"`.
  Missing extra → warn-and-fallback; present extra → hash-based
  ranking that token-overlap-heavy records score above
  substring-overlap records. Pinned by
  `tests/copilot/test_vector_backend_upgrade.py`.

**Still deferred (not this close-out):**
- **Phase 5 — Snowflake biz-lab e2e** — blocked on external creds.
- **Gate 0 — Gemini key rotation** — user declined.

### V1 world-class hardening (landed 2026-04-25)

Closes the rest of the original-plan gaps that were "shipped without
tests" and pins the v1.0 public API surface so v1.x users can rely on
their imports surviving any internal refactor.

- **Plan gap A2 closed** — `forge_datamodel/dv2/hash_keys.py` and
  `naming.py` are now pinned by
  `tests/forge_datamodel/test_dv2_hash_keys.py` (23 tests:
  determinism + algorithm switch + order-sensitivity + null/empty
  canonicalisation + custom delimiter/null-token + numeric coercion
  + empty-input edge case + algorithm validation) and
  `tests/forge_datamodel/test_dv2_naming.py` (22 tests: slug rules
  + prefix idempotence + multi-entity link ordering + standard prefix
  enumeration). The earlier "deferred to v1.1" entry is retracted.
- **Plan gap A5 closed** — `forge_datamodel/from_ddl/profiler.py`
  pinned by `tests/forge_datamodel/test_profiler.py` (21 active
  tests + 3 pyarrow-conditional Parquet round-trip tests). Soft-fail
  on missing optional deps verified via import-shim test.
- **MCP Server (originally v1.5) shipped early** — `cli/mcp.py`
  (459 LOC + 41 tests in `tests/test_mcp_cmd.py` and
  `tests/test_mcp_permission_policy.py`). The remaining v1.5 work
  (versioning under `~/.fluid/store/history/`, `--read-only` flag,
  sample `claude-code-mcp.json` config docs) carries forward as a
  v1.5.x follow-up. The next-milestone banner therefore points at
  v1.2 Semantic Reuse, then v1.5 MCP follow-up.
- **`--quiet` parser registration completed at every banner surface**
  — `forge data-model from-ddl|from-intent|validate|diff|dump-ddl`,
  `generate speed-transformation`, `ai setup|status`, and the
  `init copilot` banner path (via `quiet=` kwarg threaded through
  `run_adaptive_copilot_interview`). Previously the env-var
  (`FLUID_QUIET=1`) was the only working suppression route at
  several surfaces; the `--quiet`/`-q` flag is now actually parsed
  and propagated. Pinned by 19 tests in
  `tests/test_quiet_flag_cli_e2e.py`.
- **Banner auto-expiry pinned** — `tests/test_forge_banner.py` now
  monkeypatches `FLUID_BANNER_TODAY` to confirm the banner silently
  vanishes from 2026-05-07 onward without code change. Plus
  positive-control + suppression-precedence tests for every env-var
  / kwarg combination.
- **Provider determinism payloads pinned** —
  `tests/test_provider_determinism_payloads.py` asserts every
  registered LLM provider's `build_request` puts `temperature=0`
  (and `seed=42` where supported) into the HTTP payload.
  Surfaced and fixed a Claude-only regression: `AnthropicProvider`
  was omitting `temperature`, silently inheriting the API default
  of 1.0 — `--deterministic` on Claude was de-facto non-deterministic.
  The Anthropic API does not yet expose a public `seed`, so OpenAI
  remains the strongest determinism surface (audit metadata records
  this provider distinction).
- **Typed exception adoption** — `BaseStageAgent.call()` and
  `forge_datamodel/from_ddl/snowflake_dumper.py` now raise the
  plan-named `AgentExecutionError` / `DDLGenerationError` instead of
  bare `RuntimeError`. Pinned by
  `tests/copilot/test_typed_exception_adoption.py` (5 tests
  including a static source-grep regression that fails on any new
  `raise RuntimeError(` reappearing in those files).
- **Public-API stability snapshot** —
  `tests/test_public_api_stability.py` freezes 84
  `(module, symbol)` pairs from the v1.0 surface across agents,
  schemas, store, industry, DV2, banner, typed exceptions, and CLI
  dispatch. Removing or renaming a v1.0 public symbol fails this
  test loudly, forcing an explicit deprecation cycle (or a v2 bump).
- **Wheel packaging fix (Option B)** — `pyproject.toml` now ships
  `cli/llm_models.json` (was silently dropped from the wheel by an
  incomplete `package-data` glob) and excludes
  `providers/**/test_*.json` / `*.yaml` fixtures (they were leaking
  into the wheel via the broad `providers/**/*.json` glob).
  Verified by rebuilt wheel inspection + fresh `pip install` smoke.

### dbt Mesh preservation constraints (shipped alongside v1.3)

These constraints are intentionally locked in the v1.0 engine layer so future
milestones do not regress dbt Mesh readiness:

- `access: public` must be emitted on every dbt model that participates in
  cross-project references. `access: protected` is reserved for project-local
  staging and should not leak into exposed artefacts.
- `latest_version` + explicit `versions[]` metadata must accompany any
  versioned model; the builder stage fails closed if a versioned model omits
  either field.
- A `dependencies.yml` file must be emitted alongside the dbt project
  whenever cross-project refs are detected. The file lists `projects[]` with
  name + version, feeding dbt Cloud's cross-project mesh resolver.
- The `--mesh-hub` flag is **dbt-only**; other engines (spark, sql, dataflow,
  glue) log a one-line warn-and-drop notice and strip mesh metadata rather
  than erroring. This keeps mesh-enabled contracts portable to non-dbt
  targets without silent feature drift.

Any v1.5+ refactor that touches `engines/dbt/models.py`, `schema_yml.py`, or
the artifact fanout must re-verify these four invariants.

## Milestone v1.2 — Semantic Reuse
Target date: 2026-05-07
- Semantic and episodic memory search
- Similar-model retrieval for staged modeling

## Milestone v1.5 — MCP Server
Target date: 2026-06-11
- MCP tool surface for logical model editing, regeneration, validation, and semantic search
