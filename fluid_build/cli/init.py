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

"""
FLUID Init Command - Universal Project Onboarding

The front door to FLUID - intelligently routes users to the right experience:
- Quickstart: Working example in 2 minutes (local, no cloud)
- Scan: Import existing dbt/Terraform projects (Agent Zero)
- Template: Specific use case templates
- Blank: Empty project skeleton

Strategy: Router pattern - delegates to existing commands (product-new, scaffold-ci)
"""

import argparse
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.cli.artifact_envelope import dump_json_with_envelope
from fluid_build.cli.artifact_paths import workspace_init_receipt_path
from fluid_build.cli.artifact_receipts import ReceiptBuilder
from fluid_build.cli.artifact_scan import diff_snapshots, snapshot_workspace
from fluid_build.cli.console import cprint, success, warning
from fluid_build.cli.console import error as console_error
from fluid_build.cli.next_steps import print_next_steps
from fluid_build.cli.workspace_config import (
    WORKSPACE_FILENAME,
    discover_workspace_products,
    find_workspace_root,
    load_workspace_config,
    save_workspace_config,
)
from fluid_build.schema_manager import FluidSchemaManager
from fluid_build.util.contract import slugify_identifier

from ._logging import error, info

# Try Rich for beautiful output
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False
    console = None

COMMAND = "init"


def _latest_fluid_version() -> str:
    """Return the newest bundled FLUID schema version.

    Resolved lazily at call time so runtime schema updates (or test patches)
    are respected. Avoids the import-time-constant pattern that silently goes
    stale if ``BUNDLED_VERSIONS`` is repopulated.
    """
    return FluidSchemaManager.latest_bundled_version()


def _mark_first_run_complete():
    """Create ~/.fluid directory to signal that onboarding has happened."""
    fluid_home = Path.home() / ".fluid"
    try:
        fluid_home.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # non-fatal — directory might already exist or be unwritable


def _print_templates_list() -> int:
    """Print available templates and exit.  Used by ``fluid init --list-templates``."""
    try:
        from fluid_build.forge.simple_forge import get_template_info, list_templates
    except ImportError:
        if RICH_AVAILABLE:
            console.print("[red]Templates module is not installed.[/red]")
        else:
            cprint("Templates module is not installed.")
        return 1

    try:
        names = list_templates()
    except RecursionError:
        # The simplified forge wrapper currently re-exports a recursive
        # ``list_templates`` helper. Fall back to the registry directly so the
        # user-facing command still works in real environments.
        from fluid_build.forge.core.simple_registry import (
            get_template,
            initialize_registries,
        )
        from fluid_build.forge.core.simple_registry import (
            list_templates as registry_list_templates,
        )

        initialize_registries()
        names = registry_list_templates()

        def get_template_info(template_name: str):
            template = get_template(template_name)
            if not template:
                return None
            try:
                metadata = template.get_metadata()
            except Exception:
                return {"name": template_name}
            return {
                "name": template_name,
                "display_name": getattr(metadata, "display_name", template_name),
                "description": getattr(metadata, "description", "No description"),
            }

    if not names:
        if RICH_AVAILABLE:
            console.print("[yellow]No templates are installed.[/yellow]")
        else:
            cprint("No templates are installed.")
        return 0

    if RICH_AVAILABLE:
        from rich.table import Table

        table = Table(
            title="Available templates",
            border_style="cyan",
            show_header=True,
            header_style="bold cyan",
        )
        table.add_column("Name", style="bold")
        table.add_column("Description", style="dim")
        for name in sorted(names):
            tmpl_info = get_template_info(name) or {}
            desc = tmpl_info.get("description") or "—"
            table.add_row(name, desc)
        console.print(table)
        console.print(
            "\n[dim]Use one with:[/dim] [cyan]fluid init my-project --template <name>[/cyan]\n"
        )
    else:
        cprint("Available templates:")
        for name in sorted(names):
            tmpl_info = get_template_info(name) or {}
            cprint(f"  {name:<24}  {tmpl_info.get('description', '')}")
        cprint("\nUse one with: fluid init my-project --template <name>")
    return 0


