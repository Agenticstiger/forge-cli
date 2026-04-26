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

"""Pin M1 — builder/readme/transformation fan out in parallel.

The plan's Stage Pipeline section (Deliverable A) promises "DataModel
∥ Readme" so readme generation drops off the critical path. In the
v1.3 split, that translates to the three physical agents — Builder,
Readme, Transformation — running concurrently because none of them
reads another's output.

These tests don't time wall-clock (flaky under noisy CI). Instead:

* A ``threading.Barrier(parties=3, timeout=5)`` proves the three stages
  actually enter their work at the same time — under the old sequential
  pipeline only one agent ever reaches the barrier and it timeouts.
  Under the parallel pipeline all three arrive within microseconds and
  the barrier opens cleanly.
* Final ``PhysicalDraft`` shape stays identical to v1.0 (readme and
  transform_plan come from the matching agents, not the builder's
  internal defaults).
* Exceptions in any worker propagate out of ``_run_physical_stages``.
* The ``FLUID_COPILOT_PARALLEL_PHYSICAL=0`` escape hatch picks the
  serial code path so users can flip the switch if their custom store
  backend turns out not to be thread-safe.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Dict, Optional
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

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _build_intent() -> BusinessIntent:
    return BusinessIntent(
        data_product=DataProduct(name="orders", domain="retail"),
        grain=Grain(entity="order_line", time_dimension="order_date"),
        dimensions=Dimensions(entities=["customer", "product"]),
    )


def _make_logical(coordinator: StageCoordinator, session: StageSession) -> LogicalDraft:
    """Run just the Logical stage so each physical-stage test gets a
    realistic ``LogicalDraft`` without re-timing the logical work."""
    result = coordinator.from_intent(session, intent=_build_intent(), technique="dimensional")
    return result.logical


def _barrier_probe(parties: int = 3, timeout: float = 5.0) -> threading.Barrier:
    """Construct the barrier the parallel tests coordinate on.

    Timeout is generous (5s) — parallel execution arrives at the barrier
    in microseconds; serial execution never does (only one thread ever
    reaches it) and we *want* the barrier to time out to turn a
    sequential regression into a clean BrokenBarrierError.
    """
    return threading.Barrier(parties=parties, timeout=timeout)


# ---------------------------------------------------------------------------
# Parallel execution is proven, not assumed
# ---------------------------------------------------------------------------


def test_builder_readme_transformation_run_concurrently(monkeypatch) -> None:
    """Replace all three agents with stub runners that block on a
    shared barrier. If execution is sequential, only one thread ever
    hits the barrier and the barrier times out → BrokenBarrierError
    bubbles up from the runner. If execution is parallel, all three
    arrive within timeout, the barrier opens, and every runner returns
    its sentinel value."""
    monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)

    session = StageSession(
        store=NullBackend(), capability_matrix={"critic_errors_trigger_repair": False}
    )
    coordinator = StageCoordinator()
    logical = _make_logical(coordinator, session)
    contract: Dict[str, Any] = {"id": "stub", "metadata": {"name": "orders"}}

    barrier = _barrier_probe(parties=3, timeout=5.0)
    entered_by: list[str] = []
    lock = threading.Lock()

    def _record(label: str) -> None:
        with lock:
            entered_by.append(label)

    # --- Stub the three parallel agents -------------------------------------

    def fake_build_physical(self, sess, *, logical, contract, engine):
        _record("builder")
        barrier.wait()  # must be released by all three arriving
        return PhysicalDraft(
            contract=contract,
            logical=logical,
            transform_plan=TransformPlan(builds=[]),
            readme=ReadmeDraft(readme_markdown="builder-default"),
        )

    def fake_readme_run(self, logical, *, engine):
        _record("readme")
        barrier.wait()
        return ReadmeDraft(readme_markdown="readme-agent-output")

    def fake_transformation_run(self, logical, *, engine):
        _record("transformation")
        barrier.wait()
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

    # --- Run --------------------------------------------------------------
    physical = coordinator._run_physical_stages(
        session, logical=logical, contract=contract, engine="dbt"
    )

    # All three hit the barrier — proof of concurrency.
    assert sorted(entered_by) == ["builder", "readme", "transformation"]
    # Readme and transform_plan came from the matching agents (not from
    # the builder's internal defaults), same as the v1.0 sequential
    # pipeline's observable shape.
    assert physical.readme is not None
    assert physical.readme.readme_markdown == "readme-agent-output"
    assert physical.transform_plan.additional_files.get("from_transform_agent") == "yes"
    # Validator still ran after the fanout joined.
    assert physical.validation is not None
    assert physical.validation.passes_schema is True


# ---------------------------------------------------------------------------
# Shape preservation across parallel / serial codepaths
# ---------------------------------------------------------------------------


def test_parallel_and_serial_produce_equivalent_physical_draft(monkeypatch) -> None:
    """Running with and without the escape hatch must produce the same
    ``PhysicalDraft`` shape — otherwise the flag becomes a behavioural
    switch instead of a safety valve, and consumers that flip it for
    debugging would see different outputs."""
    session = StageSession(
        store=NullBackend(), capability_matrix={"critic_errors_trigger_repair": False}
    )
    coordinator = StageCoordinator()
    intent = _build_intent()

    # Parallel
    monkeypatch.setenv("FLUID_COPILOT_PARALLEL_PHYSICAL", "1")
    par = coordinator.from_intent(
        session, intent=intent, technique="dimensional", include_physical=True
    )

    # Serial
    monkeypatch.setenv("FLUID_COPILOT_PARALLEL_PHYSICAL", "0")
    ser = coordinator.from_intent(
        session, intent=intent, technique="dimensional", include_physical=True
    )

    assert par.logical.name == ser.logical.name
    assert par.logical.technique == ser.logical.technique
    assert par.physical is not None and ser.physical is not None
    assert par.physical.logical.technique == ser.physical.logical.technique
    # Both paths must attach the readme and transform_plan Pydantic
    # objects (not None, not missing).
    assert par.physical.readme is not None
    assert ser.physical.readme is not None
    assert par.physical.transform_plan is not None
    assert ser.physical.transform_plan is not None
    # Validator ran in both and produced a schema-pass.
    assert par.physical.validation is not None
    assert ser.physical.validation is not None
    assert par.physical.validation.passes_schema == ser.physical.validation.passes_schema


# ---------------------------------------------------------------------------
# Exception propagation
# ---------------------------------------------------------------------------


def test_exception_in_parallel_worker_propagates(monkeypatch) -> None:
    """If readme raises inside its worker, the caller must see the
    exception — we cannot swallow or convert it because downstream code
    expects a ``PhysicalDraft``, and returning one with a blank readme
    would silently corrupt caches."""
    monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)

    session = StageSession(
        store=NullBackend(), capability_matrix={"critic_errors_trigger_repair": False}
    )
    coordinator = StageCoordinator()
    logical = _make_logical(coordinator, session)
    contract: Dict[str, Any] = {"id": "stub", "metadata": {"name": "orders"}}

    def exploding_readme(self, logical, *, engine):
        raise RuntimeError("readme-worker-blew-up")

    monkeypatch.setattr(
        "fluid_build.copilot.agents.readme_agent.ReadmeAgent.run",
        exploding_readme,
        raising=True,
    )

    with pytest.raises(RuntimeError, match="readme-worker-blew-up"):
        coordinator._run_physical_stages(session, logical=logical, contract=contract, engine="dbt")


def test_exception_in_serial_worker_propagates(monkeypatch) -> None:
    """Same contract under the serial fallback — the escape hatch must
    not quietly change error semantics."""
    monkeypatch.setenv("FLUID_COPILOT_PARALLEL_PHYSICAL", "0")

    session = StageSession(
        store=NullBackend(), capability_matrix={"critic_errors_trigger_repair": False}
    )
    coordinator = StageCoordinator()
    logical = _make_logical(coordinator, session)
    contract: Dict[str, Any] = {"id": "stub", "metadata": {"name": "orders"}}

    def exploding_readme(self, logical, *, engine):
        raise RuntimeError("readme-serial-blew-up")

    monkeypatch.setattr(
        "fluid_build.copilot.agents.readme_agent.ReadmeAgent.run",
        exploding_readme,
        raising=True,
    )

    with pytest.raises(RuntimeError, match="readme-serial-blew-up"):
        coordinator._run_physical_stages(session, logical=logical, contract=contract, engine="dbt")


# ---------------------------------------------------------------------------
# Escape hatch toggles paths, not behaviour
# ---------------------------------------------------------------------------


def test_escape_hatch_routes_to_serial_code_path(monkeypatch) -> None:
    """Flipping the env var sends the coordinator through
    ``_run_physical_stages_serial`` — we assert the routing by spying
    on both methods and checking which one was called."""
    monkeypatch.setenv("FLUID_COPILOT_PARALLEL_PHYSICAL", "0")

    session = StageSession(
        store=NullBackend(), capability_matrix={"critic_errors_trigger_repair": False}
    )
    coordinator = StageCoordinator()

    with patch.object(
        coordinator, "_run_physical_stages_serial", wraps=coordinator._run_physical_stages_serial
    ) as serial_spy:
        coordinator.from_intent(
            session, intent=_build_intent(), technique="dimensional", include_physical=True
        )
        assert serial_spy.call_count == 1, (
            "FLUID_COPILOT_PARALLEL_PHYSICAL=0 must route through the "
            "serial fallback, not the parallel fanout"
        )


def test_default_routes_to_parallel_code_path(monkeypatch) -> None:
    monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)

    session = StageSession(
        store=NullBackend(), capability_matrix={"critic_errors_trigger_repair": False}
    )
    coordinator = StageCoordinator()

    with patch.object(
        coordinator, "_run_physical_stages_serial", wraps=coordinator._run_physical_stages_serial
    ) as serial_spy:
        coordinator.from_intent(
            session, intent=_build_intent(), technique="dimensional", include_physical=True
        )
        assert serial_spy.call_count == 0, (
            "Default behaviour must run the parallel fanout; the serial "
            "fallback is only for the escape hatch"
        )


@pytest.mark.parametrize("token", ["0", "false", "no", "off", "FALSE", "No", "OFF"])
def test_escape_hatch_recognises_common_disable_tokens(monkeypatch, token: str) -> None:
    """Users will set it from shell / Makefile / CI configs with any of
    these common forms — the recognition must be lenient enough that
    'FLUID_COPILOT_PARALLEL_PHYSICAL=no' behaves like '=0'."""
    monkeypatch.setenv("FLUID_COPILOT_PARALLEL_PHYSICAL", token)
    from fluid_build.copilot.agents.coordinator import _parallel_physical_enabled

    assert _parallel_physical_enabled() is False


@pytest.mark.parametrize("token", ["1", "true", "yes", "on", "anything-else"])
def test_escape_hatch_anything_non_disable_keeps_parallel(monkeypatch, token: str) -> None:
    """If the user sets the var to anything other than a disable token,
    we keep the parallel default — safer than silently disabling."""
    monkeypatch.setenv("FLUID_COPILOT_PARALLEL_PHYSICAL", token)
    from fluid_build.copilot.agents.coordinator import _parallel_physical_enabled

    assert _parallel_physical_enabled() is True
