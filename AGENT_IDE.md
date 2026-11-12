# AGENT_IDE.md — Drive forge-cli from any agentic IDE

> One command bootstraps your IDE so its agent can drive `fluid` natively. Works
> with Kiro, Cursor, Claude Code, Cline, or any AGENTS.md-aware IDE.

---

## TL;DR

```bash
pip install "data-product-forge[standard]"   # AI authoring + the MCP server
cd your-data-product-repo
fluid scaffold-ide --target kiro      # or cursor / claude-code / cline / generic
fluid forge                           # AI authoring (mode picker)
```

That's it. Your IDE now has:

- **Agent Skills** (agentskills.io `SKILL.md`): `authoring-fluid-contracts`,
  `composing-fluid-data-products`, `deploying-fluid-contracts-safely` and
  `troubleshooting-fluid-errors`, each loaded when its task comes up.
- **A short always-loaded index** (about 2k characters) that names each skill and
  the rules an agent must never break.
- **An MCP server** (`fluid mcp serve`, auto-spawned by the IDE) exposing 16
  sandboxed tools for catalog / model / contract work.
- **Hooks** (Kiro and Claude Code only — Cursor/Cline lack a hook substrate)
  that run `fluid validate` after each contract edit and `fluid bundle` plus
  `fluid validate` on the bundle before commit.

---

## What `fluid scaffold-ide` writes per target

| Target | Files emitted | Skills | Hook substrate |
|---|---|---|---|
| `kiro` | `.kiro/{settings/mcp.json, steering/fluid.md, hooks/, specs/}` | `.kiro/skills/` | yes (event-driven) |
| `cursor` | `.cursor/{mcp.json, rules/fluid.mdc, HOOKS.md}` | `.agents/skills/` | no (agent-driven) |
| `claude-code` | `.mcp.json` + `CLAUDE.md` block + `.claude/settings.local.json` | `.claude/skills/` | yes (PostToolUse) |
| `cline` | `.cline/mcp_settings.json` + `.clinerules/fluid.md` | `.agents/skills/` | no (agent-driven) |
| `generic` | `mcp.json` + `AGENTS.md` block | `.agents/skills/` | no |

All targets share **one canonical pack**, the skills under
`fluid_build/agent_skills/skills/`: any forge-cli upgrade updates the content in
one place and `fluid scaffold-ide --force` refreshes every IDE the team uses. An
existing `CLAUDE.md` or `AGENTS.md` keeps everything outside the marked block, and
the validate hook goes into `.claude/settings.local.json`, because it names this
machine's interpreter, and is added to `.gitignore`; that file keeps its other settings,
and only a handler an earlier scaffold-ide wrote into the shared `.claude/settings.json`
is removed from it.

The Claude Code hook reads the edited file's path from the JSON Claude Code passes
on stdin, validates only `*.fluid.yaml` / `*.fluid.yml` / `*.fluid.json` files, and
exits 2 with the errors when validation fails, which Claude Code shows to the agent.
It runs its script by path and calls the `fluid` console script, never `python -m`,
which would put the project directory, where the agent writes, first on `sys.path`.

## Install the skills without `scaffold-ide`

The same skills ship as a Claude Code plugin and in a layout `npx skills add` reads:

```text
/plugin marketplace add Agenticstiger/forge-cli
/plugin install fluid@forge-cli
```

```bash
npx skills add Agenticstiger/forge-cli
```

The plugin also carries the validate hook. Neither route writes the MCP config.

---

## The two channels into forge-cli

After scaffolding, the IDE's agent has **two complementary channels**:

1. **MCP tools** (16, sandboxed, structured) for surgical work:
   - `list_source_adapters`, `list_source_tables`, `inspect_source_table`,
     `list_source_lineage`, `list_source_glossary` — catalog discovery.
   - `read_logical_model`, `update_entity`, `add_relationship`,
     `regenerate_physical` — logical-model edits (round-trip via the sidecar).
   - `validate_contract`, `diff_models`, `search_semantic_memory` — checks.
   - `forge_from_source` — bootstrap a logical model from a source table.
   - `forge_run` — run `fluid forge` with the IDE's LLM through MCP sampling.
   - `score_contract_quality`, `enrich_contract_suggestions` — read-only review.
2. **CLI commands** (the 11-stage pipeline) via the IDE's shell tool:
   - `bundle → validate → generate artifacts → validate-artifacts → diff → plan →
     apply → policy-apply → verify → publish → schedule-sync`, each run as
     `fluid <stage>` (stage 3 is `fluid generate artifacts <bundle>`).

