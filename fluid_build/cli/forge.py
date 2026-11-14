# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Public Forge CLI entrypoint — AI-powered data product creation.

After the UX redesign, ``fluid forge`` is the **repeatable** command for
creating new data products inside an existing project.  It defaults to the
AI Copilot interview and has a ``--blank`` escape hatch for users who want
a bare contract scaffold without AI.

First-time project setup lives in ``fluid init``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build._console import literal_error_text, literal_text
from fluid_build.cli.artifact_envelope import dump_json_with_envelope
from fluid_build.cli.artifact_paths import product_forge_receipt_path
from fluid_build.cli.artifact_receipts import ReceiptBuilder
from fluid_build.cli.artifact_scan import ArtifactSnapshot, diff_snapshots, snapshot_workspace
from fluid_build.cli.console import cprint, cprint_literal
from fluid_build.cli.console import error as console_error
from fluid_build.cli.forge_banner import print_v2_banner
from fluid_build.cli.forge_ui import print_welcome_panel
from fluid_build.cli.next_steps import print_next_steps

# NOTE: ``forge_dialogs`` / ``forge_modes`` / ``forge_context`` and the LLM provider
# layer (``httpx``) are resolved lazily via ``__getattr__`` below, so they stay off
# the ``fluid --help`` / ``build_parser()`` cold path (``register`` only needs argparse).
#
# Several of these symbols are patched by tests at module scope
# (``patch("…cli.forge.build_capability_matrix")``, ``…ask_confirmation``,
# ``…_scaffold_ci_pipeline``), so they MUST remain resolvable module attributes
# — that is exactly what ``__getattr__`` provides. Runtime use sites resolve via
# ``import fluid_build.cli.forge as _self; _self.X`` so those patches flow through.

# alias -> (heavy source module, real symbol name)
_DEFERRED_FORGE_IMPORTS: Dict[str, tuple] = {
    "_get_cli_arg": ("forge_context", "get_cli_arg"),
    "_get_target_dir": ("forge_context", "get_target_directory"),
    "_load_ctx": ("forge_context", "load_context"),
    "get_cumulative_prompt_cache_metrics": (
        "forge_copilot_llm_providers",
        "get_cumulative_prompt_cache_metrics",
    ),
    "reset_token_usage": ("forge_copilot_llm_providers", "reset_token_usage"),
    "build_capability_matrix": ("_capability_matrix", "build_capability_matrix"),
    "ask_confirmation": ("forge_dialogs", "ask_confirmation"),
    "ask_dialog_question": ("forge_dialogs", "ask_dialog_question"),
    "_scaffold_ci_pipeline": ("forge_modes", "_scaffold_ci_pipeline"),
    "_run_guided": ("forge_modes", "run_guided_mode"),
}


def __getattr__(name: str):
    """Lazily resolve the heavy forge imports (PEP 562).

    Keeps ``httpx`` + the ``schema_manager`` → ``jsonschema`` chain off the
    ``fluid --help`` path while exposing the symbols as module attributes so the
    various ``patch("…cli.forge.<symbol>")`` test seams keep working.
    """
    spec = _DEFERRED_FORGE_IMPORTS.get(name)
    if spec is not None:
        import importlib

        module = importlib.import_module(f"fluid_build.cli.{spec[0]}")
        return getattr(module, spec[1])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


try:
    from rich.console import Console
    from rich.panel import Panel

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised through non-Rich fallbacks
    Console = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    RICH_AVAILABLE = False

from ._common import CLIError

COMMAND = "forge"
LOG = logging.getLogger("fluid.cli.forge")


class ForgeError(CLIError):
    """Base exception for Forge command errors.

    Two positional-argument shapes are accepted so callers using the
    pre-925668a ``ForgeError(exit_code, message)`` form keep working
    alongside the modern ``ForgeError(message, exit_code=...)`` form.
    The keyword-only ``exit_code`` always wins when both are supplied.
    """

    def __init__(
        self,
        *args: Any,
        event: str = "forge_error",
        context: Optional[Dict[str, Any]] = None,
        exit_code: int = 1,
    ):
        if len(args) == 1:
            message = str(args[0])
        elif len(args) == 2:
            # Legacy positional shape ``ForgeError(exit_code, message)``.
            # Accept it so older callers + tests that haven't migrated
            # don't blow up with ``TypeError: takes 2 positional args
            # but 3 were given``.  Keyword ``exit_code`` overrides the
            # positional one when both are supplied.
            exit_code = exit_code if exit_code != 1 else int(args[0])
            message = str(args[1])
        else:
            raise TypeError(
                f"{type(self).__name__} expects 1 (message) or 2 (exit_code, message) "
                f"positional arguments; got {len(args)}"
            )
        payload = {"error": message, **(context or {})}
        super().__init__(exit_code, event, payload)
        self.message = message

    def __str__(self) -> str:
        # ``CLIError`` passes the ``event`` string to ``Exception.__init__``,
        # which makes ``str(err)`` return the event key (``"forge_error"``).
        # Override so callers / tests that look at the rendered string see
        # the operator-facing message instead.
        return self.message


class InvalidProjectNameError(ForgeError):
    """Invalid project name format."""

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        super().__init__(f"Invalid project name '{name}': {reason}")


class ProjectGenerationError(ForgeError):
    """Project generation failed."""


class ContextValidationError(ForgeError):
    """Context file validation failed."""


