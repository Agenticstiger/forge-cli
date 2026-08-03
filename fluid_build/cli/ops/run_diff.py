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

"""``fluid run-diff <run-a> <run-b>`` — schema + row-count delta between two runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from fluid_build.api.state import StateStore


@dataclass
class StreamDelta:
    name: str
    records_a: int
    records_b: int
    delta: int  # b - a


@dataclass
class RunDiff:
    run_a: str
    run_b: str
    state_a: Optional[str]
    state_b: Optional[str]
    records_total_a: int
    records_total_b: int
    records_delta: int
    duration_a: float
    duration_b: float
    duration_delta: float
    streams: List[StreamDelta] = field(default_factory=list)
    error_a: Optional[str] = None
    error_b: Optional[str] = None


def run_diff(
    state_store: StateStore,
    product_id: str,
    build_id: str,
    *,
    run_a: str,
    run_b: str,
) -> RunDiff:
    """Build a ``RunDiff`` between two persisted run records."""
    rec_a = state_store.read_run_record(product_id, build_id, run_a) or {}
    rec_b = state_store.read_run_record(product_id, build_id, run_b) or {}
    streams = _stream_deltas(rec_a.get("streams") or [], rec_b.get("streams") or [])
    rt_a = int(rec_a.get("records_total") or 0)
    rt_b = int(rec_b.get("records_total") or 0)
    dur_a = float((rec_a.get("facets") or {}).get("duration_seconds") or 0)
    dur_b = float((rec_b.get("facets") or {}).get("duration_seconds") or 0)
    return RunDiff(
        run_a=run_a,
        run_b=run_b,
        state_a=rec_a.get("state"),
        state_b=rec_b.get("state"),
        records_total_a=rt_a,
        records_total_b=rt_b,
        records_delta=rt_b - rt_a,
        duration_a=dur_a,
        duration_b=dur_b,
        duration_delta=dur_b - dur_a,
        streams=streams,
        error_a=rec_a.get("error"),
        error_b=rec_b.get("error"),
    )


def _stream_deltas(
    streams_a: List[Dict[str, Any]], streams_b: List[Dict[str, Any]]
) -> List[StreamDelta]:
    by_a = {s.get("name"): int(s.get("records") or 0) for s in streams_a}
    by_b = {s.get("name"): int(s.get("records") or 0) for s in streams_b}
    names = sorted(set(by_a) | set(by_b))
    return [
        StreamDelta(
            name=n,
            records_a=by_a.get(n, 0),
            records_b=by_b.get(n, 0),
            delta=by_b.get(n, 0) - by_a.get(n, 0),
        )
        for n in names
        if n is not None
    ]
