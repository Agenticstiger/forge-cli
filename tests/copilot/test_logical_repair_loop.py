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

"""Phase 3.7 — Logical-stage repair routing.

Before this phase, OSI / DV2 / Dimensional conformance failures
diagnosed by the validator landed on a "skip — out of physical repair
scope" log line and disappeared. This test pins the new posture:

1. **Logical failures route to ``_maybe_repair_logical``** (not the
   silent skip path).
2. **Validator findings persist as scratchpad feedback** for the
   logical stage, so the next attempt / next run benefits.
3. **Logical-clean reports are no-ops** — repair never fires when
   nothing's wrong.
4. **Repair is bounded** — even when validation never converges, the
   loop honours ``_MAX_REPAIR_ATTEMPTS``.

The MVP repair path is "log + persist feedback" (the LogicalAgent has
no generic ``run()`` to re-invoke in v1.0; full re-invocation is
v1.4+). The tests below pin the routing + feedback persistence
contract; the upgrade to in-run re-invocation flips the behaviour
without changing the test shape.
"""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest

from fluid_build.copilot.agents.coordinator import (
    _LOGICAL_REPAIR_STAGES,
    _PHYSICAL_REPAIR_STAGES,
    StageCoordinator,
)
from fluid_build.copilot.schemas.data_model import DimensionalModel
from fluid_build.copilot.schemas.osi import OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import (
    ConceptualDraft,
    LogicalDraft,
    PhysicalDraft,
    TransformPlan,
    ValidationFinding,
    ValidationReport,
)

# ---------------------------------------------------------------------------
# Behaviour 1 — repair-scope sets are disjoint, logical is its own scope
# ---------------------------------------------------------------------------


def test_logical_and_physical_repair_scopes_are_disjoint():
    """A stage routes to exactly one repair handler — no double-firing."""
    assert _LOGICAL_REPAIR_STAGES.isdisjoint(_PHYSICAL_REPAIR_STAGES)


