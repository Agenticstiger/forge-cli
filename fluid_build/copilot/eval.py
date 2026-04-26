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

"""Self-eval aggregation utilities for the staged forge pipeline.

Exposes the small surface a CI step (or future ``fluid eval``
subcommand) needs to enforce the plan's "self-eval median score ≥ 8"
target across a frozen intent suite without re-implementing the
median + report-extraction logic in every consumer.

Public surface:

* :class:`SelfEvalRunResult` — one intent's score + diagnostics.
* :func:`compute_median_score` — pure aggregation, raises on empty.
* :func:`assert_self_eval_median_at_least` — gate fn that names the
  worst performers in its failure message.

The actual scoring loop (run a intent through the staged pipeline →
extract :class:`ValidationReport`) lives in the caller — this module
is intentionally I/O-free so it can be unit-tested hermetically and
re-used in any orchestration shape (pytest gate, CLI subcommand,
nightly job, etc.).

See ``tests/perf/test_self_eval_median_gate.py`` for the full
behavioural pin (gate fires correctly on regressed suites, passes
on healthy suites, default target is 8, etc.).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence

from fluid_build.copilot.schemas.stage_outputs import ValidationReport


@dataclass
class SelfEvalRunResult:
    """One intent's self-eval score + diagnostics for the suite report.

    Carries the intent identifier so a CI step can name the worst
    performers in its failure message rather than just "median was 7"
    with no idea which input dragged it down.
    """

    intent_id: str
    score: int
    passes_schema: bool
    issues_summary: str = ""

    @classmethod
    def from_report(cls, intent_id: str, report: ValidationReport) -> "SelfEvalRunResult":
        """Pull the score + structured issue summary out of a
        :class:`ValidationReport`. The summary is intentionally
        compact — it's a "quick look" string for log scrapers, not
        the authoritative findings list (which the caller still has
        on the report)."""
        return cls(
            intent_id=intent_id,
            score=int(report.score),
            passes_schema=bool(report.passes_schema),
            issues_summary="; ".join(
                f"{i.severity}:{i.field or '<no-field>'}" for i in report.issues
            ),
        )


def compute_median_score(results: Iterable[SelfEvalRunResult]) -> float:
    """Return the median of the per-intent scores.

    Pure function. Empty input raises :class:`ValueError` so callers
    see a clear "no scores to aggregate" failure instead of a silent
    zero — silently treating "ran nothing" as "everything is fine"
    would defeat the purpose of the gate.
    """
    scores = [r.score for r in results]
    if not scores:
        raise ValueError("compute_median_score: no results provided")
    return statistics.median(scores)


def assert_self_eval_median_at_least(
    results: Sequence[SelfEvalRunResult], *, target: float = 8.0
) -> None:
    """Raise :class:`AssertionError` if the median falls below ``target``.

    Designed to be called from a ``fluid eval`` CLI subcommand or a
    pytest-style frozen-suite test. The error message names the
    worst performers so operators can drill into specific intents
    without having to re-run the whole suite manually.

    The default ``target`` matches the plan's "self-eval median
    score ≥ 8" promise. Callers who want a tighter floor (e.g., an
    "elite" intent suite) can override it.
    """
    median = compute_median_score(results)
    if median >= target:
        return
    worst = sorted(results, key=lambda r: r.score)[:3]
    detail = ", ".join(f"{r.intent_id}={r.score}" for r in worst)
    raise AssertionError(
        f"self-eval median was {median} (target ≥{target}); " f"worst three: {detail}"
    )


__all__ = [
    "SelfEvalRunResult",
    "compute_median_score",
    "assert_self_eval_median_at_least",
]
