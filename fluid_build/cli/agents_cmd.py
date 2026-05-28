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

# ruff: noqa: T201 — this CLI command owns user-facing print() output by design;
# the canonical migration to console.cprint is tracked separately.
"""``fluid agents`` — manage forge agent runs (list / show / prune).

This is the operator-facing surface for the resumability primitive at
:mod:`fluid_build.copilot.checkpoint`. Three subcommands:

* ``fluid agents list``  — table of recent runs (RUN_ID, AGE, STATUS,
  STAGES, COST, LAST_STAGE). Status icons mirror the gh CLI palette
  (green/yellow/dim), columns mirror kubectl get pods. ``--incomplete``
  filters to paused/failed runs; ``--json`` for scripts.
* ``fluid agents show <run-id>`` — full stage breakdown for one run,
  same shape as ``gh run view``.
* ``fluid agents prune`` — delete (or archive) old run directories.
  ``--dry-run`` shows candidates, ``--yes`` skips confirmation.
  Reports bytes reclaimed in the success message (mirrors
  ``docker system prune``).

Design notes:

* The data source is ``.fluid/agents/<run-id>/`` per workspace — same
  invariant the preview panel and ``fluid stats`` rely on.
* When the peer-agent ``checkpoint`` module is importable, runs are
  loaded via the canonical ``CheckpointStore``. When not, we fall back
  to walking the filesystem directly so this command works in isolation
  for tests + bootstrap.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

LOG = logging.getLogger("fluid.cli.agents")

COMMAND = "agents"

# Default prune cutoff — 30 days mirrors the cutoff used by the startup
# prune-hint in ``fluid forge`` (so users see consistent thresholds).
DEFAULT_PRUNE_CUTOFF_DAYS = 30

# Status icons borrowed from gh CLI's autocolor palette:
# green = success/done, yellow = paused, dim = stale, red = failed,
# cyan = running. Falls back to ASCII when rich is unavailable.
#
# Status string sources we must accept:
#   - CheckpointStore persists ``"complete"`` (no ``d``) on full-pipeline
#     completion (see ``copilot/checkpoint.py``).
#   - Older filesystem-fallback writers persist ``"done"`` and
#     ``"completed"`` for the same concept.
# Keep all three keys mapped to the same glyph so the renderer never
# falls through to the raw string (the source of the historical
# ``"complete complete"`` double-text bug).
STATUS_ICONS: Dict[str, str] = {
    "done": "[green]✓[/green]",
    "completed": "[green]✓[/green]",
    "complete": "[green]✓[/green]",
    "paused": "[yellow]⏸[/yellow]",
    "running": "[cyan]●[/cyan]",
    "failed": "[red]✗[/red]",
    "stale": "[dim]·[/dim]",
}
STATUS_ICONS_ASCII: Dict[str, str] = {
    "done": "OK",
    "completed": "OK",
    "complete": "OK",
    "paused": "PAUSED",
    "running": "RUN",
    "failed": "FAIL",
    "stale": "STALE",
}


# ---------------------------------------------------------------------------
# CLI registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Wire ``fluid agents`` into the CLI."""
    agents = subparsers.add_parser(
        COMMAND,
        help="Manage forge agent runs (list / show / prune)",
        description=(
            "Inspect and clean up the .fluid/agents/<run-id>/ artifact stack. "
            "Use 'list' to see runs, 'show' for detail, 'prune' to clean up."
        ),
    )
    sub = agents.add_subparsers(dest="subcommand")

    # --- list ---
    lst = sub.add_parser(
        "list",
        help="List recent forge runs",
        description=(
            "Walk .fluid/agents/<run-id>/ and render a table of runs "
            "(RUN_ID, AGE, STATUS, STAGES, COST, LAST_STAGE)."
        ),
    )
    lst.add_argument(
        "--incomplete",
        action="store_true",
        help="Only show paused or failed runs (skip completed).",
    )
    lst.add_argument(
        "--since",
        default=None,
        help="Restrict to runs newer than this (e.g. 7d, 24h, 2026-04-01).",
    )
    lst.add_argument(
        "--root",
        dest="root",
        help="Workspace root to scan (default: cwd).",
    )
    lst.add_argument(
        "--json",
        dest="emit_json",
        action="store_true",
        help="Emit machine-readable JSON instead of a table.",
    )
    lst.add_argument(
        "--no-trunc",
        dest="no_trunc",
        action="store_true",
        help=(
            "Force all columns to render even on narrow terminals. "
            "Without this flag, COST is dropped first below 100 cols, "
            "then STAGES below 80 cols (mirrors gh / kubectl behaviour)."
        ),
    )
    lst.add_argument(
        "--archived",
        dest="archived",
        action="store_true",
        help=(
            "List runs from .fluid/agents/.archived/ (the prune-archive bucket) "
            "instead of the live agents dir. Mirrors git stash list semantics."
        ),
    )
    lst.set_defaults(func=run, subcommand="list")

    # --- show ---
    show = sub.add_parser(
        "show",
        help="Show all details for one run",
        description=(
            "Print every stage record, cost breakdown, judge score, and "
            "paths to the on-disk receipts under .fluid/agents/<run-id>/."
        ),
    )
    show.add_argument("run_id", help="The run id to show (full or unambiguous prefix).")
    show.add_argument(
        "--root",
        dest="root",
        help="Workspace root to scan (default: cwd).",
    )
    show.add_argument(
        "--json",
        dest="emit_json",
        action="store_true",
        help="Emit machine-readable JSON instead of a table.",
    )
    show.set_defaults(func=run, subcommand="show")

    # --- prune ---
    prune = sub.add_parser(
        "prune",
        help="Archive (or permanently delete) old forge runs",
        description=(
            "ARCHIVE .fluid/agents/<run-id>/ directories older than the cutoff "
            "(default — reversible: moves to .fluid/agents/.archived/). "
            "Pass --delete for permanent removal. Defaults to 30 days; pass "
            "--older-than 7d for a tighter window. Prints bytes reclaimed."
        ),
    )
    prune.add_argument(
        "--older-than",
        dest="older_than",
        default=f"{DEFAULT_PRUNE_CUTOFF_DAYS}d",
        help=f"Cutoff (e.g. 30d, 24h). Default: {DEFAULT_PRUNE_CUTOFF_DAYS}d.",
    )
    prune.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Show what would be removed without touching disk.",
    )
    prune.add_argument(
        "--yes",
        "-y",
        dest="yes",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    prune.add_argument(
        "--run-id",
        dest="run_id",
        default=None,
        help="Prune a specific run-id (skip the age cutoff).",
    )
    prune.add_argument(
        "--root",
        dest="root",
        help="Workspace root to scan (default: cwd).",
    )
    # Mutually-exclusive: --archive (default behaviour, kept for
    # explicit/back-compat use) vs --delete (permanent rmtree).
    mode_group = prune.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--archive",
        dest="archive",
        action="store_true",
        default=False,
        help=(
            "(default) Move pruned runs to .fluid/agents/.archived/ — "
            "reversible. Mutually exclusive with --delete."
        ),
    )
    mode_group.add_argument(
        "--delete",
        dest="delete",
        action="store_true",
        default=False,
        help=(
            "PERMANENTLY delete pruned runs (rmtree, irreversible). "
            "Without this flag, prune defaults to archive."
        ),
    )
    prune.set_defaults(func=run, subcommand="prune")

    # Default behavior when no subcommand passed: print help.
    agents.set_defaults(func=run, subcommand=None)


