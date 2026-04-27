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
            "\n[dim]Use one with:[/dim] " "[cyan]fluid init my-project --template <name>[/cyan]\n"
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

    # Provider selection
    p.add_argument(
        "--provider",
        choices=["local", "gcp", "snowflake", "aws", "azure"],
        default="local",
        help="Infrastructure provider (default: local = DuckDB, no cloud needed)",
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
            help=("Don't auto-generate Airflow DAG " "(even if contract has orchestration config)"),
        )
    )

    p.set_defaults(cmd=COMMAND, func=run)


def run(args, logger: logging.Logger) -> int:
    """Main entry point — routes to appropriate handler."""

    try:
        # Handle --list-templates early — no workspace, no mode detection.
        if getattr(args, "list_templates", False):
            return _print_templates_list()

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
                "\n[bold bright_cyan]Ready to create your first data product?" "[/bold bright_cyan]"
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
        # Build minimal args for forge
        import argparse as _argparse

        from .forge import run as forge_run

        forge_args = _argparse.Namespace(
            target_dir=getattr(args, "target_dir", None) or getattr(args, "name", "."),
            provider=getattr(args, "provider", None),
            domain=None,
            blank=False,
            dry_run=False,
            non_interactive=False,
            context=None,
            llm_provider=None,
            llm_model=None,
            llm_endpoint=None,
            discover=True,
            no_discover=False,
            discovery_path=None,
            memory=True,
            no_memory=False,
            save_memory=True,
            show_memory=False,
            reset_memory=False,
            quiet=False,
            verbose=False,
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


def _print_welcome_panel() -> None:
    """Show the compact welcome panel for first-time users."""
    if not RICH_AVAILABLE:
        return
    console.print(
        Panel(
            "[bold]FLUID[/bold] turns a YAML contract into a deployed, governed data "
            "product —\nlike [cyan]terraform plan/apply[/cyan], but for tables, views, "
            "and files.\n\n"
            "[dim]Tip: in a hurry? [bold]fluid init my-project --quickstart[/bold] "
            "ships a working\ncustomer-360 example in ~30 seconds with zero "
            "questions.[/dim]\n\n"
            "Let's set up your first project.\n\n"
            "[dim]Advanced: [bold]fluid init --help[/bold] for cloud providers "
            "(gcp/snowflake/aws).\n"
            "Migrating from dbt/Terraform? See [bold]fluid import[/bold].[/dim]",
            title="Welcome to FLUID",
            border_style="blue",
        )
    )
    console.print()


def _list_filesystem_templates() -> List[str]:
    """Return the list of template names that ``copy_template`` can
    actually find on disk.

    The registry (``simple_forge.list_templates``) returns logical
    template names (``starter``, ``analytics``, ``etl_pipeline``…)
    that do NOT correspond 1:1 to filesystem directories under
    ``fluid_build/templates/``.  If the interactive menu offers those
    logical names, ``copy_template`` crashes with "Template 'starter'
    not found".  This helper walks the filesystem directly so the
    picker only ever offers names that exist.
    """
    templates_dir = Path(__file__).parent.parent / "templates"
    try:
        return sorted(
            p.name
            for p in templates_dir.iterdir()
            if p.is_dir() and not p.name.startswith(".") and p.name != "__pycache__"
        )
    except (OSError, FileNotFoundError):
        return []


def _ask_template_name() -> Optional[str]:
    """Prompt the user to pick a template when 'Start from a template' is chosen.

    Lists the filesystem templates under ``fluid_build/templates/``
    (the ones ``copy_template`` can actually copy) and prompts for a
    choice.  Defaults to ``customer-360`` when it exists, otherwise to
    the first alphabetically.  Falls back to the default when Rich is
    unavailable or the prompt is cancelled — the caller never receives
    ``None`` so ``template_mode`` cannot crash on an empty
    ``args.template``.
    """
    default_name = "customer-360"

    names = _list_filesystem_templates()
    if not names:
        # No templates on disk — absolute fallback so at least the
        # default path is wired.  template_mode will still fail
        # cleanly if the default isn't present, but the caller won't
        # see the Path/None TypeError.
        return default_name

    if default_name not in names:
        # Default is missing from disk — pick the first alphabetical
        # template as the fallback default so the picker stays usable.
        default_name = names[0]

    if not RICH_AVAILABLE:
        return default_name

    console.print()
    console.print("[dim]Available templates:[/dim]")
    for i, name in enumerate(names, 1):
        marker = " [dim](default)[/dim]" if name == default_name else ""
        console.print(f"  [bold]{i}.[/bold] {name}{marker}")
    console.print()

    valid_indices = [str(i) for i in range(1, len(names) + 1)]
    default_index = str(names.index(default_name) + 1)
    try:
        choice = Prompt.ask("Choose template", choices=valid_indices, default=default_index)
    except Exception:  # noqa: BLE001 — never crash the init flow over a prompt
        return default_name

    try:
        return names[int(choice) - 1]
    except (ValueError, IndexError):
        return default_name


def _ask_industry(workspace_root: Path) -> Optional[str]:
    """Present the industry picker and generate ``.fluid/skills.yaml``.

    Returns the selected industry key (e.g. ``"telco"``) or ``None`` if
    Rich is unavailable.
    """
    from .industry_skills import generate_skills_file, list_industries

    industries = list_industries()

    if not RICH_AVAILABLE:
        # Non-interactive: skip industry picker, generate tools-only skills.
        generate_skills_file(None, workspace_root)
        return None

    console.print("[dim]What industry is this project for?[/dim]\n")
    for i, ind in enumerate(industries, 1):
        desc = f"  [dim]({ind['description']})[/dim]" if ind["description"] else ""
        console.print(f"  [bold]{i}.[/bold] {ind['label']}{desc}")
    console.print()

    valid = [str(i) for i in range(1, len(industries) + 1)]
    choice = Prompt.ask("Choose", choices=valid, default=str(len(industries)))
    selected = industries[int(choice) - 1]

    industry_key = selected["key"]
    out_path = generate_skills_file(industry_key, workspace_root)

    if industry_key == "other":
        console.print(
            '\n[yellow]No industry-specific skills shipped for "Other".[/yellow]\n'
            "[dim]Agents will work without domain-specific guidance.\n"
            "You can add industry skills later with:[/dim] "
            "[cyan]fluid skills update[/cyan]\n"
        )
    else:
        console.print(
            Panel(
                f"[bold]Generated .fluid/skills.yaml for {selected['label']}[/bold]\n\n"
                "This file contains industry-specific knowledge that\n"
                "all FLUID agents will use:\n"
                f"  [dim]Industry:[/dim]    {selected['label']}\n"
                f"  [dim]File:[/dim]        {out_path.relative_to(workspace_root)}\n\n"
                "Keep this file in version control — your whole\n"
                "team will benefit from shared project context.",
                border_style="green",
            )
        )

    return industry_key


def _ask_creation_mode() -> str:
    """Present the creation menu and return the selected mode."""
    if not RICH_AVAILABLE:
        return "quickstart"  # non-Rich fallback

    console.print(
        Panel(
            "A [bold]workspace[/bold] is a home for your data products.\n"
            "Each product gets its own folder and contract.\n"
            "You can add more products later with [cyan]fluid forge[/cyan].",
            title="New Project",
            border_style="blue",
        )
    )
    console.print("[dim]How would you like to create your first data product?[/dim]\n")
    console.print(
        "  [bold]1.[/bold] Quickstart                 [dim](customer-360 example, zero questions, ~30s) ← fastest[/dim]"
    )
    console.print(
        "  [bold]2.[/bold] Let AI design it           [dim](recommended — just answer questions)[/dim]"
    )
    console.print(
        "  [bold]3.[/bold] Start from a template     [dim](pre-built, customize later)[/dim]"
    )
    console.print(
        "  [bold]4.[/bold] Empty contract             [dim](for experienced users)[/dim]\n"
    )

    choice = Prompt.ask(
        "Choose",
        choices=["1", "2", "3", "4"],
        default="2",
    )
    return {"1": "quickstart", "2": "ai", "3": "template", "4": "blank"}.get(choice, "ai")


def _print_workspace_products(existing: List, ws_name: str) -> None:
    """Print the workspace product listing (shared by redirect paths)."""
    console.print(
        f"[dim]Workspace: [bold]{ws_name}[/bold] ({len(existing)} existing product"
        f"{'s' if len(existing) != 1 else ''})[/dim]"
    )
    for product in existing[:10]:
        meta = []
        if product.expose_count:
            meta.append(f"{product.expose_count} expose{'s' if product.expose_count != 1 else ''}")
        if product.provider:
            meta.append(f"provider: {product.provider}")
        suffix = f"  ({', '.join(meta)})" if meta else ""
        console.print(f"[dim]  • [bold]{product.name}[/bold]{suffix}[/dim]")
        console.print(f"[dim]    {product.path}[/dim]")
    console.print()


def _redirect_existing_workspace(
    existing: List,
    ws_root: Path,
    is_first_time: bool = False,
) -> Optional[str]:
    """Show existing products and redirect the user.

    When *is_first_time* is ``True`` the user has never run FLUID on this
    machine (no ``~/.fluid``), so we show a short welcome explaining what FLUID
    is before the redirect.
    """
    if not RICH_AVAILABLE:
        cprint(f"This is already a FLUID workspace with {len(existing)} product(s).")
        for product in existing[:10]:
            cprint(f"  • {product.name}  ({product.path})")
        cprint("Use 'fluid forge' to add another product.")
        if is_first_time:
            _mark_first_run_complete()
        return None

    ws_config = load_workspace_config(ws_root)
    name = ws_config.name or ws_root.name

    # New colleague who cloned the repo — explain what FLUID is first.
    if is_first_time:
        _print_welcome_panel()
        _print_workspace_products(existing, name)
        console.print("This workspace is already set up. To add a product:")
        console.print("  [cyan]fluid forge[/cyan]\n")
        console.print("To work with existing products:")
        console.print("  [cyan]fluid validate[/cyan]       [dim]← check all contracts[/dim]")
        console.print("  [cyan]fluid plan[/cyan]           [dim]← generate execution plan[/dim]")
        console.print("  [cyan]fluid doctor[/cyan]         [dim]← check your environment[/dim]")
        _mark_first_run_complete()
        return None

    # Returning user — short redirect.
    _print_workspace_products(existing, name)
    console.print("To add another product:  [cyan]fluid forge[/cyan]")
    return None


# ============================================================================
# DAG GENERATION HELPERS
# ============================================================================


def should_generate_dag(contract: dict, template: str = None) -> bool:
    """
    Determine if DAG should be auto-generated for this project.

    Auto-generate DAGs when:
    1. Contract has explicit orchestration config
    2. Template is orchestration-focused (customer-360, sales-analytics, ml-features, data-quality)
    3. Project has multiple provider actions (complex pipeline)
    """
    # Check for explicit orchestration config
    if "orchestration" in contract:
        return True

    # Check for orchestration-focused templates
    orchestrated_templates = ["customer-360", "sales-analytics", "ml-features", "data-quality"]
    if template and template in orchestrated_templates:
        return True

    # Check for complex pipelines (multiple actions)
    binding = contract.get("binding", {})
    provider_actions = binding.get("providerActions", [])
    if len(provider_actions) > 1:
        return True

    return False


def generate_dag_for_project(
    project_dir: Path, contract: dict, logger, console, template: str = None
) -> bool:
    """
    Generate Airflow DAG using existing generate-airflow command.

    Creates dags/ folder with:
    - DAG Python file (contract_name_dag.py)
    - README.md with usage instructions
    """
    try:
        import subprocess

        # Get contract details
        contract_name = contract.get("name", "my_product")
        orchestration = contract.get("orchestration", {})

        # Prepare DAG parameters — sanitize to prevent injection
        schedule = orchestration.get("schedule", "@daily")
        dag_id = contract_name.replace("-", "_").replace(" ", "_")
        # Strict identifier validation: only alphanumeric + underscore
        dag_id = re.sub(r"[^a-zA-Z0-9_]", "", dag_id) or "fluid_dag"
        # Validate schedule is a plausible cron/preset string
        if not re.match(r"^[@a-zA-Z0-9_ */,-]+$", schedule):
            schedule = "@daily"

        # Call generate-airflow command
        dag_dir = project_dir / "dags"
        dag_dir.mkdir(exist_ok=True)

        # Build command
        cmd = [
            "fluid",
            "generate-airflow",
            str(project_dir / "contract.fluid.yaml"),
            "--output-dir",
            str(dag_dir),
            "--dag-id",
            dag_id,
            "--schedule",
            schedule,
        ]

        if RICH_AVAILABLE:
            console.print("\n[cyan]📅 Generating Airflow DAG...[/cyan]")

        # Execute command
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(project_dir))

        if result.returncode != 0:
            # generate-airflow may not exist yet - create DAG manually
            logger.warning("generate-airflow command not available, creating basic DAG template")
            create_basic_dag(project_dir, contract, logger)

        # Create README
        dag_filename = f"{dag_id}_dag.py"
        create_dags_readme(dag_dir, dag_id, schedule, dag_filename)

        if RICH_AVAILABLE:
            console.print(f"[green]✅ DAG created: dags/{dag_filename}[/green]")

        return True

    except Exception as e:
        logger.warning(f"Failed to generate DAG: {e}")
        return False


