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

"""Pin V1.2.4 — self-eval median-score gate.

The plan's verification section sets a concrete target:

    Self-eval median score: ≥8 (was ≥7)

The validator agent emits a 1-10 score per draft (alongside the
``passes_schema`` boolean). The plan's quality target is to keep the
*median* score across a representative intent suite at or above 8.

This file ships:

1. **The aggregation harness** — :class:`SelfEvalRunResult` and
   :func:`compute_median_score` — a 30-line pure-Python utility that
   pulls scores out of ``ValidationReport`` instances and computes
   the median. This is the production code we need so a CI step
   can run "score every intent in a frozen suite, fail if median < 8."
2. **Hermetic tests for the harness** — fixtures that stub the
   validator with known scores against a known intent suite, then
   assert the median computation is correct (8 with [9, 9, 8, 7, 6],
   for instance) and that the gate triggers correctly when scores
   fall below the target.

Without this, the plan's "≥8 median" target was a documented
aspiration with no enforcement path. The new
:func:`assert_self_eval_median_at_least` helper IS the enforcement
path — a CI step (or dedicated ``fluid eval`` subcommand in v1.5+)
calls it against the frozen intent suite. This test file proves the
gate works as advertised before it's wired in.

The test deliberately does NOT run real LLM calls. Real-LLM scoring
of a frozen intent suite belongs in a separate ``tests/eval/`` tree
that's `pytest.mark.live`-gated and only exercised in nightly CI.
This file pins the *gate logic*, which is the part most likely to
silently break under a refactor.
"""

from __future__ import annotations

from typing import List

import pytest

from fluid_build.copilot.eval import (
    SelfEvalRunResult,
    assert_self_eval_median_at_least,
    compute_median_score,
)
from fluid_build.copilot.schemas.stage_outputs import ValidationReport

# ---------------------------------------------------------------------
# Tests — gate fires correctly for the known-shape suites
# ---------------------------------------------------------------------


def _result(intent_id: str, score: int, passes: bool = True) -> SelfEvalRunResult:
    return SelfEvalRunResult(intent_id=intent_id, score=score, passes_schema=passes)


class TestComputeMedianScore:
    def test_odd_length_returns_middle(self):
        results = [_result(f"b{i}", s) for i, s in enumerate([10, 9, 8, 7, 6])]
        assert compute_median_score(results) == 8

    def test_even_length_returns_average_of_middle_two(self):
        """``statistics.median`` averages the two middle values for
        even-length inputs. Pin the exact behaviour so a future
        refactor that swaps in ``median_low`` / ``median_high``
        (which give different answers) is caught here."""
        results = [_result(f"b{i}", s) for i, s in enumerate([10, 9, 8, 7])]
        assert compute_median_score(results) == 8.5

    def test_empty_input_raises(self):
        with pytest.raises(ValueError, match="no results"):
            compute_median_score([])

    def test_order_independence(self):
        """Median must be insensitive to input order — flipping the
        list shouldn't change the result. Defends against an
        accidental "first-N", "last-N", or otherwise positional
        aggregator regression."""
        results_a = [_result(f"b{i}", s) for i, s in enumerate([7, 9, 8, 10, 6])]
        results_b = [_result(f"b{i}", s) for i, s in enumerate([10, 9, 8, 7, 6])]
        assert compute_median_score(results_a) == compute_median_score(results_b)


