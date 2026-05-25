# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Orientation

Start with [`AGENTS.md`](AGENTS.md) — it's the canonical agent guide (project structure, key entry points, `contract.fluid.yaml → validate → plan → apply` lifecycle, agent-policy schema, forge-agents). This file only captures Claude-Code-specific operational notes and what's changed recently.

The CLI is `fluid` (aka `python -m fluid_build.cli`). The package is `fluid_build/`; tests mirror that tree under `tests/`.

## Running tests, lint, format (what actually works in this checkout)

Global `pip install` fails on macOS with PEP 668. Use the project-local venv directly:

```bash
# Tests
.venv/bin/python -m pytest                                            # full suite
.venv/bin/python -m pytest tests/test_security.py -v                  # one file
.venv/bin/python -m pytest tests/test_security.py::TestPathTraversalPublicApi::test_validate_input_path_rejects_raw_traversal -v   # one test
.venv/bin/python -m pytest -m "unit and not slow"                     # marker filter

# Lint / format (line-length is 100, set in pyproject.toml)
.venv/bin/ruff check fluid_build tests
.venv/bin/black --check fluid_build tests
.venv/bin/black fluid_build tests                                     # auto-format
```

Pytest markers declared in `pyproject.toml`: `unit`, `integration`, `slow`, `smoke`, `aws`, `gcp`, `snowflake`, `provider`, `datamesh`, `policy`, `runtime`.

The Makefile (`make test | lint | fmt | typecheck | doctor | demo`) is the happy path but can fall back to system Python if `.venv` isn't present — prefer the explicit `.venv/bin/…` invocations for deterministic results.

## Architecture (the parts that take reading multiple files to learn)

- **Contract-driven pipeline.** A single `contract.fluid.yaml` (schemas in `fluid_build/schemas/`, versioned) feeds `validate → plan → apply`. Providers under `fluid_build/providers/{local,gcp,aws,snowflake,odps,odcs}/` each implement the same interface; swapping `binding.platform` swaps the compiled output without touching the contract.

- **Copilot agent loop** (`cli/forge_copilot_agent_loop.py`) runs a multi-turn LLM tool-use loop. Tools register into `cli/forge_copilot_tools.py::TOOL_REGISTRY` with `{name, description, input_schema, impl}`. Path-accepting tools (`read_sample_schema`, `discover_workspace`) are confined to a `workspace_root` kwarg plumbed down from `run_copilot_agent_loop` → `dispatch_tool_call` (kwarg injection, not `functools.partial` on the global registry).

- **Two parallel redaction layers.** `observability/secret_redactor.py::SecretRedactingFilter` is wired into Python logging globally; `providers/snowflake/util/logging.py` has a Snowflake-provider-local `redact_string`/`redact_dict` with its own `SENSITIVE_PATTERNS`/`SENSITIVE_KEYS`. When adding a new secret shape, **extend both** to keep them symmetric.

- **SQL safety is centralized in `providers/_sql_safety.py`.** Every DDL f-string must route identifiers through `validate_ident` and string literals through `quote_string_literal`. Inline `.replace("'", "''")` is a regression — diverges from the central helper and breaks under non-default Snowflake escape settings.

## Files that need extra care

Drive-by changes here can break invariants. Read the surrounding comments first.

- `cli/security.py` — path-traversal + forbidden-paths validator. Subtle ordering: `_reject_raw_traversal` runs on the **pre-resolve** input; `_validate_path_security` runs on the **resolved** path. Cross-platform: `FORBIDDEN_PATHS` is platform-aware; macOS `/etc` resolves to `/private/etc`, which is explicitly included.
- `credentials/encrypted_store.py` — Fernet-encrypted credential store. `_load_store` raises `CredentialError` on `InvalidToken` (never silently wipes). Parent directory is chmod'd `0o700` best-effort via `_secure_parent_dir`.
- `providers/_sql_safety.py` + `providers/snowflake/governance.py` — see the SQL-safety note above. Any new DDL emitter in another provider must use these helpers.
- `cli/auth.py::_sanitize_argv` — strips credential-bearing flag values before DEBUG-logging the subprocess command. Extend `_SENSITIVE_FLAGS` / `_SENSITIVE_FLAG_SUFFIXES` for new provider-auth CLIs that use `--*-secret` / `--*-key` shapes.
- `providers/snowflake/util/logging.py::SENSITIVE_KEYS` / `SENSITIVE_PATTERNS` — mirror additions into `observability/secret_redactor.py`.