def create_basic_dag(project_dir: Path, contract: dict, logger):
    """Create a basic DAG template if generate-airflow is not available."""

    import re as _re

    contract_name = contract.get("name", "my_product")
    orchestration = contract.get("orchestration", {})
    # Sanitize values to prevent code injection in generated Python.
    dag_id = _re.sub(r"[^a-zA-Z0-9_]", "_", contract_name)[:128]
    schedule = _re.sub(r"[^a-zA-Z0-9@*/, _-]", "", orchestration.get("schedule", "@daily"))[:64]
    contract_name = _re.sub(r"[^a-zA-Z0-9 _.-]", "_", contract_name)[:128]

    dag_content = f'''"""
Airflow DAG for FLUID contract: {contract_name}
Auto-generated by fluid init
"""

from datetime import datetime, timedelta
from airflow import DAG
from airflow.operators.bash import BashOperator

default_args = {{
    'owner': 'fluid',
    'depends_on_past': False,
    'start_date': datetime(2024, 1, 1),
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': {orchestration.get("retries", 3)},
    'retry_delay': timedelta(minutes={orchestration.get("retry_delay", "5m").replace("m", "")}),
}}

with DAG(
    dag_id='{dag_id}',
    default_args=default_args,
    description='FLUID data product: {contract_name}',
    schedule_interval='{schedule}',
    catchup=False,
    tags=['fluid', 'data-product'],
) as dag:

    # Validate contract
    validate = BashOperator(
        task_id='validate_contract',
        bash_command='cd {project_dir.absolute()} && fluid validate contract.fluid.yaml',
    )

    # Plan execution
    plan = BashOperator(
        task_id='plan_execution',
        bash_command='cd {project_dir.absolute()} && fluid plan contract.fluid.yaml',
    )

    # Apply changes
    apply = BashOperator(
        task_id='apply_contract',
        bash_command='cd {project_dir.absolute()} && fluid apply contract.fluid.yaml --auto-approve',
    )

    validate >> plan >> apply
'''

    dag_dir = project_dir / "dags"
    dag_dir.mkdir(exist_ok=True)
    dag_file = dag_dir / f"{dag_id}_dag.py"

    with open(dag_file, "w") as f:
        f.write(dag_content)

    logger.info(f"Created basic DAG template: {dag_file}")


