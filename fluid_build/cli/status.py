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

"""``fluid status`` — one-page summary of a FLUID product.

A single command that tells the user everything they need to know
about the product they're sitting in: identity, workspace, last
forge/init run, CI provider configured, and drift status per file
category.  Runs in under 500 ms, reads no network, requires no flags.

Design goals (UX audit F4 / F5 from the plan):

1. **Zero friction.** ``fluid status`` with no arguments. The user
   should not have to read docs to discover what's here.
2. **Degraded-graceful.** Works on a bare flat contract with no
   receipt, no lockfile, no ci-state. Missing rows print as ``—``
   instead of crashing.
3. **Drift-aware.** When ci-state.json is present, show pristine vs
   drifted vs missing counts. When a ``contract.bundled.yaml`` +
   ``contract.lock.yaml`` pair is present (future slice F2), show the
   bundle sync status.
4. **Point at next steps.** Print a short "Next:" line with 3–4
   copy-pasteable commands so the user always knows what to try.

The command is a read-only scan — it never writes anything to disk,
never modifies state, and never fails in a way that aborts the
outer workflow.  On any unexpected error, it degrades to printing
whatever rows it did gather and exits 0.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from fluid_build.cli.artifact_paths import (
    product_ci_state_path,
    product_contract_path,
    product_forge_receipt_path,
    product_memory_path,
    workspace_init_receipt_path,
)
from fluid_build.cli.console import cprint
from fluid_build.cli.workspace_config import find_workspace_root, load_workspace_config

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover — Rich is optional
    Console = None  # type: ignore[assignment]
    Panel = None  # type: ignore[assignment]
    Table = None  # type: ignore[assignment]
    RICH_AVAILABLE = False


COMMAND = "status"

LOG = logging.getLogger("fluid.cli.status")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class StatusSummary:
    """Everything :func:`run` gathered — returned for testability.

    Every field is optional because ``fluid status`` must degrade
    gracefully when parts of the expected state are missing.  The
    printer just renders each field with a sensible placeholder.
    """

    workspace_root: Optional[Path] = None
    workspace_name: Optional[str] = None
    product_root: Optional[Path] = None
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    domain: Optional[str] = None
    owner: Optional[str] = None
    fluid_version: Optional[str] = None
    authoring_mode: str = "flat"  # "flat" | "fragment-first"
    fragment_count: int = 0
    overlay_count: int = 0
    last_forge_at: Optional[str] = None
    last_forge_flow: Optional[str] = None
    last_init_at: Optional[str] = None
    ci_provider: Optional[str] = None
    ci_complexity: Optional[str] = None
    ci_file_count: int = 0
    ci_pristine_count: int = 0
    ci_drifted_count: int = 0
    ci_missing_from_disk_count: int = 0
    ci_missing_from_state_count: int = 0
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Gatherers — one function per concern, each tolerant of missing state
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return raw if isinstance(raw, dict) else None


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _gather_contract_info(product_root: Path, summary: StatusSummary) -> None:
    """Populate product identity fields from contract.fluid.yaml."""
    contract_path = product_contract_path(product_root)
    if not contract_path.is_file():
        return

    doc = _load_yaml(contract_path)
    if not doc:
        return

    summary.product_id = doc.get("id") if isinstance(doc.get("id"), str) else None
    summary.product_name = doc.get("name") if isinstance(doc.get("name"), str) else None
    # fluidVersion can be loaded as either str (quoted) or float/int (unquoted)
    raw_version = doc.get("fluidVersion")
    if raw_version is not None:
        summary.fluid_version = str(raw_version)

    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}

    # Domain lives at the top level in fluid 0.7.2+; older scaffolds put it
    # under metadata.domain.  Read top-level first, fall back to metadata.
    top_domain = doc.get("domain")
    meta_domain = metadata.get("domain")
    if isinstance(top_domain, str) and top_domain:
        summary.domain = top_domain
    elif isinstance(meta_domain, str) and meta_domain:
        summary.domain = meta_domain

    owner = metadata.get("owner")
    if isinstance(owner, dict):
        summary.owner = owner.get("team") if isinstance(owner.get("team"), str) else None
    elif isinstance(owner, str):
        summary.owner = owner


def _gather_authoring_mode(product_root: Path, summary: StatusSummary) -> None:
    """Detect fragment-first authoring and count fragments/overlays.

    Fragment-first detection is heuristic: the product is considered
    fragment-first if a non-empty ``fragments/`` directory exists next
    to the contract.  This is enough for slice UX-A; slices F1–F3 will
    add stricter markers (a ``fragment_layout`` field in the contract
    or in workspace config).
    """
    fragments_dir = product_root / "fragments"
    overlays_dir = product_root / "overlays"

    if fragments_dir.is_dir():
        fragment_files = [
            p for p in fragments_dir.rglob("*.yaml") if p.is_file()
        ]
        summary.fragment_count = len(fragment_files)
        if fragment_files:
            summary.authoring_mode = "fragment-first"

    if overlays_dir.is_dir():
        summary.overlay_count = sum(
            1 for p in overlays_dir.glob("*.yaml") if p.is_file()
        )


def _gather_forge_receipt(product_root: Path, summary: StatusSummary) -> None:
    """Populate last-forge-run fields from the forge receipt, if present."""
    receipt_path = product_forge_receipt_path(product_root)
    doc = _load_json(receipt_path)
    if not doc:
        return
    generated_at = doc.get("generated_at")
    if isinstance(generated_at, str):
        summary.last_forge_at = generated_at
    flow = doc.get("flow")
    if isinstance(flow, str):
        summary.last_forge_flow = flow


def _gather_init_receipt(root: Path, summary: StatusSummary) -> None:
    """Populate last-init-run fields from the workspace init receipt."""
    receipt_path = workspace_init_receipt_path(root)
    doc = _load_json(receipt_path)
    if not doc:
        return
    generated_at = doc.get("generated_at")
    if isinstance(generated_at, str):
        summary.last_init_at = generated_at


def _gather_ci_state(product_root: Path, summary: StatusSummary) -> None:
    """Populate CI provider + drift rows from ci-state.json, if present."""
    try:
        from fluid_build.cli.artifact_ci_state import (
            classify_ci_drift,
            load_ci_state,
        )
    except ImportError:  # pragma: no cover — optional
        return

    state = load_ci_state(product_root)
    if state is None:
        return

    summary.ci_provider = state.provider
    summary.ci_complexity = state.complexity
    summary.ci_file_count = len(state.files)

    # Build a pseudo-generated_files dict so classify_ci_drift can
    # iterate — the classifier only needs the paths, not the content.
    generated_files = {entry.get("path", ""): "" for entry in state.files}
    generated_files.pop("", None)

    try:
        drift = classify_ci_drift(product_root, generated_files, state=state)
    except Exception as exc:  # noqa: BLE001 — drift check is best-effort
        LOG.debug("status_drift_check_failed: %s", exc)
        return

    summary.ci_pristine_count = len(drift.pristine)
    summary.ci_drifted_count = len(drift.drifted)
    summary.ci_missing_from_disk_count = len(drift.missing_from_disk)
    summary.ci_missing_from_state_count = len(drift.missing_from_state)


def _resolve_product_root(start: Optional[Path] = None) -> Path:
    """Return the product directory containing contract.fluid.yaml.

    Walks up from *start* (default cwd) looking for a contract file.
    Falls back to cwd when none is found — the gatherers tolerate the
    missing contract and the printer shows ``—`` placeholders.
    """
    current = (start or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        if product_contract_path(parent).is_file():
            return parent
    return current


def build_status_summary(start: Optional[Path] = None) -> StatusSummary:
    """Gather everything ``fluid status`` wants to display.

    Exposed for tests and for future callers that want the structured
    data without triggering a Rich render (e.g. ``fluid doctor``).
    """
    summary = StatusSummary()

    product_root = _resolve_product_root(start)
    summary.product_root = product_root

    workspace_root = find_workspace_root(product_root) or product_root
    summary.workspace_root = workspace_root

    ws_config = load_workspace_config(workspace_root)
    summary.workspace_name = ws_config.name or None

    _gather_contract_info(product_root, summary)
    _gather_authoring_mode(product_root, summary)
    _gather_forge_receipt(product_root, summary)
    _gather_init_receipt(workspace_root, summary)
    _gather_ci_state(product_root, summary)

    return summary


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_scalar(value: Optional[str]) -> str:
    return value if value else "—"


def _format_owner(summary: StatusSummary) -> str:
    if summary.owner and summary.workspace_name:
        return f"{summary.owner}  (workspace: {summary.workspace_name})"
    return _format_scalar(summary.owner)


def _format_authoring(summary: StatusSummary) -> str:
    if summary.authoring_mode == "fragment-first":
        parts = [f"{summary.fragment_count} fragments"]
        if summary.overlay_count:
            parts.append(f"{summary.overlay_count} overlays")
        return f"fragment-first ({', '.join(parts)})"
    return "flat"


def _format_last_forge(summary: StatusSummary) -> str:
    if not summary.last_forge_at:
        return "—"
    flow = summary.last_forge_flow or "?"
    return f"{summary.last_forge_at} (flow={flow})"


def _format_ci(summary: StatusSummary) -> str:
    if not summary.ci_provider:
        return "—"
    return (
        f"{summary.ci_provider} (complexity={summary.ci_complexity}, "
        f"{summary.ci_file_count} file{'s' if summary.ci_file_count != 1 else ''})"
    )


def _format_ci_drift(summary: StatusSummary) -> str:
    if not summary.ci_provider:
        return "—"
    total = summary.ci_file_count
    if summary.ci_drifted_count or summary.ci_missing_from_state_count:
        return (
            f"⚠ drifted: {summary.ci_drifted_count}, "
            f"unknown: {summary.ci_missing_from_state_count}, "
            f"pristine: {summary.ci_pristine_count}/{total}"
        )
    return f"✓ clean ({summary.ci_pristine_count}/{total} pristine)"


def _render_plain(summary: StatusSummary) -> None:
    cprint("Product:")
    cprint(
        f"  id:         {_format_scalar(summary.product_id)}    "
        f"name: {_format_scalar(summary.product_name)}"
    )
    cprint(f"  domain:     {_format_scalar(summary.domain)}")
    cprint(f"  owner:      {_format_owner(summary)}")
    cprint(f"  fluidVer:   {_format_scalar(summary.fluid_version)}")
    cprint("")
    cprint("Layout:")
    cprint(f"  authoring:  {_format_authoring(summary)}")
    cprint(f"  root:       {summary.product_root}")
    cprint("")
    cprint("Last runs:")
    cprint(f"  forge:      {_format_last_forge(summary)}")
    cprint(f"  init:       {_format_scalar(summary.last_init_at)}")
    cprint("")
    cprint("CI:")
    cprint(f"  provider:   {_format_ci(summary)}")
    cprint(f"  drift:      {_format_ci_drift(summary)}")
    cprint("")
    cprint("Next: fluid validate    fluid plan --env dev    fluid forge --ci <provider>")


def _render_rich(summary: StatusSummary) -> None:
    console = Console()

    # ── Identity panel ─────────────────────────────────────────────────
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim", width=12)
    table.add_column()
    table.add_row(
        "Product",
        f"[bold]{_format_scalar(summary.product_id)}[/bold]"
        + (f"  [dim]({summary.product_name})[/dim]" if summary.product_name else ""),
    )
    table.add_row("Domain", _format_scalar(summary.domain))
    table.add_row("Owner", _format_owner(summary))
    table.add_row(
        "Workspace",
        f"[bold]{_format_scalar(summary.workspace_name)}[/bold]    "
        f"[dim]{summary.workspace_root}[/dim]"
        if summary.workspace_root
        else "—",
    )
    table.add_row("Authoring", _format_authoring(summary))
    table.add_row(
        "Last forge",
        _format_last_forge(summary),
    )
    table.add_row("Last init", _format_scalar(summary.last_init_at))
    table.add_row("CI", _format_ci(summary))

    # Drift row coloured by state
    if summary.ci_provider:
        if summary.ci_drifted_count or summary.ci_missing_from_state_count:
            drift_text = f"[yellow]{_format_ci_drift(summary)}[/yellow]"
        else:
            drift_text = f"[green]{_format_ci_drift(summary)}[/green]"
    else:
        drift_text = "—"
    table.add_row("Drift", drift_text)

    console.print()
    console.print(
        Panel(
            table,
            title="[bold bright_white]fluid status[/bold bright_white]",
            border_style="bright_cyan",
            padding=(1, 2),
        )
    )

    # ── Next-steps line ────────────────────────────────────────────────
    console.print(
        "[dim]Next:[/dim]  "
        "[cyan]fluid validate[/cyan]  "
        "[cyan]fluid plan --env dev[/cyan]  "
        "[cyan]fluid forge --ci <provider>[/cyan]"
    )
    console.print()


def _render(summary: StatusSummary) -> None:
    if RICH_AVAILABLE:
        _render_rich(summary)
    else:
        _render_plain(summary)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``fluid status`` command."""
    parser = subparsers.add_parser(
        COMMAND,
        help="📋 One-page summary of the current FLUID product",
        description=(
            "Print a short summary of the product in the current directory: "
            "identity, workspace, authoring mode, last forge run, CI provider, "
            "and drift status.  No flags, runs in under 500 ms."
        ),
    )
    parser.set_defaults(cmd=COMMAND, func=run)


def run(args: Any, logger: logging.Logger) -> int:
    """Gather state and print the status panel.

    Never raises on unexpected errors — prints a short diagnostic and
    returns 0 so a CI pipeline running ``fluid status`` never aborts
    on a partial workspace.
    """
    try:
        summary = build_status_summary()
        _render(summary)
        return 0
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("status_command_failed: %s", exc)
        cprint(f"fluid status: couldn't gather state: {exc}")
        return 0