## Recent architectural changes worth knowing (2026-04-16)

A six-PR security-hardening series landed (PRs #28–#33) resolving the 14 findings in `SECURITY_REVIEW.md`. Concretely:
- Workspace confinement for copilot tools (`workspace_root` kwarg plumbed through `dispatch_tool_call`).
- Identifier + literal validation at every SQL DDL boundary.
- Platform-aware `FORBIDDEN_PATHS` + pre-resolve traversal check.
- Encrypted credential store raises on key mismatch instead of silently wiping.
- Typed tool errors (`{"error": <ExcName>, "message": "…see server logs"}`) — exception text no longer round-trips into the LLM context.
- Expanded redactor coverage for JWTs, Stripe, GitHub, and bare `key[:=]value` assignments.

## Recent architectural changes worth knowing (2026-04-30)

The `feat/source-aligned-acquisition` branch lands SDP/ADP/CDP as a Data
Mesh-aligned classification that runs alongside the medallion
`metadata.layer`. **Bronze↔SDP, Silver↔ADP, Gold↔CDP** — either-or-both
is accepted; when both are set the validator enforces consistency.
Cross-check is duplicated inline at three sites (no helper module — the
user explicitly rejected one): `schema.py::_check_metadata`,
`schema_manager.py::_validate_with_jsonschema`, and
`cli/contract_validation.py::_validate_metadata`. If the mapping ever
changes, update all three in lockstep, plus the discover emitter at
`cli/discover/emitter.py`. Templates / importers / fixtures all populate
both fields; provider tag emitters (AWS / GCP / Snowflake / forge)
propagate both into cloud labels as `fluid_layer` + `fluid_product_type`
(distinct keys, same canonical value). The marketplace surfaces both as
facets and accepts `fluid market --product-type SDP|ADP|CDP` alongside
`--layer`. Test pinning lives in `tests/test_product_type_mapping.py`.

## Recent architectural changes worth knowing (2026-04-23)

The `feat/cli-pipeline-and-publish-hardening` branch (15 commits) landed the **11-stage pipeline** and its supporting surface. AGENTS.md has the canonical lifecycle doc; the Claude-Code-specific operational notes are:

- **11 CLI stages, one command per stage.** `fluid bundle → validate → generate artifacts → validate-artifacts → diff → plan → apply → policy-apply → verify → publish → schedule-sync`. Each stage has `cli/<stage>.py`. Re-reading `AGENTS.md#the-11-stage-pipeline` is cheaper than re-deriving the flow from commit history.
- **`cli/execute.py` is gone.** Its dbt + python runners extracted to `build_runners/{dbt,python}/`. `apply --mode amend-and-build` dispatches there via `build_runners.run_builds_from_args`. If you find yourself writing `from fluid_build.cli.execute import ...` that's the old path — use `build_runners` instead.
- **`cli/compile.py` is gone too.** Renamed to `cli/bundle.py` in Phase 1; the hidden `fluid compile` alias was deleted. `fluid bundle --format tgz` is the canonical call.
- **Plan-binding is cryptographic.** `fluid plan` emits `bundleDigest` + `planDigest` into `plan.json`. `fluid apply` re-verifies both before any DDL. Helpers live in `forge/core/plan_digest.py`: `compute_plan_digest`, `inject_digests`, `verify_plan_binding`, `PlanBindingError` (`.kind` attribute is `"bundle-mismatch"` or `"plan-tamper"` — stable event tags for CI log parsers). The `--no-verify-digest` flag is the DR escape hatch and logs at WARNING level so audit trails catch it.
- **Apply actions carry BOTH `op` and `action_type` fields.** `apply.py`'s provider dispatcher reads `op`; display/viz reads `action_type`. Dropping either silently breaks the pipeline. See `plan.py::_plan_with_provider_actions` — both get `action.action_type.value`.
- **No hardcoded `"0.7.1"` fallback.** `SchemaManager.latest_bundled_version()` scans `fluid_build/schemas/fluid-schema-*.json` and returns the newest. Use it anywhere the "default fluidVersion" is needed; `cli/plan.py::_default_fluid_version()` is the canonical wrapper.
- **CI template generation got install-mode.** `fluid generate ci --system jenkins --install-mode {pypi,dev-source}`. The generated Jenkinsfile carries ONE mode's logic — no runtime branching in the file. `pypi` (default, production) exposes 4 build-time Jenkins parameters (`FLUID_PACKAGE_SPEC`, `FLUID_PIP_INDEX_URL`, `FLUID_PIP_EXTRA_INDEX_URL`, `FLUID_ALLOW_PRERELEASE`). `dev-source` (lab) uses `PYTHONPATH=/forge-cli-src` and fails LOUD if the mount is missing.
- **The Jenkins template is the reference 11-stage implementation.** `forge/core/pipeline_templates.py::JenkinsTemplate.generate()` ships the full parameterized template. The other 6 CI systems (GitHub Actions, GitLab, Azure DevOps, Bitbucket, CircleCI, Tekton) are scheduled for Phase 7-rest to port from it.
- **Smoke scripts live in `scripts/`.** `scripts/smoke_phase_6b.py` validates plan-binding + data-loss-gate against a real .venv.fluid-dev venv. `scripts/smoke_a1.py` validates the A1 variant with the new `--mode` / `--target` flags. Both auto-discover env from the launchpad; both are safe (every apply runs `--dry-run`).
- **Known gap — `unknown_action_op` (Phase 6F, deferred).** The Snowflake provider's dispatcher doesn't yet recognize the 0.7.1 high-level abstract ops (`provisionDataset`, `scheduleTask`). Stage 7 apply logs `{"event": "unknown_action_op", ...}` and no-ops instead of emitting native DDL. Pipeline reports SUCCESS but accomplishes nothing. Fix involves a translator layer in `providers/snowflake/` that maps abstract → native ops. Pre-existing, not a regression.

## Recent architectural changes worth knowing (2026-04-30, world-class forge UX)

A multi-phase push landed comprehensive UX + architecture upgrades on `feat/source-aligned-acquisition`. Key surfaces and where they live:

- **Mode picker (`fluid forge`).** Bare invocation surfaces a 5-mode menu (AI / Compose / Refine / Template / Blank) instead of dropping into AI. Lives at `cli/_forge_mode_picker.py`. Skipped via `FLUID_FORGE_NO_PICKER=1`. Picker default is highlighted by the welcome scan; user always sees the menu.
- **Welcome scan.** Detect-first parallel scan runs in <50ms before any prompt — workspace state, AI configured, CLIs installed, sample data, cloud creds, return-user state. Lives at `cli/_welcome_scan.py`. `~/.fluid/usage.json` carries `forge_count`.
- **Mode-aware interviews.** `forge_copilot_interview.py::run_adaptive_copilot_interview` short-circuits to `_run_compose_interview` (3 questions max for `--from-product`) or `_run_refine_interview` (1 question for `--refine`) when the runtime resolved upstreams or loaded an existing contract. Standard mode runs the world-class bootstrap (below).
- **World-class bootstrap.** New default fresh-product interview at `cli/_world_class_interview.py`. Inferences first (`InterviewSignals`), examples in every prompt, productType-first, `:auto` escape, schema-coverage gate. Toggle via `FLUID_INTERVIEW_LEGACY=1` to revert to the legacy bootstrap.
- **Slash commands inside the interview.** `cli/_interview_slash_commands.py` — `:ai-setup`, `:override`, `:show-work`, `:doctor`, `:help`, `:quit`. Wrapped into `forge_dialogs.ask_friendly_text` so every prompt accepts them.
- **Pre-write preview panel.** `cli/_preview_panel.py` — renders cost + file list + run-id BEFORE the writes; persists `.fluid/agents/<run-id>/{cost.json,reasoning.md,transcript.json}` so Ctrl-C at the prompt loses nothing. `--yes` skips the prompt but the panel still renders. Receipts are richened via `forge_modes.py::_populate_richer_receipt`.
- **Streaming contract preview.** `cli/_streaming_contract_preview.py` re-shapes the seed contract after every interview answer so the user sees it grow. Toggle off with `FLUID_FORGE_NO_STREAMING_PREVIEW=1`.
- **Composition pipeline.** `forge_datamodel/from_data_products/pipeline.py` resolves upstream products, validates composition rules (SDP rejects upstreams; ADP/CDP accept SDP+ADP), pre-fills `consumes[]`. Wired via `--from-product <ID|path>` (repeatable) and `--from-product-list <file>`.
- **`fluid forge --refine`** loads an existing contract, asks "what to change?", feeds the existing contract verbatim to the LLM as the seed (via `_seed_contract_override`).
- **Self-healing repair loop.** `forge_copilot_runtime.py` runs the JSON-schema validator on every emitted contract and prepends path-specific errors to the next attempt's repair feedback. Function: `forge_copilot_corrective_feedback.build_schema_validation_message`.
- **`fluid stats`.** Aggregates `.fluid/agents/*/cost.json` across runs. `--by provider/type/engine`, `--since <spec>`, `--json`.
- **Forge templates emit v0.7.3 directly.** Each of `forge/templates/{starter,analytics,etl_pipeline,ml_pipeline,streaming}.py` now uses the shared spec-driven builder at `forge/templates/_v073_builder.py`. The legacy v0.5-vintage methods are preserved as `_legacy_generate_contract_unused` for reference. The runtime coercion layer at `forge_modes.py::_coerce_template_contract_to_v073` is now a no-op for v0.7.3 contracts (fast-path) and remains as a safety net for any legacy out-of-tree template.
- **LiteLLM unified backend.** `cli/forge_copilot_llm_litellm.py` ships an opt-in `LiteLLMProvider` that subclasses `LlmProvider` and routes every provider through one API. Enable via `FLUID_LLM_BACKEND=litellm` + `pip install 'fluid-build[litellm]'`. `RunCostTracker.record_call` now accepts `usd_override` (used by litellm to feed accurate per-call cost into the cost summary). Dispatched in `forge_copilot_llm_providers.py::get_llm_provider` + short-circuits in `call_llm` and `call_llm_streaming`.
- **UX telemetry.** `cli/_ux_telemetry.py` captures `time_to_first_panel_ms`, `questions_asked`, `inferences_used`, `picker_choice`, `mode`, `preview_accepted`, `schema_repair_attempts`. Emitted onto the `forge.invocation` OTel span when an exporter is configured.
- **Mode dispatch fix.** Picking `template` now routes to `forge_modes.run_template_mode` (no AI). Picking `blank` goes to `_run_blank_mode`. Picking `refine`/`from_product` flows through the AI runtime with the right context flags. The `--template` flag is an argparse alias of `--scaffold`.
- **Datamesh-manager Silver↔Gold archetype swap fix.** `providers/datamesh_manager/datamesh_manager.py` had `Silver→consumer-aligned` and `Gold→aggregate` reversed. Now correctly reads `metadata.productType` first and falls back to the canonical layer mapping (Bronze→source-aligned / Silver→aggregate / Gold→consumer-aligned).

Test files pinning the above: `tests/test_forge_mode_picker.py`, `tests/test_welcome_scan.py`, `tests/test_world_class_interview.py`, `tests/test_interview_modes.py`, `tests/test_preview_panel.py`, `tests/test_interruptible_authoring.py`, `tests/test_phase2_tools.py`, `tests/test_from_data_products.py`, `tests/test_stats.py`, `tests/test_self_healing_repair.py`, `tests/test_litellm_backend.py`, `tests/test_ux_telemetry.py`, `tests/test_init_forge_scenarios_e2e.py` (the matrix that runs `fluid validate` on every produced contract).

Key env vars introduced or honoured by the new path:

| env var | what it does |
|---|---|
| `FLUID_LLM_BACKEND=litellm` | route every LLM call through litellm |
| `FLUID_LITELLM_MODEL_PREFIX=<x>` | override the litellm model-name prefix for niche providers |
| `FLUID_INTERVIEW_LEGACY=1` | revert to the legacy bootstrap interview |
| `FLUID_FORGE_NO_PICKER=1` | suppress the mode picker (CI / scripts) |
| `FLUID_FORGE_NO_PREVIEW=1` | suppress the pre-write preview panel + prompt |
| `FLUID_FORGE_NO_WELCOME=1` | suppress the welcome scan render |
| `FLUID_FORGE_NO_STREAMING_PREVIEW=1` | suppress the live contract growth panel |
| `FLUID_FORGE_PICKER_ALWAYS=1` | force the picker even for return users |
| `FLUID_COST_LIMIT_USD_PER_RUN=<n>` | per-run cost cap shown in the progress prefix |
| `FLUID_INTERVIEW_LEGACY=1` | use the legacy bootstrap (fallback) |

## Recent architectural changes worth knowing (2026-05-02, world-class hardening)

A multi-pass close-the-gaps push landed across providers, observability, and
streaming runners. Headline items:

- **AWS + GCP abstract op handlers route to real native ops.** The 9
  v0.7.1 abstract ops (`provisionDataset`, `scheduleTask`, `registerSchema`,
  `createView`, `grantAccess`, `revokeAccess`, `updatePolicy`, `publishEvent`,
  `custom`) translate via per-provider helpers in
  `providers/{aws,gcp}/provider.py`. Previously some translations called
  native ops the dispatcher didn't recognise — silent no-op apply. Fixed:
  - AWS adds `glue.update_table_schema`, `iam.detach_policy`,
    `iam.put_role_policy`, `events.put_events`. The dispatcher also
    accepts `events.put_rule` (alias for `ensure_rule`) and `sns.publish`
    (alias for `publish_message`).
  - GCP adds `bq.update_table_schema`, `iam.grant_role`, `iam.revoke_role`,
    `iam.set_policy`. Dispatcher accepts `ps.publish` (alias for
    `publish_message`) and `bq.execute_sql`.
  - Pin file: `tests/providers/test_abstract_op_dispatch.py` — every
    abstract handler must route to a recognised native op.
  - **Critical bug fix in passing**: `aws/provider.py` had a malformed
    method body — Athena dispatcher fell through into orphaned Redshift
    code with no proper method header. Now `_execute_redshift_action`
    is a real method.

- **Cross-CLI run-id correlation auto-stamps.** `traced_stage` decorator
  (`observability/tracing.py`) now resolves
  `fluid_build.observability.run_id.get_or_create_run_id()` and stamps
  `fluid.run_id` onto every CLI stage's root span. Every stage decorated
  with `@traced_stage(...)` auto-correlates without per-stage wiring.
  `cli/{plan,verify,publish,apply,bundle,schedule_sync}.py` carry the
  decorator; pin file `tests/observability/test_run_id_decorator_integration.py`
  asserts each one is decorated.

- **ADP auto-replay on upstream reprocess.** `build_runners/_state.py`
  `FileStateStore.set_cursor` now reads the previous cursor before
  writing the new one and routes through `build_runners._replay`:
  - `detect_cursor_rewind` — true when new < old.
  - `mark_downstream_dirty` — walks the workspace for products whose
    `consumes[]` references the rewound product, writes a JSON marker
    at `.fluid/<product>/runtime/replay-pending.json`.
  - `list_dirty_products` / `clear_dirty_marker` — lifecycle helpers.
  - Pin file: `tests/build_runners/test_replay_state_integration.py`.
  Single chokepoint integration: every runner that calls `set_cursor`
  benefits without per-runner wiring (Kafka-Connect, Debezium, DLT,
  duckdb, Meltano).

- **Streaming late-arrival policy surfaces as connector config.**
  `build_runners/_late_arrival.extract_late_arrival_policy(ctx.source,
  target_table=...)` reads `WatermarkSpec.allowed_lateness` and returns
  connector-config keys under `fluid.late_arrival.*`:
  - `fluid.late_arrival.enabled` ("true" / "false")
  - `fluid.late_arrival.allowed_lateness_seconds` (str)
  - `fluid.late_arrival.side_output_table` (`<target>__late_events`)
  Wired into `kafka_connect/runner.py` and `debezium/runner.py`. The
  side-output table name is canonical (no per-runner naming surprises).
  Pin file: `tests/build_runners/test_late_arrival_runner_integration.py`.

- **Cross-mesh federation backends ship.** `forge/federation.py` ships
  three live-fetch backends called from `fetch_federated_digest`:
  - `_fetch_digest_via_http` — plain HTTP GET against
    `<endpoint>/<product>/<version>/digest` returning `sha256:...`.
  - `_fetch_digest_via_catalog` — REST GET returning JSON
    `{"digest": "..."}`.
  - `_fetch_digest_via_git` — clones repo into
    `~/.cache/fluid/federation-git/<workspace_id>`, reads
    `<product_id>/contract.fluid.yaml`, recomputes via
    `compute_contract_digest`. Falls back to shell-out git when
    gitpython missing. Auth via env-var `secret_ref` (e.g.
    `GITHUB_TOKEN`) injected into HTTPS clone URL.
  - Pin file: `tests/forge/test_federation_backends.py`.
  - Manifest schema: `federation/upstreams.yaml` with workspaces
    `[{id, kind: git_registry|catalog|http_registry, endpoint,
      auth: {mode, secret_ref}}]`.

- **Per-product cost ceiling.** `copilot/cost.py::RunCostTracker`
  now maintains a per-product LIFO stack via `push_product` /
  `pop_product` / `current_product` / `per_product_usd`.
  `FLUID_COST_LIMIT_USD_PER_PRODUCT` enforces a per-product cap (in
  addition to the existing per-run `FLUID_COST_LIMIT_USD`). The agent
  coordinator pushes/pops on each entry point (`from_tables`,
  `from_intent`, `from_catalog`). Pin file:
  `tests/copilot/test_cost_ceiling_per_product.py`.

- **Join-key self-healing.** `cli/forge_copilot_corrective_feedback.py`
  adds `build_join_key_repair_message()` that detects "join key X not
  in upstream Y" errors specifically and routes to the LLM with a
  ranked list of plausible alternative keys (via
  `_rank_join_key_candidates`). Pin file:
  `tests/test_join_key_self_healing.py`.

- **Physical extractions reduced top-LOC files.**
  - `cli/_init_dag_helpers.py` (DAG generation, ~250 LOC) extracted
    from `cli/init.py`.
  - `cli/_interview_ask_helpers.py` (slot prompt helpers) extracted
    from `cli/forge_copilot_interview.py`.
  - `cli/_template_mode.py` extracted from `cli/forge_modes.py`.
  - `cli/_auth_provider_impls.py` extracted from `cli/auth.py`.
  - `cli/viz_renderers/{dot,output,html}.py` extracted from
    `cli/viz_graph.py`.
  - `copilot/agents/_modeler_helpers.py` extracted from
    `copilot/agents/modeler_agent.py`.
  All extractions use the **module-attribute-access indirection pattern**
  (`_module = original_module; ... _module._fn(...)`) so test patches
  on the original module flow through to the extracted helper. Apply
  this pattern when extracting from a hot file with many test-time
  patches.

Test files pinning the above (this session):
`tests/providers/test_abstract_op_dispatch.py`,
`tests/observability/test_run_id_decorator_integration.py`,
`tests/build_runners/test_replay_state_integration.py`,
`tests/build_runners/test_late_arrival_runner_integration.py`,
`tests/forge/test_federation_backends.py`,
`tests/copilot/test_cost_ceiling_per_product.py`,
`tests/test_join_key_self_healing.py`,
`tests/observability/test_run_id.py`.

Key env vars added this session:

| env var | what it does |
|---|---|
| `FLUID_RUN_ID` | override or pre-seed cross-stage run-id |
| `FLUID_COST_LIMIT_USD_PER_PRODUCT` | per-product cost ceiling (separate from the existing per-run cap) |

## Recent architectural changes worth knowing (2026-05-25, OpenTofu autogen)

The `feat/opentofu-iac-autogen` branch retires the hand-rolled per-cloud
apply paths and routes `fluid apply` (for cloud providers) through a
modular **OpenTofu emitter**. `AUTOGEN_SPIKE.md` (repo root) is the
canonical architecture doc; `HONESTLY_TESTED.md` is the coverage matrix.
Headline items:

- **New module `fluid_build/iac/`** — plugin-registry / runner / module
  builder / credentials / backend / importer / shadow / cutover. One
  `IacProviderPlugin` per cloud (`iac/providers/{aws,gcp,snowflake}.py`),
  borrowed from dbt's adapter pattern (Pulumi confirms the shape). Adding
  Azure later is one new file + one `register_iac_plugin()` line; zero
  core edits.
- **`fluid generate iac <contract>`** — emits a deterministic
  `main.tf.json` (canonical JSON, `sort_keys`, credential-free) for
  review before any apply.
- **`fluid apply` on cloud providers routes through OpenTofu** — not a
  user-facing flag, the per-provider mapping lives in `iac/cutover.py`.
  `local` keeps its native apply.
- **Plan-binding integrity is replicated** —
  `_apply_opentofu_engine.py::_verify_plan_binding_for_opentofu` mirrors
  the native engine's `_verify_plan_digests` gate so a tampered
  `plan.json` is rejected before any `tofu apply`.
- **Operational hardening** — `runner.py` carries a per-command timeout
  (default 1800s, override with `FLUID_TOFU_TIMEOUT_SECONDS`), a
  `require_tofu_version()` gate (floor 1.6.0), and the
  `--allow-data-loss` override emits an audit-trail WARNING +
  structured `opentofu_destructive_gate_override` event.
- **Catalog registrars retired** — `build_runners/catalog_registrars/`
  loses `glue.py` + `snowflake_horizon.py` (~514 LOC). Glue table
  parameters / column comments + Snowflake Horizon markdown comments
  are folded into the IaC emit (`iac/providers/aws.py::_emit_glue`,
  `iac/providers/snowflake.py::_build_horizon_table_comment`). The
  remaining catalog backends (`datahub`, `openmetadata`,
  `datamesh_manager`) keep their registrar shape.
- **Cross-account / cross-project access** uses **existing schema fields**
  — Lake Formation grants via `binding.governance.lakeFormation`, BQ
  cross-project via `metadata.policies` → dataset `access[]` block.
  No new schema fields beyond the LF block.
- **Test coverage** is three-tier: unit (`tests/iac/test_iac_*.py`),
  emulator (LocalStack Pro / GCP emulators), live cloud (gated by
  `FLUID_IAC_LIVE_{AWS,GCP,SNOWFLAKE}=1` env vars). Snowflake live tests
  source creds from the `snowflake-biz-lab` repo's `.env`. Coverage
  matrix in `HONESTLY_TESTED.md`.

Key env vars added by this branch:

| env var | what it does |
|---|---|
| `FLUID_TOFU_TIMEOUT_SECONDS` | per-`tofu` invocation wall-clock cap (default 1800) |
| `FLUID_IAC_LIVE_AWS=1` | enable Stage 3 live AWS tests (real cloud) |
| `FLUID_IAC_LIVE_GCP=1` | enable Stage 3 live GCP tests (real cloud) |
| `FLUID_IAC_LIVE_SNOWFLAKE=1` | enable Stage 3 live Snowflake tests (real cloud) |

Drafted upstream issues for known external gaps live in
`docs/upstream-issues/` (snowflakedb/snowflake v2 tag-masking
association removal; two LocalStack Pro quirks: Lambda in
docker-in-docker, Lake Formation DataLakeAdmin → GrantPermissions).

## Working style

- **Goal-driven execution.** For non-trivial tasks, state success criteria before implementing and loop until verified. Transform imperatives into tests: "add redactor coverage" → "new pattern is masked by a test assertion"; "fix the bug" → "reproducing test passes first, then fix makes it green".
- For multi-step work, write a brief plan with per-step checks (`1. step → verify: check`). Strong criteria let you iterate without asking; weak criteria ("make it work") do not.

## Commit / PR etiquette

- Conventional-commits style (`feat(scope):`, `fix(scope):`, `chore(ci):`, `security: …`). See the git log for the in-use shape.
- `main` is protected. All changes go via PR. CI (`.github/workflows/ci.yml`) must be green before merge.
- `ci.yml` triggers on `push: branches:[main]` and `pull_request:`. GitHub's rule that `GITHUB_TOKEN` merges don't trigger downstream workflows can leave a merge commit without a CI run — trigger it manually with `gh workflow run ci.yml --ref main` (`workflow_dispatch` was added for exactly this reason in PR #27).
- Never add `Co-Authored-By: Claude …` trailers to commits (repo-wide preference).