def create_dags_readme(dag_dir: Path, dag_id: str, schedule: str, dag_filename: str):
    """Create README in dags/ folder with usage instructions."""

    readme_content = f"""# Airflow DAG Configuration

This folder contains the Airflow DAG for your FLUID data product.

## Generated DAG

- **DAG ID**: `{dag_id}`
- **Schedule**: `{schedule}`
- **File**: `{dag_filename}`

## Usage

### Local Development

Run the DAG locally using Airflow:

```bash
# Start Airflow (from project root)
docker-compose --profile airflow up -d

# Access Airflow UI
open http://localhost:8080

# Default credentials
# Username: admin
# Password: admin
```

### Manual Execution

Run the FLUID pipeline manually:

```bash
# Validate contract
fluid validate contract.fluid.yaml

# Plan execution
fluid plan contract.fluid.yaml

# Apply changes
fluid apply contract.fluid.yaml --auto-approve
```

### CI/CD Integration

This DAG can be deployed to:
- Cloud Composer (GCP)
- MWAA (AWS)
- Astronomer
- Self-hosted Airflow

See `.jenkins/` folder for CI/CD pipeline configuration.

## Customization

To customize the DAG:

1. Edit `{dag_filename}`
2. Add custom operators or sensors
3. Configure alerting and notifications
4. Update schedule interval as needed

## Next Steps

- **Add data quality checks**: Use Great Expectations or Soda
- **Set up alerting**: Configure email/Slack notifications
- **Add lineage tracking**: Enable OpenLineage integration
- **Monitor performance**: Use Airflow metrics

For more information, see: https://fluid.dev/docs/orchestration
"""

    readme_path = dag_dir / "README.md"
    with open(readme_path, "w") as f:
        f.write(readme_content)


