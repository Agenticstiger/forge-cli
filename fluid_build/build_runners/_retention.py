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

"""Retention sweeper.

Reads top-level ``retention`` block: ``runState``, ``runLogs``, ``lineage``,
``dlq`` (each ISO-8601 duration). Walks the state-store tree and removes
records older than the configured horizon. Emits an audit log entry per
deletion.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

LOG = logging.getLogger("fluid.acquire.retention")

DEFAULTS = {
    "runState": "P30D",
    "runLogs": "P90D",
    "lineage": "P365D",
    "dlq": "P180D",
}

_ISO_DURATION_RE = re.compile(
    r"^P(?!$)(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)


def parse_iso_duration(s: str) -> timedelta:
    m = _ISO_DURATION_RE.match(s)
    if not m:
        raise ValueError(f"invalid ISO-8601 duration: {s}")
    years, months, weeks, days, hours, minutes, seconds = (int(g or 0) for g in m.groups())
    # Approximate years/months as days. Acceptable for retention windows.
    total_days = years * 365 + months * 30 + weeks * 7 + days
    return timedelta(days=total_days, hours=hours, minutes=minutes, seconds=seconds)


@dataclass
class RetentionConfig:
    run_state: timedelta
    run_logs: timedelta
    lineage: timedelta
    dlq: timedelta

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, str]]) -> "RetentionConfig":
        merged = dict(DEFAULTS)
        if d:
            merged.update({k: v for k, v in d.items() if v is not None})
        return cls(
            run_state=parse_iso_duration(merged["runState"]),
            run_logs=parse_iso_duration(merged["runLogs"]),
            lineage=parse_iso_duration(merged["lineage"]),
            dlq=parse_iso_duration(merged["dlq"]),
        )


@dataclass
class SweepResult:
    deleted_paths: List[Path]
    total_bytes: int


def _is_older_than(path: Path, horizon: timedelta, now: datetime) -> bool:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except FileNotFoundError:
        return False
    return (now - mtime) > horizon


def sweep_dir(root: Path, horizon: timedelta, now: Optional[datetime] = None) -> SweepResult:
    """Recursively delete files older than ``horizon`` under ``root``."""
    now = now or datetime.now(timezone.utc)
    deleted: List[Path] = []
    bytes_total = 0
    if not root.exists():
        return SweepResult(deleted, 0)
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if _is_older_than(f, horizon, now):
            try:
                bytes_total += f.stat().st_size
                f.unlink()
                deleted.append(f)
                LOG.info("retention.delete path=%s", f)
            except OSError as exc:
                LOG.warning("retention.delete-failed path=%s err=%s", f, exc)
    return SweepResult(deleted_paths=deleted, total_bytes=bytes_total)


def sweep_all(state_root: Path, config: RetentionConfig) -> Dict[str, SweepResult]:
    now = datetime.now(timezone.utc)
    return {
        "run_state": sweep_dir(state_root / "runs", config.run_state, now),
        "run_logs": sweep_dir(state_root / "logs", config.run_logs, now),
        "lineage": sweep_dir(state_root / "lineage", config.lineage, now),
        "dlq": sweep_dir(state_root / "dlq", config.dlq, now),
    }
