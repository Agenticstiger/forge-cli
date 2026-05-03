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
        choices=["provider", "type", "engine", "run"],
        default=None,
        help="Group results by this dimension.",
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


def run(args, logger: logging.Logger) -> int:
    """Execute ``fluid stats``."""
    root = Path(args.root or Path.cwd()).resolve()
    cutoff = _parse_since(args.since)
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
    """Parse a since-spec like ``30d`` / ``24h`` / ``2026-04-01``."""
    if not spec:
        return None
    spec = spec.strip().lower()
    now = datetime.now(timezone.utc)
    if spec.endswith("d"):
        try:
            return now - _delta_days(int(spec[:-1]))
        except ValueError:
            return None
    if spec.endswith("h"):
        try:
            return now - _delta_hours(int(spec[:-1]))
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(spec)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


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


__all__ = ["COMMAND", "register", "run"]