def register(subparsers: argparse._SubParsersAction):
    """Register the init command"""
    p = subparsers.add_parser(
        COMMAND,
        help="🚀 Create new FLUID project (smart setup)",
        description="Universal project initialization with smart routing to the right experience",
    )

    # Positional: project name (optional)
    p.add_argument("name", nargs="?", help="Project name (default: auto-generated)")

    # Mode selection (mutually exclusive)
    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--quickstart",
        action="store_true",
        help="⭐ Create working example with sample data (recommended, 2 min)",
    )
    mode_group.add_argument(
        "--blank", action="store_true", help="🔧 Empty project skeleton (power users)"
    )
    mode_group.add_argument(
        "--template",
        metavar="NAME",
        help="📦 Create from specific template (e.g., customer-360, ml-features)",
    )
    mode_group.add_argument(
        "--list-templates",
        action="store_true",
        help="List available templates and exit",
    )
    # Source-aligned acquisition: introspect a live source URI and emit a
    # deterministic Bronze contract per discovered stream. Mutually
    # exclusive with the other modes — discovery owns the whole project.
    mode_group.add_argument(
        "--discover",
        metavar="URI",
        help=(
            "🔍 Introspect a source (postgres://, mysql://, file://, s3://) "
            "and emit a Bronze acquisition contract per discovered stream"
        ),
    )

    # Provider selection
    p.add_argument(
        "--provider",
        choices=["local", "gcp", "snowflake", "aws", "azure"],
        default="local",
        help="Infrastructure provider (default: local = DuckDB, no cloud needed)",
    )

    # Data Mesh productType ↔ medallion layer (Phase 1)
    p.add_argument(
        "--data-product-type",
        dest="data_product_type",
        help=(
            "Data Mesh productType (SDP/ADP/CDP) or medallion layer "
            "(Bronze/Silver/Gold) for the first product. Carried "
            "through to the init→forge handoff."
        ),
    )
    p.add_argument(
        "--workspace-lock",
        dest="workspace_lock",
        choices=["SDP", "ADP", "CDP"],
        help=(
            "Lock this workspace to a single productType. Future forge "
            "runs default to this type and reject conflicting --data-product-type."
        ),
    )

    # Use case / persona (advanced — hidden from default --help)
    from fluid_build.cli.help_advanced import mark_advanced

    mark_advanced(
        p.add_argument(
            "--use-case",
            choices=["data-product", "ai-agent", "analytics", "api"],
            help="Use case configuration (adds opinionated defaults)",
        )
    )

    # Control options — the basics stay visible
    p.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompts")
    p.add_argument(
        "--dry-run", action="store_true", help="Preview what would be created without doing it"
    )
    p.add_argument(
        "--dir",
        "-C",
        dest="target_dir",
        help="Directory to initialize (default: current directory)",
    )
    p.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress the next-steps panel and other post-success hints",
    )
    p.add_argument(
        "--show-work",
        action="store_true",
        help=(
            "Stream the agent's reasoning and tool calls live during the "
            "init→forge handoff. Reasoning + transcript also persist to "
            ".fluid/agents/<run-id>/."
        ),
    )
    p.add_argument(
        "--agent",
        metavar="NAME",
        help="Scaffold a custom domain agent spec in .fluid/agents/ (e.g., --agent insurance)",
    )
    # Advanced: post-run control knobs — hidden unless --advanced is passed
    mark_advanced(
        p.add_argument(
            "--no-run",
            action="store_true",
            help="Don't auto-execute pipeline after creation",
        )
    )
    mark_advanced(
        p.add_argument(
            "--no-dag",
            action="store_true",
            help=("Don't auto-generate Airflow DAG (even if contract has orchestration config)"),
        )
    )

    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """Main entry point — routes to appropriate handler."""

    try:
        # Handle --list-templates early — no workspace, no mode detection.
        if getattr(args, "list_templates", False):
            return _print_templates_list()

        # ── --discover <uri> ─────────────────────────────────────────────
        # Source introspection mode: connect to the URI, enumerate streams,
        # emit one acquisition contract per stream. Owns the whole project
        # (mutually exclusive with --template / --quickstart / --blank).
        discover_uri = getattr(args, "discover", None)
        if discover_uri:
            from fluid_build.cli._init_discover_handler import run_discover

            return run_discover(args, logger, uri=discover_uri)

        # Handle --agent: scaffold a custom domain agent spec.
        agent_name = getattr(args, "agent", None)
        if agent_name:
            from fluid_build.cli.forge_agent_specs import scaffold_user_agent

            target = getattr(args, "target_dir", None)
            target_path = Path(target).resolve() if target else None
            path = scaffold_user_agent(agent_name, target_dir=target_path)
            success(f"Created {path}")
            cprint("Edit the file to customize questions, rules, and suggestions.")
            cprint(f"Then run: fluid forge --domain {agent_name}")

        # If --dir is specified, switch to that directory first.
        target_dir = getattr(args, "target_dir", None)
        if target_dir:
            target_path = Path(target_dir).resolve()
            target_path.mkdir(parents=True, exist_ok=True)
            os.chdir(target_path)
            logger.debug("Changed working directory to %s", target_path)

        # Determine mode (auto-detect if not specified).
        mode = detect_mode(args, logger)

        if mode is None:
            return 1  # Error already displayed or user redirected.

        # A blank init dry-run must stay fully inert: no workspace config,
        # no receipts, no ~/.fluid marker. Let the handler render its preview
        # and return directly before any write-side effects occur.
        if mode == "blank" and getattr(args, "dry_run", False):
            return blank_mode(args, logger)

        # Snapshot state before any handler runs so we can build a receipt
        # of what this run wrote.  See fluid_build.cli.artifact_scan.
        scan_root = Path.cwd()
        before_snapshot = snapshot_workspace(scan_root)

        # Ensure workspace structure exists for all modes.
        _ensure_workspace(args, logger)

        # Industry picker — only on first interactive init (no existing skills file).
        # Skip when --yes, --template, --quickstart, --scan, or --blank is set.
        ws_root = find_workspace_root(Path.cwd()) or Path.cwd()
        skills_path = ws_root / ".fluid" / "skills.yaml"
        is_non_interactive = (
            getattr(args, "yes", False)
            or getattr(args, "template", None)
            or getattr(args, "quickstart", False)
            or getattr(args, "scan", False)
            or getattr(args, "blank", False)
        )
        if not skills_path.exists() and not is_non_interactive:
            _ask_industry(ws_root)

        # Route to appropriate handler.
        handlers = {
            "ai": _ai_mode,
            "blank": blank_mode,
            "template": template_mode,
        }
        handler = handlers.get(mode)
        if handler is None:
            error(logger, "unknown_mode", mode=mode)
            return 1

        result = handler(args, logger)
        if result == 0:
            _mark_first_run_complete()
            _write_init_receipt(
                flow=mode,
                args=args,
                before_snapshot=before_snapshot,
                scan_root=scan_root,
                logger=logger,
            )
            # Slice UX-C: point the user at their second command.
            print_next_steps(
                "init",
                console=console if RICH_AVAILABLE else None,
                args=args,
            )

            # Offer to forge the first data product
            if not getattr(args, "non_interactive", False):
                _offer_first_forge(args, logger)

        return result

    except KeyboardInterrupt:
        if RICH_AVAILABLE:
            console.print("\n[yellow]⚠️  Operation cancelled by user[/yellow]")
        else:
            cprint("\n⚠️  Operation cancelled by user")
        return 130
    except Exception as e:
        # Typed user errors carry rich five-field rendering — let them
        # bubble to the top-level main() handler so the user gets the
        # Panel + structured exit, not a flat "Init failed: …" line.
        from fluid_build.cli._errors import FluidUserError as _FUE

        if isinstance(e, _FUE):
            raise
        error(logger, "init_failed", error=str(e))
        if RICH_AVAILABLE:
            console.print(f"[red]❌ Init failed: {e}[/red]")
        else:
            console_error(f"Init failed: {e}")
        return 1