The index tells the agent **when to use which channel**: MCP for
introspection and model edits, CLI for pipeline stages.

---

## User journey (post-install, end-to-end)

```
┌──────────────────────────────┐   stdio (JSON-RPC, MCP 2025-06-18)   ┌──────────────────────────┐
│  Agentic IDE                 │ ←──────────────────────────────────→ │  fluid mcp serve         │
│  (Kiro/Cursor/Claude/Cline)  │                                      │  16 sandboxed tools      │
│   LLM ↔ tool-use loop        │                                      │  --readable-paths .      │
└──────────────────────────────┘                                      │  --writable-paths .      │
          │                                                           └──────────────────────────┘
          │  shell tool also runs `fluid <stage>` for pipeline ops
          ▼
       fluid bundle → validate → … → apply → verify → publish
```

A typical first session:

1. User: *"Build a customer-360 CDP joining `users` and `orders`."*
2. Agent reads the `composing-fluid-data-products` skill, then the upstream contracts.
3. Agent calls MCP `list_source_tables` / `inspect_source_table` when a source
   lives in a catalog.
4. Agent shell-runs `fluid forge --template starter --non-interactive --yes --target-dir customer_360 --no-memory`
   and edits the contract itself.
5. Agent runs `fluid validate customer_360/contract.fluid.yaml --offline` until it is
   clean (in Claude Code the hook runs it after every edit).
6. Agent shell-runs `fluid plan customer_360/contract.fluid.yaml --env dev --mode amend --out runtime/plan.json --html`
   and stops for review.
7. User approves; agent shell-runs `fluid apply runtime/plan.json --env dev --mode amend`.

`fluid forge` AI runs persist receipts to `.fluid/agents/<run-id>/{cost.json,reasoning.md,transcript.json}`.

---

## Guardrails you get for free

- **Cost ceilings**: `FLUID_COST_LIMIT_USD` (per-run), `FLUID_COST_LIMIT_USD_PER_PRODUCT` (per-product).
- **Sandbox**: `fluid mcp serve` confines tool I/O to the workspace.
- **Secret redaction**: JWT, Stripe, GitHub, OpenAI, Anthropic, bare `key=value`
  patterns masked in every log and `.fluid/agents/<run-id>/transcript.json`.
- **Plan-binding**: `fluid plan` emits `bundleDigest` + `planDigest`; `fluid apply`
  re-verifies before any DDL. Tampering between stages → hard fail.
- **Receipts**: every authoring run writes the audit trail atomically *before*
  the confirmation prompt — Ctrl-C loses nothing.

---

## Re-running after a forge-cli upgrade

```bash
fluid scaffold-ide --target <ide> --force
```

The canonical pack ships inside the `data-product-forge` package, so a
`pip install -U 'data-product-forge[mcp]' && fluid scaffold-ide --target <ide> --force`
pulls in any skill, index or hook updates.

## Measuring the skills

`tests/agent_evals/` runs coding agents on recorded authoring scenarios with and
without the skills, and against `fluid forge`'s own AI mode, and grades each result
with `fluid validate` plus fixed fidelity checks. It is opt-in and spends real model
usage; see `tests/agent_evals/README.md`.

---

## Where to next

- Full agent guide: [AGENTS.md](AGENTS.md)
- Architecture / project structure: [AGENTS.md](AGENTS.md)
- User docs (tutorials, API ref): see the separate `forge-docs` repo.

---

## Related work (intellectual honesty)

forge-cli's agentic-IDE story doesn't exist in a vacuum. The patterns
below either inspired specific pieces of this design or solve adjacent
problems — name them up-front so the next maintainer doesn't have to
re-do the survey.

### MCP server & sampling

- **[modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk)** —
  the official Python SDK. **Borrowed-not-built**: forge's MCP server in
  `fluid_build/cli/mcp.py` runs on `FastMCP`, every tool is registered via
  `@_mcp_app.tool()`, and `MCPSamplingProvider` calls back through
  `ctx.session.create_message()` (the canonical server-side sampling
  primitive). The earlier hand-rolled stdio loop + `SamplingChannel` were
  removed when the SDK landed — the SDK is the only path forward.
