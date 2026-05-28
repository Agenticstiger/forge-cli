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
"""``fluid stats`` — aggregate cost / token / type stats across forge runs.

Walks ``.fluid/agents/*/cost.json`` under the workspace and rolls up the
totals. Optionally groups by provider, productType, or engine via
``--by``. The data source is the same artifact stack invariant **I5**
delivers, so ``fluid stats`` always reflects every run that's lived
through the preview panel.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

LOG = logging.getLogger("fluid.cli.stats")

COMMAND = "stats"


def register(subparsers: argparse._SubParsersAction) -> None:
    """Wire ``fluid stats`` into the CLI."""
    p = subparsers.add_parser(
        COMMAND,
        help="Aggregate cost / token usage across forge runs",
        description=(
            "Walks .fluid/agents/*/cost.json and rolls up totals. "
            "Use --since to restrict to recent runs and --by to group."
        ),
    )
    p.add_argument(
        "--since",
        help=("ISO date or relative spec (e.g. 7d, 30d, 24h). Default: last 30 days."),
        default="30d",
    )
    p.add_argument(
        "--by",
        choices=["provider", "type", "engine", "run", "mode"],
        default=None,
        help=(
            "Group results by this dimension. ``mode`` separates "
            "``deterministic`` runs from ``llm`` runs."
        ),
    )
    p.add_argument(
        "--root",
        dest="root",
        help="Workspace root to scan (default: cwd).",
    )
    p.add_argument(
        "--json",
        dest="emit_json",
        action="store_true",
        help="Emit machine-readable JSON instead of a table.",
    )
    p.add_argument(
        "--judge",
        dest="judge",
        action="store_true",
        help=(
            "Aggregate judge.json receipts (out-of-loop LLM-as-judge scores) "
            "instead of cost.json. Not combinable with --by — group manually "
            "from the --json output instead."
        ),
    )
    # Wire the dispatcher — `ProductionCLI._execute_command` looks up
    # `args.func`. Without this, `fluid stats` errors with "No command
    # function found" instead of running. Pinned by
    # tests/cli/test_subcommand_dispatch_smoke.py.
    p.set_defaults(func=run)


def run(args, logger: logging.Logger) -> int:
    """Execute ``fluid stats``."""
    root = Path(args.root or Path.cwd()).resolve()
    try:
        cutoff = _parse_since(args.since)
    except ValueError as exc:
        # H21: malformed --since gets a clean argparse-style error and
        # non-zero exit instead of silently no-filtering.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if getattr(args, "judge", False):
        # H20: reject ``--judge --by X`` rather than silently dropping
        # ``--by``. Grouping judge axes is a separate (non-trivial) UX
        # surface; until then, document the rejection and point at the
        # ``--json`` escape hatch.
        if getattr(args, "by", None):
            print(
                f"error: --judge --by {args.by} is not yet supported. "
                "Group manually from the --json output (each run carries "
                "axes + total + run_id).",
                file=sys.stderr,
            )
            return 2
        judge_runs = list(_collect_judge_runs(root, cutoff=cutoff))
        summary = _summarise_judge(judge_runs)
        if args.emit_json:
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 0
        _render_judge_table(summary, runs_count=len(judge_runs))
        return 0

    runs = list(_collect_runs(root, cutoff=cutoff))
    summary = _summarise(runs, group_by=args.by)
    if args.emit_json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    _render_table(summary, group_by=args.by, runs_count=len(runs))
    return 0


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _collect_runs(root: Path, *, cutoff: Optional[datetime]) -> Iterable[Dict[str, Any]]:
    """Yield one record per ``cost.json`` found under ``.fluid/agents/``."""
    for cost_path in root.rglob(".fluid/agents/*/cost.json"):
        try:
            data = json.loads(cost_path.read_text(encoding="utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            LOG.debug("stats_skip_unreadable: %s — %s", cost_path, exc)
            continue
        run_dir = cost_path.parent
        run_id = run_dir.name
        ts = _parse_run_timestamp(run_id)
        if cutoff and ts and ts < cutoff:
            continue
        receipt = _load_receipt(run_dir)
        contract = _load_contract_for_run(run_dir)
        # H22 — surface ``mode`` so deterministic runs (no LLM call)
        # are distinguishable from LLM runs in ``fluid stats --by mode``
        # output. Older cost.json files (preview-panel-authored) don't
        # carry ``mode`` — default to ``"llm"`` when tokens were spent,
        # otherwise ``"deterministic"`` so the dimension is always
        # populated even for receipts predating this field.
        mode_value = data.get("mode")
        if not mode_value:
            mode_value = (
                "deterministic"
                if int(data.get("total_tokens", 0) or 0) == 0
                and int(data.get("total_calls", 0) or 0) == 0
                else "llm"
            )
        yield {
            "run_id": run_id,
            "timestamp": ts.isoformat() if ts else "",
            "provider": data.get("provider", ""),
            "model": data.get("model", ""),
            "input_tokens": int(data.get("input_tokens", 0) or 0),
            "output_tokens": int(data.get("output_tokens", 0) or 0),
            "total_tokens": int(data.get("total_tokens", 0) or 0),
            "total_usd": data.get("total_usd"),
            "wall_clock_seconds": float(data.get("wall_clock_seconds", 0) or 0),
            "product_type": (contract or {}).get("metadata", {}).get("productType", ""),
            "layer": (contract or {}).get("metadata", {}).get("layer", ""),
            "engine": (contract or {}).get("builds", [{}])[0].get("engine", ""),
            "files_count": len((receipt or {}).get("files_written", []) or []),
            "mode": mode_value,
        }


def _parse_run_timestamp(run_id: str) -> Optional[datetime]:
    """``run_id`` format: YYYYMMDD-HHMMSS-XXXXXX (UTC)."""
    if len(run_id) < 15:
        return None
    try:
        return datetime.strptime(run_id[:15], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_since(spec: Optional[str]) -> Optional[datetime]:
    """Parse a since-spec like ``30d`` / ``24h`` / ``2026-04-01``.

    H21: malformed input raises :class:`ValueError` with a one-line
    example so the user gets immediate feedback instead of silently
    seeing every run unfiltered.  Empty / ``None`` legitimately means
    "no cutoff" — those still return ``None``.  Mirrors the pattern in
    :mod:`fluid_build.cli.memory_cmd` for ``--older-than``.
    """
    if not spec:
        return None
    raw = spec
    spec = spec.strip().lower()
    if not spec:
        return None
    now = datetime.now(timezone.utc)
    if spec.endswith("d"):
        try:
            return now - _delta_days(int(spec[:-1]))
        except ValueError as exc:
            raise ValueError(
                f"--since must look like '30d', '24h', or an ISO date "
                f"(e.g. 2026-04-01); got {raw!r}"
            ) from exc
    if spec.endswith("h"):
        try:
            return now - _delta_hours(int(spec[:-1]))
        except ValueError as exc:
            raise ValueError(
                f"--since must look like '30d', '24h', or an ISO date "
                f"(e.g. 2026-04-01); got {raw!r}"
            ) from exc
    try:
        dt = datetime.fromisoformat(spec)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(
            f"--since must look like '30d', '24h', or an ISO date "
            f"(e.g. 2026-04-01); got {raw!r}"
        ) from exc


def _delta_days(n: int):
    from datetime import timedelta

    return timedelta(days=max(0, n))


def _delta_hours(n: int):
    from datetime import timedelta

    return timedelta(hours=max(0, n))


def _load_receipt(run_dir: Path) -> Optional[Dict[str, Any]]:
    receipt = run_dir.parent.parent / "forge-receipt.json"
    if receipt.is_file():
        try:
            return json.loads(receipt.read_text(encoding="utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return None
    return None


def _load_contract_for_run(run_dir: Path) -> Optional[Dict[str, Any]]:
    """Walk back to the product directory and read its contract.fluid.yaml."""
    contract_path = run_dir.parent.parent / "contract.fluid.yaml"
    if not contract_path.is_file():
        contract_path = run_dir.parent.parent.parent / "contract.fluid.yaml"
    if contract_path.is_file():
        try:
            import yaml as _yaml

            return _yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return None
    return None


# ---------------------------------------------------------------------------
# Aggregation + rendering
# ---------------------------------------------------------------------------


def _summarise(runs: List[Dict[str, Any]], *, group_by: Optional[str]) -> Dict[str, Any]:
    overall = _zero_aggregate()
    by_group: Dict[str, Dict[str, Any]] = defaultdict(_zero_aggregate)
    for run in runs:
        _accumulate(overall, run)
        if group_by:
            key = run.get(_dim_key(group_by)) or "(unknown)"
            _accumulate(by_group[key], run)
    out: Dict[str, Any] = {"total": overall, "runs_count": len(runs)}
    if group_by:
        out["by"] = group_by
        out["groups"] = {k: by_group[k] for k in sorted(by_group)}
    return out


def _zero_aggregate() -> Dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "total_usd": 0.0,
        "wall_clock_seconds": 0.0,
        "runs": 0,
    }


def _accumulate(bucket: Dict[str, Any], run: Dict[str, Any]) -> None:
    bucket["input_tokens"] += run["input_tokens"]
    bucket["output_tokens"] += run["output_tokens"]
    bucket["total_tokens"] += run["total_tokens"]
    if run["total_usd"] is not None:
        bucket["total_usd"] = round(bucket["total_usd"] + float(run["total_usd"]), 4)
    bucket["wall_clock_seconds"] += run["wall_clock_seconds"]
    bucket["runs"] += 1


def _dim_key(group_by: str) -> str:
    return {
        "provider": "provider",
        "type": "product_type",
        "engine": "engine",
        "run": "run_id",
        "mode": "mode",
    }.get(group_by, group_by)


def _render_table(summary: Dict[str, Any], *, group_by: Optional[str], runs_count: int) -> None:
    try:
        from rich.console import Console
        from rich.table import Table

        out = Console()
        total = summary["total"]
        out.print(
            f"[bold]fluid stats[/bold] — {runs_count} run"
            f"{'s' if runs_count != 1 else ''} · "
            f"{total['total_tokens']:,} tokens · "
            f"${total['total_usd']:.4f} · "
            f"{total['wall_clock_seconds']:.1f}s wall-clock"
        )
        if group_by and "groups" in summary:
            t = Table()
            t.add_column(group_by.title())
            t.add_column("Runs", justify="right")
            t.add_column("Tokens", justify="right")
            t.add_column("USD", justify="right")
            for key, bucket in summary["groups"].items():
                t.add_row(
                    key,
                    str(bucket["runs"]),
                    f"{bucket['total_tokens']:,}",
                    f"${bucket['total_usd']:.4f}",
                )
            out.print(t)
    except Exception:  # noqa: BLE001
        total = summary["total"]
        print(
            f"fluid stats — {runs_count} runs · {total['total_tokens']} tokens · "
            f"${total['total_usd']:.4f}"
        )
        if group_by and "groups" in summary:
            for key, bucket in summary["groups"].items():
                print(
                    f"  {key:<24}  runs={bucket['runs']}  "
                    f"tokens={bucket['total_tokens']}  "
                    f"usd=${bucket['total_usd']:.4f}"
                )


# ---------------------------------------------------------------------------
# Judge aggregation — parallel surface that walks judge.json receipts.
# Out-of-loop LLM-as-judge scores live at ``.fluid/agents/<run-id>/judge.json``
# (see :mod:`fluid_build.copilot.agents.judge_agent`).
# ---------------------------------------------------------------------------


def _collect_judge_runs(root: Path, *, cutoff: Optional[datetime]) -> Iterable[Dict[str, Any]]:
    """Yield one record per ``judge.json`` found under ``.fluid/agents/``."""
    for judge_path in root.rglob(".fluid/agents/*/judge.json"):
        try:
            data = json.loads(judge_path.read_text(encoding="utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            LOG.debug("stats_skip_unreadable_judge: %s — %s", judge_path, exc)
            continue
        run_dir = judge_path.parent
        run_id = run_dir.name
        ts = _parse_run_timestamp(run_id)
        if cutoff and ts and ts < cutoff:
            continue
        axes = data.get("axes") or {}
        # Axes can be either a flat ``{axis: int}`` or a structured
        # ``{axis: {score, reasoning, suggestions}}``. Normalise to flat.
        flat_axes: Dict[str, int] = {}
        for axis, payload in axes.items():
            if isinstance(payload, dict):
                flat_axes[axis] = int(payload.get("score", 0) or 0)
            else:
                try:
                    flat_axes[axis] = int(payload or 0)
                except (TypeError, ValueError):
                    flat_axes[axis] = 0
        yield {
            "run_id": run_id,
            "timestamp": ts.isoformat() if ts else "",
            "total": int(data.get("total", sum(flat_axes.values())) or 0),
            "model": data.get("model", ""),
            "axes": flat_axes,
        }


def _summarise_judge(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Roll up judge scores: average per axis, average total, count."""
    if not runs:
        return {"runs_count": 0, "axes": {}, "average_total": 0.0}
    axes_sum: Dict[str, int] = defaultdict(int)
    axes_count: Dict[str, int] = defaultdict(int)
    total_sum = 0
    for r in runs:
        total_sum += r["total"]
        for axis, score in r["axes"].items():
            axes_sum[axis] += score
            axes_count[axis] += 1
    axes_avg = {
        axis: round(axes_sum[axis] / axes_count[axis], 2) if axes_count[axis] else 0.0
        for axis in axes_sum
    }
    return {
        "runs_count": len(runs),
        "average_total": round(total_sum / len(runs), 2),
        "axes": axes_avg,
        "latest_run": runs[-1] if runs else None,
    }


def _render_judge_table(summary: Dict[str, Any], *, runs_count: int) -> None:
    try:
        from rich.console import Console
        from rich.table import Table

        out = Console()
        out.print(
            f"[bold]fluid stats --judge[/bold] — {runs_count} run"
            f"{'s' if runs_count != 1 else ''} · "
            f"avg total {summary['average_total']:.2f}/30"
        )
        if summary.get("axes"):
            t = Table()
            t.add_column("Axis")
            t.add_column("Average (0..5)", justify="right")
            for axis in sorted(summary["axes"]):
                t.add_row(axis, f"{summary['axes'][axis]:.2f}")
            out.print(t)
    except Exception:  # noqa: BLE001
        print(
            f"fluid stats --judge — {runs_count} runs · "
            f"avg total {summary['average_total']:.2f}/30"
        )
        for axis in sorted(summary.get("axes", {})):
            print(f"  {axis:<16}  {summary['axes'][axis]:.2f}")


__all__ = ["COMMAND", "register", "run"]