class TestAssertSelfEvalMedianAtLeast:
    def test_passes_when_median_meets_target(self):
        results = [_result(f"b{i}", s) for i, s in enumerate([9, 9, 8, 8, 7])]
        # Median is 8; target is 8 → must pass without raising.
        assert_self_eval_median_at_least(results, target=8)

    def test_passes_when_median_exceeds_target(self):
        results = [_result(f"b{i}", s) for i, s in enumerate([10, 10, 10, 9, 9])]
        assert_self_eval_median_at_least(results, target=8)

    def test_fails_when_median_below_target(self):
        results = [_result(f"b{i}", s) for i, s in enumerate([8, 7, 7, 6, 5])]
        with pytest.raises(AssertionError) as exc_info:
            assert_self_eval_median_at_least(results, target=8)
        # Message must name the worst performers so operators can
        # triage — pin that explicitly.
        assert "median was 7" in str(exc_info.value)
        assert "b4=5" in str(exc_info.value)  # worst performer
        assert "target ≥8" in str(exc_info.value)

    def test_default_target_is_eight(self):
        """The plan's ≥8 target is the default — callers don't have
        to remember to pass it. Pin the default."""
        # Median of [9, 9, 8, 8, 7] is 8 → passes default.
        results = [_result(f"b{i}", s) for i, s in enumerate([9, 9, 8, 8, 7])]
        assert_self_eval_median_at_least(results)

        # Median of [9, 8, 7, 6, 5] is 7 → fails default.
        results = [_result(f"b{i}", s) for i, s in enumerate([9, 8, 7, 6, 5])]
        with pytest.raises(AssertionError):
            assert_self_eval_median_at_least(results)


class TestSelfEvalRunResultFromReport:
    def test_round_trip_carries_score_and_pass_flag(self):
        report = ValidationReport(
            score=9,
            issues=[],
            suggestions=[],
            passes_schema=True,
        )
        result = SelfEvalRunResult.from_report("intent-001", report)
        assert result.intent_id == "intent-001"
        assert result.score == 9
        assert result.passes_schema is True
        assert result.issues_summary == ""

    def test_issues_summary_records_severity_and_field(self):
        from fluid_build.copilot.schemas.stage_outputs import ValidationFinding

        report = ValidationReport(
            score=4,
            issues=[
                ValidationFinding(severity="error", field="exposes.0", message="bad"),
                ValidationFinding(severity="warning", field="osi.metrics", message="thin"),
            ],
            suggestions=[],
            passes_schema=False,
        )
        result = SelfEvalRunResult.from_report("intent-002", report)
        assert result.score == 4
        assert result.passes_schema is False
        # Pin the exact summary format so log scrapers downstream can
        # rely on it.
        assert "error:exposes.0" in result.issues_summary
        assert "warning:osi.metrics" in result.issues_summary


# ---------------------------------------------------------------------
# Frozen intent suite — the exact-shape gate the CI will eventually run
# ---------------------------------------------------------------------


# A tiny synthetic suite. The real production suite belongs in a
# separate ``tests/eval/fixtures/`` tree that runs only against live
# providers (``pytest -m live``). This synthetic shape lets CI run the
# gate at zero cost and catch regressions in the AGGREGATION code.
_FROZEN_SUITE_PASSING: List[SelfEvalRunResult] = [
    _result("retail.dimensional.simple", 9),
    _result("telco.dv2.simple", 9),
    _result("healthcare.dv2.large", 8),
    _result("finance.dimensional.simple", 8),
    _result("retail.dimensional.scd2", 8),
]
_FROZEN_SUITE_REGRESSED: List[SelfEvalRunResult] = [
    _result("retail.dimensional.simple", 8),
    _result("telco.dv2.simple", 7),  # regression
    _result("healthcare.dv2.large", 7),  # regression
    _result("finance.dimensional.simple", 6),  # regression
    _result("retail.dimensional.scd2", 5),  # regression
]


def test_passing_frozen_suite_clears_the_gate():
    """The gate must permit a "healthy" suite (median 8). Any
    regression of the gate-logic floor that flunks healthy suites is
    caught here."""
    assert_self_eval_median_at_least(_FROZEN_SUITE_PASSING, target=8)


def test_regressed_frozen_suite_trips_the_gate():
    """The complement: when the suite degrades, the gate must
    actually fire. Without this, a silent ``return`` instead of
    ``raise`` would render the gate useless."""
    with pytest.raises(AssertionError) as exc_info:
        assert_self_eval_median_at_least(_FROZEN_SUITE_REGRESSED, target=8)
    assert "median was 7" in str(exc_info.value)
