# AGENTS.md — AI Agent Integration Guide

> How AI agents, LLMs, and copilots should interact with FLUID Forge and the data products it governs.

---

## What Is FLUID Forge?

FLUID Forge is a **contract-driven build system for declarative data products**. You write a single `contract.fluid.yaml` that declares your data product — transformations, schemas, quality rules, access policies, observability, and AI governance — and the CLI compiles it into validated, planned, executable deployments across any supported cloud.

One contract. Any provider. Full governance. Zero boilerplate.

### The 11-Stage Pipeline

Every contract is delivered through an 11-stage pipeline. Each stage is a hard gate — non-zero exit halts the sequence. Stages 1–9 are structural (schema mutation + description + cryptographic binding); stages 10–11 are publication + distribution.

```
┌──────────────── STRUCTURAL ─────────────────┐  ┌─ PUBLICATION ──┐
  1. bundle → 2. validate → 3. generate-artifacts  ↓               ↓
       ↓                                           10. publish
  4. validate-artifacts → 5. diff (drift gate)      ↓
       ↓                                           11. schedule-sync
  6. plan → 7. apply → 8. policy-apply → 9. verify     (Path A only)
└─────────────────────────────────────────────┘  └────────────────┘
```

| # | Command | What it does |
|---|---|---|
| 1 | `fluid bundle contract.fluid.yaml --format tgz` | Deterministic tgz + `MANIFEST.json` (SHA-256 merkle root). Root of trust for every downstream stage. |
| 2 | `fluid validate <bundle.tgz>` | Extension-routed: schema (JSON-Schema) + sqlglot (embedded SQL) + openapi-spec-validator (OpenAPI ports). |
| 3 | `fluid generate artifacts <bundle.tgz>` | Fanout: ODCS + ODPS-Bitol + OPDS + schedule DAGs + policy bindings. Writes a unified MANIFEST. |
| 4 | `fluid validate-artifacts dist/artifacts/` | Re-verifies SHA-256 + per-format schema validators. OPA `conftest` integration for policy tests. |
| 5 | `fluid diff --exit-on-drift --env <env>` | Compares live warehouse schema against contract. Hard gate — no plan against an unknown baseline. |
| 6 | `fluid plan --out runtime/plan.json --html` | Emits `bundleDigest` + `planDigest` cryptographic binding fields. `fluid apply` verifies both before any DDL. |
| 7 | `fluid apply --mode <mode> --env <env>` | Six apply modes: `dry-run`, `create-only`, `amend` (default), `amend-and-build`, `replace`, `replace-and-build`. Destructive modes (`replace*`) require `--allow-data-loss` outside dev. |
| 8 | `fluid policy-apply dist/artifacts/policy/bindings.json --mode enforce` | Enforces IAM/GRANT bindings. Runs AFTER apply (GRANTs need target objects) + BEFORE verify. |
| 9 | `fluid verify --strict --env <env>` | Post-apply reconciliation. Catches silent DDL coercions (`TIMESTAMP_NTZ→LTZ`, length truncations, constraint drops). |
| 10 | `fluid publish --target <name> [--target <name>]...` | Multi-target catalog publisher: `command-center`, `datamesh-manager`, `datahub`, `marketplace`, etc. `--target` is repeatable; accepts `NAME:endpoint` override. |
| 11 | `fluid schedule-sync --scheduler <name>` | Path-A DAG push to MWAA / Composer / Astronomer / Prefect / Dagster / self-hosted Airflow. Path-B schedules (EventBridge / Snowflake Tasks / MWAA) are applied in stage 7 via `SchedulePlanner`. |

### Apply Mode Matrix

| Mode | DDL | DML | Existing data |
|---|---|---|---|
| `dry-run` | render only | — | untouched |
| `create-only` | `CREATE IF NOT EXISTS` + fail-if-exists | — | untouched |
| `amend` (default) | `ALTER ADD COLUMN IF NOT EXISTS`; views `CREATE OR REPLACE` | — | preserved; new cols NULL |
| `amend-and-build` | same as `amend` | `dbt run` + `dbt test` | preserved; transforms refreshed |
| `replace` | auto-snapshot → `CREATE OR REPLACE TABLE` | — | **dropped**; backup retained |
| `replace-and-build` | same as `replace` | `dbt run --full-refresh` | **dropped**; rebuilt |

