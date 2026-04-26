# LLM Provider Capability Matrix

forge-cli supports five LLM providers via a single `LlmProvider` ABC.
Exactly one provider is active per run — picked via `--llm-provider`,
`$FLUID_LLM_PROVIDER`, or the saved config in `~/.fluid/ai_config.json`.
Tiered mode (`--tiered`) chooses *different models within the same
provider*, never across providers.

## Capability matrix

| Capability                | anthropic | openai | gemini | ollama | azure-openai |
|---------------------------|:---------:|:------:|:------:|:------:|:------------:|
| Default model (catalog)   | claude-sonnet-4-6 | gpt-4.1 | gemini-2.5-pro | `$FLUID_OLLAMA_MODEL` | `$FLUID_AZURE_DEPLOYMENT` |
| `temperature=0` pinned    | ✓         | ✓      | ✓      | ✓      | ✓            |
| `seed=42` pinned          | —         | ✓      | —      | ✓      | ✓            |
| Structured output         | tools forcing | `json_schema` strict | `response_schema` | `format=json` | `json_schema` strict |
| Extended thinking         | ✓ (Opus)  | —      | —      | —      | —            |
| Streaming SSE             | ✓         | ✓      | ✓      | ✓      | ✓            |
| Tool use / agent loop     | ✓         | ✓      | ✓      | —      | ✓            |
| Provider-native caching   | ✓ `cache_control: ephemeral` | ✓ auto | — | — | ✓ auto |
| Multi-dialect OSI emit    | ANSI + engine | ANSI + engine | ANSI + engine | ANSI only | ANSI + engine |
| Tiered mode               | 3 distinct | 3 distinct (4.1, 4.1-mini, 4.1-nano) | 2 distinct (pro, flash) | collapses to single | 3 user-defined |

`temperature=0` is pinned in every provider's `build_request`. The
plan target is full sampling determinism modulo provider-side
model-version drift; pinning the temperature lever is what makes
that determinism real on Anthropic/Gemini (where no public `seed`
parameter exists yet).

## One-time setup

```bash
fluid ai setup
```

Asks for a provider, an API key, a default model, and (optionally)
whether to enable tiered mode. Provider and model choices are stored
in `~/.fluid/ai_config.json` (mode 600). API keys are stored in the
OS keyring whenever possible and are not written to the JSON config by
default.

If a machine has no usable keyring backend, the key is kept only for
the current process. Operators who deliberately want a local plaintext
fallback can opt in with `FLUID_ALLOW_PLAINTEXT_AI_SECRETS=1`.

## Per-run provider override

```bash
# Explicit at the command line
fluid forge data-model from-intent intent.yaml -o out.yaml --llm-provider gemini

# Or via env var
FLUID_LLM_PROVIDER=gemini fluid forge data-model from-intent ...
```

The active provider is asserted at the coordinator boundary — every
LLM call must run against `session.active_provider` or the run
fails fast with a typed `AgentExecutionError("Provider leak")`. This
is the safety net that enforces "one provider per run" without
requiring the user to micromanage individual stage calls.

## Adding a new provider

The `LlmProvider` ABC at
`fluid_build/cli/forge_copilot_llm_providers.py:236` defines the
contract. To add (say) AWS Bedrock:

1. Implement `class BedrockProvider(LlmProvider)` with the three
   required methods (`default_endpoint`, `build_request`,
   `extract_text`) and any optional ones (`extract_usage`,
   `build_streaming_request`, `iter_stream_chunks`,
   `build_tool_request`).
2. Register in `BUILTIN_LLM_PROVIDERS`:
   `"bedrock": BedrockProvider()`.
3. Add `PROVIDER_ENV_VARS["bedrock"] = "AWS_ACCESS_KEY_ID"` (or
   equivalent).
4. Add a `tiers.bedrock` entry to `cli/llm_models.json` mapping
   `deep` / `balanced` / `fast` to model IDs.