class ForgeMode(Enum):
    """Forge creation modes."""

    AI_COPILOT = "copilot"
    BLANK = "blank"


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction):
    """Register the Forge command — AI-powered data product creation."""
    parser = subparsers.add_parser(
        COMMAND,
        help="🔨 Create a new data product with AI Copilot",
        add_help=False,
    )
    parser.add_argument("--help", "-h", action="store_true", help="Show this help message")
    forge_subparsers = parser.add_subparsers(dest="forge_subcommand")
    # Import from the LIGHT pure-argparse register module directly (not the
    # heavy ``forge_data_model`` re-export) so building the ``forge data-model``
    # subparser stays off the httpx/jsonschema cold path.
    from fluid_build.cli._forge_data_model_register import register_forge_subcommand

    register_forge_subcommand(forge_subparsers)

    # --- Primary flags ---
    parser.add_argument(
        "--blank",
        action="store_true",
        help="Scaffold an empty contract without AI (no LLM needed)",
    )
    parser.add_argument("--target-dir", "-d", help="Target directory for project creation")
    parser.add_argument("--provider", "-p", help="Infrastructure provider to use")
    parser.add_argument(
        "--data-product-type",
        dest="data_product_type",
        help=(
            "Data Mesh productType (SDP/ADP/CDP) or medallion layer "
            "(Bronze/Silver/Gold) — accepted interchangeably via the "
            "canonical mapping (Bronze↔SDP, Silver↔ADP, Gold↔CDP). "
            "When omitted the copilot infers from the project goal."
        ),
    )
    parser.add_argument(
        "--transform-engine",
        dest="transform_engine",
        help=(
            "Override the transformation engine (dbt / sql / spark / dataform / "
            "dataflow / glue) for ADP / CDP products. SDP products pick the "
            "right acquisition engine via the capability catalog."
        ),
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Use defaults without prompting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be created without generating files",
    )
    parser.add_argument(
        "--context",
        help=(
            "AI mode: the product to build, as text or a file path (what it reads and "
            "produces, owner, schedule, rules). Required when nobody is at the keyboard."
        ),
    )

    # --- LLM flags ---
    # ``LlmProviderChoices`` accepts the built-ins eagerly and any pip-installed
    # ``fluid_build.llm_providers`` plugin lazily (membership consults the plugin
    # registry only when --llm-provider is actually supplied, so --help never
    # scans entry points). Built-ins always win a name clash.
    from fluid_build.cli._llm_provider_plugins import LlmProviderChoices

    parser.add_argument(
        "--llm-provider",
        choices=LlmProviderChoices(),
        help=(
            "LLM provider for copilot. Keyless options: "
            "claude-code / codex / cursor / kiro (local agent CLI). Third-party "
            "providers installed via the fluid_build.llm_providers entry point "
            "are also accepted."
        ),
    )
    parser.add_argument("--llm-model", help="Model identifier for copilot")
    parser.add_argument(
        "--forge-agent-mode",
        dest="forge_agent_mode",
        choices=["envelope", "agentic"],
        help=(
            "Drive mode for coding-agent providers (claude-code/codex/cursor/kiro): "
            "'agentic' (default; the agent writes contract.fluid.yaml in the workspace "
            "with the FLUID skills and its own tools) or 'envelope' (one call: the agent "
            "returns the contract and forge validates it)."
        ),
    )
    parser.add_argument(
        "--llm-endpoint",
        help="Exact HTTP endpoint override for the selected LLM adapter",
    )
    parser.add_argument(
        "--require-llm",
        action="store_true",
        help="Fail if the configured LLM cannot run; do not fall back to non-AI paths.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "Bypass the LLM response cache. Useful when iterating on prompts "
            "or comparing model output. (Same flag is on `fluid forge data-model`.)"
        ),
    )
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help=(
            "Force temperature=0 + cache off + tiering off + audit-trail "
            "metadata. Use for byte-stable proof-of-determinism reports."
        ),
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help=(
            "Force the heuristic-only forge path; never call any LLM. "
            "Equivalent to `--blank` for the full no-AI experience."
        ),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Run the fully local guided interview with no network calls: "
            "no LLM, no mode picker, no welcome scan, no remote schema "
            "fetch. Templates + schemas are already bundled, so authoring "
            "works air-gapped. Also enabled by FLUID_FORGE_OFFLINE=1 "
            "(mirrors `cargo --offline` / CARGO_NET_OFFLINE and the "
            "existing `fluid validate --offline`). Pair with "
            "--non-interactive for a headless, prompt-free run."
        ),
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help=(
            "Skip the pre-write preview confirmation prompt. The cost + file "
            "list is still rendered (invariant: cost is visible before it's "
            "spent), only the [Y/n] prompt is bypassed."
        ),
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        help=(
            "Headless agent-drivable preset. Bundles --yes + all "
            "FLUID_FORGE_NO_{PICKER,PREVIEW,WELCOME,STREAMING_PREVIEW}=1 + "
            "emits JSON-Lines events (forge.start, forge.contract_written, "
            "forge.file_written, forge.plan with --emit-plan, forge.error on "
            "a failure, forge.done) to stdout so the IDE's "
            "shell tool can parse them. Any of --llm-provider, --llm-model, "
            "--llm-endpoint or --context selects AI mode (strictly "
            "non-interactive; a failed generation exits non-zero). With no "
            "mode flag and no AI flag it defaults to --blank (avoids "
            "dropping into the interactive picker the agent cannot "
            "navigate). Use this from inside any agentic IDE (Kiro, "
            "Cursor, Claude Code, Cline)."
        ),
    )
    parser.add_argument(
        "--emit-plan",
        dest="emit_plan",
        action="store_true",
        help=(
            "Emit a one-shot `forge.plan` JSONL event with the structured "
            "checklist of what fields the agent must fill in to complete "
            "the contract (no LLM call, deterministic). Used after "
            "`--agent --blank` so the IDE's agent (which is itself an LLM) "
            "knows exactly what to author. Implies --agent."
        ),
    )
    parser.add_argument(
        "--refine",
        nargs="?",
        const="contract.fluid.yaml",
        default=None,
        metavar="CONTRACT_PATH",
        help=(
            "Iterate on an existing contract. Reads the contract + the latest "
            ".fluid/agents/<run-id>/ artifacts and asks the LLM what to change. "
            "Defaults to ./contract.fluid.yaml when no path is given."
        ),
    )
    parser.add_argument(
        "--from-product",
        action="append",
        dest="from_product",
        default=[],
        metavar="ID_OR_PATH",
        help=(
            "Compose this product from an existing upstream product. "
            "Accepts a contract id (e.g. silver.commerce.orders_v1) or a "
            "path to a contract.fluid.yaml. Repeatable; each --from-product "
            "becomes one row in consumes[]."
        ),
    )
    parser.add_argument(
        "--from-product-list",
        dest="from_product_list",
        metavar="FILE",
        help=(
            "Read upstream product ids/paths from a file (one per line). "
            "Combines additively with --from-product."
        ),
    )
    parser.add_argument(
        "--from-workspace",
        dest="from_workspace",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Search this additional workspace path for upstream contracts. "
            "Repeatable; useful when upstream products live in a sibling repo."
        ),
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help=(
            "Use the browser-based AI setup flow when picking a provider. "
            "Opens the provider's API-key dashboard in your browser; the "
            "key still pastes back into the terminal (preview)."
        ),
    )
    # ``--no-memory`` is a hidden no-op. The scaffold-ide indexes already installed in users'
    # CLAUDE.md/AGENTS.md, the authoring SKILL.md and the eval harness all pass it; dropping the
    # flag would make argparse reject those commands.
    parser.add_argument(
        "--no-memory",
        dest="no_memory",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    # --- CI/CD auto-scaffolding (post-generation hook) ---
    # Listed values are kept in sync with PipelineProvider in
    # fluid_build/forge/core/pipeline_templates.py plus the control sentinels
    # ``none`` (skip) and ``ask`` (force interactive menu even with memory).
    parser.add_argument(
        "--ci",
        choices=[
            "github_actions",
            "gitlab_ci",
            "azure_devops",
            "jenkins",
            "bitbucket",
            "circleci",
            "circle_ci",
            "tekton",
            "none",
            "ask",
        ],
        default=None,
        help="Auto-generate a CI/CD pipeline after scaffolding (e.g. 'github_actions'); 'none' skips, 'ask' always prompts",
    )
    parser.add_argument(
        "--ci-complexity",
        choices=["basic", "standard", "advanced", "enterprise"],
        default="standard",
        help="Complexity of the auto-generated CI pipeline (default: standard)",
    )
    parser.add_argument(
        "--no-ci",
        action="store_true",
        help="Skip CI/CD pipeline auto-scaffolding (equivalent to --ci none)",
    )

    # --- Scaffolding opt-in (slice UX-H) ---
    # Default: minimal-layout forge run. Only contract.fluid.yaml + the
    # .fluid/ forge receipt land on disk (plus optional CI files). Pass
    # --scaffold <template> to opt back into the legacy full-project tree
    # (extracts/, loads/, transforms/, config/, docs/, tests/, scripts/,
    # requirements.txt, .env.example, README.md, …) produced by the
    # ForgeEngine + opinionated templates.
    parser.add_argument(
        "--scaffold",
        "--template",
        dest="scaffold",
        metavar="TEMPLATE",
        default=None,
        help=(
            "Opt into the full ForgeEngine scaffold using one of the built-in "
            "templates (e.g. 'etl_pipeline', 'analytics', 'ml_pipeline', "
            "'streaming', 'starter'). ``--template`` is an alias retained "
            "for parity with ``fluid init --template``. Default: minimal — "
            "only contract.fluid.yaml and .fluid/forge-receipt.json are written."
        ),
    )
    # Mirror the parsed value to ``args.template`` so the mode-picker
    # template branch (which reads ``args.template``) sees it too.
    # argparse aliases only set the dest, not extra attributes.

    parser.set_defaults(func=run)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_target_directory(args, default_name: str = "my-fluid-project") -> Path:
    import fluid_build.cli.forge as _self

    return _self._get_target_dir(args, default_name)


def get_cli_arg(args: Any, name: str, default: Any = None) -> Any:
    import fluid_build.cli.forge as _self

    return _self._get_cli_arg(args, name, default)


def _forge_offline_requested(args: Any) -> bool:
    """True when the offline guided path should run — no network anywhere.

    Two triggers, mirroring ``cargo``'s ``--offline`` flag + its
    ``CARGO_NET_OFFLINE`` env twin (and consistent with the existing
    ``fluid validate --offline``):

    * the ``--offline`` flag (``args.offline``), or
    * ``FLUID_FORGE_OFFLINE`` set to a truthy value in the environment.

    Env parsing matches the rest of the forge toggle surface
    (``_forge_mode_picker``): any non-empty value other than the usual
    falsey spellings (``0`` / ``false`` / ``no`` / ``off``) counts as on.
    """
    if get_cli_arg(args, "offline", False):
        return True
    raw = os.environ.get("FLUID_FORGE_OFFLINE")
    if raw is None:
        return False
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


# ---------------------------------------------------------------------------
# Blank mode — scaffold empty contract, no AI
# ---------------------------------------------------------------------------


def _pick_template_subchoice(console: Any) -> Optional[str]:
    """Sub-picker shown when the user chose ``template`` in the mode menu
    but didn't pre-select one with ``--template``.

    Lists the templates registered with ``forge.core.registry`` so a new
    template auto-appears here without code edits.
    """
    try:
        from fluid_build.cli.forge_ui import ask_numbered_choice
        from fluid_build.forge.core.registry import template_registry
    except Exception as exc:  # noqa: BLE001
        LOG.debug("template_subpicker_unavailable: %s", exc)
        return "starter"

    try:
        names = sorted(template_registry.list_available())
    except Exception:  # noqa: BLE001
        names = ["starter", "analytics", "etl_pipeline", "streaming", "ml_pipeline"]
    if not names:
        return "starter"

    choices = [(name, name.replace("_", " ").title()) for name in names]
    choices.append(("cancel", "Cancel — back to forge"))
    pick = ask_numbered_choice(
        console,
        "Which template?",
        choices,
        default=1,
    )
    if pick == "cancel":
        return None
    return pick


def _run_blank_mode(args: Any, logger: logging.Logger) -> int:
    """Create a minimal empty contract scaffold without AI."""
    from fluid_build.cli.forge_contract_factory import (
        build_minimal_contract,
        create_and_validate_contract,
    )

    console = Console() if RICH_AVAILABLE else None
    target_dir = get_target_directory(args, "my-data-product")

    if get_cli_arg(args, "dry_run", False):
        if console:
            console.print(f"[dim]DRY RUN: Would create empty contract in {target_dir}[/dim]")
        return 0

    contract_path = target_dir / "contract.fluid.yaml"
    if contract_path.exists():
        if console:
            console.print(
                f"[yellow]contract.fluid.yaml already exists in {target_dir}[/yellow]\n"
                "[dim]Delete it first or use a different --target-dir.[/dim]"
            )
        return 1

    contract = build_minimal_contract()
    result_path = create_and_validate_contract(contract, target_dir, logger, console)
    if not result_path:
        return 1

    # No ``forge.contract_written`` here: ``_run_main`` writes the receipt after
    # this returns, and the receipt emits one event per file written, for every
    # mode (``_write_forge_receipt``).

    # In --agent mode we skip the interactive CI-pipeline prompt entirely.
    # The agent can shell-run ``fluid scaffold-ci --system <name>`` separately
    # if it wants CI scaffolding. Without this guard, blank-mode under
    # --agent hits an EOFError trying to read from a closed stdin.
    if not bool(get_cli_arg(args, "agent", False)):
        import fluid_build.cli.forge as _self

        _self._scaffold_ci_pipeline(
            args,
            target_dir,
            {},
            console,
            ask_dialog_question_fn=_self.ask_dialog_question,
            get_cli_arg_fn=get_cli_arg,
            dry_run=False,
        )

    _print_next_steps(console, target_dir, result_path)
    return 0


# ---------------------------------------------------------------------------
# Shared UI helpers
# ---------------------------------------------------------------------------

_DOCS_URL = "https://agenticstiger.github.io/forge_docs/concepts/contract.html"


def _print_forge_next_steps(console: Any, args: Any, scan_root: Path) -> None:
    """Print next steps, auto-detecting fragment-first layout."""
    try:
        fragments_dir = scan_root / "fragments"
        if fragments_dir.is_dir() and any(fragments_dir.rglob("*.yaml")):
            print_next_steps("forge-fragments", console=console, args=args)
            _print_split_bundle_hint(console)
            return
    except Exception:  # noqa: BLE001 — detection is best-effort
        pass
    print_next_steps("forge", console=console, args=args)
    _print_split_bundle_hint(console)


def _print_split_bundle_hint(console: Any) -> None:
    """Show hint about split/bundle after successful forge."""
    hint_text = (
        "Tip: Use 'fluid split' to break this into modular fragments.\n"
        "     FLUID auto-bundles fragments when you run validate, plan, or apply."
    )
    try:
        if console is not None and RICH_AVAILABLE:
            console.print(f"\n[dim]{hint_text}[/dim]")
        else:
            cprint(f"\n{hint_text}")
    except Exception:
        pass


def _print_next_steps(console: Any, target_dir: Path, contract_path: Path) -> None:
    """Show post-creation next steps with doc link."""
    steps = (
        f"[green]Created contract at:[/green] {contract_path}\n\n"
        "Next steps:\n"
        f"  1. cd {target_dir}\n"
        "  2. Edit contract.fluid.yaml\n"
        "  3. fluid validate contract.fluid.yaml\n"
        "  4. fluid plan contract.fluid.yaml --out runtime/plan.json\n"
        "  5. fluid apply runtime/plan.json\n\n"
        "[dim]Tip: Run 'fluid forge' (AI mode) to auto-generate transformation\n"
        "logic from your sample data, or 'fluid generate' after editing\n"
        "the contract manually.[/dim]\n\n"
        f"[dim]Docs: {_DOCS_URL}[/dim]"
    )
    if console and RICH_AVAILABLE:
        console.print(Panel(steps, title="Forge Complete", border_style="green"))
    else:
        cprint(f"Created contract at {contract_path}")
        cprint(f"Docs: {_DOCS_URL}")


# ---------------------------------------------------------------------------
# Agent-mode helpers (Phase 2B — `fluid forge --agent`)
# ---------------------------------------------------------------------------
#
# These let the IDE's agent shell-run forge cleanly: a single ``--agent``
# flag bundles all FLUID_FORGE_NO_* env vars + ``--yes`` + a default-blank
# fallback so we never drop into the interactive picker. A small JSON-Lines
# event stream over stdout lets the agent parse progress (start / done /
# contract_written / cost) without scraping Rich console output.
#
# Design choice: stdout carries the events and nothing else, one whole JSON
# object per line (NDJSON, as AGENT_IDE.md promises), so the agent's parser
# is ``for line in stdout: json.loads(line)``. ``run`` keeps the real stdout
# for the events and points ``sys.stdout`` at stderr for the rest of the run
# (``contextlib.redirect_stdout``), so
# every human-facing writer moves without being touched: Rich consoles built
# without ``file=`` (Rich looks ``sys.stdout`` up at each write), ``print``,
# and a provider SDK's banner. Outside ``--agent`` nothing changes.

#: Where ``--agent`` events go while ``run`` points ``sys.stdout`` at stderr:
#: the stdout the run started with. ``None`` outside an ``--agent`` run.
_AGENT_EVENTS: Optional[Any] = None


def _emit_agent_jsonl(event: str, **kvs: Any) -> None:
    """Emit one canonical JSON-Lines event on the run's real stdout for the agent.

    Only used when ``--agent`` is active; callers gate via ``args.agent``.
    Failures are swallowed so a stdout pipe-closed never poisons the run.
    """
    try:
        stream = _AGENT_EVENTS or sys.stdout
        payload = {"event": event, "ts": time.time(), **kvs}
        stream.write(json.dumps(payload, default=str) + "\n")
        stream.flush()
    except Exception:  # noqa: BLE001
        pass


def _setup_agent_mode(args: Any) -> None:
    """Mutate env + args so the run is non-interactive and machine-friendly."""
    # Suppress all of forge's interactive UI surfaces so the agent never
    # gets stuck on a prompt it can't answer.
    for var in (
        "FLUID_FORGE_NO_PICKER",
        "FLUID_FORGE_NO_PREVIEW",
        "FLUID_FORGE_NO_WELCOME",
    ):
        os.environ.setdefault(var, "1")
    args.yes = True

    # An explicit AI flag is the agent choosing AI mode: no blank forcing,
    # and the AI path must never block on a prompt (``non_interactive`` is
    # what the rest of forge reads to skip inline setup and confirmations).
    if _agent_chose_ai(args):
        args.non_interactive = True

    # If the agent didn't pick a mode, default to --blank: it's the
    # only mode that succeeds without an LLM round-trip, so it's the
    # safest fallback when invoked under ``--agent --data-product-type X``
    # with no other intent. The agent can override by passing
    # --from-product, --refine, or --template explicitly.
    no_mode = not any(
        [
            bool(getattr(args, "blank", False)),
            bool(getattr(args, "template", None)),
            bool(getattr(args, "scaffold", None)),
            bool(getattr(args, "refine", None)),
            bool(getattr(args, "from_product", None)),
            bool(getattr(args, "from_product_list", None)),
            _agent_chose_ai(args),
        ]
    )
    if no_mode:
        args.blank = True


def _agent_chose_ai(args: Any) -> bool:
    """True when the agent passed any explicit AI flag (selects AI mode)."""
    return any(
        bool(getattr(args, flag, None))
        for flag in ("llm_provider", "llm_model", "llm_endpoint", "context")
    )


def _agent_run_id() -> str:
    """Short, stable run-id for the JSONL event stream (12 hex chars)."""
    import uuid

    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# --emit-plan — deterministic "what to fill in" checklist for the IDE agent
# ---------------------------------------------------------------------------
#
# Architectural note: the IDE's agent already has an LLM (paid for by the
# IDE's subscription). We don't want forge to ALSO run an LLM — that would
# need a second API key. Instead, we emit a deterministic checklist of
# fields the agent must fill in, and let the agent (the LLM in the IDE) do
# the authoring using its own Edit tools. Pattern 1 from AGENT_IDE.md.
#
# The checklist is keyed by productType so SDP / ADP / CDP each get the
# right next-steps (e.g. SDP has acquisition[], ADP has transformations[],
# CDP has consumes[] + transformations[] + access[]).

_PLAN_BY_PRODUCT_TYPE: Dict[str, List[Dict[str, Any]]] = {
    "SDP": [
        {
            "step": "Define acquisition[]",
            "description": "How data is acquired from the source system",
            "fields": ["acquisition[].source", "acquisition[].mode"],
            "mcp_tools": ["list_source_adapters", "list_source_tables", "inspect_source_table"],
        },
        {
            "step": "Define models[].entities[]",
            "description": "Mirror the upstream schema — entity per source table",
            "fields": [
                "models[].name",
                "models[].entities[].name",
                "models[].entities[].fields[]",
                "models[].entities[].primaryKey",
            ],
            "mcp_tools": ["inspect_source_table", "list_source_lineage"],
        },
        {
            "step": "Add quality[]",
            "description": "Declarative quality rules (uniqueness, freshness, schema)",
            "fields": ["quality[].rule", "quality[].entity"],
            "mcp_tools": [],
        },
    ],
    "ADP": [
        {
            "step": "Define consumes[]",
            "description": "Upstream SDP/ADP products this aggregate is built from",
            "fields": ["consumes[].productId", "consumes[].version"],
            "mcp_tools": ["list_source_lineage"],
        },
        {
            "step": "Define models[].entities[] for the aggregate",
            "description": "Aggregate-level entities (joined/grouped)",
            "fields": ["models[].entities[].name", "models[].entities[].fields[]"],
            "mcp_tools": ["inspect_source_table", "read_logical_model"],
        },
        {
            "step": "Define transformations[]",
            "description": "dbt model refs, inline SQL, or external .sql files",
            "fields": ["transformations[].engine", "transformations[].sql"],
            "mcp_tools": [],
        },
        {
            "step": "Add quality[]",
            "description": "Aggregate-level quality rules",
            "fields": ["quality[].rule", "quality[].entity"],
            "mcp_tools": [],
        },
    ],
    "CDP": [
        {
            "step": "Define consumes[]",
            "description": "Upstream products feeding this consumer-aligned product",
            "fields": ["consumes[].productId", "consumes[].version"],
            "mcp_tools": ["list_source_lineage"],
        },
        {
            "step": "Define models[].entities[] for the consumer view",
            "description": "Consumer-facing entities (denormalised, BI-friendly)",
            "fields": ["models[].entities[].name", "models[].entities[].fields[]"],
            "mcp_tools": ["read_logical_model"],
        },
        {
            "step": "Define transformations[]",
            "description": "Joins + denormalisation SQL",
            "fields": ["transformations[].engine", "transformations[].sql"],
            "mcp_tools": [],
        },
        {
            "step": "Define access[]",
            "description": "IAM/GRANT bindings for downstream consumers",
            "fields": ["access[].principal", "access[].grants[]"],
            "mcp_tools": [],
        },
        {
            "step": "Add exposes block (optional)",
            "description": "Agent-policy for downstream AI consumers",
            "fields": ["exposes.agentPolicy.allowedUseCases", "exposes.semantics"],
            "mcp_tools": [],
        },
    ],
}


def _emit_forge_plan(args: Any, contract_path: Optional[Path]) -> None:
    """Emit a single ``forge.plan`` JSONL event with the structured checklist
    for what the IDE's agent should fill in. Deterministic; no LLM call.
    """
    dpt_raw = (getattr(args, "data_product_type", None) or "").strip().upper()
    # Map medallion → DataMesh layer per the canonical mapping in CLAUDE.md.
    layer_map = {"BRONZE": "SDP", "SILVER": "ADP", "GOLD": "CDP"}
    dpt = layer_map.get(dpt_raw, dpt_raw)
    steps = _PLAN_BY_PRODUCT_TYPE.get(dpt, _PLAN_BY_PRODUCT_TYPE["SDP"])
    _emit_agent_jsonl(
        "forge.plan",
        contract_path=str(contract_path) if contract_path else None,
        data_product_type=dpt or None,
        next_steps=steps,
        validation_command=(
            f"fluid validate {contract_path}" if contract_path else "fluid validate <path>"
        ),
        completion_signal=(
            "After filling in the contract, call MCP `validate_contract` to "
            "gate-check, then `fluid bundle && fluid plan` for the pipeline."
        ),
        note=(
            "You (the IDE's agent) are the LLM: fill in these fields using "
            "your own Edit tool. Running `fluid forge --agent --llm-provider "
            "<name>` instead has forge's own AI author the contract, which "
            "needs a second LLM."
        ),
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run(args, logger: logging.Logger) -> int:
    """Agent-mode-aware wrapper around the main forge entry point.

    When ``--agent`` is set, configures the run for headless invocation and
    emits a JSONL event stream so the IDE's shell tool can parse progress.
    """
    # ``--emit-plan`` implies ``--agent`` — the plan event is part of the
    # JSONL stream contract.
    if getattr(args, "emit_plan", False) and not getattr(args, "agent", False):
        args.agent = True

    is_agent = bool(getattr(args, "agent", False))
    if not is_agent:
        from fluid_build.cli._forge_mode_errors import take_forge_failure

        try:
            return _run_main(args, logger)
        finally:
            take_forge_failure()  # read by --agent only: drop it and its frames

    _setup_agent_mode(args)
    run_id = _agent_run_id()
    mode = (
        "from-product"
        if getattr(args, "from_product", None)
        else (
            "refine"
            if getattr(args, "refine", None)
            else (
                "template"
                if getattr(args, "template", None) or getattr(args, "scaffold", None)
                else ("blank" if getattr(args, "blank", False) else "ai")
            )
        )
    )
    global _AGENT_EVENTS  # noqa: PLW0603 — one run at a time, reset below
    _AGENT_EVENTS = sys.stdout
    rc = 1
    try:
        # Everything a human reads goes to stderr; the events keep stdout.
        with contextlib.redirect_stdout(sys.stderr):
            _emit_agent_jsonl(
                "forge.start",
                run_id=run_id,
                mode=mode,
                data_product_type=getattr(args, "data_product_type", None),
                target_dir=str(getattr(args, "target_dir", None) or ""),
                emit_plan=bool(getattr(args, "emit_plan", False)),
            )
            # A missing extra still propagates, as it always has: the CLI entry
            # point says the remedy on stderr under --agent
            # (``cli._render_missing_optional``), and an in-process caller
            # gets the exception itself.
            rc = _run_main_noting_failure(args, logger)
            # On success, optionally emit the structured plan checklist BEFORE
            # forge.done so consumers parsing the stream see the plan in order.
            if rc == 0 and getattr(args, "emit_plan", False):
                target_dir = getattr(args, "target_dir", None)
                contract_path = Path(target_dir) / "contract.fluid.yaml" if target_dir else None
                _emit_forge_plan(args, contract_path)
            return rc
    finally:
        _emit_forge_error(run_id, rc)
        _emit_agent_jsonl("forge.done", run_id=run_id, exit_code=int(rc or 0))
        _AGENT_EVENTS = None


def _run_main_noting_failure(args: Any, logger: logging.Logger) -> int:
    """``_run_main`` for an ``--agent`` run, noting an exception that propagates.

    A handler that catches its failure notes it itself (``log_forge_failure``);
    one that escapes ``_run_main`` (a missing extra) is noted here, then re-raised.
    """
    from fluid_build.cli._forge_mode_errors import note_forge_failure, take_forge_failure

    take_forge_failure()  # an earlier in-process run's failure is not this run's
    try:
        return _run_main(args, logger)
    except Exception as exc:
        note_forge_failure(exc)
        raise


def _emit_forge_error(run_id: str, rc: int) -> None:
    """Send ``forge.error`` for a failed run: why it failed, as data.

    The failure is the one a handler noted. A run that failed without an
    exception (blank mode finding a contract in place) has none to send.
    """
    from fluid_build.cli._forge_mode_errors import describe_forge_failure, take_forge_failure

    failure = take_forge_failure()
    if rc == 0 or failure is None:
        return
    try:
        fields = describe_forge_failure(failure)
    except Exception:  # noqa: BLE001 — never at the cost of forge.done
        LOG.debug("forge_error_event_failed", exc_info=True)
        return
    _emit_agent_jsonl("forge.error", run_id=run_id, **fields)


def _run_main(args, logger: logging.Logger) -> int:
    """Main entry point for ``fluid forge``."""
    console = Console() if RICH_AVAILABLE else None
    try:
        if getattr(args, "forge_subcommand", None) == "data-model":
            from fluid_build.cli.forge_data_model import run as run_data_model

            quiet = getattr(args, "quiet", False)
            if getattr(args, "data_model_action", None) == "from-intent" and (
                getattr(args, "example", None)
                or getattr(args, "schema", False)
                or getattr(args, "validate_intent", None)
            ):
                quiet = True
            print_v2_banner("forge_data_model", quiet=quiet)
            return run_data_model(args, logger)

        # --- Help ---
        if getattr(args, "help", False):
            if console:
                from .help_formatter import print_forge_help

                print_forge_help()
                return 0
            cprint(
                "Run 'fluid forge' to start the AI Copilot, or 'fluid forge --blank' for an empty contract."
            )
            return 0

        # --- Snapshot for the forge receipt ---
        # Scan cwd (and an explicit --target-dir) before any mode runs so the
        # diff catches every file the mode handler wrote.  find_workspace_root
        # is cheap and localised; if this isn't a workspace yet, we fall back
        # to cwd and still produce a receipt.
        scan_root = Path.cwd()
        before_snapshot = _forge_snapshot(scan_root, args)
        import fluid_build.cli.forge as _self

        _self.reset_token_usage()

        # --- Top-level flag aliases ---
        # ``--no-llm`` and ``--deterministic`` were added at the
        # top-level forge surface so operators can opt out of AI
        # without remembering ``--blank``.  Both currently route
        # through the existing blank-mode path because the AI
        # copilot doesn't yet honour the flags directly; data-model
        # subcommands still consume them natively.  Wiring the AI
        # copilot to read them is tracked as a follow-up.
        if get_cli_arg(args, "no_llm", False) or get_cli_arg(args, "deterministic", False):
            args.blank = True

        # --- Offline guided mode (Trello #69d4c9ca — Forge UX: offline guided) ---
        # ``--offline`` (or FLUID_FORGE_OFFLINE=1) forces the fully local,
        # no-network authoring path: the heuristic guided interview, no
        # LLM, no mode picker, no welcome scan, no remote schema fetch.
        # Modeled on ``cargo --offline`` / CARGO_NET_OFFLINE and
        # consistent with the existing ``fluid validate --offline``.
        # Templates + schemas are already bundled, so this works
        # air-gapped. An explicit ``--blank`` (already fully offline) is
        # the more specific "empty contract" intent, so it wins and falls
        # through to the blank path below.
        if _forge_offline_requested(args) and not get_cli_arg(args, "blank", False):
            LOG.debug("Forge: offline guided mode selected")
            result = run_guided_mode(args, logger)
            if result == 0:
                _write_forge_receipt(
                    flow="guided",
                    args=args,
                    before_snapshot=before_snapshot,
                    scan_root=scan_root,
                    logger=logger,
                )
                _print_forge_next_steps(console, args, scan_root)
            return result

        # --- Mode picker (Phase 0.2) ---
        # When the user runs bare ``fluid forge`` with no mode flag and
        # stdin is a TTY, surface the menu of authoring paths instead
        # of dropping straight into AI mode. Pre-highlights the most
        # likely choice from the welcome scan (existing contract →
        # refine; existing products → from_product; otherwise → AI).
        _picker_ran = False
        _picked_mode = "ai"
        try:
            from fluid_build.cli._forge_mode_picker import pick_mode, should_show_picker

            if should_show_picker(args):
                _picked_mode = pick_mode(args, console=console)
                _picker_ran = True
                LOG.debug("Forge: mode picker selected %s", _picked_mode)
                # Print a sticky confirmation so the user always sees
                # which path the run is taking — even when downstream
                # panels (welcome / Ollama detection) push the picker
                # off-screen on short terminals.
                if console:
                    try:
                        _label_map = {
                            "ai": "🧠 AI Copilot",
                            "blank": "🧱 Blank scaffold",
                            "refine": "✏️  Refine existing contract",
                            "from_product": "🔗 Compose from existing products",
                            "template": "📋 Template-based",
                        }
                        console.print(
                            f"\n[bold green]→[/bold green] [bold]{_label_map.get(_picked_mode, _picked_mode)}[/bold] mode\n"
                        )
                    except Exception:  # noqa: BLE001
                        pass
                # ``from_product`` mode picker — gather upstream picks
                # interactively so the AI mode below can use them.
                if _picked_mode == "from_product":
                    try:
                        from fluid_build.cli._forge_from_product_picker import (
                            pick_upstream_products,
                        )

                        picks = pick_upstream_products(console=console)
                        if picks:
                            args.from_product = list(picks)
                    except Exception:  # noqa: BLE001 — picker is best-effort
                        LOG.debug("from_product_picker_failed", exc_info=True)
        except Exception:  # noqa: BLE001 — never let the picker break forge
            LOG.debug("mode_picker_failed", exc_info=True)

        # --- Determine effective mode ---
        is_blank = get_cli_arg(args, "blank", False)
        flow = "blank" if is_blank else "copilot"

        if is_blank:
            LOG.debug("Forge: blank mode selected")
            result = _run_blank_mode(args, logger)
            if result == 0:
                _write_forge_receipt(
                    flow=flow,
                    args=args,
                    before_snapshot=before_snapshot,
                    scan_root=scan_root,
                    logger=logger,
                )
                _print_forge_next_steps(console, args, scan_root)
            return result

        # Template mode: route to the dedicated handler, no AI required.
        # Triggered by the picker's "template" choice OR by
        # ``--template``/``--scaffold`` on the command line. argparse
        # stores both flags in ``args.scaffold``; ``run_template_mode``
        # reads ``args.template``, so we mirror across them.
        _scaffold_value = getattr(args, "scaffold", None) or getattr(args, "template", None)
        # Only treat as explicit when the value is a real string —
        # tests sometimes pass MagicMock placeholders that are truthy
        # but not actual template names.
        _template_explicit = isinstance(_scaffold_value, str) and bool(_scaffold_value)
        if _template_explicit:
            args.template = _scaffold_value
            args.scaffold = _scaffold_value

        if (_picker_ran and _picked_mode == "template") or _template_explicit:
            LOG.debug(
                "Forge: template mode (picker=%s, explicit=%s)", _picker_ran, _template_explicit
            )
            # Default template when the user hasn't set one explicitly.
            if not getattr(args, "template", None):
                tpl = _pick_template_subchoice(console)
                if tpl is None:
                    return 0
                args.template = tpl
                args.scaffold = tpl
            from fluid_build.cli.forge_modes import (
                run_template_mode as _run_template,
            )

            result = _run_template(
                args,
                logger,
                get_target_directory_fn=get_target_directory,
            )
            if result == 0:
                _write_forge_receipt(
                    flow="template",
                    args=args,
                    before_snapshot=before_snapshot,
                    scan_root=scan_root,
                    logger=logger,
                )
                _print_forge_next_steps(console, args, scan_root)
            return result

        # --- Default: AI Copilot with inline LLM setup ---
        LOG.debug("Forge: copilot mode")
        # Skip the redundant FLUID-Forge welcome panel when the
        # mode picker already showed; the picker IS the welcome.
        if console and not get_cli_arg(args, "non_interactive", False) and not _picker_ran:
            print_welcome_panel(console)

        # Check LLM readiness; load saved config or offer inline setup. Setup
        # cannot see the run's flags, so a run that names its provider
        # (--llm-provider, FLUID_LLM_PROVIDER, FLUID_FORGE_AGENT) skips it and
        # resolves as a run without a TTY does. The copilot's preflight still
        # fails first on a provider this install cannot run (no litellm), before
        # anything reads the keyring.
        from fluid_build.llm.providers import named_llm_provider

        if not get_cli_arg(args, "non_interactive", False) and not named_llm_provider(args):
            from fluid_build.cli.ai_setup import run_ai_setup_inline

            # Inline setup handles every run that names no provider:
            # 1. Config file exists with key → loads it, sets env vars, returns config
            # 2. Keyring has key → loads it, sets env vars, returns config
            # 3. Nothing found → prompts user interactively
            llm_config = run_ai_setup_inline(console)
            if llm_config:
                from fluid_build.llm.providers import fill_llm_args_from_setup

                # Setup fills only what the operator left unstated
                # (--llm-model, --llm-endpoint and their FLUID_LLM_* twins win),
                # and its key goes into the session only with its own provider.
                if fill_llm_args_from_setup(args, llm_config) and llm_config.api_key:
                    from fluid_build.cli.ai_setup import set_session_env

                    set_session_env(llm_config.provider, llm_config.api_key)
                LOG.debug("LLM config loaded: provider=%s", llm_config.provider)
            else:
                # AI not available — fall back to guided mode
                if console:
                    import fluid_build.cli.forge as _self

                    proceed = _self.ask_confirmation(
                        console,
                        "Continue with guided mode (no AI)?",
                        default=True,
                    )
                else:
                    proceed = False
                if proceed:
                    result = run_guided_mode(args, logger)
                    if result == 0:
                        _write_forge_receipt(
                            flow="guided",
                            args=args,
                            before_snapshot=before_snapshot,
                            scan_root=scan_root,
                            logger=logger,
                        )
                        _print_forge_next_steps(console, args, scan_root)
                    return result
                if console:
                    console.print(
                        "[yellow]Use 'fluid forge --blank' for a bare contract,[/yellow]\n"
                        "[yellow]or run 'fluid ai setup' to configure an LLM provider.[/yellow]"
                    )
                return 1

        result = run_ai_copilot_mode(args, logger)
        if result == 0:
            _write_forge_receipt(
                flow="copilot",
                args=args,
                before_snapshot=before_snapshot,
                scan_root=scan_root,
                logger=logger,
                provenance=getattr(args, "_copilot_provenance", None),
            )
            _print_forge_next_steps(console, args, scan_root)
        return result

    except KeyboardInterrupt:
        logger.info("Forge cancelled by user")
        return 130
    except Exception as exc:  # noqa: BLE001
        # World-class error UX — every typed Fluid error carries
        # ``suggestions``; we surface those instead of a raw stack
        # trace. Stack trace is logged at DEBUG for triage, but the
        # operator-facing output is just the message + actionable
        # next steps.
        from fluid_build._optional import MissingOptionalDependency
        from fluid_build.cli._forge_mode_errors import (
            describe_forge_failure,
            is_typed_forge_error,
            log_forge_failure,
        )

        if isinstance(exc, MissingOptionalDependency):
            # "Install the extra" is the whole remedy: let the CLI entry point
            # print the install line and exit 1, with no traceback.
            raise

        log_forge_failure(logger, "Forge command failed", exc)
        if is_typed_forge_error(exc):
            # Typed Fluid error — clean message + suggestions, the ones the
            # ``--agent`` event carries (a FluidUserError's ``fix`` included).
            failure = describe_forge_failure(exc)
            message, suggestions = failure["message"], failure["suggestions"]
            if console:
                console.print(
                    literal_text("Forge failed:", "red") + literal_error_text(f" {message}")
                )
                if suggestions:
                    console.print("[bold]Next actions:[/bold]")
                    for s in suggestions:
                        console.print(literal_error_text(f"  • {s}"))
            else:
                console_error(f"Forge failed: {message}")
                if suggestions:
                    cprint("Next actions:")
                    for s in suggestions:
                        cprint_literal(f"  - {s}")
        else:
            # Unexpected — full message; suggest doctor.
            if console:
                console.print(literal_error_text(f"Forge failed: {exc}", "red"))
                console.print(f"[dim]Run 'fluid doctor' to diagnose, or see {_DOCS_URL}[/dim]")
            else:
                console_error(f"Forge failed: {exc}")
                cprint(f"Run 'fluid doctor' to diagnose, or see {_DOCS_URL}")
        return 1


# ---------------------------------------------------------------------------
# Mode wrappers (thin delegation)
# ---------------------------------------------------------------------------


def run_ai_copilot_mode(args, logger: logging.Logger) -> int:
    """AI mode: the shipped FLUID skills drive the authoring (``cli/forge_ai.py``)."""
    import fluid_build.cli.forge_ai as _forge_ai

    return _forge_ai.run_ai_mode(args, logger)


def run_guided_mode(args, logger: logging.Logger) -> int:
    """Lightweight guided prompts — no AI needed."""
    import fluid_build.cli.forge as _self

    return _self._run_guided(
        args,
        logger,
        get_target_directory_fn=get_target_directory,
        console_factory=Console if RICH_AVAILABLE else None,
    )


def load_context(
    context_input: str,
    console: Optional[Any] = None,
    *,
    context_error_cls: type[Exception] = ContextValidationError,
) -> Dict[str, Any]:
    import fluid_build.cli.forge as _self

    return _self._load_ctx(
        context_input,
        console,
        context_error_cls=context_error_cls,
    )


# ``diff_snapshots`` action -> the agent JSONL event's ``action``.
_AGENT_EVENT_ACTIONS = {"create": "created", "update": "modified"}


def _forge_snapshot(scan_root: Path, args: Any) -> ArtifactSnapshot:
    """Snapshot *scan_root*, plus the explicit ``--target-dir`` when one is given.

    The cwd scan reaches contracts two directory levels down. A target outside
    cwd, or deeper than that, needs its own scan, or the receipt and the
    ``--agent`` ``forge.contract_written`` event miss the contract written
    there. A file under both roots is keyed by its resolved path, so it is
    counted once; one outside cwd is reported by its absolute path.
    """
    snapshot = snapshot_workspace(scan_root)
    target_dir = get_cli_arg(args, "target_dir", None)
    if isinstance(target_dir, (str, os.PathLike)) and str(target_dir):
        # ``~`` as the writer reads it (``forge_modes`` expands the target too).
        snapshot.files.update(snapshot_workspace(Path(target_dir).expanduser()).files)
    return snapshot


def _write_forge_receipt(
    *,
    flow: str,
    args,
    before_snapshot,
    scan_root: Path,
    logger: logging.Logger,
    provenance: Optional[Dict[str, Any]] = None,
) -> None:
    """Write ``<product>/.fluid/forge-receipt.json`` for this run.

    Never raises — receipt writing is best-effort post-success.  The
    forge receipt lives next to the product it describes, so we
    identify the product by diffing the workspace snapshot and scoping
    to the directory of whichever contract was just (re)written.

    If the diff turns up multiple contracts (e.g. an interactive flow
    that touched several products), the receipt is scoped to the most
    recently modified one.
    """
    try:
        after_snapshot = _forge_snapshot(scan_root, args)
        entries = diff_snapshots(before_snapshot, after_snapshot)
        # Ignore no-op rows — the receipt only records what changed.
        changed = [e for e in entries if e.action != "unchanged"]
        if not changed:
            return

        # Find the product directory for this run — the dir containing
        # the (new or updated) contract.fluid.yaml is the scope root for
        # the receipt.  Fall back to cwd when the flow wrote files
        # without touching any contract (shouldn't happen in practice,
        # but be defensive).
        product_root = _resolve_product_root(changed, scan_root)
        if product_root is None:
            logger.debug("forge_receipt_no_product_root")
            return

        builder = ReceiptBuilder(flow=flow, dry_run=False)
        is_agent = bool(get_cli_arg(args, "agent", False))
        for entry in changed:
            # Re-anchor the path to the product root so the receipt is
            # self-contained and portable across clones.
            abs_path = (scan_root / entry.path).resolve()
            try:
                rel = abs_path.relative_to(product_root.resolve())
                path_str = str(rel)
            except ValueError:
                path_str = str(abs_path)
            builder.record_entry(
                path=Path(path_str),
                action=entry.action,
                sha256=entry.sha256,
                size=entry.size,
                reason=entry.reason,
            )
            # Agent-mode JSONL: one event per changed file. The agent's
            # caller can highlight new contracts in its UI without
            # parsing the receipt. ``diff_snapshots`` says "create" /
            # "update"; the events use the blank path's "created" /
            # "modified" so every mode speaks one vocabulary.
            event_action = _AGENT_EVENT_ACTIONS.get(entry.action)
            if is_agent and event_action:
                _emit_agent_jsonl(
                    (
                        "forge.contract_written"
                        if str(entry.path).endswith("contract.fluid.yaml")
                        else "forge.file_written"
                    ),
                    path=str(entry.path),
                    action=event_action,
                    size=entry.size,
                )

        inputs: Dict[str, Any] = {
            "blank": bool(get_cli_arg(args, "blank", False)) or None,
            "non_interactive": bool(get_cli_arg(args, "non_interactive", False)) or None,
            "context": get_cli_arg(args, "context", None),
            "target_dir": get_cli_arg(args, "target_dir", None),
        }
        if provenance:
            inputs["provenance"] = provenance
        builder.set_inputs(**inputs)

        doc = builder.build_document()

        try:
            from fluid_build import __version__ as tool_version
        except Exception:  # pragma: no cover — defensive
            tool_version = ""

        command = _format_forge_command(args, flow)
        payload = doc.to_payload()
        performance = _forge_performance_payload()
        if performance:
            payload["performance"] = performance
        payload_bytes = dump_json_with_envelope(
            payload,
            kind="ForgeReceipt",
            command=command,
            tool_version=str(tool_version),
        )

        receipt_path = product_forge_receipt_path(product_root)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(payload_bytes, encoding="utf-8")
        logger.debug("forge_receipt_written", extra={"path": str(receipt_path)})
    except Exception as exc:  # noqa: BLE001 — never abort forge on receipt failure
        logger.debug("forge_receipt_write_failed", extra={"error": str(exc)})


def _forge_performance_payload() -> Dict[str, Any]:
    import fluid_build.cli.forge as _self

    metrics = _self.get_cumulative_prompt_cache_metrics()
    if metrics.get("total_tokens", 0) <= 0:
        return {}
    return {"prompt_cache": metrics}


def _resolve_product_root(changed_entries, scan_root: Path) -> Optional[Path]:
    """Return the product directory the forge receipt should live under.

    The product is identified by the `contract.fluid.yaml` file that
    appears in the diff.  When multiple contracts changed, the most
    recently modified one wins.
    """
    candidates: List[Path] = []
    for entry in changed_entries:
        if not entry.path.endswith("contract.fluid.yaml"):
            continue
        abs_path = (scan_root / entry.path).resolve()
        candidates.append(abs_path.parent)

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0]

    # Multiple contracts changed — pick the one whose contract file has
    # the newest mtime.  Falls back to the first candidate on stat error.
    def _mtime(parent: Path) -> float:
        try:
            return (parent / "contract.fluid.yaml").stat().st_mtime
        except OSError:
            return 0.0

    return max(candidates, key=_mtime)


def _format_forge_command(args, flow: str) -> str:
    """Build a short human-readable command string for the receipt."""
    parts = ["fluid forge"]
    if get_cli_arg(args, "blank", False):
        parts.append("--blank")
    if get_cli_arg(args, "non_interactive", False):
        parts.append("--non-interactive")
    if get_cli_arg(args, "target_dir", None):
        parts.append(f"--target-dir {args.target_dir}")
    return " ".join(parts)


__all__ = [
    "COMMAND",
    "ContextValidationError",
    "ForgeError",
    "ForgeMode",
    "InvalidProjectNameError",
    "ProjectGenerationError",
    "get_cli_arg",
    "get_target_directory",
    "load_context",
    "register",
    "run",
    "run_ai_copilot_mode",
    "run_guided_mode",
]