`--allow-data-loss` is required for `replace*` when `FLUID_ENV != dev` OR target has rows. `--no-verify-digest` is an emergency DR waiver for plan-binding checks (logged at WARNING level so audit trails catch it).

### Plan-Binding (Terraform-Style "Apply Consumes Exact Plan")

Stage 6 `fluid plan` emits two cryptographic fields in `plan.json`:
- `bundleDigest` — SHA-256 merkle root of the input bundle's MANIFEST. When input is a `.tgz` this pins the exact bundle.
- `planDigest` — SHA-256 over the plan body (digest fields masked). Catches tampering between stages 6 and 7.

Stage 7 `fluid apply` re-verifies both before any DDL. Mismatch → hard-fail with stable events:
- `apply_plan_digest_bundle_mismatch` — bundle was swapped after plan ran
- `apply_plan_digest_plan_tamper` — plan body edited since stage 6

### Install-Mode (Generated Jenkinsfiles)

`fluid generate ci --system jenkins --install-mode {pypi,dev-source}` picks how the GENERATED Jenkinsfile installs fluid at build time:

- **`pypi`** (default, production) — single `pip install data-product-forge` from stable PyPI. Exposes 4 build-time Jenkins params so operators swap TestPyPI / private mirror / pin a version from the "Build with Parameters" dialog:
  - `FLUID_PACKAGE_SPEC` (pin: `data-product-forge==X.Y.Z`)
  - `FLUID_PIP_INDEX_URL` (e.g. `https://test.pypi.org/simple/`)
  - `FLUID_PIP_EXTRA_INDEX_URL` (fallback for transitive deps)
  - `FLUID_ALLOW_PRERELEASE` (pip `--pre` for alpha/rc releases)
- **`dev-source`** (lab/contributor) — sets `PYTHONPATH=/forge-cli-src` so imports resolve LIVE from a bind-mounted forge-cli checkout. Fails LOUD with the exact docker-compose line to add if the mount is missing — no silent fallback to PyPI.

---

## For AI Coding Agents (Copilot, Cursor, Cline, Kiro, Claude Code, etc.)

> **Quick start for any agentic IDE.** Run `fluid scaffold-ide --target {kiro|cursor|claude-code|cline|generic}` in your data-product repo. It drops the right steering files, hooks, and MCP server config for your IDE so its agent can drive `fluid` natively. See [AGENT_IDE.md](AGENT_IDE.md) for the full walkthrough.

### Project Structure

```
forge-cli/
├── fluid_build/              # Python package — the CLI
│   ├── cli/                  # Command implementations (argparse-based)
│   │   ├── bundle.py         # Stage 1 — deterministic tgz + MANIFEST
│   │   ├── validate.py       # Stage 2 — extension-routed validators
│   │   ├── generate_artifacts.py  # Stage 3 — ODCS/ODPS/schedule/policy fanout
│   │   ├── validate_artifacts.py  # Stage 4 — SHA-256 re-verify + per-format validators
│   │   ├── diff.py           # Stage 5 — drift gate
│   │   ├── plan.py           # Stage 6 — emits bundleDigest + planDigest
│   │   ├── apply.py          # Stage 7 — 6-mode matrix + plan-binding verification
│   │   ├── policy_apply.py   # Stage 8 — IAM/GRANT enforcement
│   │   ├── verify.py         # Stage 9 — post-apply reconciliation
│   │   ├── publish.py        # Stage 10 — multi-target catalog publisher
│   │   └── schedule_sync.py  # Stage 11 — Path-A DAG push (Phase 7-rest)
│   ├── providers/            # Provider plugins: local, gcp, aws, snowflake, odps, odcs
│   │   ├── aws/plan/schedule.py    # SchedulePlanner — Path-B (EventBridge/MWAA) via stage 6
│   │   └── snowflake/orchestration/  # Path-B for Snowflake Tasks + MWAA
│   ├── build_runners/        # dbt + python script runners (extracted from legacy cli/execute.py)
│   ├── forge/                # AI-assisted project creation engine
│   │   └── core/
│   │       ├── bundle.py     # Phase 2 — tgz builder + MANIFEST + fragment extraction
│   │       ├── plan_digest.py    # Phase 6B — bundleDigest + planDigest helpers
│   │       ├── apply_modes.py    # Phase 6A — 6-mode enum + data-loss gate
│   │       └── pipeline_templates.py  # 7-system CI generator (Jenkins 11-stage + install-mode)
│   ├── policy/               # Policy compiler, agent policy, sovereignty
│   ├── blueprints/           # Enterprise blueprint registry
│   ├── credentials/          # Credential resolution (keyring, dotenv, encrypted)
│   ├── schemas/              # FLUID JSON Schema versions (0.5.7, 0.7.1, 0.7.2 latest)
│   ├── templates/            # Init templates (hello-world, customer-360, etc.)
│   └── tools/                # Diagnostic utilities
├── tests/                    # Pytest suite — unit, integration, provider-specific
├── scripts/                  # Smoke scripts — smoke_phase_6b.py, smoke_a1.py
├── examples/                 # Progressive learning examples (01-hello-world → customer360)
├── docs/                     # Documentation site source
├── pyproject.toml            # Package metadata, dependencies, tool configs
├── Makefile                  # Developer ergonomics — `make setup`, `make test`, `make build`
└── AGENTS.md                 # This file
```