def run(args: argparse.Namespace, logger: logging.Logger) -> int:
    """Execute ``fluid agents <subcommand>``."""
    sub = getattr(args, "subcommand", None)
    if sub == "list":
        return _run_list(args, logger)
    if sub == "show":
        return _run_show(args, logger)
    if sub == "prune":
        return _run_prune(args, logger)
    # No subcommand — print help.
    print(
        "Usage: fluid agents <subcommand>\n"
        "\n"
        "  list          List recent forge runs\n"
        "  show <id>     Show one run's stage records + cost + receipts\n"
        "  prune         Delete old run directories\n"
        "\n"
        "Run 'fluid agents <subcommand> --help' for details."
    )
    return 0


# ---------------------------------------------------------------------------
# `list` — table of recent runs
# ---------------------------------------------------------------------------


def _run_list(args: argparse.Namespace, logger: logging.Logger) -> int:
    root = Path(getattr(args, "root", None) or Path.cwd()).resolve()
    cutoff = _parse_since(getattr(args, "since", None))
    incomplete_only = bool(getattr(args, "incomplete", False))
    emit_json = bool(getattr(args, "emit_json", False))
    no_trunc = bool(getattr(args, "no_trunc", False))
    archived = bool(getattr(args, "archived", False))

    if archived:
        runs = list(_collect_archived_runs(root, cutoff=cutoff))
    else:
        runs = list(collect_runs(root, cutoff=cutoff))
    if incomplete_only:
        runs = [r for r in runs if r["status"] in ("paused", "failed", "running")]

    if emit_json:
        print(json.dumps({"runs": runs, "count": len(runs)}, indent=2, sort_keys=True, default=str))
        return 0

    _render_list_table(runs, root=root, no_trunc=no_trunc, archived=archived)
    # When not in --archived mode, surface a hint if there's anything
    # in the archive — operators reach for prune frequently and
    # benefit from the rolling-budget signal.
    if not archived:
        _maybe_render_archive_hint(root)
    return 0


