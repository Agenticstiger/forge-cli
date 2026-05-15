# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""``fluid scaffold-ide`` — emit per-IDE agentic config so a data team can drive
forge-cli from any agentic IDE (Kiro, Cursor, Claude Code, Cline, or a generic
target).

Design: one canonical pack of steering/hooks/MCP content, multiple per-IDE
adapters that translate paths and frontmatter. Sibling to ``scaffold-ci``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Dict

from ._common import CLIError
from ._io import atomic_write
from ._logging import info
from .console import cprint, success

COMMAND = "scaffold-ide"
LOG = logging.getLogger(__name__)

TARGETS = ("kiro", "cursor", "claude-code", "cline", "generic")


def register(subparsers: argparse._SubParsersAction):
    p = subparsers.add_parser(
        COMMAND,
        help=(
            "Generate agentic-IDE config (.kiro/, .cursor/rules/, .claude/, "
            ".clinerules/, or generic AGENTS.md + mcp.json) so any agentic "
            "IDE can drive forge-cli."
        ),
    )
    p.add_argument(
        "--target",
        choices=TARGETS,
        default="kiro",
        help="Which agentic IDE to scaffold for. Default: kiro.",
    )
    p.add_argument(
        "--out",
        default=".",
        help="Workspace root to scaffold into. Default: current directory.",
    )
    p.add_argument(
        "--python",
        default=None,
        help=(
            "Path to the python interpreter that `fluid` is installed under. "
            "Baked into the MCP config so the IDE can launch `fluid mcp serve` "
            "without relying on PATH. Default: sys.executable."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files. Off by default — safer for re-runs.",
    )
    p.set_defaults(cmd=COMMAND, func=run)


# ---------------------------------------------------------------------------
# Canonical pack (vendor-neutral; per-IDE adapters translate paths + frontmatter)
# ---------------------------------------------------------------------------

_STEERING_FORGE_CLI = """\
# forge-cli — agentic guide

forge-cli (`fluid` on PATH) is a contract-driven build system for declarative
data products. One `contract.fluid.yaml` compiles via an 11-stage pipeline into
validated, planned, executable deployments across local / GCP / AWS / Snowflake
/ ODPS / ODCS providers.

## Two channels into forge-cli

You have two ways to drive forge:

1. **MCP tools** — surgical, sandboxed operations (read/update logical models,
   validate contracts, inspect source tables, search semantic memory). The
   `fluid` MCP server runs as a subprocess of this IDE and exposes 13 tools
   over JSON-RPC. Path-confined to the workspace.
2. **CLI commands** — the 11 pipeline stages, executed via the IDE's shell
   tool: `fluid bundle → validate → generate-artifacts → validate-artifacts →
   diff → plan → apply → policy-apply → verify → publish → schedule-sync`.

Use MCP tools for **introspection and model edits**. Use the CLI for **pipeline
stages** (anything that mutates a warehouse or emits artifacts).

## The 11 stages

| # | Command | When to call it |
|---|---|---|
| 1 | `fluid bundle <contract.fluid.yaml> --format tgz` | Once per change; root-of-trust SHA-256 merkle. |
| 2 | `fluid validate <bundle.tgz>` | Schema + SQL + OpenAPI gate. |
| 3 | `fluid generate-artifacts <bundle.tgz>` | Fanout ODCS/ODPS/schedule/policy. |
| 4 | `fluid validate-artifacts dist/artifacts/` | Re-verify SHA-256 + per-format. |
| 5 | `fluid diff --exit-on-drift --env <env>` | Hard gate — no plan against unknown baseline. |
| 6 | `fluid plan --out runtime/plan.json --html` | Emits `bundleDigest` + `planDigest`. |
| 7 | `fluid apply --mode <mode> --env <env>` | Modes: dry-run, create-only, amend, amend-and-build, replace, replace-and-build. |
| 8 | `fluid policy-apply dist/artifacts/policy/bindings.json --mode enforce` | IAM/GRANT. |
| 9 | `fluid verify --strict --env <env>` | Post-apply reconciliation. |
| 10 | `fluid publish --target <name>` | datamesh-manager, datahub, marketplace, etc. |
| 11 | `fluid schedule-sync --scheduler <name>` | Path-A DAG push (airflow / composer / dagster). |

## Authoring a new product

**Always use `--agent` when shell-running `fluid forge` from an IDE.** Bare
`fluid forge` drops into an interactive mode picker the agent cannot navigate.
`--agent` bundles `--yes`, suppresses all interactive UI, defaults to `--blank`
when no other mode is set, and emits JSON-Lines progress events to stdout:

```bash
# Headless: blank contract, no LLM needed
fluid forge --agent --blank --data-product-type SDP --target-dir my-product

# Compose from upstream products (LLM needed for inference)
fluid forge --agent --from-product users --from-product orders --data-product-type CDP

# Iterate on an existing contract
fluid forge --agent --refine contract.fluid.yaml
```

JSON-Lines events you can parse from the agent's shell wrapper:

```json
{"event":"forge.start","run_id":"…","mode":"compose","data_product_type":"CDP","ts":…}
{"event":"forge.contract_written","path":"my-product/contract.fluid.yaml","action":"created","size":2048}
{"event":"forge.done","run_id":"…","exit_code":0,"ts":…}
```

Every authoring run persists receipts to `.fluid/agents/<run-id>/`:
`cost.json`, `reasoning.md`, `transcript.json`. These are the audit trail.
"""

_STEERING_CONTRACT_SCHEMA = """\
# contract.fluid.yaml — schema cheatsheet

Top-level keys an agent must know:

- `fluidVersion` — schema version (default to latest bundled).
- `metadata` — `{name, version, owner, productType, layer, domain}`.
  - `productType` ∈ `SDP | ADP | CDP` (Data Mesh: source-aligned, aggregate,
    consumer-aligned).
  - `layer` ∈ `Bronze | Silver | Gold` (medallion).
  - Canonical mapping: **Bronze↔SDP, Silver↔ADP, Gold↔CDP**. Either-or-both
    is accepted; both must agree if set.
- `binding.platform` — `local | gcp | aws | snowflake | odps | odcs`.
- `consumes[]` — upstream products referenced by `id` or path.
- `models[]` — physical schemas (entities + fields + types).
- `transformations[]` — dbt model refs, inline SQL, or external `.sql` files.
- `quality[]` — declarative tests (uniqueness, freshness, custom SQL).
- `access[]` — IAM/GRANT bindings.
- `schedule` — cron + scheduler (airflow / composer / snowflake-tasks / mwaa).
- `exposes` — what downstream AI agents may consume (agent-policy block).

## Apply mode matrix

| Mode | DDL | DML | Existing data |
|---|---|---|---|
| `dry-run` | render only | — | untouched |
| `create-only` | CREATE IF NOT EXISTS + fail-if-exists | — | untouched |
| `amend` (default) | ALTER ADD COLUMN IF NOT EXISTS; views CREATE OR REPLACE | — | preserved; new cols NULL |
| `amend-and-build` | same as amend | dbt run + dbt test | preserved; transforms refreshed |
| `replace` | auto-snapshot → CREATE OR REPLACE TABLE | — | **dropped**; backup retained |
| `replace-and-build` | same as replace | dbt run --full-refresh | **dropped**; rebuilt |

`--allow-data-loss` is required for `replace*` when `FLUID_ENV != dev` OR target
has rows. Never bypass without explicit user confirmation.

## Plan-binding (Terraform-style "apply consumes exact plan")

`fluid plan` emits two cryptographic fields into `plan.json`:

- `bundleDigest` — SHA-256 merkle root of the bundle MANIFEST.
- `planDigest` — SHA-256 over the plan body.

`fluid apply` verifies BOTH before any DDL. Mismatch → hard-fail
(`apply_plan_digest_bundle_mismatch` or `apply_plan_digest_plan_tamper`).
`--no-verify-plan-binding` (plan/bundle digest gate) and
`--no-verify-federation` (federated-consumes upstream-digest gate) are
DR escape hatches only; both log at WARNING level.
"""

_STEERING_PIPELINE_DECISIONS = """\
# When to call which `fluid` command — decision tree

## Architectural principle: YOU are the LLM. forge is the toolkit.

You (the IDE's agent) already have an LLM — the IDE pays for it. **Do not**
shell-run `fluid forge --ai` (or any mode that would call an LLM); that would
need a *second* LLM API key on the user's machine. Instead, **you do the
authoring** using forge's MCP tools for structured operations and forge's CLI
for deterministic pipeline stages.

The canonical authoring flow:

1. **Discover** the upstream sources using MCP tools (read-only):
   - `list_source_adapters` → which catalogs are reachable
   - `list_source_tables` → tables in a scope
   - `inspect_source_table` → schema + sample rows
   - `list_source_lineage` → upstream dependencies
   - `list_source_glossary` → business terms
2. **Scaffold** a deterministic blank contract using the CLI (no LLM):
   - `fluid forge --agent --blank --data-product-type {SDP|ADP|CDP} -d <product-dir>`
3. **Get a checklist** of what to fill in (no LLM):
   - `fluid forge --agent --emit-plan -d <product-dir>` → emits one
     `forge.plan` JSONL event with required fields, examples, suggestions,
     and the relevant MCP tools for each step
4. **Author** the contract — *you* edit `contract.fluid.yaml` with your own
   Edit/Write tools, using your LLM's reasoning. Fill in `models[]`,
   `transformations[]`, `consumes[]`, `quality[]`. The agent voices and
   policies forge ships with are loaded into your steering automatically.
5. **Validate** as you iterate (no LLM):
   - MCP `validate_contract` (one-shot, structured response), or
   - shell `fluid validate <contract.fluid.yaml>`
6. **Pipeline** stages (no LLM, deterministic):
   - `fluid bundle … --format tgz` → root-of-trust digest
   - `fluid plan --out runtime/plan.json --html` → bundleDigest + planDigest
   - `fluid apply --mode <mode> --env <env>` → ALWAYS ask the user before
     applying outside `dev`. Destructive modes (`replace*`) need
     `--allow-data-loss` and explicit user consent.
   - `fluid policy-apply … --mode enforce` → IAM/GRANT
   - `fluid verify --strict --env <env>` → reconciliation
   - `fluid publish --target <name>` → catalog (datamesh-manager / datahub)

## What if I want forge's "world-class" copilot (interview, agent voices, repair loop)?

Use the MCP tool **`forge_run`** (when available — its presence depends on
your IDE's MCP sampling support). It runs `fluid forge`'s full authoring
loop **inside the MCP subprocess** and routes every LLM call back to your
IDE via MCP `sampling/createMessage`. You (the IDE) supply the LLM; forge
supplies the orchestration. No separate API key.

If `forge_run` isn't advertised in `tools/list`, your IDE doesn't yet
support MCP sampling — stick with the "you author, forge tools assist" flow
above.

## Quick reference by user intent

| User says | You do |
|---|---|
| *"build a new product from X and Y"* | discover via MCP → `--agent --blank` → `--emit-plan` → edit contract → validate → plan → apply |
| *"refine my existing contract"* | read the contract → MCP `validate_contract` → edit → validate → plan |
| *"what will this deploy?"* | `fluid plan --out runtime/plan.json --html` → show the HTML diff |
| *"ship it"* | confirm mode → `fluid apply --mode amend --env dev` → `policy-apply` → `verify` → `publish` |
| *"discover sources"* | MCP tools only — `list_source_adapters`, `list_source_tables`, `inspect_source_table` |
| *"edit a logical model"* | MCP tools — `read_logical_model`, `update_entity`, `add_relationship`, `regenerate_physical` |

## Never

- ❌ `fluid forge` (bare) — drops into interactive picker the agent can't navigate
- ❌ `fluid forge --ai` — needs a second LLM API key the user shouldn't have to manage
- ❌ Hand-edit `contract.fluid.yaml` without running `validate_contract` after
- ❌ `apply --mode replace*` without user consent + `--allow-data-loss`
"""

_STEERING_GUARDRAILS = """\
# Guardrails — what's free with `pip install data-product-forge`

## Cost ceilings

- `FLUID_COST_LIMIT_USD` — per-run cap on LLM cost (recommended: `5`).
- `FLUID_COST_LIMIT_USD_PER_PRODUCT` — per-product cap (recommended: `2`).
- Aborts cleanly when crossed; cost panel renders before any spend.

## Sandbox

The MCP server (`fluid mcp serve`) confines all tool I/O to the workspace
(default `--readable-paths .` and `--writable-paths .`). Tools that accept
paths route them through a `workspace_root` check. Tools that need
credentials look them up via `credential_id` — raw secrets never cross the
wire.

## Secret redaction

`SecretRedactingFilter` is wired into Python logging globally. JWTs, Stripe,
GitHub, OpenAI, Anthropic keys, and bare `key[:=]value` assignments are
masked in every log line and every `.fluid/agents/<run-id>/transcript.json`.

## Plan-binding (cryptographic apply gate)

`fluid plan` emits SHA-256 digests; `fluid apply` re-verifies before any DDL.
Tampering between stages is detected. Stable event tags for CI:
`apply_plan_digest_bundle_mismatch`, `apply_plan_digest_plan_tamper`.

## Receipts

Every authoring run writes `.fluid/agents/<run-id>/{cost.json,reasoning.md,
transcript.json}` atomically BEFORE the confirmation prompt — Ctrl-C loses
nothing.
"""

_HOOK_ON_SAVE_CONTRACT = """\
# Hook — on save of contract.fluid.yaml

When the user saves a `contract.fluid.yaml`, run `fluid validate` and surface
errors inline. Fast (~200ms for the schema check).

Shell command:

```bash
fluid validate "$FILE"
```

Event: file saved (pattern: `**/contract.fluid.yaml`).
"""

_HOOK_PRE_COMMIT_BUNDLE = """\
# Hook — before commit, refresh bundle digests

Before any `git commit`, re-bundle and re-validate so the SHA-256 digests in
`MANIFEST.json` track the working tree. Blocks the commit on validation
failure.

Shell command:

```bash
fluid bundle contract.fluid.yaml --format tgz && \\
  fluid validate-artifacts dist/artifacts/
```

Event: pre-commit (or matching IDE lifecycle event).
"""

_SPEC_FIRST_DATA_PRODUCT = """\
# Spec — your first data product

Fill in the blanks below, then ask the agent to "implement this spec".
The agent will drive `fluid forge`, `fluid validate`, and `fluid plan`
autonomously.

## Goal

I want to build a **{SDP | ADP | CDP}** named `____________` that produces
`____________` for downstream consumers.

## Upstream sources

- Source 1: `____________` (table / kafka topic / S3 prefix / existing product id)
- Source 2: `____________`
- Source 3: `____________` (optional)

## Transformations

In plain English: `____________________________________________________`
(e.g. "join users to orders on user_id, group by month, sum order_total").

## Quality rules

- `____________` must be unique.
- `____________` must be non-null.
- Freshness: refreshed at most every `____` hours.

## Target

- Platform: `{local | snowflake | bigquery | redshift | duckdb}`
- Schedule: `{daily 02:00 UTC | hourly | event-driven}`
- Catalog: `{datamesh-manager | datahub | marketplace}`

## Acceptance

- [ ] `fluid validate` passes
- [ ] `fluid plan --html` reviewed
- [ ] `fluid apply --mode amend --env dev` succeeds
- [ ] `fluid verify --strict --env dev` clean
- [ ] Published to catalog
"""


def _mcp_server_block(python_bin: str) -> Dict:
    """Canonical MCP server entry for the `fluid mcp serve` subprocess.

    Uses absolute path to the python interpreter so the IDE can launch it
    without relying on PATH (venv activation isn't guaranteed in IDE subprocs).
    """
    return {
        "command": python_bin,
        "args": ["-m", "fluid_build.cli", "mcp", "serve"],
        "env": {},
        "disabled": False,
        "autoApprove": [
            # Read-only tools the agent can auto-approve. Mutating tools
            # (update_entity, add_relationship, regenerate_physical,
            # forge_from_source) still surface a confirmation.
            "read_logical_model",
            "validate_contract",
            "diff_models",
            "search_semantic_memory",
            "list_source_adapters",
            "list_source_tables",
            "inspect_source_table",
            "list_source_lineage",
            "list_source_glossary",
        ],
    }


def _frontmatter(kvs: Dict[str, str]) -> str:
    lines = ["---"]
    for k, v in kvs.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-target adapters — path + frontmatter translation, NO content branching
# ---------------------------------------------------------------------------


def _write(root: Path, rel: str, body: str, force: bool) -> Path:
    target = root / rel
    if target.exists() and not force:
        raise CLIError(
            2,
            "refusing_to_overwrite_existing_file_pass_force_to_override",
            {"path": str(target)},
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(str(target), body)
    return target


def _emit_kiro(root: Path, python_bin: str, force: bool) -> list[Path]:
    """Kiro: .kiro/{settings,steering,hooks,specs}/.

    Steering files use Kiro's `inclusion: always` frontmatter so the agent
    reads them on every turn.
    """
    written: list[Path] = []
    mcp_config = {"mcpServers": {"fluid": _mcp_server_block(python_bin)}}
    written.append(
        _write(
            root,
            ".kiro/settings/mcp.json",
            json.dumps(mcp_config, indent=2) + "\n",
            force,
        )
    )

    always_fm = _frontmatter({"inclusion": "always"})
    for name, body in (
        ("01-forge-cli.md", _STEERING_FORGE_CLI),
        ("02-contract-schema.md", _STEERING_CONTRACT_SCHEMA),
        ("03-pipeline-decisions.md", _STEERING_PIPELINE_DECISIONS),
        ("04-guardrails.md", _STEERING_GUARDRAILS),
    ):
        written.append(_write(root, f".kiro/steering/{name}", always_fm + body, force))

    hook_fm = _frontmatter({"event": "file.saved", "pattern": "'**/contract.fluid.yaml'"})
    written.append(
        _write(
            root,
            ".kiro/hooks/on-save-contract.md",
            hook_fm + _HOOK_ON_SAVE_CONTRACT,
            force,
        )
    )
    hook_fm_commit = _frontmatter({"event": "git.precommit"})
    written.append(
        _write(
            root,
            ".kiro/hooks/pre-commit-bundle.md",
            hook_fm_commit + _HOOK_PRE_COMMIT_BUNDLE,
            force,
        )
    )

    written.append(
        _write(
            root,
            ".kiro/specs/first-data-product.md",
            _SPEC_FIRST_DATA_PRODUCT,
            force,
        )
    )
    return written


def _emit_cursor(root: Path, python_bin: str, force: bool) -> list[Path]:
    """Cursor: .cursor/rules/*.mdc + .cursor/mcp.json.

    Steering = MDC rules with `alwaysApply: true`. Cursor has no native
    hooks; we emit a HOOKS.md note explaining the gap.
    """
    written: list[Path] = []
    mcp_config = {"mcpServers": {"fluid": _mcp_server_block(python_bin)}}
    written.append(
        _write(
            root,
            ".cursor/mcp.json",
            json.dumps(mcp_config, indent=2) + "\n",
            force,
        )
    )

    for name, desc, body in (
        ("01-forge-cli.mdc", "forge-cli agentic guide (always loaded)", _STEERING_FORGE_CLI),
        (
            "02-contract-schema.mdc",
            "contract.fluid.yaml schema cheatsheet",
            _STEERING_CONTRACT_SCHEMA,
        ),
        (
            "03-pipeline-decisions.mdc",
            "decision tree: when to call which fluid command",
            _STEERING_PIPELINE_DECISIONS,
        ),
        (
            "04-guardrails.mdc",
            "cost ceilings, sandbox, redaction, plan-binding",
            _STEERING_GUARDRAILS,
        ),
    ):
        fm = _frontmatter(
            {
                "description": desc,
                "globs": "",
                "alwaysApply": "true",
            }
        )
        written.append(_write(root, f".cursor/rules/{name}", fm + body, force))

    written.append(
        _write(
            root,
            ".cursor/HOOKS.md",
            "# Hooks — Cursor has no native on-save / pre-commit hook substrate\n\n"
            "Cursor relies on agent-driven action. To get the equivalent of the\n"
            "Kiro / Claude Code hooks shipped here, ask the agent to run:\n\n"
            f"{_HOOK_ON_SAVE_CONTRACT}\n\n{_HOOK_PRE_COMMIT_BUNDLE}\n",
            force,
        )
    )
    return written


def _emit_claude_code(root: Path, python_bin: str, force: bool) -> list[Path]:
    """Claude Code: append to CLAUDE.md + write .claude/settings.json hooks +
    .mcp.json at workspace root.
    """
    written: list[Path] = []
    mcp_config = {"mcpServers": {"fluid": _mcp_server_block(python_bin)}}
    written.append(_write(root, ".mcp.json", json.dumps(mcp_config, indent=2) + "\n", force))

    # Concat all four steering files into one CLAUDE.md append-block.
    # CLAUDE.md is the canonical "always-loaded" surface for claude-code.
    block = "\n\n".join(
        [
            "<!-- BEGIN forge-cli scaffold-ide block -->",
            _STEERING_FORGE_CLI,
            _STEERING_CONTRACT_SCHEMA,
            _STEERING_PIPELINE_DECISIONS,
            _STEERING_GUARDRAILS,
            "<!-- END forge-cli scaffold-ide block -->",
        ]
    )
    claude_md = root / "CLAUDE.md"
    if claude_md.exists() and not force:
        # Append rather than overwrite — CLAUDE.md may already have project
        # content. Idempotent via the BEGIN/END markers (re-runs append; we
        # detect a pre-existing block and skip).
        existing = claude_md.read_text()
        if "BEGIN forge-cli scaffold-ide block" in existing:
            LOG.info(
                "CLAUDE.md already has a scaffold-ide block — skipping. " "Pass --force to replace."
            )
        else:
            atomic_write(str(claude_md), existing.rstrip() + "\n\n" + block + "\n")
            written.append(claude_md)
    else:
        atomic_write(str(claude_md), block + "\n")
        written.append(claude_md)

    hooks = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Edit|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "if": "Edit(*contract.fluid.yaml) | Write(*contract.fluid.yaml)",
                            "command": "fluid validate ${tool_input.file_path}",
                            "timeout": 30,
                        }
                    ],
                }
            ]
        }
    }
    written.append(
        _write(
            root,
            ".claude/settings.json",
            json.dumps(hooks, indent=2) + "\n",
            force,
        )
    )
    return written


