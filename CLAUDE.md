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

- **AI mode runs on the shipped skills** (`cli/forge_ai.py`). With a coding agent, forge opens it (interactive) or drives Claude Code headless with the skills pack as a plugin, allowed to edit the one contract file and to run `fluid validate` on it (no wider rule); otherwise one litellm call whose system prompt is the skills' text, then a `fluid validate --offline` repair loop (3 attempts). The embedded copilot (interview, prompt library, envelope, agent loop, memory, missions, judge) was deleted on 2026-11-13; `copilot/agents/` stays as the data-model engine.

- **Two parallel redaction layers.** `observability/secret_redactor.py::SecretRedactingFilter` is wired into Python logging globally; `providers/snowflake/util/logging.py` has a Snowflake-provider-local `redact_string`/`redact_dict` with its own `SENSITIVE_PATTERNS`/`SENSITIVE_KEYS`. When adding a new secret shape, **extend both** to keep them symmetric.

- **SQL safety is centralized in `providers/_sql_safety.py`.** Every DDL f-string must route identifiers through `validate_ident` and string literals through `quote_string_literal`. Inline `.replace("'", "''")` is a regression — diverges from the central helper and breaks under non-default Snowflake escape settings.

- **Lazy-import pattern for the light CLI (`__getattr__` + module-self indirection).** The `fluid --help` / `build_parser()` cold path must stay free of heavy SDKs (`httpx`, `jsonschema` via `schema_manager`, `litellm`, the `mcp` server SDK). The convention that keeps it that way, used pervasively across `cli/forge.py`, `cli/ai_setup.py`, `schema_manager.py`, etc.:
  - **Defer the import, not just the call.** Heavy symbols are resolved through a module-level `def __getattr__(name)` (PEP 562) that `importlib.import_module`s the heavy module on *attribute access*, and intra-function work uses function-local `import`s. A module-scope `from …forge_copilot_agent import …` would pull the whole AI runtime onto `--help` — that's the regression.
  - **Module-self indirection preserves the test seam.** Inside a deferred builder/function, reach back to this module's own (possibly-patched) symbols via `import fluid_build.cli.<mod> as _self; _self.X(...)` rather than a direct local name. A `patch("…cli.<mod>.X")` sets a *real* module attribute that **shadows** `__getattr__`, so `_self.X` sees the patch; a bare local `X` would not.
  - **Memoise deferred class builders with `functools.lru_cache(maxsize=1)`**, not a hand-rolled module global + `if is None` check — it's lock-guarded (thread-safe first build, stable `isinstance` identity) and gives `.cache_clear()` as a test reset hook. Canonical example: `cli/forge.py::_build_copilot_agent_class`.
  - **The invariant is guarded, not just documented.** `tests/perf/test_startup_budget.py` enforces a `MAX_MODULES` ceiling and a `FORBIDDEN_ON_HELP` set (`mcp`, `httpx`, `jsonschema`, `litellm`, …) that must not be imported on `fluid --help`. Run it after touching any cold-path module; if it regresses, a module-scope heavy import sneaked in.

## Files that need extra care

Drive-by changes here can break invariants. Read the surrounding comments first.

- `cli/security.py` — path-traversal + forbidden-paths validator. Subtle ordering: `_reject_raw_traversal` runs on the **pre-resolve** input; `_validate_path_security` runs on the **resolved** path. Cross-platform: `FORBIDDEN_PATHS` is platform-aware; macOS `/etc` resolves to `/private/etc`, which is explicitly included.
- `credentials/encrypted_store.py` — Fernet-encrypted credential store. `_load_store` raises `CredentialError` on `InvalidToken` (never silently wipes). Parent directory is chmod'd `0o700` best-effort via `_secure_parent_dir`.
- `providers/_sql_safety.py` + `providers/snowflake/governance.py` — see the SQL-safety note above. Any new DDL emitter in another provider must use these helpers.
- `cli/auth.py::_sanitize_argv` — strips credential-bearing flag values before DEBUG-logging the subprocess command. Extend `_SENSITIVE_FLAGS` / `_SENSITIVE_FLAG_SUFFIXES` for new provider-auth CLIs that use `--*-secret` / `--*-key` shapes.
- `providers/snowflake/util/logging.py::SENSITIVE_KEYS` / `SENSITIVE_PATTERNS` — mirror additions into `observability/secret_redactor.py`.

## Recent architectural changes worth knowing (2026-11-13, skills-first AI mode)

The founder's rule that the AI setup be world-class AND lightweight. On #851's eval the embedded
copilot scored 0/9 and a coding agent with the four shipped skills 11/11; on Sonnet 5.5 the
copilot's envelope prompt is refused outright (`[reasoning_extraction]` safeguard). So:

- **`fluid forge` AI mode = `cli/forge_ai.py`**, about 1,090 lines with docstrings. `run_ai_copilot_mode` in
  `cli/forge.py` delegates to it. Seams for tests: `call_llm` (llm.providers), `_run_agent`
  (llm.coding_agent), `_run_interactive` and `_run_validate` (forge_ai). Never `python -m` for
  the validate step: it resolves the install's `fluid` with the skills hook's `find_fluid()`.
- **Deleted:** the interview (world-class + legacy + slash commands + streaming preview), the
  prompt library and profiles/overlays, the response envelope and its repair runtime, the agent
  loop and tool registry, copilot memory (`fluid memory`, the `--*memory*` flags), missions
  (`fluid mission`), the judge (`stats --judge`), resume/fork/watch, domain agents (`--domain`,
  `fluid init --agent`), tiered routing, MCP sampling, and the five MCP tools that ran forge's
  own LLM (`forge_run`, `forge_from_source`, `score_contract_quality`,
  `enrich_contract_suggestions`, `search_semantic_memory`). `--no-memory` stays as a hidden no-op:
  installed scaffold-ide indexes and the authoring skill pass it.
- **Kept on purpose:** `copilot/agents` minus the judge, `copilot/catalog`, `copilot/schemas`,
  the store's session/audit parts, `copilot/checkpoint`, `copilot/cost`. They are the
  deterministic data-model engine behind `fluid forge data-model *`, `fluid generate
  transformation`, the MCP output port's audit trail and the 11 remaining MCP tools.
- **Command Center contract** (`tests/cli/test_command_center_contract.py`): `forge data-model
  from-intent` (`--deterministic` | `--llm-provider`), `stats --json`, and the in-process
  imports `copilot.pii`, `copilot.enrichment`, `forge_datamodel.emit.validator`,
  `cli.forge_copilot_runtime.build_capability_matrix` (now a shim over `cli/_capability_matrix.py`).
- The sections below that describe the interview, slash commands, streaming preview, self-healing
  repair loop, agent loop, missions, memory, judge or MCP sampling are history.

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
The cross-check is centralized in
`forge/product_types.py::normalize_metadata_in_place` (raises
`ProductTypeError` on an inconsistent layer/productType pair), invoked from
`cli/validate.py` and `cli/contract.py`; the schema validators
`schema_manager.py::_validate_with_jsonschema` and
`cli/contract_validation.py::_validate_metadata` validate the metadata at
their boundaries. (A former inline `schema.py::_check_metadata` site has been
removed — `schema.py` no longer exists; the logic was consolidated into the
helper.) If the mapping ever changes, update the helper plus the discover
emitter at `cli/discover/emitter.py` (which populates both fields).

**Two different things are called "templates" here, and only one populates
both fields.** The *forge* templates (`forge/templates/*.py`, used by
`fluid forge --template`) do, via `forge/templates/_v073_builder.py`. The
shipped *quickstart* contracts (`fluid_build/templates/*/contract.fluid.yaml`,
used by `fluid init --template`) now populate `productType` on 12 of 13, set
from what each template actually reads: SDP for single-source ingests, ADP for
the ones that join or derive, CDP for the two that declare `metadata.layer:
Gold`. `hello-world` is deliberately left unclassified — it reads nothing real,
and a Hello World shipping a Data Mesh classification invites cargo-culting it.
Either field alone is sufficient and neither is required, so that is valid.
Importers and fixtures are still partial. Provider tag emitters (AWS / GCP / Snowflake / forge)
propagate both into cloud labels as `fluid_layer` + `fluid_product_type`
(distinct keys, same canonical value). The marketplace surfaces both as
facets and accepts `fluid market --product-type SDP|ADP|CDP` alongside
`--layer`. Test pinning lives in `tests/test_product_type_mapping.py`.

## Recent architectural changes worth knowing (2026-04-23)

The `feat/cli-pipeline-and-publish-hardening` branch (15 commits) landed the **11-stage pipeline** and its supporting surface. AGENTS.md has the canonical lifecycle doc; the Claude-Code-specific operational notes are:

- **11 CLI stages, one command per stage.** `fluid bundle → validate → generate artifacts → validate-artifacts → diff → plan → apply → policy-apply → verify → publish → schedule-sync`. Each stage has `cli/<stage>.py`. Re-reading `AGENTS.md#the-11-stage-pipeline` is cheaper than re-deriving the flow from commit history.
- **`cli/execute.py` is gone.** Its dbt + python runners extracted to `build_runners/{dbt,python}/`. `apply --mode amend-and-build` dispatches there via `build_runners.run_builds_from_args`. If you find yourself writing `from fluid_build.cli.execute import ...` that's the old path — use `build_runners` instead.
- **`cli/compile.py` is gone too.** Renamed to `cli/bundle.py` in Phase 1; the hidden `fluid compile` alias was deleted. `fluid bundle --format tgz` is the canonical call.
- **Plan-binding is cryptographic.** `fluid plan` emits `bundleDigest` + `planDigest` into `plan.json`. `fluid apply` re-verifies both before any DDL. Helpers live in `forge/core/plan_digest.py`: `compute_plan_digest`, `inject_digests`, `verify_plan_binding`, `PlanBindingError` (`.kind` attribute is `"bundle-mismatch"` or `"plan-tamper"` — stable event tags for CI log parsers). The `--no-verify-plan-binding` flag is the DR escape hatch and logs at WARNING level so audit trails catch it.
- **Apply actions carry BOTH `op` and `action_type` fields.** `apply.py`'s provider dispatcher reads `op`; display/viz reads `action_type`. Dropping either silently breaks the pipeline. See `plan.py::_plan_with_provider_actions` — both get `action.action_type.value`.
- **No hardcoded `"0.7.1"` fallback.** `SchemaManager.latest_bundled_version()` scans `fluid_build/schemas/fluid-schema-*.json` and returns the newest. Use it anywhere the "default fluidVersion" is needed; `cli/plan.py::_default_fluid_version()` is the canonical wrapper.
- **CI template generation got install-mode.** `fluid generate ci --system jenkins --install-mode {pypi,dev-source}`. The generated Jenkinsfile carries ONE mode's logic — no runtime branching in the file. `pypi` (default, production) exposes 4 build-time Jenkins parameters (`FLUID_PACKAGE_SPEC`, `FLUID_PIP_INDEX_URL`, `FLUID_PIP_EXTRA_INDEX_URL`, `FLUID_ALLOW_PRERELEASE`). `dev-source` (lab) uses `PYTHONPATH=/forge-cli-src` and fails LOUD if the mount is missing.
- **The Jenkins template is the reference 11-stage implementation.** `forge/core/pipeline_templates.py::JenkinsTemplate.generate()` ships the full parameterized template. The other 6 CI systems (GitHub Actions, GitLab, Azure DevOps, Bitbucket, CircleCI, Tekton) are scheduled for Phase 7-rest to port from it.
- **Smoke scripts live in `scripts/`.** `scripts/smoke_phase_6b.py` validates plan-binding + data-loss-gate against a real .venv.fluid-dev venv. `scripts/smoke_a1.py` validates the A1 variant with the new `--mode` / `--target` flags. Both auto-discover env from the launchpad; both are safe (every apply runs `--dry-run`).
- **RESOLVED (was: `unknown_action_op`, Phase 6F).** The gap where the Snowflake dispatcher silently no-op'd the 0.7.1 abstract ops (`provisionDataset`, `scheduleTask`) and logged `{"event": "unknown_action_op", ...}` has been **retired by the OpenTofu cutover** (see the 2026-05-25 section): cloud `apply` routes through the OpenTofu emitter, and `unknown_action_op` is no longer emitted anywhere in `fluid_build/`. Kept here for history.

## Recent architectural changes worth knowing (2026-04-30, world-class forge UX)

A multi-phase push landed comprehensive UX + architecture upgrades on `feat/source-aligned-acquisition`. Key surfaces and where they live:

- **Mode picker (`fluid forge`).** Bare invocation surfaces a 5-mode menu (AI / Compose / Refine / Template / Blank) instead of dropping into AI. Lives at `cli/_forge_mode_picker.py`. Skipped via `FLUID_FORGE_NO_PICKER=1`. Picker default is highlighted by the welcome scan; user always sees the menu.
- **Welcome scan.** Detect-first parallel scan runs before any prompt (budget: see the 2026-11-03 section) — workspace state, AI configured, CLIs installed, sample data, cloud creds, return-user state. Lives at `cli/_welcome_scan.py`. `~/.fluid/usage.json` carries `forge_count`.
- *(Deleted 2026-11-13.)* **Mode-aware interviews.** `forge_copilot_interview.py::run_adaptive_copilot_interview` short-circuits to `_run_compose_interview` (3 questions max for `--from-product`) or `_run_refine_interview` (1 question for `--refine`) when the runtime resolved upstreams or loaded an existing contract. Standard mode runs the world-class bootstrap (below).
- *(Deleted 2026-11-13.)* **World-class bootstrap.** New default fresh-product interview at `cli/_world_class_interview.py`. Inferences first (`InterviewSignals`), examples in every prompt, productType-first, `:auto` escape, schema-coverage gate. Toggle via `FLUID_INTERVIEW_LEGACY=1` to revert to the legacy bootstrap.
- *(Deleted 2026-11-13.)* **Slash commands inside the interview.** `cli/_interview_slash_commands.py`: `:ai-setup`, `:override`, `:show-work`, `:doctor`, `:help`, `:quit`. Wrapped into `forge_dialogs.ask_friendly_text` so every prompt accepts them.
- **Pre-write preview panel.** `cli/_preview_panel.py` — renders cost + file list + run-id BEFORE the writes; persists `.fluid/agents/<run-id>/{cost.json,reasoning.md,transcript.json}` so Ctrl-C at the prompt loses nothing. `--yes` skips the prompt but the panel still renders. Receipts are richened via `forge_modes.py::_populate_richer_receipt`.
- *(Deleted 2026-11-13.)* **Streaming contract preview.** `cli/_streaming_contract_preview.py` re-shapes the seed contract after every interview answer so the user sees it grow. Toggle off with `FLUID_FORGE_NO_STREAMING_PREVIEW=1`.
- **Composition pipeline.** `forge_datamodel/from_data_products/pipeline.py` resolves upstream products, validates composition rules (SDP rejects upstreams; ADP/CDP accept SDP+ADP), pre-fills `consumes[]`. Wired via `--from-product <ID|path>` (repeatable) and `--from-product-list <file>`.
- **`fluid forge --refine`** loads an existing contract, asks "what to change?", feeds the existing contract verbatim to the LLM as the seed (via `_seed_contract_override`).
- *(Deleted 2026-11-13.)* **Self-healing repair loop.** `forge_copilot_runtime.py` runs the JSON-schema validator on every emitted contract and prepends path-specific errors to the next attempt's repair feedback. Function: `forge_copilot_corrective_feedback.build_schema_validation_message`.
- **`fluid stats`.** Aggregates `.fluid/agents/*/cost.json` across runs. `--by provider/type/engine`, `--since <spec>`, `--json`.
- **Forge templates emit v0.7.3 directly.** Each of `forge/templates/{starter,analytics,etl_pipeline,ml_pipeline,streaming}.py` now uses the shared spec-driven builder at `forge/templates/_v073_builder.py`. The legacy v0.5-vintage methods are preserved as `_legacy_generate_contract_unused` for reference. The runtime coercion layer at `forge_modes.py::_coerce_template_contract_to_v073` is now a no-op for v0.7.3 contracts (fast-path) and remains as a safety net for any legacy out-of-tree template.
- **LiteLLM unified backend.** `cli/forge_copilot_llm_litellm.py` ships an opt-in `LiteLLMProvider` that subclasses `LlmProvider` and routes every provider through one API. Enable via `FLUID_LLM_BACKEND=litellm` + `pip install 'data-product-forge[litellm]'`. `RunCostTracker.record_call` now accepts `usd_override` (used by litellm to feed accurate per-call cost into the cost summary). Dispatched in `forge_copilot_llm_providers.py::get_llm_provider` + short-circuits in `call_llm` and `call_llm_streaming`.
- **UX telemetry.** `cli/_ux_telemetry.py` captures `time_to_first_panel_ms`, `questions_asked`, `inferences_used`, `picker_choice`, `mode`, `preview_accepted`, `schema_repair_attempts`. Emitted onto the `forge.invocation` OTel span when an exporter is configured.
- **Mode dispatch fix.** Picking `template` now routes to `forge_modes.run_template_mode` (no AI). Picking `blank` goes to `_run_blank_mode`. Picking `refine`/`from_product` flows through the AI runtime with the right context flags. The `--template` flag is an argparse alias of `--scaffold`.
- **Datamesh-manager Silver↔Gold archetype swap fix.** `providers/datamesh_manager/datamesh_manager.py` had `Silver→consumer-aligned` and `Gold→aggregate` reversed. Now correctly reads `metadata.productType` first and falls back to the canonical layer mapping (Bronze→source-aligned / Silver→aggregate / Gold→consumer-aligned).

Test files pinning the above: `tests/test_forge_mode_picker.py`, `tests/test_welcome_scan.py`, `tests/test_preview_panel.py`, `tests/test_from_data_products.py`, `tests/test_stats.py`, `tests/test_litellm_backend.py`, `tests/test_init_forge_scenarios_e2e.py` (the matrix that runs `fluid validate` on every produced contract).

