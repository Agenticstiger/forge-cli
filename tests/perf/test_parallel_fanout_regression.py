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

"""Pin V1.2.2 — cold-run parallel-fanout wall-clock reduction target.

The plan's verification section (Deliverable A) sets a concrete target:

    Cold run on ≥4-build contract: ≥40% latency reduction
    (parallel builds[])

The repository ships the parallel path as M1: ``StageCoordinator
._run_physical_stages`` runs Builder ∥ Readme ∥ Transformation on a
3-worker ``ThreadPoolExecutor`` whenever ``FLUID_COPILOT_PARALLEL_PHYSICAL``
is unset or truthy. Concurrency is *proven* by the
``threading.Barrier``-based test in
``tests/copilot/test_coordinator_parallel_readme.py``; this file pins
the *user-visible payoff* — that the wall-clock cost of the three
agents drops to roughly one stage's cost instead of three.

Hermetic harness:

* All three physical agents are stubbed to sleep ``_SLEEP_SECONDS``
  (default 100 ms) and return canned outputs that satisfy the
  Pydantic shapes the coordinator expects.
* The validator stub returns a passing :class:`ValidationReport` so
  the run completes after the first physical pass.
* We measure end-to-end ``_run_physical_stages`` wall-clock for both
  the parallel (default) and serial (escape-hatch) code paths.

The serial run takes ``~3 × _SLEEP_SECONDS``; the parallel run takes
``~1 × _SLEEP_SECONDS`` plus a small per-thread overhead. The plan's
≥40% gate translates to ``parallel ≤ 0.6 × serial`` — comfortably
within reach for a 3-worker executor.

If a future refactor accidentally serializes the physical stages
(e.g., by introducing a shared lock or a futures-on-the-same-thread
pattern), this test fails loudly *before* a release degrades the
critical path.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.coordinator import StageCoordinator
from fluid_build.copilot.schemas.intent import BusinessIntent, DataProduct, Dimensions, Grain
from fluid_build.copilot.schemas.stage_outputs import (
    LogicalDraft,
    PhysicalDraft,
    ReadmeDraft,
    TransformPlan,
    ValidationReport,
)
from fluid_build.copilot.store.backends.null import NullBackend

# Per-stub sleep. 100 ms is large enough to exceed
# ThreadPoolExecutor scheduling latency on every CI runner we care
# about, and small enough that the whole test stays sub-second.
_SLEEP_SECONDS = 0.1
# Acceptance gate from the plan's verification section.
_TARGET_REDUCTION = 0.40


def _build_intent() -> BusinessIntent:
    return BusinessIntent(
        data_product=DataProduct(name="orders", domain="retail"),
        grain=Grain(entity="order_line", time_dimension="order_date"),
        dimensions=Dimensions(entities=["customer", "product"]),
    )


def _make_logical(coordinator: StageCoordinator, session: StageSession) -> LogicalDraft:
    """Run just the Logical stage so the parallel timing measurement
    is scoped to the physical fan-out (which is what the plan's gate
    targets)."""
    result = coordinator.from_intent(session, intent=_build_intent(), technique="dimensional")
    return result.logical


def _relative(spans: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Spans in ms relative to the earliest start, for failure messages."""
    if not spans:
        return []
    base = min(s for s, _ in spans)
    return [(round((s - base) * 1000, 1), round((e - base) * 1000, 1)) for s, e in spans]


def _all_three_overlapped(spans: List[Tuple[float, float]]) -> bool:
    """True when all three stubs were in flight at the same instant.

    ``max(start) < min(end)`` is exactly that: some moment lies inside
    every interval. Deterministic, and indifferent to how fast or how
    loaded the machine is -- which is the whole point of asking this way
    rather than timing the total.
    """
    return len(spans) == 3 and max(s for s, _ in spans) < min(e for _, e in spans)