# ============================================================================
# MODE HANDLERS
# ============================================================================


def demo_mode(args, logger: logging.Logger) -> int:
    """Scaffold and run a working customer-360 example.

    This is the handler for ``fluid demo``. It scaffolds customer-360
    template files, initializes a local DuckDB database, optionally
    generates an Airflow DAG, and executes the pipeline end-to-end.

    Note: ``fluid init --quickstart`` does NOT route here — it is
    rewritten in ``detect_mode`` to ``--template customer-360 --yes``
    and dispatches through ``template_mode`` (scaffold only, no run).
    """

    project_name = slugify_identifier(args.name, fallback="my-first-product")
    template = "customer-360"  # Default template

    if RICH_AVAILABLE:
        console.print(
            Panel(
                f"🚀 Creating [bold cyan]{project_name}[/bold cyan] with customer analytics...\n\n"
                f"This will create a working data product with sample data.\n"
                f"No cloud account needed - runs locally with DuckDB.",
                title="Quickstart Mode",
                border_style="cyan",
            )
        )
    else:
        cprint(f"🚀 Creating {project_name} with customer analytics...")

    project_dir = Path(project_name)

    # Guard against symlink attacks.
    if project_dir.is_symlink():
        if RICH_AVAILABLE:
            console.print(f"[red]❌ '{project_name}' is a symlink — refusing to write[/red]")
        else:
            console_error(f"'{project_name}' is a symlink — refusing to write")
        return 1

    # Check if directory already exists
    if project_dir.exists() and any(project_dir.iterdir()):
        if RICH_AVAILABLE:
            console.print(
                f"[red]❌ Directory '{project_name}' already exists and is not empty[/red]"
            )
        else:
            console_error(f"Directory '{project_name}' already exists")
        return 1

    if args.dry_run:
        if RICH_AVAILABLE:
            console.print("[yellow]🔍 Dry run - would create:[/yellow]")
            console.print(f"  📁 {project_name}/")
            console.print(f"  📄 {project_name}/contract.fluid.yaml")
            console.print(f"  📊 {project_name}/data/customers.csv")
            console.print(f"  📊 {project_name}/data/orders.csv")
            console.print(f"  💾 {project_name}/.fluid/db.duckdb")
        return 0

    try:
        # Create project directory
        project_dir.mkdir(parents=True, exist_ok=True)

        # Copy template files
        success = copy_template(project_dir, template, logger)
        if not success:
            return 1

        # Copy sample data
        copy_sample_data(project_dir, template, logger)

        # Initialize local database
        init_local_db(project_dir, args.provider, logger)

        # Generate DAG if contract has orchestration config
        has_dag = False
        if not getattr(args, "no_dag", False):
            try:
                import yaml

                contract_path = project_dir / "contract.fluid.yaml"
                if contract_path.exists():
                    with open(contract_path) as f:
                        contract = yaml.safe_load(f)

                    if should_generate_dag(contract, template):
                        has_dag = generate_dag_for_project(
                            project_dir,
                            contract,
                            logger,
                            console if RICH_AVAILABLE else None,
                            template,
                        )
            except Exception as e:
                logger.warning(f"Failed to generate DAG: {e}")

        # Run pipeline if not --no-run
        if not args.no_run and args.provider == "local":
            run_local_pipeline(project_dir, logger)

        # NOTE: CI/CD scaffolding intentionally removed from this path.
        # Users who want Jenkinsfile / GitHub Actions / GitLab CI / Cloud
        # Build configs should run `fluid scaffold-ci` explicitly — init
        # should produce predictable artifacts, not interactively prompt
        # for cloud-platform-specific files.

        # Show next steps
        show_success_message(project_dir, args.provider, logger, has_dag=has_dag)

        return 0

    except Exception as e:
        error(logger, "demo_failed", error=str(e))
        if RICH_AVAILABLE:
            console.print(f"[red]❌ Demo failed: {e}[/red]")
        return 1