Key env vars introduced or honoured by the new path:

| env var | what it does |
|---|---|
| `FLUID_LLM_BACKEND=litellm` | route every LLM call through litellm |
| `FLUID_LITELLM_MODEL_PREFIX=<x>` | override the litellm model-name prefix for niche providers |
| `FLUID_FORGE_NO_PICKER=1` | suppress the mode picker (CI / scripts) |
| `FLUID_FORGE_NO_PREVIEW=1` | suppress the pre-write preview panel + prompt |
| `FLUID_FORGE_NO_WELCOME=1` | suppress the welcome scan render |
| `FLUID_FORGE_PICKER_ALWAYS=1` | force the picker even for return users |
| `FLUID_COST_LIMIT_USD_PER_RUN=<n>` | per-run cost cap shown in the progress prefix |

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
  - Pin file: `tests/providers/test_abstract_op_dispatch.py` *(since removed —
    this per-provider abstract-op dispatch was superseded by the OpenTofu
    cutover; see the 2026-05-25 section)*.
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
    `GITHUB_TOKEN`), handed to git only through the process
    environment: entries appended to `GIT_CONFIG_PARAMETERS` setting
    `http.<endpoint>.extraHeader` (and
    `http.<endpoint>.followRedirects=false`), git >= 2.31, keyed to the
    endpoint URL and read after any inherited `GIT_CONFIG_COUNT` pairs and
    `GIT_CONFIG_PARAMETERS` entries; an empty `extraHeader` first clears
    the configured list and the operator's non-Authorization headers for the
    endpoint are handed back after it, so one Authorization header goes out
    and a gateway's header still does. A `url.<base>.insteadOf` rewrite of
    the endpoint to another scheme, host, port or path means no token; one
    that only adds userinfo keeps it, with the settings also keyed to that
    `user@` URL. Never in the clone URL, argv or the cache's `.git/config`.
  - Credential policy, checked before anything is sent: `GITHUB_TOKEN`
    / `GH_TOKEN` go only to GitHub hosts by default; any other variable
    needs an operator-set `FLUID_FEDERATION_SECRET_REF_ALLOWLIST` entry
    (recommended `NAME@host`); https only. User docs:
    `docs/FEDERATION.md`. Pin files:
    `tests/forge/test_federation_credentials.py`,
    `tests/forge/test_federation_operator_policy.py`,
    `tests/forge/test_federation_git_redirects.py`.
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

- *(Deleted 2026-11-13.)* **Join-key self-healing.** `cli/forge_copilot_corrective_feedback.py`
  adds `build_join_key_repair_message()` that detects "join key X not
  in upstream Y" errors specifically and routes to the LLM with a
  ranked list of plausible alternative keys (via
  `_rank_join_key_candidates`). Pin file:
  `tests/test_join_key_self_healing.py`.

- **Physical extractions reduced top-LOC files.**
  - `cli/_init_dag_helpers.py` (DAG generation, ~250 LOC) extracted
    from `cli/init.py`.
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
  patches. Import the original module at the end of the helper, or
  inside the function that uses it, never at the helper's top: the
  original re-exports the helper's names, so a top import makes
  `import <helper>` fail when it runs first. `tests/test_import_cycles.py`
  imports every module first, each in a fresh interpreter.

Test files pinning the above (this session):
`tests/providers/test_abstract_op_dispatch.py` *(since removed — see note above)*,
`tests/observability/test_run_id_decorator_integration.py`,
`tests/build_runners/test_replay_state_integration.py`,
`tests/build_runners/test_late_arrival_runner_integration.py`,
`tests/forge/test_federation_backends.py`,
`tests/copilot/test_cost_ceiling_per_product.py`,
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

## Recent architectural changes worth knowing (2026-06-01, keyless coding-agent authoring)

`fluid forge` can now author **without an LLM API key of its own** by reusing an
AI coding agent the user already has. Two complementary topologies, both on the
existing `LlmProvider` seam (no new backend switch — selection is by provider name):

- *(Deleted 2026-11-13 with the `forge_run` MCP tool.)* **In-IDE (MCP sampling, already existed; this branch fixed two bugs).**
  `MCPSamplingProvider` routes the LLM call back to the host IDE via
  `sampling/createMessage`. It was reachable only through the `forge_run` MCP
  tool, but was broken keyless: (1) `--llm-provider mcp-sampling` was rejected by
  the `forge` / `forge data-model` argparse `choices` allowlist, and (2)
  `resolve_llm_config` raised `copilot_missing_llm_api_key` for it. Both fixed —
  `mcp-sampling` is now in `choices` and in `_KEYLESS_PROVIDERS`
  (`forge_copilot_llm_providers.py`). Pin: `tests/test_mcp_sampling_keyless.py`.