def _render_list_table(
    runs: List[Dict[str, Any]],
    *,
    root: Path,
    no_trunc: bool = False,
    archived: bool = False,
) -> None:
    """Render the runs table — kubectl/gh-style columns + autocolor status.

    Responsive width strategy (mirrors ``gh run list`` + ``kubectl get
    pods``): when ``no_trunc`` is False, COST is dropped first when the
    terminal is < 100 cols, then STAGES is dropped < 80 cols. ``RUN_ID``,
    ``AGE``, ``STATUS``, ``LAST_STAGE`` are always rendered — losing
    those would defeat the listing's purpose.

    When ``archived`` is True, the title flips to ``ARCHIVED RUNS`` and
    the source-not-found message names the archive bucket.
    """
    title_suffix = " (archived)" if archived else ""
    source_label = ".fluid/agents/.archived/" if archived else ".fluid/agents/"

    width = _terminal_width()
    show_cost = no_trunc or width >= 100
    show_stages = no_trunc or width >= 80

    try:
        from rich.console import Console
        from rich.table import Table

        out = Console()
        if not runs:
            out.print(
                f"[dim]No forge runs found under {root}/{source_label}[/dim]\n"
                "[dim]Run 'fluid forge' to start one.[/dim]"
            )
            return
        t = Table(
            show_header=True,
            header_style="bold",
            title=(f"forge runs{title_suffix}" if archived else None),
        )
        t.add_column("RUN_ID", style="cyan", no_wrap=True)
        t.add_column("AGE", justify="right", no_wrap=True)
        t.add_column("STATUS", no_wrap=True)
        if show_stages:
            t.add_column("STAGES", justify="right", no_wrap=True)
        if show_cost:
            t.add_column("COST", justify="right", no_wrap=True)
        t.add_column("LAST_STAGE", no_wrap=True)
        for r in runs:
            status_icon = STATUS_ICONS.get(r["status"], r["status"])
            stages = (
                f"{r['stages_completed']}/{r['stages_total']}"
                if r.get("stages_total", 0) > 0
                else "-/-"
            )
            cost = f"${r['total_usd']:.4f}" if r.get("total_usd") is not None else "—"
            cells: List[str] = [
                _truncate(r["run_id"], 28),
                _format_age(r["age_seconds"]),
                f"{status_icon} {r['status']}",
            ]
            if show_stages:
                cells.append(stages)
            if show_cost:
                cells.append(cost)
            cells.append(r.get("last_stage") or "—")
            t.add_row(*cells)
        out.print(t)
    except Exception:  # noqa: BLE001 — rich is optional
        # Plain-text fallback for CI / no-rich environments.
        if not runs:
            print(f"No forge runs found under {root}/{source_label}")
            print("Run 'fluid forge' to start one.")
            return
        # Header — drop the same columns the rich path drops.
        header_parts = [f"{'RUN_ID':<28}", f"{'AGE':<6}", f"{'STATUS':<10}"]
        if show_stages:
            header_parts.append(f"{'STAGES':<7}")
        if show_cost:
            header_parts.append(f"{'COST':<10}")
        header_parts.append("LAST_STAGE")
        print(" ".join(header_parts))
        for r in runs:
            stages = (
                f"{r['stages_completed']}/{r['stages_total']}"
                if r.get("stages_total", 0) > 0
                else "-/-"
            )
            cost = f"${r['total_usd']:.4f}" if r.get("total_usd") is not None else "—"
            row_parts = [
                f"{_truncate(r['run_id'], 28):<28}",
                f"{_format_age(r['age_seconds']):<6}",
                f"{STATUS_ICONS_ASCII.get(r['status'], r['status']):<10}",
            ]
            if show_stages:
                row_parts.append(f"{stages:<7}")
            if show_cost:
                row_parts.append(f"{cost:<10}")
            row_parts.append(r.get("last_stage") or "—")
            print(" ".join(row_parts))


# ---------------------------------------------------------------------------
# `show` — one run's details
# ---------------------------------------------------------------------------


def _run_show(args: argparse.Namespace, logger: logging.Logger) -> int:
    root = Path(getattr(args, "root", None) or Path.cwd()).resolve()
    run_id_query = args.run_id
    emit_json = bool(getattr(args, "emit_json", False))

    matches = _resolve_run_id(root, run_id_query)
    if not matches:
        print(f"No run found matching '{run_id_query}' under {root}/.fluid/agents/")
        return 1
    if len(matches) > 1:
        print(f"Ambiguous run id '{run_id_query}'. Candidates:")
        for m in matches:
            print(f"  {m}")
        return 1

    run_dir = matches[0]
    detail = _load_run_detail(run_dir)

    if emit_json:
        print(json.dumps(detail, indent=2, sort_keys=True, default=str))
        return 0

    _render_show(detail, run_dir=run_dir)
    return 0


def _render_show(detail: Dict[str, Any], *, run_dir: Path) -> None:
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table

        out = Console()
        status_icon = STATUS_ICONS.get(detail["status"], detail["status"])
        header = (
            f"[bold]Run {detail['run_id']}[/bold]\n"
            f"Status: {status_icon} {detail['status']}\n"
            f"Started: {detail.get('started_iso', '—')}\n"
            f"Last update: {detail.get('updated_iso', '—')}"
        )
        out.print(Panel(header, border_style="cyan"))

        # Stages table
        stages = detail.get("stages") or []
        if stages:
            t = Table(title="Stages", show_header=True, header_style="bold")
            t.add_column("#", justify="right")
            t.add_column("STAGE")
            t.add_column("STATUS")
            t.add_column("DURATION", justify="right")
            t.add_column("COST", justify="right")
            for i, s in enumerate(stages, 1):
                s_icon = STATUS_ICONS.get(s.get("status", "stale"), s.get("status", "—"))
                dur = _format_duration(s.get("duration_seconds"))
                cost = f"${s.get('cost_usd'):.4f}" if s.get("cost_usd") is not None else "—"
                t.add_row(
                    str(i),
                    s.get("name", "—"),
                    f"{s_icon} {s.get('status', '—')}",
                    dur,
                    cost,
                )
            out.print(t)

        # Cost block
        cost = detail.get("cost") or {}
        if cost:
            out.print(
                f"[bold]Total cost:[/bold] ${cost.get('total_usd', 0.0) or 0:.4f}  "
                f"[dim]{cost.get('total_tokens', 0):,} tokens · "
                f"{cost.get('wall_clock_seconds', 0):.1f}s wall-clock[/dim]"
            )
        # Judge block
        judge = detail.get("judge")
        if judge:
            total = judge.get("total")
            axes = judge.get("axes") or {}
            max_total = (len(axes) or 6) * 5
            out.print(
                f"[bold]Judge score:[/bold] {total}/{max_total} "
                f"[dim]({judge.get('model', 'unknown')})[/dim]"
            )
            for axis, score in axes.items():
                # Real judge.json values are dicts with the shape
                # ``{"score": N, "reasoning": ..., "suggestions": [...]}``
                # but older / synthetic writers emit bare ints. Accept
                # both — extract ``.score`` when present, otherwise use
                # the value verbatim.
                score_value = score.get("score") if isinstance(score, dict) else score
                out.print(f"  [dim]{axis:<14}[/dim] {score_value}/5")

        # Receipts paths
        out.print("[bold]Receipts:[/bold]")
        for label, fname in (
            ("cost", "cost.json"),
            ("reasoning", "reasoning.md"),
            ("transcript", "transcript.json"),
            ("judge", "judge.json"),
            ("checkpoint", "checkpoint.json"),
        ):
            p = run_dir / fname
            if p.is_file():
                out.print(f"  [dim]{label:<10}[/dim] {p}")
    except Exception:  # noqa: BLE001
        print(f"Run {detail['run_id']}")
        print(f"Status: {detail['status']}")
        for s in detail.get("stages") or []:
            print(
                f"  - {s.get('name', '—')}: {s.get('status', '—')} "
                f"({_format_duration(s.get('duration_seconds'))})"
            )
        cost = detail.get("cost") or {}
        if cost.get("total_usd") is not None:
            print(f"Total cost: ${cost['total_usd']:.4f}")
        print(f"Run directory: {run_dir}")