def blank_mode(args, logger: logging.Logger) -> int:
    """Empty project skeleton.

    Slice UX-F rewrite: goes directly through
    :func:`fluid_build.cli.forge_contract_factory.build_minimal_contract`
    + :func:`fluid_build.cli.forge_contract_factory.write_contract` so
    the output shape matches what ``fluid forge --blank`` produces —
    v0.7.2 YAML contract with ``metadata.provenance`` envelope
    alongside the workspace config, the ``.gitignore`` template, and
    the init receipt.

    The previous implementation delegated to ``product_new.run``, which
    emitted a legacy v0.5.7 JSON contract under ``bronze_<name>/``.
    That left three different scaffolding paths with three different
    contract formats (blank / forge-blank / template).  This slice
    unifies them.
    """
    from fluid_build.cli.artifact_envelope import dump_json_with_envelope
    from fluid_build.cli.artifact_paths import product_forge_receipt_path
    from fluid_build.cli.artifact_receipts import ReceiptBuilder
    from fluid_build.cli.forge_contract_factory import (
        build_minimal_contract,
        create_and_validate_contract,
    )

    project_name = slugify_identifier(args.name, fallback="my-project")
    project_dir = Path(project_name)

    if RICH_AVAILABLE:
        console.print(
            Panel(
                f"🔧 Creating minimal project: [bold]{project_name}[/bold]\n\n"
                f"Empty skeleton with no assumptions.",
                title="Blank Mode",
                border_style="white",
            )
        )
    else:
        cprint(f"🔧 Creating minimal project: {project_name}")

    # Guard against symlink attacks.
    if project_dir.is_symlink():
        if RICH_AVAILABLE:
            console.print(f"[red]❌ '{project_name}' is a symlink — refusing to write[/red]")
        return 1

    if project_dir.exists() and any(project_dir.iterdir()):
        if RICH_AVAILABLE:
            console.print(
                f"[red]❌ Directory '{project_name}' already exists and is not empty[/red]"
            )
        else:
            console_error(f"Directory '{project_name}' already exists and is not empty")
        return 1

    if getattr(args, "dry_run", False):
        preview_lines = [
            f"  📁 {project_name}/",
            f"  📄 {project_name}/contract.fluid.yaml",
            f"  📄 {project_name}/.fluid/forge-receipt.json",
        ]
        if RICH_AVAILABLE:
            console.print("[yellow]🔍 Dry run - would create:[/yellow]")
            for line in preview_lines:
                console.print(line)
        else:
            cprint("Dry run - would create:")
            for line in preview_lines:
                cprint(line)
        return 0

    # Derive a contract id from the workspace name — mirrors what
    # ``fluid forge --blank`` does when no --context is supplied.
    product_slug = slugify_identifier(project_name, fallback="my-data-product")

    contract = build_minimal_contract(
        product_id=product_slug,
        name=project_name,
    )

    result_path = create_and_validate_contract(
        contract,
        project_dir,
        logger,
        console=console if RICH_AVAILABLE else None,
    )
    if result_path is None:
        return 1

    # Write a forge-receipt.json inside the product so `fluid status`,
    # drift detection, and any downstream tooling see the same shape
    # `fluid forge --blank` produces.
    try:
        from fluid_build import __version__ as tool_version
    except Exception:  # pragma: no cover — defensive
        tool_version = ""

    builder = ReceiptBuilder(flow="blank", dry_run=False)
    # Record the contract write with a sha256 for drift-awareness.
    import hashlib

    try:
        sha = hashlib.sha256(result_path.read_bytes()).hexdigest()
        size = result_path.stat().st_size
    except OSError:
        sha, size = None, 0
    builder.record_entry(
        path=Path("contract.fluid.yaml"),
        action="create",
        sha256=sha,
        size=size,
    )
    builder.set_inputs(
        blank=True,
        flow="init-blank",
        provider=getattr(args, "provider", None),
        name=project_name,
    )

    try:
        receipt_path = product_forge_receipt_path(project_dir)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_bytes = dump_json_with_envelope(
            builder.build_document().to_payload(),
            kind="ForgeReceipt",
            command=f"fluid init {project_name} --blank",
            tool_version=str(tool_version),
        )
        receipt_path.write_text(receipt_bytes, encoding="utf-8")
        logger.debug("blank_mode_receipt_written", extra={"path": str(receipt_path)})
    except Exception as exc:  # noqa: BLE001 — receipt is best-effort
        logger.debug("blank_mode_receipt_write_failed", extra={"error": str(exc)})

    if RICH_AVAILABLE:
        console.print(f"\n✅ Created [cyan]{project_name}/contract.fluid.yaml[/cyan]")
        console.print(f"[dim]Next:[/dim] [cyan]cd {project_name} && fluid validate[/cyan]")
    else:
        cprint(f"Created {project_name}/contract.fluid.yaml")

    return 0