def _emit_cline(root: Path, python_bin: str, force: bool) -> list[Path]:
    """Cline: .clinerules/*.md (directory form) + .cline/mcp_settings.json.

    Cline's user-level MCP config lives at ~/.cline/data/settings/cline_mcp_settings.json
    — project-level support is in discussion (cline#2418). We emit project-local
    files; users may need to copy mcp_settings.json to the user-level path until
    project-level lands.
    """
    written: list[Path] = []
    mcp_config = {"mcpServers": {"fluid": _mcp_server_block(python_bin)}}
    written.append(
        _write(
            root,
            ".cline/mcp_settings.json",
            json.dumps(mcp_config, indent=2) + "\n",
            force,
        )
    )

    for name, body in (
        ("01-forge-cli.md", _STEERING_FORGE_CLI),
        ("02-contract-schema.md", _STEERING_CONTRACT_SCHEMA),
        ("03-pipeline-decisions.md", _STEERING_PIPELINE_DECISIONS),
        ("04-guardrails.md", _STEERING_GUARDRAILS),
    ):
        written.append(_write(root, f".clinerules/{name}", body, force))

    written.append(
        _write(
            root,
            ".clinerules/MCP_SETUP.md",
            "# MCP setup — Cline\n\n"
            "Project-level MCP support is in discussion upstream "
            "(cline#2418). Until it lands, copy `.cline/mcp_settings.json` "
            "into Cline's user-level config:\n\n"
            "```bash\n"
            "mkdir -p ~/.cline/data/settings\n"
            "cp .cline/mcp_settings.json ~/.cline/data/settings/cline_mcp_settings.json\n"
            "```\n\n"
            "Or merge it into your existing `cline_mcp_settings.json` if you "
            "already have one.\n",
            force,
        )
    )
    return written


