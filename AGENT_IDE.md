# AGENT_IDE.md — Drive forge-cli from any agentic IDE

> One command bootstraps your IDE so its agent can drive `fluid` natively. Works
> with Kiro, Cursor, Claude Code, Cline, or any AGENTS.md-aware IDE.

---

## TL;DR

```bash
pip install data-product-forge
cd your-data-product-repo
fluid scaffold-ide --target kiro      # or cursor / claude-code / cline / generic
fluid forge                           # AI authoring (mode picker)
```

That's it. Your IDE now has:

- **Steering** (always-loaded guidance about forge-cli, the 11-stage pipeline,
  and `contract.fluid.yaml`).
- **An MCP server** (`fluid mcp serve`, auto-spawned by the IDE) exposing 13
  sandboxed tools for catalog / model / contract work.
- **Hooks** (Kiro and Claude Code only — Cursor/Cline lack a hook substrate)
  that run `fluid validate` on save and `fluid bundle && fluid validate-artifacts`
  before commit.

---

## What `fluid scaffold-ide` writes per target

| Target | Files emitted | Hook substrate |
|---|---|---|
| `kiro` | `.kiro/{settings/mcp.json, steering/, hooks/, specs/}` | yes (event-driven) |
| `cursor` | `.cursor/{mcp.json, rules/*.mdc, HOOKS.md}` | no (agent-driven) |
| `claude-code` | `.mcp.json` + `CLAUDE.md` block + `.claude/settings.json` | yes (PostToolUse) |
| `cline` | `.cline/mcp_settings.json` + `.clinerules/` | no (agent-driven) |
| `generic` | `mcp.json` + `AGENTS.md` block + `.ai/steering/` | no |

All targets share **one canonical pack**: any forge-cli upgrade updates the
content in one place and `fluid scaffold-ide --force` refreshes every IDE the
team uses.

---

## The two channels into forge-cli

After scaffolding, the IDE's agent has **two complementary channels**:

1. **MCP tools** (13, sandboxed, structured) for surgical work:
   - `list_source_adapters`, `list_source_tables`, `inspect_source_table`,
     `list_source_lineage`, `list_source_glossary` — catalog discovery.
   - `read_logical_model`, `update_entity`, `add_relationship`,
     `regenerate_physical` — logical-model edits (round-trip via the sidecar).
   - `validate_contract`, `diff_models`, `search_semantic_memory` — checks.
   - `forge_from_source` — bootstrap a logical model from a source table.
2. **CLI commands** (the 11-stage pipeline) via the IDE's shell tool:
   - `fluid bundle → validate → generate-artifacts → validate-artifacts →
     diff → plan → apply → policy-apply → verify → publish → schedule-sync`.

Steering files tell the agent **when to use which channel**: MCP for
introspection and model edits, CLI for pipeline stages.

---

## User journey (post-install, end-to-end)

```
┌──────────────────────────────┐   stdio (JSON-RPC, MCP 2025-06-18)   ┌──────────────────────────┐
│  Agentic IDE                 │ ←──────────────────────────────────→ │  fluid mcp serve         │
│  (Kiro/Cursor/Claude/Cline)  │                                      │  13 sandboxed tools      │
│   LLM ↔ tool-use loop        │                                      │  --readable-paths .      │
└──────────────────────────────┘                                      │  --writable-paths .      │
          │                                                           └──────────────────────────┘
          │  shell tool also runs `fluid <stage>` for pipeline ops
          ▼
       fluid bundle → validate → … → apply → verify → publish
```

A typical first session:

1. User: *"Build a customer-360 CDP joining `users` and `orders`."*
2. Agent calls MCP `list_source_tables` → discovers tables.
3. Agent calls MCP `inspect_source_table` → schemas, sample rows.
4. Agent shell-runs `fluid forge --from-product users --from-product orders --data-product-type CDP --yes`.
5. Agent calls MCP `validate_contract` → schema clean.
6. Agent shell-runs `fluid plan --out runtime/plan.json --html`.
7. User approves; agent shell-runs `fluid apply --mode amend --env dev`.

Receipts persist to `.fluid/agents/<run-id>/{cost.json,reasoning.md,transcript.json}`.

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
`pip install -U data-product-forge && fluid scaffold-ide --target <ide> --force`
pulls in any pipeline-stage or steering updates.

---

## Where to next

- Full agent guide: [AGENTS.md](AGENTS.md)
- Architecture / project structure: [AGENTS.md](AGENTS.md)
- User docs (tutorials, API ref): see the separate `forge-docs` repo.