# ---------------------------------------------------------------------------
# `prune` — delete old runs
# ---------------------------------------------------------------------------


def _run_prune(args: argparse.Namespace, logger: logging.Logger) -> int:
    root = Path(getattr(args, "root", None) or Path.cwd()).resolve()
    older_than = getattr(args, "older_than", f"{DEFAULT_PRUNE_CUTOFF_DAYS}d")
    dry_run = bool(getattr(args, "dry_run", False))
    yes = bool(getattr(args, "yes", False))
    # New semantics — archive is the safe default; --delete is opt-in.
    # The legacy --archive flag was always opt-in (default delete). We
    # invert: archive is implicit unless --delete is passed. The
    # mutually-exclusive group on the parser enforces "not both"; here
    # we just pick the prevailing mode.
    delete_mode = bool(getattr(args, "delete", False))
    archive_mode = not delete_mode  # default-on
    target_run_id = getattr(args, "run_id", None)

    cutoff_delta = _parse_duration(older_than)
    if cutoff_delta is None:
        print(f"Invalid --older-than value: {older_than!r}. Use forms like 30d, 24h.")
        return 1
    cutoff = datetime.now(timezone.utc) - cutoff_delta

    candidates: List[Tuple[Path, int]] = []
    for run_dir in _iter_run_dirs(root):
        if target_run_id is not None:
            if run_dir.name != target_run_id and not run_dir.name.startswith(target_run_id):
                continue
        else:
            ts = _parse_run_timestamp(run_dir.name)
            if ts is None:
                continue
            if ts > cutoff:
                continue
        size = _dir_size(run_dir)
        candidates.append((run_dir, size))

    if not candidates:
        print(f"No runs older than {older_than} found under {root}/.fluid/agents/.")
        return 0

    total_bytes = sum(s for _, s in candidates)

    print(
        f"Found {len(candidates)} run"
        f"{'s' if len(candidates) != 1 else ''} "
        f"({_format_bytes(total_bytes)})"
    )
    for run_dir, size in candidates:
        print(f"  {run_dir.name}  ({_format_bytes(size)})  {run_dir}")

    if dry_run:
        print("\n[dry-run] No files removed.")
        return 0

    if not yes:
        # Word the prompt according to whichever mode prevails so the
        # operator sees the destructive variant in red/bold when --delete
        # is set. Mirrors docker / kubectl conventions where any
        # irreversible action shouts.
        if not _confirm_prune_prompt(len(candidates), archive=archive_mode):
            print("Aborted.")
            return 1

    reclaimed = 0
    archived_root: Optional[Path] = None
    if archive_mode:
        archived_root = root / ".fluid" / "agents" / ".archived"
        archived_root.mkdir(parents=True, exist_ok=True)

    # Try to delegate to the peer's CheckpointStore.discard for runs
    # written via the canonical layout — it already does the
    # collision-safe rename and skips empty dirs. Fall back to the
    # local move/rmtree path when the peer module isn't importable or
    # the run wasn't written by it.
    saver = None
    if archive_mode:
        try:
            from fluid_build.copilot.checkpoint import (  # type: ignore[import-not-found]
                FileCheckpointStore,
            )

            saver = FileCheckpointStore(workspace_root=root)
        except Exception as exc:  # noqa: BLE001
            LOG.debug("checkpoint_store_unavailable_for_discard: %s", exc)

    for run_dir, size in candidates:
        try:
            if archive_mode:
                _archive_one(run_dir, archived_root=archived_root, saver=saver)
            else:
                # PERMANENT removal (rmtree) — only reached via --delete.
                shutil.rmtree(run_dir, ignore_errors=False)
            reclaimed += size
        except Exception as exc:  # noqa: BLE001
            logger.warning("prune_failed: %s — %s", run_dir, exc)
            print(f"  warn: failed to prune {run_dir}: {exc}")

    if archive_mode:
        print(
            f"\nArchived {len(candidates)} run"
            f"{'s' if len(candidates) != 1 else ''} to {archived_root}/  "
            f"({_format_bytes(reclaimed)})"
        )
        # Trailing reclaimed line — mirrors docker prune, works in
        # both archive + delete modes per the spec.
        print(f"Total reclaimed space: {_format_bytes(reclaimed)}")
    else:
        # Mirrors docker system prune's trailing line.
        print(f"\nTotal reclaimed space: {_format_bytes(reclaimed)}")
    return 0


