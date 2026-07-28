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

"""In-memory cost tracker that satisfies ``api.cost.CostTracker``.

OTel metric emission is wired in via callbacks; if no observer is set, the
tracker just accumulates locally. Budget gate runs *before* bytes are read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from fluid_build.api.cost import BudgetCap, ChargebackTag, CostTracker

_BYTES_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTP]?B)\s*$", re.IGNORECASE)
_UNIT = {"B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12, "PB": 10**15}


def parse_bytes(s: Optional[str]) -> Optional[int]:
    if s is None:
        return None
    if isinstance(s, int):
        return s
    m = _BYTES_RE.match(str(s))
    if not m:
        return None
    return int(float(m.group(1)) * _UNIT[m.group(2).upper()])


@dataclass
class InMemoryCostTracker(CostTracker):
    chargeback: Optional[ChargebackTag] = None
    on_record: Optional[Callable[[str, float], None]] = None  # (metric_name, value)
    _records: int = 0
    _bytes_read: int = 0
    _bytes_written: int = 0
    _compute_seconds: float = 0.0

    def check_budget(self, cap: BudgetCap, current_usage: Dict[str, int]) -> bool:
        if cap.rows is not None and current_usage.get("rows", 0) > cap.rows:
            return False
        if cap.bytes is not None and current_usage.get("bytes", 0) > cap.bytes:
            return False
        if cap.compute_minutes is not None:
            seconds_cap = cap.compute_minutes * 60
            if current_usage.get("compute_seconds", 0) > seconds_cap:
                return False
        return True

    def record_records(self, n: int) -> None:
        self._records += n
        if self.on_record:
            self.on_record("fluid_acquisition_records_total", float(n))

    def record_bytes(self, n: int, direction: str = "read") -> None:
        if direction == "read":
            self._bytes_read += n
        else:
            self._bytes_written += n
        if self.on_record:
            self.on_record(f"fluid_acquisition_bytes_total_{direction}", float(n))

    def record_compute_seconds(self, seconds: float) -> None:
        self._compute_seconds += seconds
        if self.on_record:
            self.on_record("fluid_acquisition_compute_seconds_total", float(seconds))

    def usage(self) -> Dict[str, int]:
        return {
            "rows": self._records,
            "bytes": self._bytes_read + self._bytes_written,
            "bytes_read": self._bytes_read,
            "bytes_written": self._bytes_written,
            "compute_seconds": int(self._compute_seconds),
        }


# Re-export the typed catalog class. Symbol-stable for existing imports;
# single class identity across the runtime + CLI layers.
from fluid_build._errors import BudgetExceededError  # noqa: E402,F401


def gate_or_raise(tracker: CostTracker, cap: BudgetCap, prior_usage: Dict[str, int]) -> None:
    """Pre-flight check. ``cap.on_exceed`` decides what to do.

    The contract validates ``cap.on_exceed ∈ {warn, abort}``; here we honour
    abort by raising. Warn-mode callers ignore the exception by handling it.
    """
    ok = tracker.check_budget(cap, prior_usage)
    if not ok and cap.on_exceed == "abort":
        # Surface the dimension that exceeded so the typed error renders a
        # five-field message ("monthly budget exceeded: <dim>"). prior_usage
        # is a per-dimension dict; pick the first dimension whose value is
        # non-zero as the most meaningful breach. Fall back to ``usage``.
        dim = next(
            (k for k, v in (prior_usage or {}).items() if v),
            "usage",
        )
        used = int((prior_usage or {}).get(dim, 0))
        raise BudgetExceededError.for_cap(
            dimension=dim,
            used=used,
            cap=int(getattr(cap, "monthly", 0) or 0),
        )