def _is_ai_configured() -> bool:
    """Return True if a saved AI config (or environment fallback) is usable.

    Mirrors the resolution order in :func:`ai_setup.run_ai_setup_inline`:
    saved config → cloud provider env vars (including the
    ``GEMINI_API_KEY`` alias for ``GOOGLE_API_KEY``) → ``OLLAMA_HOST``.
    Used to decide whether the post-init handoff should offer to set AI
    up before launching forge.
    """
    try:
        from .ai_setup import PROVIDER_ENV_VARS, _load_ai_config
    except Exception:  # pragma: no cover — import guard for partial installs
        return False

    saved = _load_ai_config()
    if saved and saved.get("provider"):
        return True

    for env_var in PROVIDER_ENV_VARS.values():
        if os.environ.get(env_var):
            return True

    # ``GEMINI_API_KEY`` is the user-facing alias for ``GOOGLE_API_KEY``
    # accepted by the copilot provider resolver — keep the post-init
    # handoff in sync so a user who pastes that env var doesn't get
    # asked to "set up AI" again.
    if os.environ.get("GEMINI_API_KEY"):
        return True

    return bool(os.environ.get("OLLAMA_HOST"))


def _offer_first_forge(args, logger: logging.Logger) -> None:
    """After successful init, offer the AI-setup → forge handoff.

    Card 69d4c9bf — "Forge UX: Init → Forge handoff". Three states:

    * Contract already exists in target dir → skip (forge already ran)
    * AI config missing → "Set up AI and create your first data product?"
      runs ``fluid ai setup`` inline, then ``fluid forge``
    * AI config present → "Ready to create your first data product?"
      jumps straight to ``fluid forge``

    The two-step path matches the card's explicit ask: surface AI setup
    as part of the post-init prompt rather than burying it inside forge's
    inline-fallback path.
    """
    try:
        # Determine the product directory
        target = getattr(args, "target_dir", None) or getattr(args, "name", ".")
        target_path = Path(target).resolve()
        contract_path = target_path / "contract.fluid.yaml"

        # If a contract already exists, forge has already run — skip the offer
        if contract_path.exists():
            return

        ai_ready = _is_ai_configured()
        if ai_ready:
            prompt_text = "Ready to create your first data product?"
            prompt_rich = (
                "\n[bold bright_cyan]Ready to create your first data product?[/bold bright_cyan]"
            )
        else:
            prompt_text = "Set up AI and create your first data product?"
            prompt_rich = (
                "\n[bold bright_cyan]Set up AI and create your first data "
                "product?[/bold bright_cyan]\n"
                "[dim]Configures your LLM provider, then launches forge "
                "to scaffold the contract.[/dim]"
            )

        if RICH_AVAILABLE:
            from rich.prompt import Confirm

            proceed = Confirm.ask(prompt_rich, default=True)
        else:
            answer = input(f"\n{prompt_text} [Y/n] ").strip().lower()
            proceed = answer in ("", "y", "yes")

        if not proceed:
            return

        # If AI isn't configured, run setup before forge so the user picks
        # provider/model up front and forge skips its inline fallback.
        if not ai_ready and RICH_AVAILABLE:
            try:
                from .ai_setup import run_ai_setup_interactive

                setup_result = run_ai_setup_interactive(console)
                if setup_result is None:
                    # User skipped the picker — let forge fall back to
                    # template/blank rather than crash.
                    cprint(
                        "\nAI setup was skipped — continuing with forge in "
                        "non-AI mode. Run 'fluid ai setup' anytime to enable AI."
                    )
            except Exception as setup_exc:  # pragma: no cover — defensive
                logger.debug("Inline AI setup failed: %s", setup_exc)
                cprint(
                    "\nAI setup didn't complete — continuing with forge. "
                    "Run 'fluid ai setup' to retry."
                )

        cprint("")
        # Build args for forge — carry through every flag the init user
        # already passed so we don't ask twice (invariant **I3**).
        import argparse as _argparse

        from .forge import run as forge_run

        forge_args = _argparse.Namespace(
            target_dir=getattr(args, "target_dir", None) or getattr(args, "name", "."),
            provider=getattr(args, "provider", None),
            domain=getattr(args, "domain", None),
            blank=False,
            dry_run=False,
            non_interactive=bool(getattr(args, "non_interactive", False)),
            context=getattr(args, "context", None),
            # LLM flags — carry through so the user picks provider/model
            # ONCE (in init or via env var) and forge inherits the choice.
            llm_provider=getattr(args, "llm_provider", None),
            llm_model=getattr(args, "llm_model", None),
            llm_endpoint=getattr(args, "llm_endpoint", None),
            llm_routing_model=getattr(args, "llm_routing_model", None),
            llm_routing_endpoint=getattr(args, "llm_routing_endpoint", None),
            tiered=bool(getattr(args, "tiered", False)),
            require_llm=bool(getattr(args, "require_llm", False)),
            # Discovery — init already scanned; let forge re-use the result.
            discover=True,
            no_discover=False,
            discovery_path=getattr(args, "discovery_path", None),
            # Memory / output
            memory=True,
            no_memory=False,
            save_memory=True,
            show_memory=False,
            reset_memory=False,
            quiet=getattr(args, "quiet", False),
            verbose=getattr(args, "verbose", False),
            # Pre-write preview UX (Phase 0): plumbed through so the
            # init handoff respects --yes / --show-work.
            yes=bool(getattr(args, "yes", False)),
            show_work=bool(getattr(args, "show_work", False)),
            no_llm=bool(getattr(args, "no_llm", False)),
            no_cache=bool(getattr(args, "no_cache", False)),
            deterministic=bool(getattr(args, "deterministic", False)),
            # Phase 1 — type-aware authoring
            data_product_type=getattr(args, "data_product_type", None),
            transform_engine=None,
        )
        forge_run(forge_args, logger)
    except (KeyboardInterrupt, EOFError):
        cprint("\nYou can run 'fluid forge' anytime to create a data product.")
    except Exception as e:
        logger.debug(f"First-forge offer failed: {e}")
        cprint("\nYou can run 'fluid forge' anytime to create a data product.")