5. Add a `to_bedrock_spec()` helper in
   `fluid_build/copilot/schemas/stage_outputs.py` (or reuse
   `to_anthropic_tool()` if the API surface is Anthropic-shaped).

Zero changes to the coordinator, the stage agents, the cache, or
the schemas. That's the modularity contract `LlmProvider` exists to
provide.

## Observability per provider

Every staged LLM call emits an OTEL span with provider, model,
stage, latency, and (when supported) token usage. The retry
envelope (`retry_with_backoff`, 3 attempts, exponential backoff)
wraps every provider invocation so transient 5xx / network blips
recover without a hard fail. See `tests/copilot/test_base_agent_retry.py`
for the per-attempt timing contract.

## Provider-specific quirks

### Anthropic

* `temperature` was historically omitted (Claude defaults to 1.0);
  V1.3.3 added the explicit pin so `--deterministic` actually
  delivers determinism on Claude.
* The `system` field uses a blocks array with `cache_control:
  ephemeral` so the system prompt prefix is cached for ~10× input-
  token savings on repeated runs.
* Structured output is forced via a single `emit_forge_contract`
  tool call — the model can't return text outside the schema.

### OpenAI

* Strict `response_format: json_schema` mode for `gpt-4o-mini` and
  newer models (`gpt-4o-2024-08-06+`); falls back to `json_object`
  for older models.
* `seed=42` is the only provider-side knob that produces byte-
  identical responses across runs; the audit-replay path depends
  on it.

### Gemini

* `responseSchema` is supported but has trouble with deeply-nested
  free-form objects (the `contract` field is 10+ levels deep) —
  forge-cli skips structured-output enforcement on Gemini and
  relies on the natural-language JSON nudge in the system prompt
  + the validator's repair loop instead.

### Ollama

* Local-only; the bearer-token header is stripped after
  `super().build_request`. Recent Ollama builds (≥0.4) accept the
  OpenAI-compat `format=json` directive; older ones fall back to
  the natural-language JSON nudge.
* Structured-output enablement is catalog-gated per local model.
  `gemma4:latest` is pinned because it is part of the provider E2E
  matrix; unknown Ollama models keep prompt-only JSON discipline until
  they are added to `llm_models.json` or the user override catalog.
* No distinct tiers in the catalog — `--tiered` collapses to
  single-model with a one-line warning. See
  `tests/test_tier_collapse_warning.py` for the regression pin.

## Provider E2E trend monitoring

Phase 7 of `scripts/e2e_all_modes.py` writes provider scorecard history to
`.fluid/e2e_report/provider_scorecard_history.jsonl`. Each row is scoped by
provider, model, and scenario set so latency and repair-rate regressions are
only compared against like-for-like runs.

Trend gates are enabled by default:

* `FLUID_E2E_MAX_REPAIR_RATE_DELTA` defaults to `0.0`; any repair-rate
  regression against the recent baseline is an error.
* `FLUID_E2E_MAX_AVG_RUN_SECONDS_DELTA` defaults to `60.0`; latency can move
  by one minute before it is treated as a regression.
* `FLUID_E2E_TREND_BASELINE_RUNS` defaults to `5`.
* `FLUID_E2E_TREND_MIN_HISTORY_RUNS` defaults to `1`.
* `FLUID_E2E_SCORECARD_TRENDS=0` disables trend comparison.
* `FLUID_E2E_SCORECARD_HISTORY=0` disables appending new history rows.
* `FLUID_E2E_SCORECARD_HISTORY_LIMIT` defaults to `200` retained rows.

When no JSONL history exists yet, the harness backfills a baseline from older
`.fluid/e2e_report/*/results.json` files, so local exploratory runs can still
surface drift before CI has built up its own history.

### Azure OpenAI

* Same wire format as OpenAI; deployment names replace model IDs.
* `tiers.azure-openai` maps deployment names per tier; users set
  `$FLUID_AZURE_DEEP`, `$FLUID_AZURE_BALANCED`, `$FLUID_AZURE_FAST`
  to their organisation's deployment names.
