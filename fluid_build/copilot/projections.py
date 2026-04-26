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

"""Latency budgets + cost projections (E18).

Today the cost summary is post-hoc — operators see "$0.32 spent"
after the run. World-class agentic systems offer "this forge
will likely cost ~$X — proceed?" before the run starts. They
also enforce per-stage latency budgets so a slow modeler call
gets killed instead of dragging the whole forge.

Projections (this module's primitives) feed off historical cost
data from the run tracker + episodic memory:

1. :func:`project_run_cost` — given the input shape (table count,
   technique, source kind), return an expected USD range based
   on past similar runs.
2. :class:`StageBudget` — per-stage time budget; the
   :meth:`StageBudget.check` raises ``StageBudgetExceeded`` when
   wall-clock elapsed past the limit.
3. :func:`recent_run_costs` — read past run-cost samples from
   ``memory/episodic`` (where the episodic-memory writer A2 has
   already been logging them).

All three are best-effort: missing history → no projection.
Missing budgets → no enforcement. Tooling adopts incrementally.

Public surface:

* :class:`CostProjection` — typed projection result.
* :class:`StageBudget` / :class:`StageBudgetExceeded` — typed
  budget primitives.
* :func:`project_run_cost`, :func:`recent_run_costs`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass
class CostProjection:
    """Projected USD cost for an upcoming forge.

    ``low`` and ``high`` are the 25th / 75th percentile of past
    similar runs. ``samples`` is how many past runs informed the
    projection. ``confidence`` is ``"high"`` (≥ 5 samples),
    ``"medium"`` (2-4), ``"low"`` (1), or ``"none"`` (no history).
    """

    low_usd: float
    high_usd: float
    samples: int
    confidence: str  # "high" | "medium" | "low" | "none"

    def summary(self) -> str:
        if self.confidence == "none":
            return "Cost projection: no prior runs to base estimate on."
        return (
            f"Cost projection: ${self.low_usd:.4f} – ${self.high_usd:.4f} "
            f"(based on {self.samples} prior run(s); confidence={self.confidence})"
        )


def recent_run_costs(
    *,
    store: Any,
    technique: Optional[str] = None,
    source_type: Optional[str] = None,
    limit: int = 20,
) -> List[float]:
    """Read past run-cost samples from ``memory/episodic``.

    Filters by technique / source_type when provided so the
    projection is relevant to the upcoming run's shape.
    Returns an empty list when no history is available.
    """
    if store is None or not hasattr(store, "query"):
        return []
    try:
        records = store.query("memory/episodic", limit=limit) or []
    except Exception:  # pragma: no cover — defensive
        return []
    samples: List[float] = []
    for record in records:
        value = getattr(record, "value", None)
        if not isinstance(value, dict):
            continue
        if technique and value.get("technique") != technique:
            continue
        if source_type and value.get("source_type") != source_type:
            continue
        # The A2 episodic writer doesn't yet record cost — when v1.6
        # extends it to capture the run cost, this module will read
        # the field. Today we return an empty list so the
        # projection helper degrades to "no history".
        cost = value.get("total_usd")
        if isinstance(cost, (int, float)) and cost >= 0:
            samples.append(float(cost))
    return samples


def project_run_cost(
    *,
    store: Any,
    technique: str,
    source_type: str,
) -> CostProjection:
    """Project the upcoming run's USD cost from history.

    Returns a :class:`CostProjection` with low/high quartile
    estimates. Empty history → ``confidence="none"``.
    """
    samples = recent_run_costs(
        store=store,
        technique=technique,
        source_type=source_type,
    )
    if not samples:
        return CostProjection(
            low_usd=0.0,
            high_usd=0.0,
            samples=0,
            confidence="none",
        )
    if len(samples) == 1:
        return CostProjection(
            low_usd=samples[0],
            high_usd=samples[0],
            samples=1,
            confidence="low",
        )
    samples_sorted = sorted(samples)
    # Quartile-style band — robust to outliers without bringing in
    # numpy.
    q1_idx = max(0, (len(samples_sorted) - 1) // 4)
    q3_idx = min(len(samples_sorted) - 1, (3 * (len(samples_sorted) - 1)) // 4)
    confidence = "high" if len(samples_sorted) >= 5 else "medium"
    return CostProjection(
        low_usd=samples_sorted[q1_idx],
        high_usd=samples_sorted[q3_idx],
        samples=len(samples_sorted),
        confidence=confidence,
    )


class StageBudgetExceeded(RuntimeError):
    """Raised when a stage's wall-clock budget is exceeded."""

    def __init__(self, *, stage: str, elapsed_s: float, budget_s: float) -> None:
        self.stage = stage
        self.elapsed_s = elapsed_s
        self.budget_s = budget_s
        super().__init__(
            f"Stage {stage!r} exceeded budget: {elapsed_s:.1f}s > "
            f"{budget_s:.1f}s. Increase the budget via "
            "FLUID_STAGE_BUDGET_<STAGE>_S=<seconds> or set "
            "behavior.stage_budgets in ~/.fluid/config.yaml."
        )


@dataclass
class StageBudget:
    """Per-stage wall-clock budget enforcement.

    Usage::

        budget = StageBudget(stage="modeler", limit_s=60)
        budget.start()
        # ... do work ...
        budget.check()   # raises StageBudgetExceeded if past limit

    The primitive is **opt-in** — coordinators that want
    per-stage budgets construct one per stage; existing flows
    that don't want budgets skip the check call.
    """

    stage: str
    limit_s: float
    started_at: Optional[float] = None

    def start(self) -> None:
        self.started_at = time.perf_counter()

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.perf_counter() - self.started_at

    def check(self) -> None:
        elapsed = self.elapsed()
        if self.limit_s > 0 and elapsed > self.limit_s:
            raise StageBudgetExceeded(
                stage=self.stage,
                elapsed_s=elapsed,
                budget_s=self.limit_s,
            )


__all__ = [
    "CostProjection",
    "StageBudget",
    "StageBudgetExceeded",
    "project_run_cost",
    "recent_run_costs",
]