- **[anyio](https://anyio.readthedocs.io/)** — bridges forge's sync
  `LlmProvider` interface to the SDK's async `ctx.session.create_message`.
  We use `anyio.from_thread.run(coro, token=token)` with the token obtained
  via `anyio.lowlevel.current_token()` — the canonical "call async from a
  non-event-loop thread" idiom. Mirrors the SDK's own anyio-based internals,
  cheaper than `asyncio.run_coroutine_threadsafe` to maintain since it works
  across event-loop impls. Already a transitive dep via the MCP SDK.
- **[contextvars](https://docs.python.org/3/library/contextvars.html)**
  (Python stdlib) — request-scoped state for the
  `(Context, anyio_token)` pair the `forge_run` tool installs. Propagates
  automatically across the `asyncio.to_thread` boundary that forge runs
  under (Python ≥3.9 guarantee). Replaces an earlier module-level
  `threading.Lock`-guarded globals pattern — closes a `/borrow-before-build`
  miss caught in the second-pass audit.
- **MCP spec** — protocol version `2025-06-18`. forge's MCP server
  speaks this version (negotiated automatically by the SDK at
  `initialize`). Sampling is documented at
  [modelcontextprotocol.io/specification/.../sampling](https://modelcontextprotocol.io/specification/draft/client/sampling).

### Cross-IDE rules / config

- **[rule-porter](https://forum.cursor.com/t/rule-porter-convert-your-mdc-rules-to-claude-md-agents-md-or-copilot/153197)** —
  zero-dependency CLI that converts Cursor `.mdc` rules to `CLAUDE.md`,
  `AGENTS.md`, GitHub Copilot, and Windsurf. The closest analog to
  `fluid scaffold-ide` (one source, many IDE targets); rule-porter is
  general-purpose, while our scaffolder is forge-specific (knows the
  11-stage pipeline, agent voices, and MCP server invocation).
- **[agent-sh/agentsys](https://github.com/agent-sh/agentsys)** —
  bigger framework spanning Claude Code, OpenCode, Codex, Cursor, Kiro
  with plugins, agents, and skills.
- **[antigravity-awesome-skills](https://github.com/sickn33/antigravity-awesome-skills)** —
  installable library of skills across Claude Code, Cursor, Codex CLI,
  Gemini CLI, Kiro, and others.

### Agent Skills

- **[Agent Skills specification](https://agentskills.io/specification)** — the
  `SKILL.md` format the pack follows; its `skills-ref` validator's rules are ported
  into `tests/agent_skills/test_skill_pack.py`.
- **[dbt-labs/dbt-agent-skills](https://github.com/dbt-labs/dbt-agent-skills)** and
  **[hashicorp/agent-skills](https://github.com/hashicorp/agent-skills)** — the
  marketplace-plus-plugin layout, and dbt's `evals/` scenarios with A/B skill sets,
  which `tests/agent_evals/` adapts with deterministic grading.
- **[vercel-labs/skills](https://github.com/vercel-labs/skills)** — `npx skills add`,
  which finds the pack through `.claude-plugin/marketplace.json`.
- **[Vercel: AGENTS.md outperforms skills](https://vercel.com/blog/agents-md-outperforms-skills-in-our-agent-evals)**
  and **SkillsBench** (arXiv 2602.12670) — why the pack pairs on-demand skills
  with a short always-loaded index, and keeps each skill to three supporting files.

### CLI output for agents

- **[NDJSON / JSON Lines](https://ndjson.org)** — the convention forge's
  `--agent` mode follows: one JSON object per line on stdout so the
  IDE's shell wrapper can parse progress events line-by-line. Same
  pattern ripgrep's `--json` and [ndjson-cli](https://github.com/mbostock/ndjson-cli)
  use.

### Deterministic agent completion contracts

- **[DoneSpec](https://pypi.org/project/donespec/)** — JSON-based
  completion contract for AI coding agents with `must_pass` /
  `must_not` conditions. Closer in spirit to `fluid forge --agent
  --emit-plan` than anything else we found; our plan is forge-specific
  (per-productType field checklists) but the philosophy is the same:
  agents shouldn't claim done without a deterministic gate.

### AGENTS.md convention

- **[agents.md initiative](https://agents.md)** (cross-IDE) —
  AGENTS.md is the emerging vendor-neutral substrate that Cursor,
  Claude Code, Cline, Aider, Continue, Zed, and Kiro all read. forge's
  `--target generic` writes into this format; our root-level
  [AGENTS.md](AGENTS.md) is the example.