def _archive_one(
    run_dir: Path,
    *,
    archived_root: Optional[Path],
    saver: Any = None,
) -> None:
    """Move a single run_dir to ``.archived/``.

    Prefers ``CheckpointStore.discard`` (peer-canonical, handles the
    collision-safe rename centrally). Falls back to a local
    ``shutil.move`` when the saver isn't available — the result on
    disk is identical.
    """
    if saver is not None:
        try:
            saver.discard(run_dir.name)
            return
        except Exception as exc:  # noqa: BLE001
            LOG.debug("checkpoint_store_discard_failed_falling_back: %s", exc)

    assert archived_root is not None
    dest = archived_root / run_dir.name
    if dest.exists():
        dest = archived_root / f"{run_dir.name}-{_timestamp_suffix()}"
    shutil.move(str(run_dir), str(dest))


def _confirm_prune_prompt(n: int, *, archive: bool) -> bool:
    """Prompt the operator and return True on y/yes, False otherwise.

    Wording distinguishes archive (safe, reversible) from delete
    (PERMANENT). With ``rich`` available the destructive variant
    renders in bold-red so the operator never confuses the two.
    """
    plural = "s" if n != 1 else ""
    if archive:
        question = f"\nArchive these {n} run{plural}? [y/N]: "
        try:
            from rich.console import Console

            Console().print(
                f"\n[bold]Archive these {n} run{plural}?[/bold] [dim]\\[y/N][/dim]: ",
                end="",
            )
            try:
                ans = input("").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return False
        except Exception:  # noqa: BLE001
            try:
                ans = input(question).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return False
    else:
        question = f"\n**PERMANENTLY DELETE** these {n} run{plural}? [y/N]: "
        try:
            from rich.console import Console

            Console().print(
                f"\n[bold red]PERMANENTLY DELETE these {n} run{plural}?[/bold red] "
                f"[dim]\\[y/N][/dim]: ",
                end="",
            )
            try:
                ans = input("").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return False
        except Exception:  # noqa: BLE001
            try:
                ans = input(question).strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                return False
    return ans in ("y", "yes")


# ---------------------------------------------------------------------------
# Filesystem discovery — independent of the peer-agent checkpoint module
# ---------------------------------------------------------------------------


def collect_runs(root: Path, *, cutoff: Optional[datetime] = None) -> Iterable[Dict[str, Any]]:
    """Yield one summary dict per ``.fluid/agents/<run-id>/`` directory.

    Tries the canonical :class:`fluid_build.copilot.checkpoint.CheckpointStore`
    first; falls back to direct filesystem scan when the peer-agent module
    isn't importable (so this command works during bootstrap and tests).

    Sorted most-recent first.
    """
    runs: List[Dict[str, Any]] = []
    seen_ids: set = set()

    # Try the canonical store first.
    try:
        # The peer-agent checkpoint module is optional — falls back to
        # filesystem scan when not importable (bootstrap, tests).
        from fluid_build.copilot.checkpoint import (  # type: ignore[import-not-found]
            get_default_saver,
        )

        saver = get_default_saver(workspace_root=root)
        for summary in saver.list_runs():
            run_id = _summary_field(summary, "run_id")
            if not run_id or run_id in seen_ids:
                continue
            seen_ids.add(run_id)
            run_dir = root / ".fluid" / "agents" / run_id
            runs.append(_summary_to_record(summary, run_dir=run_dir))
    except Exception as exc:  # noqa: BLE001 — peer module optional
        LOG.debug("checkpoint_store_unavailable, using filesystem fallback: %s", exc)

    # Filesystem fallback: scan every directory the canonical store didn't.
    for run_dir in _iter_run_dirs(root):
        if run_dir.name in seen_ids or run_dir.name.startswith("."):
            continue
        rec = _scan_run_dir(run_dir)
        if rec is not None:
            runs.append(rec)
            seen_ids.add(rec["run_id"])

    now = datetime.now(timezone.utc)
    for r in runs:
        ts = _parse_run_timestamp(r["run_id"])
        if ts is not None:
            r["started_iso"] = ts.isoformat()
            r["age_seconds"] = max(0.0, (now - ts).total_seconds())
        else:
            r["age_seconds"] = r.get("age_seconds", 0.0)
        if cutoff and ts and ts < cutoff:
            r["_filter_out"] = True

    runs = [r for r in runs if not r.get("_filter_out")]
    runs.sort(key=lambda r: r.get("age_seconds", 0))
    return runs


def _iter_run_dirs(root: Path) -> Iterable[Path]:
    base = root / ".fluid" / "agents"
    if not base.is_dir():
        return []
    out = []
    for d in base.iterdir():
        if not d.is_dir():
            continue
        if d.name.startswith("."):
            # Skip .archived/ and other dotted directories.
            continue
        out.append(d)
    return out