def _write_init_receipt(
    *,
    flow: str,
    args,
    before_snapshot,
    scan_root: Path,
    logger: logging.Logger,
) -> None:
    """Write ``.fluid/init-receipt.json`` describing what this run wrote.

    Never raises — receipt writing is a best-effort post-success side
    effect.  If the workspace root can't be located or the write fails,
    the command still reports success to the user.
    """
    try:
        ws_root = find_workspace_root(Path.cwd()) or scan_root
        after_snapshot = snapshot_workspace(ws_root)
        entries = diff_snapshots(before_snapshot, after_snapshot)

        # Drop no-op entries — the receipt is about what this run changed.
        entries = [e for e in entries if e.action != "unchanged"]
        if not entries:
            return  # Nothing to record; skip the write entirely.

        builder = ReceiptBuilder(flow=flow, dry_run=False)
        for entry in entries:
            builder.record_entry(
                path=Path(entry.path),
                action=entry.action,
                sha256=entry.sha256,
                size=entry.size,
                reason=entry.reason,
            )

        builder.set_inputs(
            template=getattr(args, "template", None),
            provider=getattr(args, "provider", None),
            use_case=getattr(args, "use_case", None),
            quickstart=bool(getattr(args, "quickstart", False)) or None,
            blank=bool(getattr(args, "blank", False)) or None,
        )

        doc = builder.build_document()

        try:
            from fluid_build import __version__ as tool_version
        except Exception:  # pragma: no cover — defensive
            tool_version = ""

        command = _format_init_command(args, flow)
        payload_bytes = dump_json_with_envelope(
            doc.to_payload(),
            kind="InitReceipt",
            command=command,
            tool_version=str(tool_version),
        )

        receipt_path = workspace_init_receipt_path(ws_root)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(payload_bytes, encoding="utf-8")
        logger.debug("init_receipt_written", extra={"path": str(receipt_path)})
    except Exception as exc:  # noqa: BLE001 — receipt write must never abort init
        logger.debug("init_receipt_write_failed", extra={"error": str(exc)})