def template_mode(args, logger: logging.Logger) -> int:
    """Create from specific template"""

    template_name = args.template
    if not template_name:
        # Defensive: template_mode should never be reached with an empty
        # ``args.template``.  The interactive menu path runs
        # ``_ask_template_name`` before dispatching, and every CLI flag
        # path requires a value.  This guard catches direct callers and
        # prints an actionable error instead of crashing inside
        # ``copy_template`` with a cryptic Path/None TypeError.
        if RICH_AVAILABLE:
            console.print(
                "[red]❌ No template name provided.[/red]\n"
                "[dim]Pass [bold]--template NAME[/bold] or pick one from "
                "[bold]fluid init --list-templates[/bold].[/dim]"
            )
        else:
            console_error("No template name provided. Pass --template NAME.")
        return 1

    project_name = slugify_identifier(args.name or template_name, fallback="my-project")
    project_dir = Path(project_name)

    if RICH_AVAILABLE:
        console.print(
            Panel(
                f"📦 Creating from template: [bold]{template_name}[/bold]\n"
                f"Project: [bold]{project_name}[/bold]",
                title="Template Mode",
                border_style="blue",
            )
        )
    else:
        cprint(f"📦 Creating from template: {template_name}")

    success = copy_template(project_dir, template_name, logger)
    if not success:
        return 1

    # Slice UX-F: after the template files have landed, rewrite the
    # product's contract.fluid.yaml through ``write_contract`` so it
    # carries the ``metadata.provenance`` envelope (slice 4) and
    # record a forge receipt under ``.fluid/forge-receipt.json`` so
    # the product looks the same as one produced by ``fluid forge``.
    _finalise_template_product(
        template_name=template_name,
        project_name=project_name,
        project_dir=project_dir,
        logger=logger,
    )

    if RICH_AVAILABLE:
        console.print(f"\n✅ Created project from {template_name} template")

    return 0