### Key Entry Points

| What | Where |
|------|-------|
| CLI entrypoint | `fluid_build/cli/__init__.py` → `main()` |
| Command implementations | `fluid_build/cli/*.py` (one per 11-stage step, plus supporting commands) |
| Provider plugins | `fluid_build/providers/{local,gcp,aws,snowflake,odps,odcs}/` |
| Path-B schedulers | `fluid_build/providers/aws/plan/schedule.py` (EventBridge/MWAA/Lambda/Step-Functions via stage 6 `SchedulePlanner`) |
| Contract schemas | `fluid_build/schemas/*.json` (0.7.2 is latest; `SchemaManager.latest_bundled_version()` is the dynamic lookup) |
| Policy engine | `fluid_build/policy/` (compiler, agent_policy, sovereignty, guardrails) |
| Plan-binding helpers | `fluid_build/forge/core/plan_digest.py` — `inject_digests`, `verify_plan_binding`, `PlanBindingError` |
| Apply mode matrix | `fluid_build/forge/core/apply_modes.py` — `ApplyMode` enum + `check_data_loss_gate` + `resolve_mode_with_build_alias` |
| Bundle builder | `fluid_build/forge/core/bundle.py` — deterministic tgz + MANIFEST + fragment extraction + `$source` sentinel |
| Build runners | `fluid_build/build_runners/{dbt,python}/` — extracted from legacy `cli/execute.py` in Phase 1 |
| CI template generator | `fluid_build/forge/core/pipeline_templates.py` — 7-system generator; Jenkins ships the full 11-stage parameterized template with `--install-mode {pypi,dev-source}` |
| Forge (AI creation) | `fluid_build/forge/` (templates, generators, extensions) |
| Log redaction (global) | `fluid_build/observability/secret_redactor.py` — `SecretRedactingFilter` wired into Python logging |
| SQL safety helpers | `fluid_build/providers/_sql_safety.py` — `validate_ident`, `quote_string_literal` (required for every DDL f-string) |
| Test suite | `tests/` — mirrors `fluid_build/` structure |
| Smoke scripts | `scripts/smoke_phase_6b.py` + `scripts/smoke_a1.py` — operator / contributor validation against real Snowflake |

### Development Commands

```bash
make setup          # One-command setup: venv + deps + doctor
make test           # Run pytest suite
make lint           # Ruff + Black check
make fmt            # Auto-format
make build          # Build wheel
make doctor         # Run system diagnostics
make demo           # validate → plan → apply on example contract
```

### When Modifying Code