def _format_init_command(args, flow: str) -> str:
    """Build a short human-readable command string for the receipt envelope.

    The command string goes into ``generated_by.command`` so the receipt
    carries enough context to tell two runs apart.  Secrets are never
    included — only the mode flags and explicit knobs.
    """
    parts = ["fluid init"]
    if getattr(args, "name", None):
        parts.append(str(args.name))
    if getattr(args, "blank", False):
        parts.append("--blank")
    elif getattr(args, "quickstart", False):
        parts.append("--quickstart")
    elif getattr(args, "template", None):
        parts.append(f"--template {args.template}")
    elif flow == "ai":
        # No explicit flag — AI is the interactive default.
        pass
    if getattr(args, "provider", None) and args.provider != "local":
        parts.append(f"--provider {args.provider}")
    if getattr(args, "yes", False):
        parts.append("--yes")
    return " ".join(parts)


def _ensure_workspace(args, logger: logging.Logger) -> None:
    """Create ``fluid.workspace.yaml`` if it doesn't exist yet."""
    cwd = Path.cwd()
    ws_root = find_workspace_root(cwd)
    if ws_root is not None:
        return  # Workspace already exists.

    # Determine workspace name: CLI arg → interactive prompt → directory name.
    ws_name = getattr(args, "name", None)
    is_non_interactive = (
        getattr(args, "yes", False)
        or getattr(args, "template", None)
        or getattr(args, "quickstart", False)
        or getattr(args, "scan", False)
        or getattr(args, "blank", False)
    )
    if not ws_name and RICH_AVAILABLE and not is_non_interactive:
        ws_name = Prompt.ask("Workspace name", default=cwd.name)
    ws_name = ws_name or cwd.name

    provider = getattr(args, "provider", None) or "local"

    save_workspace_config(
        cwd,
        name=ws_name,
        provider=provider,
        data_product_type_lock=getattr(args, "workspace_lock", "") or "",
    )

    # Slice 9: drop a .gitignore template alongside the workspace config
    # so the engineer-personal state files (init-receipt.json,
    # forge-receipt.json, copilot-memory.json, logs/) stay out of git
    # while the team-shared state (.fluid/skills.yaml,
    # .fluid/ci-state.json) remains committed.  See the git-policy
    # matrix in the redesign plan.
    _ensure_gitignore_template(cwd)

    # Scaffold team memory template so the team can share conventions.
    try:
        from fluid_build.cli.forge_team_memory import scaffold_team_memory

        tm_path = scaffold_team_memory(cwd)
        logger.debug("Scaffolded team memory at %s", tm_path)
    except Exception:  # noqa: BLE001 — best-effort
        pass

    if RICH_AVAILABLE:
        console.print(
            f"[dim]Created {WORKSPACE_FILENAME} — workspace [bold]{ws_name}[/bold][/dim]\n"
        )


# The header + rule block appended to .gitignore when missing.  Stored as
# a module-level constant so tests can import and assert exact contents.
FLUID_GITIGNORE_BLOCK = """\
# --- fluid-cli: engineer-personal state (never commit) -------------------
# Receipts, logs, and per-engineer learning history live under .fluid/
# but must not travel to other clones.  See
# https://fluid-build.dev/docs/git-policy for the full matrix.
.fluid/init-receipt.json
.fluid/forge-receipt.json
.fluid/copilot-memory.json
.fluid/logs/

# Build outputs (never commit)
runtime/

# Everything else under .fluid/ STAYS committed:
#   .fluid/skills.yaml      — industry reference pack (team-shared)
#   .fluid/ci-state.json    — records inputs that produced committed CI files
# --- end fluid-cli -------------------------------------------------------
"""

#: Sentinel string looked for to decide whether the block already exists.
_GITIGNORE_SENTINEL = "# --- fluid-cli: engineer-personal state"


def _ensure_gitignore_template(workspace_root: Path) -> None:
    """Write or extend ``.gitignore`` to gitignore fluid-cli state files.

    Behavior:

    * No ``.gitignore`` → create one with the fluid block.
    * ``.gitignore`` exists but does not contain the sentinel → append
      the block (one blank line separator).
    * ``.gitignore`` already has the sentinel → do nothing (idempotent).

    Never raises — the call is a side effect of workspace creation and
    a filesystem error never aborts init.
    """
    try:
        gitignore = workspace_root / ".gitignore"
        if gitignore.exists():
            existing = gitignore.read_text(encoding="utf-8")
            if _GITIGNORE_SENTINEL in existing:
                return
            needs_newline = not existing.endswith("\n")
            appended = existing + ("\n" if needs_newline else "") + "\n" + FLUID_GITIGNORE_BLOCK
            gitignore.write_text(appended, encoding="utf-8")
        else:
            gitignore.write_text(FLUID_GITIGNORE_BLOCK, encoding="utf-8")
    except OSError:
        pass  # Best-effort — gitignore is a nice-to-have, not load-bearing.