- **Standalone terminal (new — headless shell-out).** New module
  `cli/forge_copilot_coding_agent.py`: one parametrized `CodingAgentProvider`
  driven by an `AgentSpec` table for `claude-code` / `codex` / `cursor` / `kiro`.
  `invoke_blocking` shells out to the agent's headless CLI (`claude -p`,
  `codex exec`, `cursor-agent -p`, `kiro-cli chat --no-interactive`) and returns
  the response **envelope string** — the runtime's `extract_json_object` +
  validate + repair loop is unchanged. **The only subprocess seam is
  `_run_agent`** (list argv, never `shell=True`); tests monkeypatch it — no real
  CLI in CI. Schema-constrained output via `--json-schema` (Claude) /
  `--output-schema` (Codex); cursor/kiro fall back to the self-healing JSON-parse
  loop, and agent stdout is ANSI-stripped (`_ANSI_RE`) before extraction (kiro-cli
  decorates its output). **Auth is each CLI's own job — a stored interactive
  login (`claude`, `cursor-agent login`, `kiro-cli login`) OR the key env; we do
  NOT pre-block on a missing key** (verified live: a current `kiro-cli login` runs
  headless keyless with no `KIRO_API_KEY`; an *expired* token is what makes
  kiro-cli open a browser). All four are in `_KEYLESS_PROVIDERS` so the generic
  resolve-time api-key gate (which only knows the standard cloud keys) doesn't
  reject them; only Claude Code is *truly zero-setup* keyless (subscription OAuth,
  never `--bare`). Claude's `total_cost_usd` feeds `usd_override` via a
  thread-local; the others surface as `$?` (honest, never a misleading `$0`).
  NB: the **Kiro IDE** (`/Applications/Kiro.app`) is NOT the `kiro-cli` product —
  the IDE's `code chat` is GUI-bound; the IDE's keyless route is MCP sampling.
  Pins: `tests/test_coding_agent_provider.py`, `tests/test_coding_agent_resolve.py`.

- **Detection + "offer, don't force" UX.** `_welcome_scan._probe_coding_agents`
  detects installed agent CLIs and the panel surfaces "Keyless: … (no API key)".
  `ai_setup.run_ai_setup_inline` loads a saved coding-agent keyless and auto-offers
  Claude Code (step 4.5, mirroring the Ollama branch). Pins:
  `tests/test_welcome_scan_coding_agents.py`, `tests/test_coding_agent_ai_setup.py`.

- **Drive modes (superseded 2026-11-13: agentic is now the default, see above).** Default was **envelope** (agent returns the contract JSON).
  `--forge-agent-mode agentic` / `FLUID_FORGE_AGENT_MODE=agentic` selects the
  opt-in **agentic** mode (agent writes `contract.fluid.yaml` into the workspace) —
  the `LlmConfig.agent_mode` plumbing is wired; the full agentic read+validate loop
  is the remaining follow-up.

Key env vars: `FLUID_FORGE_AGENT` (select agent), `FLUID_FORGE_AGENT_MODE`
(envelope|agentic), `FLUID_FORGE_AGENT_TIMEOUT_SECONDS`, `FLUID_FORGE_AGENT_CWD`
(default = scratch tempdir, so the agent doesn't auto-load the project's
CLAUDE.md/AGENTS.md). All four are in the `fluid doctor` env dump. ACP (Agent
Client Protocol) is the noted v2 transport — `_run_agent` is the only seam it
would replace; when building it, **depend on the official `agent-client-protocol`
PyPI SDK** (Pydantic models + async base classes + stdio JSON-RPC) rather than
hand-rolling the protocol.

## Recent architectural changes worth knowing (2026-11-03, welcome-scan budget)

`cli/_welcome_scan.py::run_welcome_scan` now has a budget that means what it says:

- **One wall-clock cap.** `_TOTAL_TIMEOUT_S` (1.0 s) is the only scan budget; the old per-probe `_PROBE_TIMEOUT_S` is gone. Probes run on daemon threads (`_spawn_probe`: one `Future` each, deliberately not a `ThreadPoolExecutor`, whose non-daemon workers are joined at interpreter exit and would hold the CLI open for an abandoned probe). The scan returns at the deadline and drops stragglers. Python cannot kill a thread, so an abandoned probe keeps running until it finishes (the one that used to run for minutes is the tree walk, below). Probes must not write to stdout/stderr (a daemon thread caught mid-write at interpreter exit can abort the process).
- **Cold imports are off the clock.** `_warm_probe_imports` imports `_WARM_MODULES` (the AI-config pydantic chain, the dbt runner) from the main thread while the probes run, then the budget starts. Probe imports stay function-local: if a probe starts importing something new, list it in `_WARM_MODULES` or `tests/test_welcome_scan_budget.py::test_probes_import_nothing_new_after_the_warm_up` fails. NB `build_parser()` already imports `ai_setup`, the dbt runner and pydantic, so in a real CLI process only `copilot.unified_config` is still cold; the warm-up matters for tests and library callers.
- **dbt has its own cap.** `_DBT_VERSION_TIMEOUT_S` (0.32 s) sizes the `dbt --version` subprocess and is not derived from the scan budget and must stay below it, so the child is killed by its own timeout instead of being orphaned. With Python dbt-core on PATH it usually waits out that cap, which is the scan's floor (~0.35 s).
- **The specialization walk is bounded and ends with its scan.** `_probe_specialization_suggestion` used to `rglob` the whole cwd tree (279 s under a directory holding many repos), and the picker, the interview signals and the welcome panel each start a scan, so up to three abandoned walkers overlapped (~0.5 core). Its contracts now come from `_find_contracts`: depth-first in name order, so the first 8 are exactly `sorted(rglob(...))[:8]` wherever the bounds do not cut. It skips `util/upstream_discovery`'s `IGNORED_DIRS | BUILD_OUTPUT_DIRS`, lists at most `_SPECIALIZATION_MAX_DEPTH` (6) levels, reads at most `_SPECIALIZATION_MAX_ENTRIES` (10,000) entries, and does not follow symlinks or take anything but a regular file. `run_welcome_scan` sets a `threading.Event` when it returns and the walk ends at its next directory; `_WALK_GATE` runs one walk at a time, so a walker stuck in a filesystem call (a dead mount) is the only one a process can hold. `tests/fragments/test_no_raw_readers.py` registers `_probe_specialization_suggestion` as an accepted raw reader. The scanner finds it through the word `contract` in that function (the `contract.fluid.yaml` in its docstring, the `_find_contracts` call); the registry only checks that the function exists, so losing both silently orphans the entry.
- `scan_duration_ms` is the real wall-clock of the call, imports included. Pins: `tests/test_welcome_scan_budget.py`, `tests/test_welcome_scan_walk.py`.

## Recent architectural changes worth knowing (2026-11-12, agent skills pack)