def _patch_three_slow_agents(
    monkeypatch, sleep_seconds: float, spans: Optional[List[Tuple[float, float]]] = None
) -> None:
    """Patch all three physical-stage agents with delayed stubs.

    Each stub sleeps ``sleep_seconds`` to simulate one provider
    round-trip, then returns a Pydantic shape the coordinator's
    serial / parallel join logic accepts unchanged.

    When ``spans`` is supplied each stub appends its own
    ``(start, end)``. That is what lets the parallel tests ask the
    question they actually mean -- "did these three run at the same
    time?" -- instead of inferring it from total wall-clock, which on a
    shared CI runner measures the runner.
    """

    def _sleep_recording() -> None:
        start = time.perf_counter()
        time.sleep(sleep_seconds)
        if spans is not None:
            spans.append((start, time.perf_counter()))

    def fake_build_physical(self, sess, *, logical, contract, engine):
        _sleep_recording()
        return PhysicalDraft(
            contract=contract,
            logical=logical,
            transform_plan=TransformPlan(builds=[]),
            readme=ReadmeDraft(readme_markdown="builder-default"),
        )

    def fake_readme_run(self, logical, *, engine):
        _sleep_recording()
        return ReadmeDraft(readme_markdown="readme-agent-output")

    def fake_transformation_run(self, logical, *, engine):
        _sleep_recording()
        return TransformPlan(builds=[], additional_files={"from_transform_agent": "yes"})

    def fake_validator_run(
        self, *, logical=None, contract=None, industry_pack=None, scratchpad=None
    ):
        return ValidationReport(score=10, issues=[], suggestions=[], passes_schema=True)

    monkeypatch.setattr(
        "fluid_build.copilot.agents.builder_agent.BuilderAgent.build_physical",
        fake_build_physical,
        raising=True,
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.readme_agent.ReadmeAgent.run",
        fake_readme_run,
        raising=True,
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.transformation_agent.TransformationAgent.run",
        fake_transformation_run,
        raising=True,
    )
    monkeypatch.setattr(
        "fluid_build.copilot.agents.validator_agent.ValidatorAgent.run",
        fake_validator_run,
        raising=True,
    )


def _measure_physical_run(coordinator, session, logical, contract) -> float:
    """Wall-clock-time the physical fan-out only (excluding the
    Logical stage that has already produced ``logical``)."""
    start = time.perf_counter()
    coordinator._run_physical_stages(session, logical=logical, contract=contract, engine="dbt")
    return time.perf_counter() - start


# ---------------------------------------------------------------------
# Parallel-fanout latency reduction
# ---------------------------------------------------------------------