def _ai_mode(args, logger: logging.Logger) -> int:
    """Let AI design the data product — delegates to forge copilot inline."""
    if RICH_AVAILABLE:
        console.print(
            Panel(
                "🤖 [bold]AI Copilot[/bold]\n\n"
                "I'll ask about your data and goals, then generate\n"
                "a production-ready contract tailored to your needs.",
                title="AI-Assisted Setup",
                border_style="blue",
            )
        )

    try:
        from .ai_setup import run_ai_setup_inline, set_session_env
        from .forge import run_ai_copilot_mode

        # Run inline LLM setup — same as forge.py:run() does.
        _console = console if RICH_AVAILABLE else None
        llm_config = run_ai_setup_inline(_console)
        if llm_config:
            args.llm_provider = llm_config.provider
            args.llm_model = llm_config.model
            args.llm_endpoint = llm_config.endpoint
            if llm_config.api_key:
                set_session_env(llm_config.provider, llm_config.api_key)
        else:
            if RICH_AVAILABLE:
                from .forge_dialogs import print_dialog_status

                print_dialog_status(
                    console,
                    status="info",
                    message="No AI provider configured — using template mode instead.",
                    detail="Run 'fluid ai setup' anytime to enable AI-assisted generation.",
                )
            args.template = "customer-360"
            return template_mode(args, logger)

        # Determine target directory for the product.
        product_name = args.name
        if not product_name and RICH_AVAILABLE:
            product_name = Prompt.ask("Product name", default="my-data-product")
        product_name = product_name or "my-data-product"

        ws_root = find_workspace_root(Path.cwd())
        if ws_root:
            ws_config = load_workspace_config(ws_root)
            products_dir = (ws_root / ws_config.products_dir).resolve()
            # Guard against path traversal in products_dir from workspace YAML.
            try:
                products_dir.relative_to(ws_root.resolve())
            except ValueError:
                products_dir = ws_root
        else:
            products_dir = Path.cwd()

        target = products_dir / slugify_identifier(product_name)
        target.mkdir(parents=True, exist_ok=True)

        # Inject target dir so the forge wrapper writes there.
        args.target_dir = str(target)
        if not hasattr(args, "non_interactive"):
            args.non_interactive = bool(getattr(args, "yes", False))

        result = run_ai_copilot_mode(args, logger)

        if result == 0:
            _show_init_success(target, ws_root or Path.cwd())
        return result

    except ImportError:
        if RICH_AVAILABLE:
            console.print(
                "[yellow]AI copilot not available. Falling back to template mode.[/yellow]"
            )
        args.template = "customer-360"
        return template_mode(args, logger)


def _show_init_success(product_dir: Path, workspace_root: Path) -> None:
    """Show the success panel with structure and next steps."""
    if not RICH_AVAILABLE:
        success(f"Created {product_dir}")
        return

    try:
        rel = product_dir.relative_to(workspace_root)
    except ValueError:
        rel = product_dir.name
    lines = [
        f"[bold green]✅ Project created: {workspace_root.name}/[/bold green]",
        "",
        f"  {WORKSPACE_FILENAME}    [dim]← project config (team, domain, provider)[/dim]",
        f"  {rel}/",
        "  └── contract.fluid.yaml",
        "",
        "[bold]What's next?[/bold]",
        f"  [cyan]cd {workspace_root.name}[/cyan]",
        "  [cyan]fluid validate[/cyan]          [dim]← check your contract[/dim]",
        "  [cyan]fluid forge[/cyan]             [dim]← add another data product[/dim]",
        "  [cyan]fluid doctor[/cyan]            [dim]← check your environment[/dim]",
    ]
    console.print(Panel("\n".join(lines), border_style="green"))


def detect_mode(args, logger: logging.Logger) -> Optional[str]:
    """Smart detection of best mode based on context."""

    # Explicit mode flags take precedence.
    if args.quickstart:
        # --quickstart is now an alias for --template customer-360 --yes
        args.template = args.template or "customer-360"
        args.yes = True
        return "template"
    if args.blank:
        return "blank"
    if args.template:
        return "template"
    if getattr(args, "yes", False):
        return "ai"

    cwd = Path.cwd()
    is_first_time = not (Path.home() / ".fluid").exists()

    # --- Inside an existing workspace? Redirect instead of blocking. ---
    ws_root = find_workspace_root(cwd)
    if ws_root is not None:
        existing = discover_workspace_products(ws_root)
        if existing:
            return _redirect_existing_workspace(existing, ws_root, is_first_time)

    def _resolve_menu_choice(mode: str) -> str:
        """Normalize menu return values so they match the CLI-flag code paths.

        The menu's 'Quickstart' label is rewritten to
        ``--template customer-360 --yes`` so it dispatches through
        ``template_mode`` — same as ``fluid init --quickstart``.

        The menu's 'Start from a template' label triggers a second
        prompt that asks *which* template the user wants, defaulting to
        ``customer-360``.  Without that second prompt ``args.template``
        would stay ``None`` and ``template_mode`` / ``copy_template``
        would crash trying to concatenate ``Path / None``.
        """
        if mode == "quickstart":
            args.template = "customer-360"
            args.yes = True
            return "template"
        if mode == "template" and not getattr(args, "template", None):
            args.template = _ask_template_name()
            if not args.template:
                return "template"  # let template_mode handle the empty case
        return mode

    # --- Existing contract at root (legacy single-product project) ---
    if (cwd / "contract.fluid.yaml").exists():
        if RICH_AVAILABLE:
            if is_first_time:
                _print_welcome_panel()
            console.print("[dim]📂 This directory already has a contract.fluid.yaml.[/dim]\n")
            console.print("To add another product:")
            console.print("  [cyan]fluid forge[/cyan]          ← all creation modes\n")
            console.print("To work with the existing contract:")
            console.print("  [cyan]fluid validate[/cyan]")
            console.print("  [cyan]fluid plan[/cyan]")
            console.print("  [cyan]fluid viz --open[/cyan]")
        else:
            cprint("This directory already has a contract. Use 'fluid forge' to add products.")
        if is_first_time:
            _mark_first_run_complete()
        return None

    # --- First-time user (no ~/.fluid directory) ---
    if is_first_time:
        if RICH_AVAILABLE:
            _print_welcome_panel()
        return _resolve_menu_choice(_ask_creation_mode())

    # --- Returning user, empty directory ---
    return _resolve_menu_choice(_ask_creation_mode())


