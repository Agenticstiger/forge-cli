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

"""`fluid demo` — zero-question, fully-working example for first-time users.

This is the canonical "just show me it working" command.  It creates a
``fluid-demo/`` directory, scaffolds the customer-360 template with sample
data, runs the pipeline locally with DuckDB, and then prints a
retention-focused success panel showing exactly what was created and what
to try next.

Design goals (user retention for first-time data engineers):

1. **Instant gratification** — one command, no questions, no API key.
2. **Visible result** — show what was built (contract path, data files,
   pipeline output) so the user has an "aha moment" they can inspect.
3. **Concrete next steps** — 4 exact commands they can copy/paste to
   explore further, plus a bridge to their own project.
4. **Easy cleanup** — tell them how to remove it when done, so they don't
   feel locked in.
5. **Friendly recovery** — if it fails, explain what happened and how to
   recover without losing confidence.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, List, Tuple

from fluid_build.cli.artifact_envelope import dump_json_with_envelope
from fluid_build.cli.artifact_paths import workspace_init_receipt_path
from fluid_build.cli.artifact_receipts import ReceiptBuilder
from fluid_build.cli.artifact_scan import diff_snapshots, snapshot_workspace
from fluid_build.cli.console import cprint
from fluid_build.cli.console import error as console_error
from fluid_build.cli.next_steps import print_next_steps

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    RICH_AVAILABLE = True
    _console = Console()
except ImportError:  # pragma: no cover — Rich is an optional dep
    Console = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]
    RICH_AVAILABLE = False
    _console = None


COMMAND = "demo"
DEFAULT_NAME = "fluid-demo"


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``fluid demo`` command."""
    parser = subparsers.add_parser(
        COMMAND,
        help="🚀 Try FLUID in ~30s — zero setup, no API key, local DuckDB",
        description=(
            "Create and run a working customer-360 example with sample data. "
            "No API key, no cloud account, no questions — uses local DuckDB. "
            "The fastest way to see what FLUID can do."
        ),
    )
    parser.add_argument(
        "name",
        nargs="?",
        default=DEFAULT_NAME,
        help=f"Directory name for the demo project (default: {DEFAULT_NAME})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be created without doing it",
    )
    parser.add_argument(
        "--no-run",
        action="store_true",
        help="Scaffold the project but skip running the pipeline",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress the next-steps panel and other post-success hints",
    )
    parser.set_defaults(cmd=COMMAND, func=run)


# ---------------------------------------------------------------------------
# UI panels
# ---------------------------------------------------------------------------


def _print_intro_panel(name: str) -> None:
    """The 'here's what I'm about to do' panel — builds anticipation."""
    if not RICH_AVAILABLE or _console is None:
        cprint(f"Running FLUID demo in ./{name}/ ...")
        return
    _console.print()
    _console.print(
        Panel(
            f"[bold bright_cyan]🚀 Starting the FLUID demo[/bold bright_cyan]\n\n"
            f"I'll create [bold]{name}/[/bold] with the customer-360 example, then:\n\n"
            "  [dim]1.[/dim] Scaffold a real [cyan]contract.fluid.yaml[/cyan] and sample data\n"
            "  [dim]2.[/dim] Validate the contract and plan execution\n"
            "  [dim]3.[/dim] Run the pipeline end-to-end with [cyan]DuckDB[/cyan]\n\n"
            "[dim]No API key. No cloud account. Runs entirely on your machine.\n"
            "Takes about 30 seconds.[/dim]\n\n"
            "[dim]Options: [bold]fluid demo --help[/bold] "
            "(--dry-run to preview, --no-run to skip pipeline).[/dim]",
            title="[bold bright_white]FLUID Demo[/bold bright_white]",
            border_style="bright_cyan",
            padding=(1, 2),
        )
    )
    _console.print()


def _find_artifacts(project_dir: Path) -> Tuple[List[Path], List[Path]]:
    """Return (contract_files, data_files) found under *project_dir*."""
    contracts: List[Path] = []
    data: List[Path] = []
    if not project_dir.exists():
        return contracts, data
    contracts = sorted(project_dir.rglob("contract.fluid.yaml"))
    # Limit to the top-level data/ directory to keep the panel short.
    data_dir = project_dir / "data"
    if data_dir.is_dir():
        data = sorted(p for p in data_dir.iterdir() if p.is_file() and not p.name.startswith("."))
    return contracts, data


