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

## Working style

- **Goal-driven execution.** For non-trivial tasks, state success criteria before implementing and loop until verified. Transform imperatives into tests: "add redactor coverage" → "new pattern is masked by a test assertion"; "fix the bug" → "reproducing test passes first, then fix makes it green".
- For multi-step work, write a brief plan with per-step checks (`1. step → verify: check`). Strong criteria let you iterate without asking; weak criteria ("make it work") do not.

## Commit / PR etiquette

- Conventional-commits style (`feat(scope):`, `fix(scope):`, `chore(ci):`, `security: …`). See the git log for the in-use shape.
- `main` is protected. All changes go via PR. CI (`.github/workflows/ci.yml`) must be green before merge.
- `ci.yml` triggers on `push: branches:[main]` and `pull_request:`. GitHub's rule that `GITHUB_TOKEN` merges don't trigger downstream workflows can leave a merge commit without a CI run — trigger it manually with `gh workflow run ci.yml --ref main` (`workflow_dispatch` was added for exactly this reason in PR #27).
- Never add `Co-Authored-By: Claude …` trailers to commits (repo-wide preference).