- **Adding a CLI command**: Create `fluid_build/cli/<command>.py`, register in `fluid_build/cli/__init__.py`
- **Adding a provider**: Create `fluid_build/providers/<name>/`, implement `BaseProvider` interface
- **Adding a template**: Create `fluid_build/templates/<name>.j2` or add to `fluid_build/forge/templates/`
- **Adding a policy rule**: Extend validators in `fluid_build/policy/`
- **Modifying the contract schema**: Update `fluid_build/schemas/` and bump `fluidVersion`. Never hardcode the new version as a literal — `SchemaManager.latest_bundled_version()` scans the schemas directory and returns the newest, so fallbacks track future bumps automatically.
- **Data-product type vocabulary** (v0.7.3+): contracts can use medallion (`metadata.layer ∈ {Bronze, Silver, Gold, Platinum}`) or Data Mesh (`metadata.productType ∈ {SDP, ADP, CDP}`) — they're equivalent. Canonical mapping: **Bronze↔SDP** (Source-Aligned Data Product), **Silver↔ADP** (Aggregated), **Gold↔CDP** (Consumption-Aligned). Either-or-both is accepted; when both are set the validator enforces consistency. Cross-check is duplicated inline at three call sites (`fluid_build/schema.py::_check_metadata`, `fluid_build/schema_manager.py::_validate_with_jsonschema`, `fluid_build/cli/contract_validation.py::_validate_metadata`) — keep them in lockstep when changing the mapping. Provider tag/label propagation in `providers/{aws,gcp,snowflake}` and `forge/core/provider_actions.py` emits both as separate cloud labels (`fluid_layer` + `fluid_product_type`).
- **Adding a new apply mode**: extend `fluid_build/forge/core/apply_modes.py::ApplyMode` enum + `CANONICAL_CHOICES` list + predicates (`is_destructive`, `needs_build`, etc.) + `check_data_loss_gate` if the mode is destructive. The mode must map to a provider-native DDL flavor in `providers/<provider>/actions/*.py`.
- **Changing what `fluid plan` emits**: `plan.json` must carry both `planDigest` and `bundleDigest` (re-run `inject_digests` after any mutation to the dict). Actions must carry BOTH `op` and `action_type` fields — apply.py's provider dispatcher reads `op`, display/viz reads `action_type`. Dropping either silently breaks the pipeline.
- **Generating a new CI system template**: extend `fluid_build/forge/core/pipeline_templates.py`. The 11-stage sequence + the `--install-mode {pypi,dev-source}` semantics must be preserved across systems. Jenkins is the reference implementation; GitHub Actions / GitLab / Azure / Bitbucket / CircleCI / Tekton port from it.
- **Adding a new Path-B scheduler**: extend `fluid_build/providers/<provider>/plan/schedule.py` with a `SchedulePlanner`-compatible interface. Stage 6 `fluid plan` invokes it; stage 7 apply executes the resulting actions alongside DDL.
- **Adding a new publish target**: register in `fluid_build/cli/publish.py` + `fluid_build/cli/market.py`. `--target` is repeatable; the target name is matched case-insensitively against the registry. Use the `NAME:endpoint` override shape for per-target URL customization.
- **Adding a copilot tool that takes a path**: accept `workspace_root: Optional[Path]` as a kwarg from `dispatch_tool_call`; confine the LLM-supplied path with `resolved.relative_to(workspace_root)` and apply a suffix allow-list + size cap. Reference implementation: `cli/forge_copilot_tools.py::_dispatch_read_sample_schema`.
- **Emitting new SQL DDL**: identifiers must go through `fluid_build/providers/_sql_safety.py::validate_ident`; string literals through `quote_string_literal`. Inline `.replace("'", "''")` is considered a regression — it diverges from the central helper.
- **Adding a secret pattern to logs**: extend `fluid_build/providers/snowflake/util/logging.py::SENSITIVE_PATTERNS`/`SENSITIVE_KEYS` AND mirror the addition into `fluid_build/observability/secret_redactor.py` to keep the two redactor layers symmetric.

### Code Conventions

- Python 3.10+ target
- Line length: 100 (Ruff + Black)
- Type hints encouraged but not enforced (`mypy` runs with `ignore_missing_imports`)
- Logging via `fluid_build/structured_logging.py` (structured JSON logs)
- Rich console output for user-facing messages
- Test markers: `@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.gcp`, etc.

### Security Invariants