def _emit_generic(root: Path, python_bin: str, force: bool) -> list[Path]:
    """Generic: AGENTS.md (cross-IDE convention) + mcp.json + .ai/steering/.

    AGENTS.md is the emerging cross-IDE standard read by Cursor, Claude Code,
    Cline, Aider, Continue, Zed, Kiro, and others. Generic target gives the
    data team a vendor-neutral substrate that works with any AGENTS.md-aware
    agent.
    """
    written: list[Path] = []
    mcp_config = {"mcpServers": {"fluid": _mcp_server_block(python_bin)}}
    written.append(_write(root, "mcp.json", json.dumps(mcp_config, indent=2) + "\n", force))

    block = "\n\n".join(
        [
            "<!-- BEGIN forge-cli scaffold-ide block -->",
            _STEERING_FORGE_CLI,
            _STEERING_CONTRACT_SCHEMA,
            _STEERING_PIPELINE_DECISIONS,
            _STEERING_GUARDRAILS,
            "<!-- END forge-cli scaffold-ide block -->",
        ]
    )
    agents_md = root / "AGENTS.md"
    if agents_md.exists() and not force:
        existing = agents_md.read_text()
        if "BEGIN forge-cli scaffold-ide block" in existing:
            LOG.info(
                "AGENTS.md already has a scaffold-ide block — skipping. " "Pass --force to replace."
            )
        else:
            atomic_write(str(agents_md), existing.rstrip() + "\n\n" + block + "\n")
            written.append(agents_md)
    else:
        atomic_write(str(agents_md), block + "\n")
        written.append(agents_md)

    # Also drop forward-compatible .ai/steering/ shards for tools that prefer
    # per-file steering (some emerging IDEs use this convention).
    for name, body in (
        ("01-forge-cli.md", _STEERING_FORGE_CLI),
        ("02-contract-schema.md", _STEERING_CONTRACT_SCHEMA),
        ("03-pipeline-decisions.md", _STEERING_PIPELINE_DECISIONS),
        ("04-guardrails.md", _STEERING_GUARDRAILS),
    ):
        written.append(_write(root, f".ai/steering/{name}", body, force))
    return written