def _iter_archived_run_dirs(root: Path) -> Iterable[Path]:
    """Yield run dirs inside ``.fluid/agents/.archived/``.

    The archive bucket is a flat ``<run-id>/`` layout — mirrors what
    ``CheckpointStore.discard`` produces. Sub-archives (timestamp
    suffixes added on collision) are surfaced as their own runs.
    """
    base = root / ".fluid" / "agents" / ".archived"
    if not base.is_dir():
        return []
    out = []
    for d in base.iterdir():
        if not d.is_dir():
            continue
        out.append(d)
    return out


def _collect_archived_runs(
    root: Path, *, cutoff: Optional[datetime] = None
) -> Iterable[Dict[str, Any]]:
    """Yield one summary dict per ``.fluid/agents/.archived/<run-id>/``.

    Mirrors :func:`collect_runs` but walks the archive bucket. The
    canonical CheckpointStore doesn't enumerate archived runs (by
    design — discarded means out-of-sight), so we walk the filesystem
    directly via :func:`_scan_run_dir`.
    """
    runs: List[Dict[str, Any]] = []
    seen_ids: set = set()
    for run_dir in _iter_archived_run_dirs(root):
        if run_dir.name in seen_ids:
            continue
        rec = _scan_run_dir(run_dir)
        if rec is None:
            # Surface even content-less dirs so ``--archived`` shows
            # the full restore-list rather than silently dropping
            # rows. The user can spot suspicious archives this way.
            rec = {
                "run_id": run_dir.name,
                "status": "stale",
                "stages_completed": 0,
                "stages_total": 0,
                "total_usd": None,
                "total_tokens": 0,
                "provider": "",
                "model": "",
                "last_stage": "",
            }
        runs.append(rec)
        seen_ids.add(run_dir.name)

    now = datetime.now(timezone.utc)
    for r in runs:
        # Strip any trailing -YYYYMMDDHHMMSS collision suffix so the
        # timestamp parser still works on archived names. We DON'T
        # mutate ``run_id`` — only the parse input.
        parse_target = r["run_id"]
        ts = _parse_run_timestamp(parse_target)
        if ts is not None:
            r["started_iso"] = ts.isoformat()
            r["age_seconds"] = max(0.0, (now - ts).total_seconds())
        else:
            r["age_seconds"] = r.get("age_seconds", 0.0)
        if cutoff and ts and ts < cutoff:
            r["_filter_out"] = True

    runs = [r for r in runs if not r.get("_filter_out")]
    runs.sort(key=lambda r: r.get("age_seconds", 0))
    return runs


def _archive_summary(root: Path) -> Tuple[int, int]:
    """Return ``(count, total_bytes)`` of the archive bucket.

    Used by the hint banner under ``list`` so operators see a rolling
    archive-budget signal without having to invoke ``--archived``.
    """
    base = root / ".fluid" / "agents" / ".archived"
    if not base.is_dir():
        return (0, 0)
    count = 0
    total = 0
    for d in base.iterdir():
        if not d.is_dir():
            continue
        count += 1
        total += _dir_size(d)
    return (count, total)


def _maybe_render_archive_hint(root: Path) -> None:
    """Print a one-line hint when archived runs exist.

    Mirrors ``docker system df``'s reclaimable-space line — gives the
    operator a nudge that the archive is filling up without forcing
    them to know about ``--archived`` upfront.
    """
    count, total = _archive_summary(root)
    if count == 0:
        return
    try:
        from rich.console import Console

        Console().print(
            f"[dim]Found {count} archived run{'s' if count != 1 else ''} taking "
            f"{_format_bytes(total)}. "
            f"Use 'fluid agents list --archived' to view.[/dim]"
        )
    except Exception:  # noqa: BLE001
        print(
            f"Found {count} archived run{'s' if count != 1 else ''} taking "
            f"{_format_bytes(total)}. "
            f"Use 'fluid agents list --archived' to view."
        )


def _terminal_width() -> int:
    """Best-effort detection of the terminal width.

    Mirrors gh CLI's ``TerminalWidth()`` — prefers ``rich.Console``'s
    detection (handles pipes, ``COLUMNS`` env, ``/dev/tty`` open), then
    falls back to ``shutil.get_terminal_size`` (stdlib, well-tested),
    then defaults to 100 so we render the full table by default rather
    than a column-stripped one.
    """
    try:
        from rich.console import Console

        return Console().size.width
    except Exception:  # noqa: BLE001
        pass
    try:
        import shutil as _shutil

        return _shutil.get_terminal_size((100, 24)).columns
    except Exception:  # noqa: BLE001
        return 100