A 2026-04-16 security review (see `SECURITY_REVIEW.md` if committed, or the PR #28–#33 series) established these invariants. Changes to the listed code paths should preserve them.

- **Two redaction layers stay symmetric.** `observability/secret_redactor.py::SecretRedactingFilter` is the global log filter; `providers/snowflake/util/logging.py` has a Snowflake-provider-local twin. New secret-shaped patterns (JWT, Stripe, GitHub token, provider-specific key names) should land in both.
- **Path validation is order-sensitive.** `cli/security.py::validate_input_path` calls `_reject_raw_traversal(raw)` BEFORE `Path.resolve()` (so `..` can't be silently collapsed), then runs `_validate_path_security(resolved)` for forbidden-path checks. `FORBIDDEN_PATHS` is platform-aware via `_build_forbidden_paths()` — macOS includes `/private/etc` because `/etc` resolves there.
- **Copilot tool confinement.** Path-accepting copilot tools (`read_sample_schema`, `discover_workspace`) receive a `workspace_root` kwarg from `cli/forge_copilot_tools.py::dispatch_tool_call` and MUST confine the LLM's path argument. `discover_workspace` ignores the LLM's own `workspace_path` argument by design — scope is fixed by the invoking CLI.
- **Encrypted credential store fails loud.** `credentials/encrypted_store.py::_load_store` raises `CredentialError` on `InvalidToken` — never silently returns `{}` (which previously caused destructive overwrite on next write with the wrong key).
- **Tool errors are typed, not text.** `dispatch_tool_call` returns `{"error": <ExcName>, "message": "Tool … failed — see server logs"}` on exception; the full `exc` goes to `LOG.warning(..., exc_info=True)` where the redactor can scrub it. Exception text is never round-tripped into the LLM context (prevents path / hostname / env-var leaks).
- **Subprocess argv sanitisation.** `cli/auth.py::_sanitize_argv` redacts values of `--password`, `--token`, `--api-key`, `--key-file`, any flag ending in `-secret`/`-key`/`-token`/`-password`/`-passphrase`. Called from `AuthProvider._run_command` before DEBUG-logging the command.

---

## For AI Agents Consuming Data Products

FLUID contracts include first-class **agent policies** that govern how AI/LLM systems may access, process, and store data. This is the `agentPolicy` block in `contract.fluid.yaml`.

### Agent Policy Schema

```yaml
exposes:
  - exposeId: sales_metrics
    kind: table
    policy:
      agentPolicy:
        # Which models may access this data
        allowedModels:
          - gpt-4
          - claude-3-opus
        deniedModels:
          - llama-3-70b          # No open-source models for this data

        # What the AI may do with the data
        allowedUseCases:
          - inference            # Read-only analysis
          - summarization        # Report generation
          - analysis             # Trend analysis
        deniedUseCases:
          - training             # Never train on this data
          - fine_tuning          # Never fine-tune on this data
          - embedding            # No vector embeddings

        # Operational limits
        maxTokensPerRequest: 8192
        maxTokensPerDay: 1000000

        # Storage and reasoning
        canStore: false          # May the agent persist this data?
        canReason: true          # May the agent do multi-step reasoning?

        # Retention
        retentionPolicy:
          maxRetentionDays: 90
          requireDeletion: true

        # Audit
        auditRequired: true
        purposeLimitation: "Sales reporting only — no customer profiling"
```

### Policy Enforcement Levels

| Level | `allowedModels` | `allowedUseCases` | `canStore` | Example |
|-------|-----------------|-------------------|------------|---------|
| **Blocked** | `[]` (empty) | All denied | `false` | Raw PII, financial transactions |
| **Restricted** | Named models only | Named use cases only | `false` | Internal analytics, HR data |
| **Moderate** | Named models | Broad use cases | `true` with retention | Aggregated metrics, dashboards |
| **Open** | Not specified | Not specified | `true` | Public datasets, market data |

### How Agents Should Respect Policies

1. **Before accessing a data product**, read its `agentPolicy` from the contract
2. **Check your model identity** against `allowedModels` / `deniedModels`
3. **Check your use case** against `allowedUseCases` / `deniedUseCases`
4. **Respect token limits** — honour `maxTokensPerRequest` and `maxTokensPerDay`
5. **Respect storage rules** — if `canStore: false`, do not persist any data beyond the session
6. **Respect retention** — if `retentionPolicy.requireDeletion: true`, delete data after `maxRetentionDays`
7. **Log access** if `auditRequired: true`

### Validating Agent Policies

```bash
# Validate all policies in a contract
fluid policy-check contract.fluid.yaml

# Compile policies to provider-native IAM
fluid policy-compile contract.fluid.yaml

# Run the full agent policy validator
fluid contract-validation contract.fluid.yaml
```

The `AgentPolicyValidator` in `fluid_build/policy/agent_policy.py` checks for:
- Unknown models (warns if not in the known model registry)
- Conflicting allow/deny lists
- Missing required fields for restricted data
- Use case validity against the FLUID schema
- Token limit sanity checks

---

## For AI-Assisted Project Creation (Forge)

FLUID Forge includes AI-powered project creation via a 5-mode picker:

```bash
fluid forge                          # 5-mode picker: AI / Compose / Refine / Template / Blank
fluid forge --blank                  # No-AI scaffold
fluid forge --template starter       # Template (alias --scaffold)
fluid forge --refine contract.yaml   # Iterate on existing contract (1 question)
fluid forge --from-product <id>      # Compose ADP/CDP from existing products
fluid forge --data-product-type ADP  # Set Data Mesh productType (or Silver alias)
fluid forge --yes                    # Skip the [Y/n] prompt; cost still shown
fluid forge --show-work              # Stream agent reasoning + tool calls
```

The interview is **mode-aware**: compose runs ~3 questions seeded from upstream schemas, refine asks "what to change?", standard mode runs the world-class bootstrap (detect-first inferences, examples in every prompt, productType-first, `:auto` escape, schema-coverage gate). Slash commands inside the interview: `:ai-setup`, `:override`, `:show-work`, `:doctor`, `:help`, `:quit`.

Cost is visible before it's spent: every run renders `.fluid/agents/<run-id>/{cost.json,reasoning.md,transcript.json}` and `.fluid/forge-receipt.json`. Aggregate across runs via `fluid stats --by provider|type|engine`.

Opt into the **LiteLLM unified backend** for one API across OpenAI / Anthropic / Gemini / Bedrock / Azure / Vertex / Groq / Cohere / Mistral / Ollama:

```bash
pip install "fluid-build[litellm]"
export FLUID_LLM_BACKEND=litellm
fluid forge --llm-provider gemini --llm-model gemini-2.5-flash
```

### Domain Agents

The `fluid_build/cli/forge_agents.py` module implements domain-specific AI agents:

| Agent | Domain | What It Creates |
|-------|--------|----------------|
| Analytics Agent | Business analytics | Dashboards, KPI tracking, trend analysis |
| ML Pipeline Agent | Machine learning | Feature stores, model serving, experiment tracking |
| ETL Agent | Data engineering | Ingestion pipelines, transformations, quality gates |
| Streaming Agent | Real-time data | Event processing, windowing, alerting |
| Compliance Agent | Governance | Policy-first contracts with full access control |

Each agent:
1. Asks domain-specific questions about your requirements
2. Analyzes requirements and recommends templates, providers, and patterns
3. Generates a complete FLUID project with contract, tests, and CI config
4. Validates the generated contract against the FLUID schema

### Extending with Custom Agents

```python
from fluid_build.cli.forge_agents import AIAgentBase

class MyDomainAgent(AIAgentBase):
    def __init__(self):
        super().__init__(
            name="my-domain",
            description="Custom agent for my domain",
            domain="my-domain"
        )

    def get_questions(self):
        return [
            {"id": "goal", "text": "What is your data product goal?", "type": "text"},
            {"id": "provider", "text": "Target cloud?", "type": "choice",
             "options": ["local", "gcp", "aws", "snowflake"]},
        ]

    def analyze_requirements(self, context):
        return {
            "recommended_template": "analytics",
            "recommended_provider": context.get("provider", "local"),
            "suggested_quality_rules": ["not_null", "uniqueness"],
        }
```

---

## Data Sovereignty & Guardrails

FLUID contracts support data sovereignty constraints that agents must respect:

```yaml
sovereignty:
  jurisdiction: EU
  residency: strict
  allowedRegions:
    - europe-west1
    - europe-west3
```

The CLI enforces these at plan time — any deployment to a non-allowed region is rejected. AI agents operating on governed data should similarly restrict data movement to allowed jurisdictions.

---

## MCP / Tool Use Integration

FLUID CLI commands are designed to be composable and machine-readable:

```bash
# All commands support --out for structured JSON output
fluid validate contract.fluid.yaml --out validation.json
fluid plan contract.fluid.yaml --out plan.json
fluid apply contract.fluid.yaml --out apply.json

# Graph output for dependency analysis
fluid graph contract.fluid.yaml --format dot
fluid graph contract.fluid.yaml --format json

# Policy reports
fluid policy-check contract.fluid.yaml --out policy-report.json
```

For MCP (Model Context Protocol) tool servers, these JSON outputs can be piped directly into agent tool responses. The structured output includes:
- Validation results with line-level error locations
- Execution plans with resource diffs
- Policy compliance reports with violation severity

---

## Cross-stage run-id, ADP replay, late-arrival, federation (2026-05-02)

These four orthogonal hardening bands landed in one push; the
machinery is described in detail in `CLAUDE.md` under "Recent
architectural changes worth knowing (2026-05-02, world-class hardening)".

### Run-id correlation

Every `fluid <stage>` decorated with `@traced_stage(...)` auto-stamps
`fluid.run_id` on its OTel root span via
`observability/run_id.py::get_or_create_run_id()`. Resolution order:

1. `$FLUID_RUN_ID` env var (operator override / CI injection).
2. `.fluid/run-id.txt` (persisted between stages).
3. Newly generated 12-char hex id (first stage of a run).

All 11 CLI stages now wear the decorator: `bundle, plan, apply,
verify, publish, schedule_sync, validate, diff, generate_artifacts,
validate_artifacts, policy_apply`. Operators query by
`fluid.run_id="..."` to reconstruct a multi-stage run.

### ADP auto-replay (replay marker schema)

`build_runners/_state.py::FileStateStore.set_cursor` detects rewinds,
walks `consumes[]` to find downstream products, writes a JSON marker
at `.fluid/<downstream-product-id>/runtime/replay-pending.json`:

```json
{
  "upstream_product_id": "bronze.crm.customers",
  "upstream_build_id": "main_build",
  "upstream_stream": "default",
  "old_cursor_value": "2026-04-30T00:00:00Z",
  "new_cursor_value": "2026-04-15T00:00:00Z",
  "detected_at": "2026-05-02T12:30:00Z",
  "reason": "upstream cursor rewound from <old> to <new>"
}
```

`fluid status` surfaces the dirty list via `list_dirty_products()`.
`clear_dirty_marker()` runs on `fluid apply --replay` completion.
Single chokepoint integration: every cursor-writing runner
(Kafka-Connect / Debezium / DLT / duckdb / Meltano) benefits without
per-runner wiring.

### Streaming late-arrival (connector config keys + SQL splitter)

`build_runners/_late_arrival.extract_late_arrival_policy` reads
`WatermarkSpec.allowed_lateness` (ISO-8601 duration) and surfaces as
connector config under stable keys:

| Connector config key | Type | Meaning |
|---|---|---|
| `fluid.late_arrival.enabled` | string | `"true"` / `"false"` |
| `fluid.late_arrival.allowed_lateness_seconds` | string | budget in seconds |
| `fluid.late_arrival.side_output_table` | string | `<target>__late_events` |

Wired into `kafka_connect/runner.py` and `debezium/runner.py` (declarative;
SMT / sink-side enforcer reads these keys).

For Python-side runners (duckdb / DLT) where there's no Kafka SMT,
`split_late_events_in_duckdb()` does the actual SQL split:
``rows where event_time < max(event_time) - budget`` move to
``<basename>__late_events.<ext>`` and are deleted from the main file.
Wired into `duckdb/runner.py::_enforce_late_arrival_split`.

### Cross-mesh federation

`forge/federation.py` ships three live-fetch backends called from
`fetch_federated_digest`:

* `_fetch_digest_via_http` — GET `<endpoint>/<product>/<version>/digest`
  returning `sha256:...`.
* `_fetch_digest_via_catalog` — REST GET returning JSON
  `{"digest": "..."}`.
* `_fetch_digest_via_git` — gitpython primary (in-process clone +
  pull); shell-out fallback when gitpython unavailable. Caches
  cloned repos under `~/.cache/fluid/federation-git/<workspace_id>`.

Manifest at `federation/upstreams.yaml`:

```yaml
workspaces:
  - id: telco-billing
    kind: git_registry           # | catalog | http_registry
    endpoint: https://github.com/acme/telco-billing-mesh
    auth:
      mode: github_token         # | basic | oidc | bearer | none
      secret_ref: GITHUB_TOKEN   # env var name; resolved at fetch time
```

`validate_federated_consumes` is wired into `cli/apply.py` as a
stage-7 gate. Drift produces `CLIError(event="apply_consumes_drift",
context={"kind": "upstream-mismatch", "violations": [...]})`. The
`--no-verify-digest` flag is the DR escape hatch and logs at
WARNING level so audit trails catch the override.

### Phase 2.6 — `from-source --source postgres|mysql|sqlite`

`fluid forge data-model from-source --source postgres --uri
postgresql://...` now uses the duckdb-extension scanner via
`cli/discover/_jdbc_introspect.py::introspect_jdbc()`. Supported
shapes:

* `postgresql://user:pass@host:5432/db`
* `postgres://user:pass@host:5432/db` (alias)
* `mysql://user:pass@host:3306/db`
* `sqlite:///path/to/db.sqlite`

The dispatcher in `cli/forge_data_model.py::run_from_source_command`
branches on `args.source ∈ {postgres, postgresql, mysql, sqlite}` to
the JDBC path; everything else goes through the existing catalog
adapter resolver chain.

### File LOC reductions in flight

Top files >1500 LOC are being extracted to natural seams using the
**module-attribute-access indirection pattern** (`_module = original;
... _module._fn(...)`) so test patches on the original module flow
through to the extracted helper. Reductions this session:

* `cli/market.py` — 2614 → 2493 (-121 LOC) via `cli/_market_render.py`
  (format_table_output + format_detailed_output + format_json_output).
* Prior sessions: forge_modes (-1204), auth (-1409), modeler_agent
  (-654), interview (-936), viz_graph (-486), init (-238).

Files still over 1500 LOC (slated for follow-up): `datamesh_manager.py`
(2595), `forge_modes.py` (2459), `init.py` (1920), `ai_setup.py`
(1888), `forge_data_model.py` (1825 — gained ~120 from Phase 2.6),
`forge_copilot_llm_providers.py` (1687), `forge_copilot_tools.py`
(1614), `forge_copilot_runtime.py` (1590), `apply.py` (~1580 with
the federation gate), `coordinator.py` (1522).

## Plugin extension points (2026-05-12)

The CLI exposes three Python `entry_points` groups that external packages
can register against to extend functionality. All three are discovered via
`importlib.metadata.entry_points()` at runtime; no in-tree plugin manifest
is required. Failure-trap and pre-redaction behavior is consistent across
all three (see `SECURITY.md` for the trust model).

| Group | Hook site | Plugin signature | What it does |
|---|---|---|---|
| `fluid_build.commands` | `cli/bootstrap.py::register_core_commands` | `register(subparsers: argparse._SubParsersAction) -> None` | Add new `fluid <name>` subcommands. Run once at CLI startup. |
| `fluid_build.extension_validators` | `cli/validate.py::_run_extension_validators` | `validate(extensions_block: Dict, errors: List[str]) -> None` | Validate sub-keys of `contract.extensions`. Errors fold into `ValidationResult`. |
| `fluid_build.apply_hooks` | `cli/apply.py::_run_apply_hooks` | `hook(contract_dir: Path, contract: Dict, errors: List[str]) -> None` | Apply-time invariant checks (e.g. scaffold bundle digest drift). |

**Trust boundary.** Plugins are uncontained Python loaded in-process.
Trust = pip trust. The CLI defends against three failure modes:
(1) plugin exceptions don't crash the CLI (wrapped in `try/except`);
(2) plugin exception text and error messages are pre-scrubbed with
`redact_secret_text` before reaching logs or the errors list; (3) apply
hooks receive `copy.deepcopy(contract)` so a buggy or malicious hook
cannot mutate the contract for the rest of apply. Test pinning lives at
`tests/test_cli_plugin_hooks.py`.

**Override flags.** `fluid apply --force-pattern-drift` downgrades apply
hook errors to warnings (escape hatch for legitimate drift, e.g. a tag
moved underneath you in a development scenario). Nothing else silently
ignores hook output.

The reference plugin for `extension_validators` + `commands` is the
`data-product-forge-custom-scaffold` package; reading its entry-point
declarations in its `pyproject.toml` is the canonical "how to write a
plugin" example.

## Links

- **Documentation**: [https://agenticstiger.github.io/forge_docs/](https://agenticstiger.github.io/forge_docs/)
- **PyPI**: [https://pypi.org/project/data-product-forge](https://pypi.org/project/data-product-forge/)
- **Repository**: [https://github.com/Agenticstiger/forge-cli](https://github.com/Agenticstiger/forge-cli)
- **Agent Policy Examples**: `examples/0.7.1/ai-restricted-data.yaml`
- **Policy Validator Source**: `fluid_build/policy/agent_policy.py`
- **Forge Agents Source**: `fluid_build/cli/forge_agents.py`

---

*FLUID Forge — Declarative Data Products for the Agentic Era*
*Copyright 2024–2026 Agentics Transformation Pty Ltd — [fluidhq.io](https://fluidhq.io)*