def _print_success_panel(name: str, project_dir: Path) -> None:
    """The 'here's what you just built + what to try next' panel."""
    if not RICH_AVAILABLE or _console is None:
        cprint(f"✅ Demo ready in ./{name}/")
        cprint("Next steps:")
        cprint(f"  cd {name}")
        cprint("  fluid validate")
        cprint("  fluid viz --open")
        return

    contracts, data = _find_artifacts(project_dir)

    # ── what was built ─────────────────────────────────────────────────
    artifacts_lines = []
    if contracts:
        rel = contracts[0].relative_to(project_dir.parent)
        artifacts_lines.append(f"  [cyan]{rel}[/cyan]  [dim]← the single source of truth[/dim]")
    for d in data[:3]:
        rel = d.relative_to(project_dir.parent)
        artifacts_lines.append(f"  [cyan]{rel}[/cyan]  [dim]← sample data[/dim]")
    if len(data) > 3:
        artifacts_lines.append(f"  [dim]… and {len(data) - 3} more file(s) in data/[/dim]")

    if artifacts_lines:
        _console.print()
        _console.print(
            Panel(
                "[bold bright_green]✅ Demo ready![/bold bright_green] Here's what "
                "I just built for you:\n\n" + "\n".join(artifacts_lines),
                title="[bold bright_white]What you got[/bold bright_white]",
                border_style="bright_green",
                padding=(1, 2),
            )
        )

    # ── what to try next ───────────────────────────────────────────────
    next_cmds = Table(
        show_header=False,
        box=None,
        padding=(0, 2),
    )
    next_cmds.add_column(style="bold bright_yellow", width=3)
    next_cmds.add_column(style="bright_cyan", width=40)
    next_cmds.add_column(style="dim bright_white")

    next_cmds.add_row("1.", f"cd {name}", "Move into the project")
    next_cmds.add_row("2.", "cat contract.fluid.yaml", "Read the contract you just built")
    next_cmds.add_row("3.", "fluid validate", "Check the contract is still valid")
    next_cmds.add_row("4.", "fluid viz --open", "See the data product as a diagram")

    _console.print()
    _console.print("[bold bright_green]Next, try these (copy-paste friendly):[/bold bright_green]")
    _console.print(next_cmds)
    _console.print()

    # ── bridge to their own project + cleanup ──────────────────────────
    _console.print(
        Panel(
            "[bold]Ready for your own project?[/bold]\n"
            "  [bright_cyan]fluid init my-project[/bright_cyan]   "
            "[dim]← AI will help you design it (a free Gemini key works)[/dim]\n"
            "  [bright_cyan]fluid init --list-templates[/bright_cyan]   "
            "[dim]← browse other templates[/dim]\n\n"
            f"[dim]Done playing? Clean up with [bold]rm -rf {name}[/bold].[/dim]",
            title="[bold bright_white]What's next?[/bold bright_white]",
            border_style="bright_white",
            padding=(1, 2),
        )
    )
    _console.print()