_DISPATCH: Dict[str, Callable[[Path, str, bool], list[Path]]] = {
    "kiro": _emit_kiro,
    "cursor": _emit_cursor,
    "claude-code": _emit_claude_code,
    "cline": _emit_cline,
    "generic": _emit_generic,
}


def run(args, logger: logging.Logger | None = None) -> int:
    log = logger or LOG
    target = args.target
    out_root = Path(args.out).resolve()
    python_bin = args.python or sys.executable
    force = bool(getattr(args, "force", False))

    if target not in _DISPATCH:
        raise CLIError(
            2,
            "unknown_scaffold_ide_target",
            {"target": target, "choices": ", ".join(TARGETS)},
        )
    if not os.path.isabs(python_bin):
        raise CLIError(
            2,
            "python_must_be_an_absolute_path",
            {"python": python_bin},
        )
    if not out_root.exists():
        out_root.mkdir(parents=True, exist_ok=True)

    written = _DISPATCH[target](out_root, python_bin, force)
    info(log, "scaffold_ide_written", target=target, count=len(written), out=str(out_root))
    # User-facing summary.
    success(f"scaffold-ide({target}): wrote {len(written)} file(s) under {out_root}")
    for p in written:
        cprint(f"  - {p.relative_to(out_root)}")
    cprint("\nNext steps:")
    cprint("  1. Open this workspace in your IDE.")
    cprint("  2. The IDE will auto-spawn `fluid mcp serve` (13 tools).")
    cprint("  3. Run `fluid forge` to author your first contract.fluid.yaml.")
    cprint("  4. Receipts land in .fluid/agents/<run-id>/.")
    return 0