def _scan_run_dir(run_dir: Path) -> Optional[Dict[str, Any]]:
    """Best-effort scrape of cost.json + checkpoint + .paused marker.

    PREFERS the canonical :mod:`fluid_build.copilot.checkpoint`
    layout — ``<run-dir>/checkpoints/manifest.json`` and
    ``<run-dir>/checkpoints/.paused`` — and falls back to the legacy
    flat layout (``<run-dir>/checkpoint.json`` + ``<run-dir>/.paused``)
    for runs written before the CheckpointStore landed.

    The two layouts differ in schema, not just location:

    * New (CheckpointStore manifest):
      ``{"completed_stages": [...], "last_stage", "total_cost_usd",
         "started_at", "status"}``
    * Legacy (filesystem fallback):
      ``{"stages_completed": int, "stages_total": int, "last_stage",
         "started_iso", "updated_iso", "stages": [...]}``

    The normalised dict we return is the **legacy field set** so
    everything downstream (``_render_list_table``, ``--json``) keeps
    working unchanged. ``stages_total`` for the new layout is filled
    from ``STAGE_NAMES`` (8 — the canonical pipeline length) when not
    otherwise known.
    """
    cost = _load_json(run_dir / "cost.json")

    # --- Try the canonical layout first. ---
    new_manifest = _load_json(run_dir / "checkpoints" / "manifest.json")
    new_paused = (run_dir / "checkpoints" / ".paused").is_file()
    if new_manifest is not None:
        completed_stages = list(new_manifest.get("completed_stages") or [])
        stages_total = _canonical_stages_total()
        status = str(new_manifest.get("status") or "running")
        last_stage = new_manifest.get("last_stage") or (
            completed_stages[-1] if completed_stages else ""
        )
        if new_paused:
            # The marker is the authoritative pause signal — it's
            # dropped by the SIGINT handler on graceful exit.
            status = "paused"
        # Prefer the manifest's total_cost_usd; fall back to cost.json.
        total_usd = new_manifest.get("total_cost_usd")
        if total_usd is None and cost is not None:
            total_usd = cost.get("total_usd")
        return {
            "run_id": run_dir.name,
            "status": status,
            "stages_completed": len(completed_stages),
            "stages_total": stages_total,
            "total_usd": total_usd,
            "total_tokens": (cost or {}).get("total_tokens") or 0,
            "provider": (cost or {}).get("provider") or "",
            "model": (cost or {}).get("model") or "",
            "last_stage": last_stage,
        }

    # --- Legacy flat layout (back-compat for pre-CheckpointStore runs). ---
    chk = _load_json(run_dir / "checkpoint.json")
    paused_marker = run_dir / ".paused"

    status = "stale"
    last_stage = ""
    stages_completed = 0
    stages_total = 0

    if chk:
        status = str(chk.get("status") or "stale")
        last_stage = chk.get("last_stage") or chk.get("current_stage") or ""
        stages_completed = int(chk.get("stages_completed") or 0)
        stages_total = int(chk.get("stages_total") or 0)

    if paused_marker.is_file():
        # The marker overrides whatever the checkpoint reports — it's
        # written by the SIGINT handler at exit and is authoritative.
        marker = _load_json(paused_marker)
        status = "paused"
        if marker:
            last_stage = marker.get("last_stage") or last_stage
            stages_completed = int(marker.get("stages_completed") or stages_completed)
            stages_total = int(marker.get("stages_total") or stages_total)

    # Nothing on disk at all — neither new nor legacy. Skip; otherwise
    # the listing fills with empty rows for unrelated directories.
    if not chk and not paused_marker.is_file() and cost is None:
        return None

    return {
        "run_id": run_dir.name,
        "status": status,
        "stages_completed": stages_completed,
        "stages_total": stages_total,
        "total_usd": (cost or {}).get("total_usd"),
        "total_tokens": (cost or {}).get("total_tokens") or 0,
        "provider": (cost or {}).get("provider") or "",
        "model": (cost or {}).get("model") or "",
        "last_stage": last_stage,
    }


def _canonical_stages_total() -> int:
    """Return the canonical full-pipeline stage count.

    Imports ``STAGE_NAMES`` lazily from
    :mod:`fluid_build.copilot.checkpoint` so this CLI works in
    isolation when the peer-agent module is unavailable (tests,
    bootstrap). Falls back to ``8`` — the pipeline length at the
    time of writing — when the import fails.
    """
    try:
        from fluid_build.copilot.checkpoint import STAGE_NAMES  # type: ignore[import-not-found]

        return len(STAGE_NAMES)
    except Exception:  # noqa: BLE001
        return 8


def _summary_field(summary: Any, name: str) -> Any:
    if isinstance(summary, dict):
        return summary.get(name)
    return getattr(summary, name, None)


def _summary_to_record(summary: Any, *, run_dir: Path) -> Dict[str, Any]:
    """Normalise a CheckpointStore ``RunSummary`` (dict or dataclass) to dict.

    The peer's :class:`~fluid_build.copilot.checkpoint.RunSummary` carries
    different field names than the legacy listing dict — translate them
    so the renderer sees a consistent shape:

    ======================  ====================
    RunSummary field        our listing key
    ======================  ====================
    ``total_cost_usd``      ``total_usd``
    ``started_at``          ``started_iso``
    ``completed_stages``    len → ``stages_completed``
    ======================  ====================
    """
    rec: Dict[str, Any] = {}
    for field in (
        "run_id",
        "status",
        "stages_completed",
        "stages_total",
        "total_usd",
        "total_tokens",
        "provider",
        "model",
        "last_stage",
    ):
        rec[field] = _summary_field(summary, field)

    # RunSummary → record field translations.
    if rec.get("total_usd") is None:
        total_cost = _summary_field(summary, "total_cost_usd")
        if total_cost is not None:
            rec["total_usd"] = total_cost
    if rec.get("stages_completed") is None:
        completed_stages = _summary_field(summary, "completed_stages")
        if completed_stages is not None:
            rec["stages_completed"] = len(completed_stages)
    if rec.get("stages_total") is None:
        rec["stages_total"] = _canonical_stages_total()
    started_at = _summary_field(summary, "started_at")
    if started_at:
        rec["started_iso"] = started_at

    # Fill in any holes from the run directory.
    cost = _load_json(run_dir / "cost.json") or {}
    rec.setdefault("total_usd", cost.get("total_usd"))
    rec.setdefault("total_tokens", cost.get("total_tokens", 0))
    rec.setdefault("provider", cost.get("provider", ""))
    rec.setdefault("model", cost.get("model", ""))

    # Pause-marker check honours BOTH layouts so a peer-written paused
    # run still flags correctly even when the manifest's status string
    # hasn't been updated yet.
    paused_new = (run_dir / "checkpoints" / ".paused").is_file()
    paused_legacy = (run_dir / ".paused").is_file()
    if (paused_new or paused_legacy) and rec.get("status") not in ("paused", "failed"):
        rec["status"] = "paused"
    if rec.get("status") is None:
        rec["status"] = "stale"
    rec["stages_completed"] = int(rec.get("stages_completed") or 0)
    rec["stages_total"] = int(rec.get("stages_total") or 0)
    return rec