def test_logical_repair_scope_contains_logical_only():
    assert "logical" in _LOGICAL_REPAIR_STAGES
    assert _LOGICAL_REPAIR_STAGES == frozenset({"logical"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _physical_with_logical_failure() -> PhysicalDraft:
    """Build a PhysicalDraft whose validation report fails with a
    logical-scope finding (field='osi.entities')."""
    return PhysicalDraft(
        contract={
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": "test.repair",
            "name": "test",
            "description": "x",
            "domain": "test",
            "metadata": {},
            "consumes": [],
            "builds": [],
            "exposes": [],
        },
        logical=LogicalDraft(
            name="test",
            description="x",
            technique="dimensional",
            conceptual=ConceptualDraft(
                name="test",
                description="",
                entities=[],
                relationships=[],
            ),
            dimensional=DimensionalModel(),
            osi=OSISemanticModel(name="test"),
        ),
        transform_plan=TransformPlan(stages=[]),
        validation=ValidationReport(
            score=2,
            passes_schema=False,
            issues=[
                ValidationFinding(
                    message="OSI block missing required entities",
                    severity="error",
                    field="osi.entities",
                ),
            ],
        ),
    )


def _physical_clean() -> PhysicalDraft:
    """A clean physical draft — nothing for the repair loop to do."""
    p = _physical_with_logical_failure()
    p.validation = ValidationReport(score=10, passes_schema=True, issues=[], suggestions=[])
    return p


# ---------------------------------------------------------------------------
# Behaviour 2 — logical failures route to _maybe_repair_logical
# ---------------------------------------------------------------------------


def test_logical_failure_routes_to_maybe_repair_logical():
    """When _diagnose_failing_stage returns 'logical',
    _maybe_repair_physical must delegate to _maybe_repair_logical
    rather than returning silently."""
    coordinator = StageCoordinator.__new__(StageCoordinator)  # bypass __init__
    physical = _physical_with_logical_failure()

    # Mock _maybe_repair_logical so we can assert it was called.
    with mock.patch.object(coordinator, "_maybe_repair_logical") as mock_logical:
        # _maybe_repair_physical also calls _emit_validator_feedback,
        # which needs a session; mock it too so the coordinator's
        # routing logic runs without setup overhead.
        with mock.patch.object(coordinator, "_emit_validator_feedback"):
            coordinator._maybe_repair_physical(
                session=mock.MagicMock(),
                physical=physical,
                logical=physical.logical,
                contract=physical.contract,
                engine="dbt",
            )

    mock_logical.assert_called_once()
    call_kwargs = mock_logical.call_args.kwargs
    assert call_kwargs["physical"] is physical
    assert call_kwargs["logical"] is physical.logical


# ---------------------------------------------------------------------------
# Behaviour 3 — clean validation skips repair entirely
# ---------------------------------------------------------------------------


def test_clean_report_skips_logical_repair():
    coordinator = StageCoordinator.__new__(StageCoordinator)
    physical = _physical_clean()

    with (
        mock.patch.object(coordinator, "_maybe_repair_logical") as mock_logical,
        mock.patch.object(coordinator, "_rerun_physical_stage") as mock_physical_rerun,
        mock.patch.object(coordinator, "_emit_validator_feedback"),
    ):
        coordinator._maybe_repair_physical(
            session=mock.MagicMock(),
            physical=physical,
            logical=physical.logical,
            contract=physical.contract,
            engine="dbt",
        )

    mock_logical.assert_not_called()
    mock_physical_rerun.assert_not_called()


# ---------------------------------------------------------------------------
# Behaviour 4 — _maybe_repair_logical writes scratchpad feedback + logs
# ---------------------------------------------------------------------------


def test_maybe_repair_logical_persists_scratchpad_feedback(caplog):
    """The MVP repair path writes feedback for the logical stage so
    the operator (and the next run) can see what failed."""
    coordinator = StageCoordinator.__new__(StageCoordinator)
    coordinator.validator_agent = mock.MagicMock()
    coordinator.validator_agent.run.return_value = ValidationReport(
        score=10, passes_schema=True, issues=[]
    )
    coordinator.logical_agent = mock.MagicMock()

    physical = _physical_with_logical_failure()

    # Capture the scratchpad feedback + the rerun's loud-log line.
    with (
        mock.patch.object(coordinator, "_emit_validator_feedback") as mock_emit,
        mock.patch.object(coordinator, "_rerun_logical_stage") as mock_rerun,
    ):
        mock_rerun.return_value = physical.logical
        coordinator._maybe_repair_logical(
            session=mock.MagicMock(),
            physical=physical,
            logical=physical.logical,
            contract=physical.contract,
            engine="dbt",
        )

    # Validator findings written as scratchpad feedback for the logical stage.
    mock_emit.assert_called_once()
    feedback_kwargs = mock_emit.call_args.kwargs
    assert feedback_kwargs["stage"] == "logical"


# ---------------------------------------------------------------------------
# Behaviour 5 — repair loop is bounded (never infinite)
# ---------------------------------------------------------------------------


def test_logical_repair_loop_is_bounded():
    """When validation never converges, the loop exits at
    _MAX_REPAIR_ATTEMPTS — not infinite recursion."""
    from fluid_build.copilot.agents.coordinator import _MAX_REPAIR_ATTEMPTS

    coordinator = StageCoordinator.__new__(StageCoordinator)
    coordinator.validator_agent = mock.MagicMock()
    # Always fail validation → loop stays in the while.
    coordinator.validator_agent.run.return_value = ValidationReport(
        score=2,
        passes_schema=False,
        issues=[ValidationFinding(message="still bad", severity="error", field="osi.entities")],
    )

    physical = _physical_with_logical_failure()

    with (
        mock.patch.object(coordinator, "_emit_validator_feedback"),
        mock.patch.object(coordinator, "_rerun_logical_stage") as mock_rerun,
    ):
        mock_rerun.return_value = physical.logical
        coordinator._maybe_repair_logical(
            session=mock.MagicMock(),
            physical=physical,
            logical=physical.logical,
            contract=physical.contract,
            engine="dbt",
        )

    # The rerun fires _MAX_REPAIR_ATTEMPTS times (one extra attempt).
    assert mock_rerun.call_count == _MAX_REPAIR_ATTEMPTS


# ---------------------------------------------------------------------------
# Behaviour 6 — _rerun_logical_stage returns the original draft (MVP)
# ---------------------------------------------------------------------------


def test_rerun_logical_stage_returns_original_in_mvp():
    """The v1.0 repair path persists feedback for the next run; it
    does NOT in-run re-invoke the LogicalAgent (no generic run()
    method exists). Pin the MVP behaviour so the v1.4+ upgrade is
    a clear test diff."""
    coordinator = StageCoordinator.__new__(StageCoordinator)

    # No-op session whose scratchpad has zero findings.
    session = mock.MagicMock()
    session.get_scratchpad.return_value.feedback_for_stage.return_value = []

    original = _physical_with_logical_failure().logical
    out = coordinator._rerun_logical_stage(
        session=session,
        logical=original,
        contract={},
    )
    assert out is original