def _finalise_template_product(
    *,
    template_name: str,
    project_name: str,
    project_dir: Path,
    logger: logging.Logger,
) -> None:
    """Post-copy hook: inject provenance envelope + write forge receipt.

    Runs after ``copy_template`` / ``create_from_template`` lands the
    template files.  Best-effort: never raises.  When the template
    doesn't include a ``contract.fluid.yaml``, the provenance step is
    silently skipped.
    """
    import hashlib

    from fluid_build.cli.artifact_envelope import build_envelope, dump_json_with_envelope
    from fluid_build.cli.artifact_paths import product_forge_receipt_path
    from fluid_build.cli.artifact_receipts import ReceiptBuilder

    try:
        import yaml
    except ImportError:  # pragma: no cover — yaml is a hard dep
        return

    contract_path = project_dir / "contract.fluid.yaml"
    if not contract_path.is_file():
        return  # some templates don't ship a contract; nothing to stamp.

    try:
        from fluid_build import __version__ as tool_version
    except Exception:  # pragma: no cover — defensive
        tool_version = ""

    # Load the template's contract and inject metadata.provenance.
    try:
        doc = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        if not isinstance(doc, dict):
            return

        metadata = doc.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata = dict(metadata)
        metadata["provenance"] = build_envelope(
            kind="ContractMetadata",
            command=f"fluid init {project_name} --template {template_name}",
            tool_version=str(tool_version),
        )
        doc["metadata"] = metadata

        contract_path.write_text(
            "# FLUID Data Product Contract\n"
            "# Docs: https://fluid-build.dev/docs/contracts\n"
            + yaml.dump(doc, default_flow_style=False, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 — provenance is best-effort
        logger.debug("template_provenance_inject_failed", extra={"error": str(exc)})

    # Write the forge-receipt.json inside the product.
    try:
        builder = ReceiptBuilder(flow="template", dry_run=False)
        try:
            sha = hashlib.sha256(contract_path.read_bytes()).hexdigest()
            size = contract_path.stat().st_size
        except OSError:
            sha, size = None, 0
        builder.record_entry(
            path=Path("contract.fluid.yaml"),
            action="create",
            sha256=sha,
            size=size,
        )
        builder.set_inputs(
            template=template_name,
            name=project_name,
        )
        receipt_path = product_forge_receipt_path(project_dir)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            dump_json_with_envelope(
                builder.build_document().to_payload(),
                kind="ForgeReceipt",
                command=f"fluid init {project_name} --template {template_name}",
                tool_version=str(tool_version),
            ),
            encoding="utf-8",
        )
        logger.debug("template_receipt_written", extra={"path": str(receipt_path)})
    except Exception as exc:  # noqa: BLE001 — receipt is best-effort
        logger.debug("template_receipt_write_failed", extra={"error": str(exc)})


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


#: Filename suffixes that should NEVER be copied from a template source.
#: ``*.old`` files are template-author scratch artifacts (backup copies of
#: old contract shapes), not something the user should see inherit into
#: their new project.
_TEMPLATE_IGNORE_SUFFIXES = (".old", ".bak", ".tmp", ".swp")
_TEMPLATE_IGNORE_NAMES = {"__pycache__", ".DS_Store"}


def _should_copy_template_entry(entry: Path) -> bool:
    """Return True if *entry* should be copied into a new project dir."""
    if entry.name in _TEMPLATE_IGNORE_NAMES:
        return False
    if any(entry.name.endswith(suffix) for suffix in _TEMPLATE_IGNORE_SUFFIXES):
        return False
    return True


def copy_template(project_dir: Path, template_name: str, logger: logging.Logger) -> bool:
    """Copy template files to project directory"""

    # Find template directory
    cli_dir = Path(__file__).parent
    templates_dir = cli_dir.parent / "templates" / template_name

    if not templates_dir.exists():
        if RICH_AVAILABLE:
            console.print(f"[yellow]⚠️  Template '{template_name}' not found[/yellow]")
            console.print(f"Looking in: {templates_dir}")
            console.print("\nAvailable templates:")
            console.print("  - customer-360 (customer analytics)")
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