# Interactive UI helpers (welcome panel, template/industry/mode pickers,
# workspace redirect) — physically extracted to
# ``cli/_init_interactive_helpers.py``. ~241 LOC of pure UI code lifted
# without behavior change. Re-exported here under the same names so
# existing test patches on ``fluid_build.cli.init.<helper>`` flow
# through to the moved functions via the module-attribute-access
# indirection pattern documented in CLAUDE.md.
# ============================================================================
# DAG GENERATION HELPERS
# ============================================================================
# ── DAG-generation helpers (extracted) ────────────────────────────
# The Airflow DAG emission utilities live in the
# ``_init_dag_helpers`` sibling module. Re-imported at module top
# so test patches on ``fluid_build.cli.init.<helper>`` still
# resolve via this module's namespace.
from fluid_build.cli._init_dag_helpers import (  # noqa: E402,F401
    create_basic_dag,
    create_dags_readme,
    generate_dag_for_project,
    should_generate_dag,
)
from fluid_build.cli._init_interactive_helpers import (  # noqa: E402,F401
    _ask_creation_mode,
    _ask_industry,
    _ask_template_name,
    _list_filesystem_templates,
    _print_welcome_panel,
    _print_workspace_products,
    _redirect_existing_workspace,
)

# Mode handlers (demo / blank / template) — physically extracted to
# ``cli/_init_modes.py``. ~480 LOC of mode-handler logic lifted with
# the module-attribute-access indirection so test patches on
# ``fluid_build.cli.init.<helper>`` flow through to the moved
# functions.
from fluid_build.cli._init_modes import (  # noqa: E402,F401
    _bridge_init_template_to_forge,
    _finalise_template_product,
    _should_copy_template_entry,
    blank_mode,
    demo_mode,
    template_mode,
)


def copy_template(project_dir: Path, template_name: str, logger: logging.Logger) -> bool:
    """Copy template files to project directory"""

    # Find template directory
    cli_dir = Path(__file__).parent
    templates_dir = cli_dir.parent / "templates" / template_name

    if not templates_dir.exists():
        # Bridge to the forge.core.registry — when a name isn't a
        # directory-template, fall back to a code-template (starter,
        # analytics, etl_pipeline, streaming, ml_pipeline). This unifies
        # ``fluid init --template X`` with ``fluid forge --template X``
        # so the user picks a single name and it works in both flows.
        try:
            if _bridge_init_template_to_forge(project_dir, template_name, logger):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("init_template_forge_bridge_failed: %s", exc)

        # Show every option (directory templates + forge code templates).
        try:
            from fluid_build.forge.core.registry import template_registry as _tr

            forge_names = sorted(_tr.list_available())
        except Exception:  # noqa: BLE001
            forge_names = []
        if RICH_AVAILABLE:
            console.print(f"[yellow]⚠️  Template '{template_name}' not found[/yellow]")
            console.print(f"Looking in: {templates_dir}")
            console.print("\nAvailable templates:")
            console.print("  - customer-360 (customer analytics)")
            for name in forge_names:
                console.print(f"  - {name} (code template)")
        else:
            warning(f"Template '{template_name}' not found")
        return False

    try:
        project_dir.mkdir(parents=True, exist_ok=True)

        # Copy all files from template, skipping template-author scratch
        # artifacts (``*.old``/``*.bak``/``*.tmp``/``*.swp``) and
        # ``__pycache__``.  Historically the customer-360 template
        # carried a ``contract.fluid.yaml.old`` backup that every new
        # project inherited — see slice UX-F for the fix.
        def _copy_tree_filtered(src: Path, dst: Path) -> None:
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                if not _should_copy_template_entry(item):
                    continue
                target = dst / item.name
                if item.is_file():
                    shutil.copy2(item, target)
                elif item.is_dir():
                    _copy_tree_filtered(item, target)

        _copy_tree_filtered(templates_dir, project_dir)

        if RICH_AVAILABLE:
            console.print(f"✅ Copied template files from {template_name}")
        return True

    except Exception as e:
        error(logger, "template_copy_failed", template=template_name, error=str(e))
        if RICH_AVAILABLE:
            console.print(f"[red]❌ Failed to copy template: {e}[/red]")
        return False