- **`fluid_build/agent_skills/` is one directory with three jobs.** It is package data (`fluid scaffold-ide` copies `skills/` into `.claude/skills/` or `.agents/skills/`), a Claude Code plugin root (`.claude-plugin/plugin.json`, `hooks/hooks.json`, `skills/`) listed by the root `.claude-plugin/marketplace.json`, and the layout `npx skills add` finds through that marketplace. Keep the skills to the six agentskills.io frontmatter fields and at most three supporting files each; `tests/agent_skills/test_skill_pack.py` enforces both, checks every `fluid` command against `build_parser()`, and validates every YAML example (its first line names its `$defs` fragment, e.g. `# exposes[].binding`).
- **Generated, never hand-edited:** `references/contract-schema.md`, `references/error-codes.md` and the `composition-rules` block in a SKILL.md. Run `python scripts/gen_agent_skill_references.py` after a schema promotion, a new enum value or a new `_error_catalog` entry; `tests/agent_skills/test_reference_drift.py` fails until you do.
- **The plugin has no `version` on purpose:** Claude Code then versions a relative-path plugin by commit SHA, so users get each push.
- **The validate hook** (`agent_skills/hooks/validate_contract.py`) reads the edited path from stdin JSON: hook commands get no `${tool_input.*}` substitution, and an `if` rule is one permission rule (`|` is not OR). One file serves the plugin (`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/validate_contract.py"`) and scaffold-ide (`<python> <installed path>/hooks/validate_contract.py`, written to `.claude/settings.local.json`). **Never `python -m` in a hook or an agent-reachable shim:** it puts the working directory (the agent's project) first on `sys.path`, so a planted `json.py` runs (reproduced in the 2026-11-12 security review); the hook calls the `fluid` console script instead.
- **`tests/agent_evals/`** is the eval harness (arms no-skill / skill / forge, plus codex-no-skill / codex-skill; deterministic grading). `--runs N` adds pass@N and pass^N; scenarios with `should_trigger: false` are negative controls a skill arm fails when a FLUID skill fires; each record's `run_meta` names the agent, its version and the pinned model and effort. A Codex arm gets a fresh `CODEX_HOME` and `HOME` per run (Codex reads `$HOME/.agents/skills`), its API key through `codex login --with-api-key` into that `CODEX_HOME` (Codex 0.154 passed a `*KEY*` variable to its commands despite `shell_environment_policy.exclude`), and a permissions profile that confines reads too. Its workspaces live in the system temp dir, never inside a repository: Claude Code loads every `CLAUDE.md` on the way up from the working directory, and a first run with workspaces under `tests/agent_evals/runs/` fed forge-cli's own CLAUDE.md (33.8k tokens) to every arm. The live test is marker `agent_eval` (deselected in `addopts`) plus `FLUID_AGENT_EVAL=1`; `test_harness.py` grades every scenario's reference solution in the default lane. A local DuckDB build that reads a sibling product's output needs `fluid.workspace.yaml` at the repo root. Under `--mode amend` the provider path never binds `consumes[]` and reads a `parameters.inputs` entry without a `format` as CSV (`providers/local/planner.py` fills in `csv` before the extension is looked at); `--mode amend-and-build` binds each `consumes[]` entry the SQL reads by its `exposeId` (`build_runners/_embedded_sql_io.py`) and infers an input's format from its extension. An input with a `format` works in both.

## Recent architectural changes worth knowing (2026-11-12, Databricks job runs)

- **`-and-build` runs a Databricks dbt build now.** The seam: `cli/apply.py` passes `applied_state=[]` to `apply_via_opentofu` (only when `_databricks_job_runs` finds a non-continuous Databricks dbt build), the engine fills it with `runner.tofu_state_resources` after `post_apply` binds masks, and `jobs_in_state` (tags `databricks_jobs.PRODUCT_TAG` / `BUILD_TAG`, never the job name) hands `run_builds_from_args(databricks_jobs=...)` the ids. `build_runners/_databricks_job_run.py` does run-now (idempotency token, `queue.enabled`, tried 3 times with the same token), polls `runs/get` (5 failed reads in a row end the wait without a cancel: the SDK does not repeat a 500/502/504), and on failure reads `runs/get-output` per failed task. Ctrl-C, SIGTERM and SIGHUP cancel the run only while it is waited on. An id-less build's job is tagged by its position (`databricks_jobs.job_build_id`). `databricks_jobs=None` keeps the old "runs on its trigger" report. `_gate_databricks_sdk_for_apply` refuses a missing SDK before `tofu`.
- **Live-verified on Free Edition (2026-11-12):** a queued run's `runs/get` carries only `status.state: QUEUED` and an empty `state.state_message` (no `queue_details`), so the reason comes from `runs/list?active_only=true`; a dbt task's stdout is in `get-output.logs` with ANSI codes; `get-output.dbt_output.artifacts_link` is a pre-signed URL and is never printed; the deprecated run-now `dbt_commands` is still accepted and overrides the commands for that run only.
- Pins: `tests/build_runners/test_databricks_job_run.py`, `tests/build_runners/test_databricks_job_builds.py`, `tests/cli/test_apply_databricks_job_runs.py`, and the real-`tofu` `tests/iac/test_iac_databricks_jobs_fake_e2e.py` (`_fake_databricks.py` serves runs).

## Working style

- **Goal-driven execution.** For non-trivial tasks, state success criteria before implementing and loop until verified. Transform imperatives into tests: "add redactor coverage" → "new pattern is masked by a test assertion"; "fix the bug" → "reproducing test passes first, then fix makes it green".
- For multi-step work, write a brief plan with per-step checks (`1. step → verify: check`). Strong criteria let you iterate without asking; weak criteria ("make it work") do not.

## Commit / PR etiquette

- Conventional-commits style (`feat(scope):`, `fix(scope):`, `chore(ci):`, `security: …`). See the git log for the in-use shape.
- **Commit incrementally, never in one big drop.** Land each logical unit (one fix, one refactor, one feature) as its own green, reviewable commit the moment it passes its checks — do not batch many unrelated concerns into a mega-commit.
- `main` is protected. All changes go via PR. CI (`.github/workflows/ci.yml`) must be green before merge.
- `ci.yml` triggers on `push: branches:[main]` and `pull_request:`. GitHub's rule that `GITHUB_TOKEN` merges don't trigger downstream workflows can leave a merge commit without a CI run — trigger it manually with `gh workflow run ci.yml --ref main` (`workflow_dispatch` was added for exactly this reason in PR #27).
- Never add `Co-Authored-By: Claude …` trailers to commits (repo-wide preference).
