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

"""``fluid status <product-id>`` — render a per-product summary.

Reads run records from the ``StateStore`` and computes:
- Last N run rows (run id, started, status, records, duration, error)
- Freshness (age of latest succeeded run)
- Lag (now - cursor watermark when available)
- Error rate over the last 24h
- Per-stream counts on the most recent run
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.api.runner import RunState
from fluid_build.api.state import StateStore


@dataclass
class RunSummary:
    run_id: str
    state: str
    started_at: Optional[str]
    finished_at: Optional[str]
    records_total: int
    duration_seconds: float
    error: Optional[str] = None
    streams: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class StatusReport:
    product_id: str
    build_id: str
    runs: List[RunSummary]
    freshness_seconds: Optional[float] = None
    error_rate_24h: float = 0.0  # 0..1
    last_state: Optional[str] = None
    facets: Dict[str, Any] = field(default_factory=dict)


def build_status_report(
    state_store: StateStore,
    product_id: str,
    build_id: str,
    *,
    limit: int = 5,
    now: Optional[datetime] = None,
) -> StatusReport:
    """Compose a ``StatusReport`` from the StateStore.

    Pure function: testable without filesystem state if a fake StateStore
    yields the right shape.
    """
    now = now or datetime.now(timezone.utc)
    records = state_store.list_runs(product_id, build_id, limit=max(limit, 100))
    runs = [_to_summary(r) for r in records[:limit]]

    freshness_seconds = _compute_freshness(records, now)
    error_rate_24h = _compute_error_rate_24h(records, now)
    last_state = records[0].get("state") if records else None
    return StatusReport(
        product_id=product_id,
        build_id=build_id,
        runs=runs,
        freshness_seconds=freshness_seconds,
        error_rate_24h=error_rate_24h,
        last_state=last_state,
        facets={"total_runs_seen": len(records)},
    )


# ── Helpers ────────────────────────────────────────────────────────────


def _to_summary(record: Dict[str, Any]) -> RunSummary:
    facets = record.get("facets") or {}
    duration = facets.get("duration_seconds", 0.0)
    if not duration and record.get("started_at") and record.get("finished_at"):
        try:
            duration = (
                _parse_iso(record["finished_at"]) - _parse_iso(record["started_at"])
            ).total_seconds()
        except Exception:  # noqa: BLE001
            duration = 0.0
    return RunSummary(
        run_id=record.get("run_id", "?"),
        state=record.get("state", "unknown"),
        started_at=record.get("started_at"),
        finished_at=record.get("finished_at"),
        records_total=int(record.get("records_total") or 0),
        duration_seconds=float(duration or 0.0),
        error=record.get("error"),
        streams=record.get("streams") or [],
    )


def _compute_freshness(records: List[Dict[str, Any]], now: datetime) -> Optional[float]:
    for r in records:
        if r.get("state") == RunState.SUCCEEDED.value:
            ts = r.get("finished_at") or r.get("started_at")
            if ts:
                try:
                    return (now - _parse_iso(ts)).total_seconds()
                except Exception:  # noqa: BLE001
                    return None
    return None


def _compute_error_rate_24h(records: List[Dict[str, Any]], now: datetime) -> float:
    cutoff = now - timedelta(hours=24)
    recent: List[str] = []
    for r in records:
        ts = r.get("finished_at") or r.get("started_at")
        if not ts:
            continue
        try:
            t = _parse_iso(ts)
        except Exception:  # noqa: BLE001
            continue
        if t < cutoff:
            continue
        recent.append(r.get("state", "unknown"))
    if not recent:
        return 0.0
    failures = sum(1 for s in recent if s in (RunState.FAILED.value, RunState.PARTIAL.value))
    return failures / len(recent)


def _parse_iso(s: str) -> datetime:
    # Accept the runner's canonical "...Z" format and full ISO-8601.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)