class TestParallelFanoutLatencyReduction:
    def test_parallel_run_meets_40_pct_reduction_target(self, monkeypatch) -> None:
        """The headline pin: the parallel path must complete in
        ≤60% of the serial path's wall-clock for a 3-stage fan-out.

        Logical reasoning: serial takes ``3 × _SLEEP_SECONDS``;
        parallel (3-worker pool) takes ``~1 × _SLEEP_SECONDS``. The
        ideal reduction is 67%; the 40% gate gives ample headroom for
        ThreadPoolExecutor scheduling jitter without going so loose
        that a partial-serialization regression sneaks through."""
        coordinator = StageCoordinator()
        session = StageSession(
            store=NullBackend(), capability_matrix={"critic_errors_trigger_repair": False}
        )
        logical = _make_logical(coordinator, session)
        contract: Dict[str, Any] = {"id": "stub", "metadata": {"name": "orders"}}

        # --- Serial path (escape-hatch on) ---
        monkeypatch.setenv("FLUID_COPILOT_PARALLEL_PHYSICAL", "0")
        _patch_three_slow_agents(monkeypatch, _SLEEP_SECONDS)
        serial_elapsed = _measure_physical_run(coordinator, session, logical, contract)

        # --- Parallel path (default) ---
        monkeypatch.setenv("FLUID_COPILOT_PARALLEL_PHYSICAL", "1")
        # Re-patch to reset stub state; otherwise side-effect-laden
        # assertions could leak across the two timing windows.
        _patch_three_slow_agents(monkeypatch, _SLEEP_SECONDS)
        parallel_elapsed = _measure_physical_run(coordinator, session, logical, contract)

        # The actual perf gate.
        reduction = (serial_elapsed - parallel_elapsed) / serial_elapsed
        assert reduction >= _TARGET_REDUCTION, (
            f"parallel-fanout latency reduction was {reduction:.2%} "
            f"(serial={serial_elapsed * 1000:.1f}ms, "
            f"parallel={parallel_elapsed * 1000:.1f}ms); "
            f"plan target ≥{_TARGET_REDUCTION:.0%}"
        )

    def test_the_three_physical_stages_actually_overlap(self, monkeypatch) -> None:
        """Did the three stages run at the same time? Asked directly.

        This used to infer the answer from total wall-clock against a
        budget, and that budget could not be made to work. An absolute
        200ms ceiling measured the runner, not the code, and failed at
        228.9ms / 262.7ms / 406.5ms while the fan-out was healthy. Making
        it relative to the machine's own serial time then had to pick a
        fraction, and there is no good one: two-thirds is exactly where a
        2-worker pool lands (0.683 against a 0.667 bound -- a coin flip),
        and half rejects a healthy run on a loaded runner (measured
        156.9ms against a 152ms bound on the 3.13 leg). I tightened it
        from two-thirds to half and turned a too-loose bound into a
        too-tight one.

        The property was never really about duration. Each stub now
        records its own (start, end), and `max(start) < min(end)` says
        some instant lay inside all three intervals -- which is what
        "ran in parallel" means. Deterministic, and indifferent to how
        loaded the machine is.
        """
        coordinator = StageCoordinator()
        session = StageSession(
            store=NullBackend(), capability_matrix={"critic_errors_trigger_repair": False}
        )
        logical = _make_logical(coordinator, session)
        contract: Dict[str, Any] = {"id": "stub", "metadata": {"name": "orders"}}

        monkeypatch.setenv("FLUID_COPILOT_PARALLEL_PHYSICAL", "1")
        spans: List[Tuple[float, float]] = []
        _patch_three_slow_agents(monkeypatch, _SLEEP_SECONDS, spans)
        _measure_physical_run(coordinator, session, logical, contract)

        assert _all_three_overlapped(spans), (
            "the three physical stages did not overlap, so the fan-out ran "
            "serially or only partially in parallel. Spans in ms from the "
            f"first start: {_relative(spans)}"
        )

    def test_serial_path_takes_at_least_three_stage_windows(self, monkeypatch) -> None:
        """Inverse pin to keep the harness honest: the serial path
        MUST take ≥ 3 × _SLEEP_SECONDS. If a regression made the
        stubs faster than expected (e.g., shared sleep state), the
        ratio test above would falsely pass. This anchors the
        baseline so the parallel comparison is meaningful."""
        coordinator = StageCoordinator()
        session = StageSession(
            store=NullBackend(), capability_matrix={"critic_errors_trigger_repair": False}
        )
        logical = _make_logical(coordinator, session)
        contract: Dict[str, Any] = {"id": "stub", "metadata": {"name": "orders"}}

        monkeypatch.setenv("FLUID_COPILOT_PARALLEL_PHYSICAL", "0")
        _patch_three_slow_agents(monkeypatch, _SLEEP_SECONDS)
        elapsed = _measure_physical_run(coordinator, session, logical, contract)

        floor = 3 * _SLEEP_SECONDS * 0.95  # 5% slack for OS scheduler
        assert elapsed >= floor, (
            f"serial run took {elapsed * 1000:.1f}ms; expected ≥ {floor * 1000:.0f}ms "
            "(stubs may not be sleeping as advertised — perf gate is unsound)."
        )

    def test_parallel_default_when_env_var_unset(self, monkeypatch) -> None:
        """The escape-hatch is opt-OUT: when ``FLUID_COPILOT_PARALLEL_PHYSICAL``
        is not set at all, the coordinator must default to the parallel
        path (==fast). Removes ambiguity if a CI environment scrubs the
        var without the user knowing."""
        monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)

        coordinator = StageCoordinator()
        session = StageSession(
            store=NullBackend(), capability_matrix={"critic_errors_trigger_repair": False}
        )
        logical = _make_logical(coordinator, session)
        contract: Dict[str, Any] = {"id": "stub", "metadata": {"name": "orders"}}

        spans: List[Tuple[float, float]] = []
        _patch_three_slow_agents(monkeypatch, _SLEEP_SECONDS, spans)
        _measure_physical_run(coordinator, session, logical, contract)

        assert _all_three_overlapped(spans), (
            "with FLUID_COPILOT_PARALLEL_PHYSICAL unset the three physical "
            "stages did not overlap, so the default is no longer the parallel "
            f"path. Spans in ms from the first start: {_relative(spans)}"
        )