def _load_run_detail(run_dir: Path) -> Dict[str, Any]:
    """Build the full detail dict for ``fluid agents show``.

    PREFERS the canonical CheckpointStore layout
    (``checkpoints/manifest.json`` + per-stage JSON files), falls back
    to the legacy flat ``checkpoint.json`` layout.

    The new layout doesn't carry an explicit ``stages`` array — one
    file per stage lives under ``checkpoints/<stage>.json``. We
    synthesise the show-friendly stage list from those files; each
    entry carries ``{"name", "status": "complete", "duration_seconds":
    None, "cost_usd"}``.
    """
    base = _scan_run_dir(run_dir) or {"run_id": run_dir.name, "status": "stale"}
    base["cost"] = _load_json(run_dir / "cost.json") or {}
    base["judge"] = _load_json(run_dir / "judge.json")

    # --- New layout first. ---
    new_manifest = _load_json(run_dir / "checkpoints" / "manifest.json")
    if new_manifest is not None:
        stages: List[Dict[str, Any]] = []
        completed_stages = list(new_manifest.get("completed_stages") or [])
        ckpt_dir = run_dir / "checkpoints"
        for stage_name in completed_stages:
            stage_record = _load_json(ckpt_dir / f"{stage_name}.json") or {}
            stages.append(
                {
                    "name": stage_name,
                    "status": "complete",
                    "duration_seconds": stage_record.get("duration_seconds"),
                    "cost_usd": stage_record.get("cost_usd"),
                    "completed_at": stage_record.get("completed_at", ""),
                }
            )
        base["stages"] = stages
        base["started_iso"] = new_manifest.get("started_at") or ""
        base["updated_iso"] = ""
        # Derive ``updated_iso`` from the most-recent stage's
        # completed_at, when available.
        if stages:
            most_recent = max((s.get("completed_at") or "") for s in stages)
            base["updated_iso"] = most_recent or ""
        return base

    # --- Legacy flat layout. ---
    chk = _load_json(run_dir / "checkpoint.json") or {}
    stages = chk.get("stages") or []
    base["stages"] = stages
    base["started_iso"] = chk.get("started_iso") or ""
    base["updated_iso"] = chk.get("updated_iso") or ""
    return base


def _resolve_run_id(root: Path, query: str) -> List[Path]:
    """Return run dirs matching ``query`` (exact name or prefix).

    Searches BOTH live ``.fluid/agents/`` and ``.fluid/agents/.archived/``
    so ``agents show <id>`` resolves an archived run-id without the
    operator having to remember which bucket it landed in.
    """
    matches: List[Path] = []
    for run_dir in _iter_run_dirs(root):
        if run_dir.name == query or run_dir.name.startswith(query):
            matches.append(run_dir)
    for run_dir in _iter_archived_run_dirs(root):
        if run_dir.name == query or run_dir.name.startswith(query):
            matches.append(run_dir)
    return matches


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001
        LOG.debug("agents_cmd_skip_unreadable: %s — %s", path, exc)
        return None


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


# ---------------------------------------------------------------------------
# Time + size formatters
# ---------------------------------------------------------------------------


def _parse_run_timestamp(run_id: str) -> Optional[datetime]:
    """run_id format: ``YYYYMMDD-HHMMSS-XXXXXX`` (UTC). Mirrors stats.py."""
    if len(run_id) < 15:
        return None
    try:
        return datetime.strptime(run_id[:15], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_since(spec: Optional[str]) -> Optional[datetime]:
    if not spec:
        return None
    delta = _parse_duration(spec)
    if delta is None:
        return None
    return datetime.now(timezone.utc) - delta


def _parse_duration(spec: Optional[str]) -> Optional[timedelta]:
    """Parse ``30d`` / ``24h`` / ``45m`` / ``600s``. Returns None on garbage."""
    if not spec:
        return None
    s = spec.strip().lower()
    if not s:
        return None
    try:
        unit = s[-1]
        n = int(s[:-1])
    except (ValueError, IndexError):
        # Maybe it's a bare ISO timestamp — handle in _parse_since via fromisoformat.
        try:
            dt = datetime.fromisoformat(s)
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) - dt
        except ValueError:
            return None
    if n < 0:
        return None
    if unit == "d":
        return timedelta(days=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "s":
        return timedelta(seconds=n)
    return None


def _format_age(seconds: float) -> str:
    """Compact age — '12m', '3h', '5d'. Mirrors kubectl AGE column."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60)}s"


def _format_bytes(n: int) -> str:
    """Mirrors docker's reclaimed-space formatter — B/kB/MB/GB."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} kB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _timestamp_suffix() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


__all__ = [
    "COMMAND",
    "DEFAULT_PRUNE_CUTOFF_DAYS",
    "collect_runs",
    "register",
    "run",
]