def copy_sample_data(project_dir: Path, template_name: str, logger: logging.Logger):
    """Copy sample CSV data (already handled by copy_template, but can enhance)"""

    data_dir = project_dir / "data"
    if data_dir.exists():
        csv_files = list(data_dir.glob("*.csv"))
        if csv_files and RICH_AVAILABLE:
            console.print(f"✅ Sample data loaded: {len(csv_files)} CSV files")


def init_local_db(project_dir: Path, provider: str, logger: logging.Logger):
    """Initialize DuckDB database"""

    if provider != "local":
        return  # Only for local provider

    try:
        import duckdb

        db_dir = project_dir / ".fluid"
        db_dir.mkdir(exist_ok=True)

        db_path = db_dir / "db.duckdb"
        conn = duckdb.connect(str(db_path))
        conn.close()

        if RICH_AVAILABLE:
            console.print("✅ Local database initialized (DuckDB)")

    except ImportError:
        if RICH_AVAILABLE:
            console.print("[yellow]⚠️  DuckDB not installed (pip install duckdb)[/yellow]")
    except Exception as e:
        error(logger, "db_init_failed", error=str(e))


def run_local_pipeline(project_dir: Path, logger: logging.Logger):
    """Execute pipeline with local provider"""

    if not RICH_AVAILABLE:
        return

    console.print("\n🚀 [bold]Running pipeline locally...[/bold]\n")

    contract_path = project_dir / "contract.fluid.yaml"

    try:
        # Try to use existing apply command
        from .apply import run as apply_run

        class ApplyArgs:
            def __init__(self):
                self.contract = str(contract_path)
                self.provider = "local"
                self.env = None
                self.project = None
                self.region = None
                self.yes = True
                self.dry_run = False

        result = apply_run(ApplyArgs(), logger)

        if result == 0:
            console.print("\n✅ [green bold]Pipeline executed successfully![/green bold]")

    except Exception as e:
        console.print(f"[yellow]⚠️  Could not auto-run pipeline: {e}[/yellow]")
        console.print("You can run it manually:")
        console.print(f"  [cyan]$ cd {project_dir.name}[/cyan]")
        console.print("  [cyan]$ fluid apply contract.fluid.yaml --provider local[/cyan]")


def show_success_message(
    project_dir: Path, provider: str, logger: logging.Logger, has_dag: bool = False
):
    """Show next steps after successful init"""

    # Mark first-run complete so subsequent `fluid` invocations show full help
    _mark_first_run_complete()

    if not RICH_AVAILABLE:
        cprint("\n✅ Your data product is ready!")
        if has_dag:
            cprint("\n📅 Airflow DAG created in dags/ folder")
        cprint("\nNext steps:")
        cprint(f"  $ cd {project_dir.name}")
        cprint("  $ fluid validate contract.fluid.yaml")
        return

    console.print()
    console.print(
        Panel(
            f"[bold green]Your data product is ready![/bold green]\n\n"
            f"Project: [bold cyan]{project_dir.name}/[/bold cyan]",
            title="[bold bright_white]🎉 Success[/bold bright_white]",
            title_align="left",
            border_style="green",
            padding=(1, 2),
        )
    )

    # Show results for local provider
    if provider == "local":
        output_dir = project_dir / "output"
        if output_dir.exists():
            csv_files = list(output_dir.glob("*.csv"))
            if csv_files:
                console.print("\n[bold]Results:[/bold]")
                for csv_file in csv_files:
                    console.print(f"  📊 {csv_file.name}: {csv_file.relative_to(project_dir)}")

        db_file = project_dir / ".fluid" / "db.duckdb"
        if db_file.exists():
            console.print("  💾 Local database: .fluid/db.duckdb")

    # Show DAG info
    if has_dag:
        dag_dir = project_dir / "dags"
        if dag_dir.exists():
            dag_files = list(dag_dir.glob("*_dag.py"))
            console.print("\n[bold]Orchestration:[/bold]")
            for dag_file in dag_files:
                console.print(f"  📅 Airflow DAG: dags/{dag_file.name}")

    # Concise numbered next-steps — always 3
    console.print()
    console.print("[bold bright_white]Next steps:[/bold bright_white]\n")
    console.print(f"  [bold yellow]1.[/bold yellow]  [cyan]cd {project_dir.name}[/cyan]")
    console.print(
        "  [bold yellow]2.[/bold yellow]  [cyan]fluid validate contract.fluid.yaml[/cyan]   [dim]# check the contract[/dim]"
    )

    if provider == "local":
        console.print(
            "  [bold yellow]3.[/bold yellow]  [cyan]fluid apply contract.fluid.yaml --yes[/cyan]  [dim]# run the pipeline[/dim]"
        )
    else:
        console.print(
            f"  [bold yellow]3.[/bold yellow]  [cyan]fluid plan contract.fluid.yaml --provider {provider}[/cyan]"
        )

    console.print()
    console.print("[dim]Run [bright_cyan]fluid --help[/bright_cyan] for all commands.[/dim]\n")