def _print_failure_panel(name: str, exc: Exception) -> None:
    """Recovery panel — don't leave the user stranded."""
    if not RICH_AVAILABLE or _console is None:
        console_error(f"Demo failed: {exc}")
        cprint("Run 'fluid doctor' to check your environment.")
        return
    _console.print()
    _console.print(
        Panel(
            f"[bold red]Demo didn't finish successfully.[/bold red]\n\n"
            f"[dim]Error:[/dim] {exc}\n\n"
            "Try these to get unstuck:\n"
            "  [bright_cyan]fluid doctor[/bright_cyan]   "
            "[dim]← check Python version, dependencies, local providers[/dim]\n"
            f"  [bright_cyan]rm -rf {name}[/bright_cyan]   "
            "[dim]← remove the half-built project and try again[/dim]\n\n"
            "[dim]If it keeps failing, please open an issue with the error above\n"
            "at https://github.com/open-data-protocol/fluid/issues[/dim]",
            title="[bold bright_white]Something went wrong[/bold bright_white]",
            border_style="red",
            padding=(1, 2),
        )
    )
    _console.print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(args: Any, logger: logging.Logger) -> int:
    """Entry point for ``fluid demo``.

    Delegates the actual scaffolding + pipeline run to
    :func:`fluid_build.cli.init.demo_mode` but wraps it with
    demo-specific UX (intro panel, retention-focused success panel,
    friendly recovery).
    """
    name = getattr(args, "name", None) or DEFAULT_NAME

    target = Path(name)

    # Refuse symlinks — mirrors demo_mode's guard.  A malicious
    # symlink pointing at a system directory would otherwise let us
    # write files outside the demo's intended location.
    if target.is_symlink():
        if RICH_AVAILABLE and _console is not None:
            _console.print(
                f"\n[red]❌ '{name}' is a symlink — refusing to write.[/red]\n"
                f"[dim]Pick a different name ([bold]fluid demo my-other-name[/bold]).[/dim]\n"
            )
        else:
            console_error(f"'{name}' is a symlink — refusing to write")
        return 1

    # Refuse to clobber an existing non-empty directory — mirrors demo_mode.
    if target.exists() and any(target.iterdir()):
        if RICH_AVAILABLE and _console is not None:
            _console.print(
                f"\n[yellow]A directory called [bold]{name}[/bold] already exists "
                f"and is not empty.[/yellow]\n"
                f"[dim]Remove it first ([bold]rm -rf {name}[/bold]) or pick a "
                f"different name ([bold]fluid demo my-other-name[/bold]).[/dim]\n"
            )
        else:
            console_error(f"Directory '{name}' already exists and is not empty")
        return 1

    _print_intro_panel(name)

    # Snapshot the parent directory so the receipt can report everything
    # demo_mode wrote under target/.
    scan_root = target.parent.resolve() if target.parent else Path.cwd()
    before_snapshot = snapshot_workspace(scan_root)

    # Build a minimal args namespace for demo_mode.  It reads:
    #   name, dry_run, no_dag, no_run, provider
    from fluid_build.cli.init import demo_mode

    shim = argparse.Namespace(
        name=name,
        dry_run=getattr(args, "dry_run", False),
        no_dag=True,  # Demo should not scatter an Airflow DAG on the user's machine
        no_run=getattr(args, "no_run", False),
        provider="local",  # Demo is always local-only
    )

    try:
        rc = demo_mode(shim, logger)
    except Exception as exc:  # noqa: BLE001 — we want friendly recovery for any failure
        logger.exception("fluid demo: demo_mode raised")
        _print_failure_panel(name, exc)
        return 1

    if rc == 0 and not getattr(args, "dry_run", False):
        _write_demo_receipt(
            name=name,
            target=target,
            before_snapshot=before_snapshot,
            scan_root=scan_root,
            logger=logger,
        )
        _print_success_panel(name, target.resolve())
        # Slice UX-C: show the shared next-steps panel after the demo's
        # own success panel.  Skips when --quiet is passed.
        print_next_steps(
            "demo",
            console=_console if RICH_AVAILABLE else None,
            args=args,
        )
    elif rc != 0:
        _print_failure_panel(name, RuntimeError(f"exit code {rc}"))

    return rc


def _write_demo_receipt(
    *,
    name: str,
    target: Path,
    before_snapshot,
    scan_root: Path,
    logger: logging.Logger,
) -> None:
    """Write ``<demo>/.fluid/init-receipt.json`` recording the demo run.

    Same receipt format as ``fluid init`` runs so every artifact-producing
    command in the CLI has identical provenance shape.  ``flow="demo"``
    marks the run so future tooling can differentiate.

    Best-effort: a failure never propagates — the user's demo still
    succeeded even if we can't record it.
    """
    try:
        after_snapshot = snapshot_workspace(scan_root)
        entries = diff_snapshots(before_snapshot, after_snapshot)
        changed = [e for e in entries if e.action != "unchanged"]
        if not changed:
            return

        builder = ReceiptBuilder(flow="demo", dry_run=False)
        for entry in changed:
            builder.record_entry(
                path=Path(entry.path),
                action=entry.action,
                sha256=entry.sha256,
                size=entry.size,
                reason=entry.reason,
            )
        builder.set_inputs(name=name, demo=True, provider="local")

        doc = builder.build_document()

        try:
            from fluid_build import __version__ as tool_version
        except Exception:  # pragma: no cover — defensive
            tool_version = ""

        payload_bytes = dump_json_with_envelope(
            doc.to_payload(),
            kind="InitReceipt",
            command=f"fluid demo {name}",
            tool_version=str(tool_version),
        )

        # Demo receipts live under the demo product's .fluid/ dir (not
        # the parent scan_root) so everything the demo created is
        # self-contained inside target/.
        receipt_path = workspace_init_receipt_path(target.resolve())
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(payload_bytes, encoding="utf-8")
        logger.debug("demo_receipt_written", extra={"path": str(receipt_path)})
    except Exception as exc:  # noqa: BLE001 — never abort demo on receipt write failure
        logger.debug("demo_receipt_write_failed", extra={"error": str(exc)})
