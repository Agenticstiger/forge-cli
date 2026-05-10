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

"""Pin M3 — targeted repair re-runs ONLY the failing physical stage.

The detailed plan's Stage Pipeline section promises that the validator
"repair loop re-enters Transform or DataModel only" — not the whole
pipeline. In the Lean v1.3 split that translates to: when the
ValidatorAgent rejects a draft, the coordinator diagnoses which stage
(builder / transformation / logical / readme) produced the bad slice
and reruns just that one agent, bypassing the LLM cache so the
second call is genuinely fresh.

Two surfaces to pin:

1. :func:`_diagnose_failing_stage` — pure function mapping a
   :class:`ValidationReport` to a stage name. Field-prefix table first,
   message-scan fallback second, ``None`` when the signal is too noisy
   to route safely.
2. :meth:`StageCoordinator._maybe_repair_physical` — the in-place
   repair wiring. Bounded to one extra attempt; reruns only the
   stages in ``_PHYSICAL_REPAIR_STAGES``; toggles
   ``session.no_cache`` in a ``try/finally`` so the bypass is
   isolated to the re-run.

What we do NOT pin here:

* Wall-clock perf — repair is invoked from a rare failure path; the
  gain is correctness, not latency.
* LogicalAgent repair — out of scope for v1.0; the function diagnoses
  it (as telemetry) but the coordinator skips it. A v1.4+ pipeline
  repair will own that path.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.coordinator import (
    _MAX_REPAIR_ATTEMPTS,
    _PHYSICAL_REPAIR_STAGES,
    StageCoordinator,
    _diagnose_failing_stage,
)
from fluid_build.copilot.schemas.data_model import DimensionalModel, FactTable
from fluid_build.copilot.schemas.intent import BusinessIntent, DataProduct, Dimensions, Grain
from fluid_build.copilot.schemas.osi import OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import (
    LogicalDraft,
    PhysicalDraft,
    ReadmeDraft,
    TransformPlan,
    ValidationFinding,
    ValidationReport,
)
from fluid_build.copilot.store.backends.null import NullBackend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _passing_report() -> ValidationReport:
    return ValidationReport(score=10, issues=[], suggestions=[], passes_schema=True)


def _failing_report(field: str, message: str = "stub error") -> ValidationReport:
    return ValidationReport(
        score=2,
        issues=[ValidationFinding(field=field, message=message, severity="error")],
        suggestions=[],
        passes_schema=False,
    )


def _failing_report_msg(message: str) -> ValidationReport:
    """Failing report with no ``field`` — forces the message-scan fallback."""
    return ValidationReport(
        score=2,
        issues=[ValidationFinding(field=None, message=message, severity="error")],
        suggestions=[],
        passes_schema=False,
    )


def _build_intent() -> BusinessIntent:
    return BusinessIntent(
        data_product=DataProduct(name="orders", domain="retail"),
        grain=Grain(entity="order_line", time_dimension="order_date"),
        dimensions=Dimensions(entities=["customer", "product"]),
    )


# ===========================================================================
# Pure-function: _diagnose_failing_stage
# ===========================================================================


class TestDiagnosisTable:
    """The routing table is a feature the downstream repair depends on.
    Pin each row explicitly so a refactor can't silently drop a prefix."""

    def test_passing_report_returns_none(self) -> None:
        assert _diagnose_failing_stage(_passing_report()) is None

    @pytest.mark.parametrize(
        "field",
        [
            "osi",
            "osi.semantic_models",
            "osi.datasets[0].fields",
        ],
    )
    def test_osi_field_routes_to_logical(self, field: str) -> None:
        assert _diagnose_failing_stage(_failing_report(field)) == "logical"

    @pytest.mark.parametrize("field", ["dv2", "dv2.hubs", "dv2.satellites[2].hash_diff"])
    def test_dv2_field_routes_to_logical(self, field: str) -> None:
        assert _diagnose_failing_stage(_failing_report(field)) == "logical"

    @pytest.mark.parametrize(
        "field", ["dimensional", "dimensional.facts", "dimensional.dimensions[0].attributes"]
    )
    def test_dimensional_field_routes_to_logical(self, field: str) -> None:
        assert _diagnose_failing_stage(_failing_report(field)) == "logical"

    @pytest.mark.parametrize("field", ["exposes", "exposes[0]", "exposes[0].contract.semantics"])
    def test_exposes_field_routes_to_builder(self, field: str) -> None:
        assert _diagnose_failing_stage(_failing_report(field)) == "builder"

    @pytest.mark.parametrize(
        "field", ["transform_plan", "transform_plan.builds", "builds", "builds[0].sql"]
    )
    def test_transform_field_routes_to_transformation(self, field: str) -> None:
        assert _diagnose_failing_stage(_failing_report(field)) == "transformation"

    @pytest.mark.parametrize("field", ["readme", "readme.markdown"])
    def test_readme_field_routes_to_readme(self, field: str) -> None:
        assert _diagnose_failing_stage(_failing_report(field)) == "readme"

    def test_unknown_field_returns_none(self) -> None:
        """Fields outside the vocabulary return ``None`` — we prefer
        'don't repair' over 'repair the wrong stage'."""
        assert _diagnose_failing_stage(_failing_report("totally_made_up_field")) is None

    def test_warnings_only_no_signal_returns_none(self) -> None:
        """A failing report whose only findings are warnings has no
        errors to route on — return None so the coordinator doesn't
        burn a repair attempt on non-errors."""
        report = ValidationReport(
            score=5,
            issues=[
                ValidationFinding(field="exposes", message="style nit", severity="warning"),
            ],
            suggestions=[],
            passes_schema=False,
        )
        assert _diagnose_failing_stage(report) is None

    # --- Message-scan fallback ------------------------------------------------

    def test_message_scan_transform_keyword(self) -> None:
        assert _diagnose_failing_stage(_failing_report_msg("transform emitted bad SQL")) == (
            "transformation"
        )

    def test_message_scan_builds_keyword(self) -> None:
        assert _diagnose_failing_stage(_failing_report_msg("builds[0] missing engine")) == (
            "transformation"
        )

    def test_message_scan_exposes_keyword(self) -> None:
        assert _diagnose_failing_stage(_failing_report_msg("exposes section invalid")) == (
            "builder"
        )

    def test_message_scan_contract_keyword(self) -> None:
        assert _diagnose_failing_stage(_failing_report_msg("contract missing id")) == "builder"

    def test_message_scan_osi_keyword(self) -> None:
        assert _diagnose_failing_stage(_failing_report_msg("osi semantic model malformed")) == (
            "logical"
        )

    def test_message_scan_no_signal_returns_none(self) -> None:
        assert _diagnose_failing_stage(_failing_report_msg("something broke")) is None


# ===========================================================================
# Coordinator integration — repair wiring
# ===========================================================================


class _Spy:
    """Counts how many times a stubbed agent method was invoked, plus
    records the ``session.no_cache`` value at invocation time so tests
    can assert the bypass is active during repair and restored after."""

    def __init__(self) -> None:
        self.calls: int = 0
        self.no_cache_during_call: List[bool] = []


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Stub builder/readme/transformation/validator so each test
    controls return values + call counts without any real LLM work.
    Yields the four spy objects (one per agent)."""

    builder_spy = _Spy()
    readme_spy = _Spy()
    transformation_spy = _Spy()
    validator_spy = _Spy()

    # Validator: a queue of reports so tests can drive the repair
    # sequence deterministically (first report triggers repair; second
    # report may still fail OR pass — tests configure both).
    validator_reports: List[ValidationReport] = []

    def fake_build_physical(self, sess, *, logical, contract, engine):
        builder_spy.calls += 1
        builder_spy.no_cache_during_call.append(bool(sess.no_cache))
        return PhysicalDraft(
            contract=contract,
            logical=logical,
            transform_plan=TransformPlan(builds=[]),
            readme=ReadmeDraft(readme_markdown=f"builder#{builder_spy.calls}"),
        )

    def fake_readme_run(self, logical, *, engine):
        readme_spy.calls += 1
        return ReadmeDraft(readme_markdown=f"readme#{readme_spy.calls}")

    def fake_transformation_run(self, logical, *, engine):
        transformation_spy.calls += 1
        return TransformPlan(builds=[], additional_files={"iter": str(transformation_spy.calls)})

    def fake_validator_run(
        self, *, logical=None, contract=None, industry_pack=None, scratchpad=None
    ):
        validator_spy.calls += 1
        if validator_reports:
            return validator_reports.pop(0)
        # Default: pass. Tests push failing reports before running.
        return _passing_report()

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

    # Make the logical stage cheap too so the tests don't actually
    # hit an LLM when they build the session. The stub draft carries
    # the minimum schema shape for technique=dimensional: a populated
    # ``osi`` and a one-fact ``dimensional`` branch.
    def _fake_logical_from_intent(self, session, *, intent, technique):
        return LogicalDraft(
            name=intent.data_product.name,
            technique=technique,
            description="stub",
            source_summary={},
            osi=OSISemanticModel(name=intent.data_product.name),
            dimensional=DimensionalModel(
                facts=[FactTable(name="fact_stub", grain_statement="stub")]
            ),
        )

    monkeypatch.setattr(
        "fluid_build.copilot.agents.logical_agent.LogicalAgent.from_intent",
        _fake_logical_from_intent,
        raising=True,
    )

    yield {
        "builder": builder_spy,
        "readme": readme_spy,
        "transformation": transformation_spy,
        "validator": validator_spy,
        "reports": validator_reports,
    }


def _session() -> StageSession:
    return StageSession(store=NullBackend())


class TestCoordinatorRepair:
    # -------------------------------------------------------------------
    # Happy path: passing validator → no repair, every agent runs once.
    # -------------------------------------------------------------------
    def test_passing_validator_runs_no_repair(self, stub_pipeline, monkeypatch) -> None:
        monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)
        session = _session()
        coordinator = StageCoordinator()
        result = coordinator.from_intent(
            session, intent=_build_intent(), technique="dimensional", include_physical=True
        )
        assert result.physical is not None
        assert result.physical.validation.passes_schema is True
        assert stub_pipeline["builder"].calls == 1
        assert stub_pipeline["readme"].calls == 1
        assert stub_pipeline["transformation"].calls == 1
        # Validator runs exactly once when the first report passes.
        assert stub_pipeline["validator"].calls == 1

    # -------------------------------------------------------------------
    # Builder blamed: re-runs builder once, preserves fanout readme/tx.
    # -------------------------------------------------------------------
    def test_builder_failure_reruns_builder_once(self, stub_pipeline, monkeypatch) -> None:
        monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)
        # First validator call: fails with exposes-scope error → blame builder.
        # Second validator call (after repair): passes.
        stub_pipeline["reports"].extend([_failing_report("exposes"), _passing_report()])

        session = _session()
        coordinator = StageCoordinator()
        result = coordinator.from_intent(
            session, intent=_build_intent(), technique="dimensional", include_physical=True
        )
        assert result.physical is not None
        assert result.physical.validation.passes_schema is True
        assert stub_pipeline["builder"].calls == 2, (
            "builder must be re-run exactly once after exposes-scope failure"
        )
        # Readme + transformation are NOT re-run — they weren't blamed.
        assert stub_pipeline["readme"].calls == 1
        assert stub_pipeline["transformation"].calls == 1
        # Validator ran once for the original draft and once post-repair.
        assert stub_pipeline["validator"].calls == 2

    def test_builder_repair_preserves_parallel_readme_and_transform(
        self, stub_pipeline, monkeypatch
    ) -> None:
        """When builder repair triggers, the readme/transform_plan the
        parallel fanout produced must survive — the repair is scoped to
        builder output only."""
        monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)
        stub_pipeline["reports"].extend([_failing_report("exposes"), _passing_report()])

        session = _session()
        coordinator = StageCoordinator()
        result = coordinator.from_intent(
            session, intent=_build_intent(), technique="dimensional", include_physical=True
        )
        # The transform_plan's ``additional_files["iter"]`` records which
        # invocation it came from; with only one transformation call,
        # it must read "1" even after builder re-ran.
        assert result.physical.transform_plan.additional_files["iter"] == "1"
        # Readme similarly — it ran once, tag should reflect invocation 1.
        assert result.physical.readme.readme_markdown == "readme#1"

    # -------------------------------------------------------------------
    # Transformation blamed: re-runs transformation, not builder.
    # -------------------------------------------------------------------
    def test_transformation_failure_reruns_transformation_only(
        self, stub_pipeline, monkeypatch
    ) -> None:
        monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)
        stub_pipeline["reports"].extend(
            [_failing_report("builds", "builds[0].sql invalid"), _passing_report()]
        )

        session = _session()
        coordinator = StageCoordinator()
        result = coordinator.from_intent(
            session, intent=_build_intent(), technique="dimensional", include_physical=True
        )
        assert stub_pipeline["builder"].calls == 1, "builder must not re-run for transform failure"
        assert stub_pipeline["transformation"].calls == 2
        # The repaired transform_plan wins — ``iter`` now reflects the
        # second call.
        assert result.physical.transform_plan.additional_files["iter"] == "2"

    # -------------------------------------------------------------------
    # Logical failure: routes to _maybe_repair_logical (Phase 3.7).
    # -------------------------------------------------------------------
    def test_logical_failure_routes_to_logical_repair_loop(
        self, stub_pipeline, monkeypatch
    ) -> None:
        """Phase 3.7 — logical-scope failures (``osi`` prefix and
        friends) now route to ``_maybe_repair_logical`` instead of
        being silently dropped.

        Physical agents (builder / readme / transformation) still
        run exactly once each — the repair loop targets the logical
        stage, not the physicals. The validator runs once for the
        original assessment plus one per repair attempt
        (``_MAX_REPAIR_ATTEMPTS=1``) so the bounded count is 2.
        """
        from fluid_build.copilot.agents.coordinator import _MAX_REPAIR_ATTEMPTS

        monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)
        # Push reports for: original validation + N repair-loop validations.
        stub_pipeline["reports"].extend([_failing_report("osi")] * (1 + _MAX_REPAIR_ATTEMPTS))

        session = _session()
        coordinator = StageCoordinator()
        result = coordinator.from_intent(
            session, intent=_build_intent(), technique="dimensional", include_physical=True
        )
        # Physical agents still run exactly once each — repair targeted
        # the logical stage, not the physicals.
        assert stub_pipeline["builder"].calls == 1
        assert stub_pipeline["readme"].calls == 1
        assert stub_pipeline["transformation"].calls == 1
        # Validator: 1 original + N repair-loop attempts.
        assert stub_pipeline["validator"].calls == 1 + _MAX_REPAIR_ATTEMPTS
        # The MVP repair path doesn't actually re-run the LogicalAgent
        # in v1.0 (no generic ``run()`` exists yet) so the final
        # validation still flags the original logical-scope failure;
        # the operator gets the failing report PLUS scratchpad
        # feedback they can act on.
        assert result.physical.validation.passes_schema is False
        assert result.physical.validation.issues[0].field == "osi"

    # -------------------------------------------------------------------
    # Un-routable failure: no stage diagnosed → NO re-run.
    # -------------------------------------------------------------------
    def test_unroutable_failure_skips_repair(self, stub_pipeline, monkeypatch) -> None:
        monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)
        stub_pipeline["reports"].append(_failing_report("totally_made_up_field"))

        session = _session()
        coordinator = StageCoordinator()
        coordinator.from_intent(
            session, intent=_build_intent(), technique="dimensional", include_physical=True
        )
        assert stub_pipeline["builder"].calls == 1
        assert stub_pipeline["transformation"].calls == 1

    # -------------------------------------------------------------------
    # Bounded attempts: persistent failure → exactly ONE retry, not loop.
    # -------------------------------------------------------------------
    def test_bounded_to_one_repair_attempt(self, stub_pipeline, monkeypatch) -> None:
        """If the builder fails again post-repair, we must not loop. The
        caller sees the still-failing report; they can decide what to do
        next (human review, re-prompt with different intent, etc.)."""
        monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)
        # Both attempts fail.
        stub_pipeline["reports"].extend(
            [_failing_report("exposes"), _failing_report("exposes", "still broken")]
        )

        session = _session()
        coordinator = StageCoordinator()
        result = coordinator.from_intent(
            session, intent=_build_intent(), technique="dimensional", include_physical=True
        )
        assert stub_pipeline["builder"].calls == 1 + _MAX_REPAIR_ATTEMPTS
        assert stub_pipeline["validator"].calls == 1 + _MAX_REPAIR_ATTEMPTS
        # Caller still gets the fresh report, not a stale one.
        assert result.physical.validation.passes_schema is False
        assert result.physical.validation.issues[0].message == "still broken"

    # -------------------------------------------------------------------
    # session.no_cache: flipped on during repair, restored after.
    # -------------------------------------------------------------------
    def test_no_cache_is_set_during_repair_and_restored_after(
        self, stub_pipeline, monkeypatch
    ) -> None:
        """The repair re-prompt must bypass the LLM cache (otherwise
        the second call returns the cached bad response and can't
        improve). After repair the flag must be restored to its
        caller-provided value — we can't leak the bypass."""
        monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)
        stub_pipeline["reports"].extend([_failing_report("exposes"), _passing_report()])

        session = _session()
        # Caller-provided initial state is False — confirm it's restored.
        assert session.no_cache is False
        coordinator = StageCoordinator()
        coordinator.from_intent(
            session, intent=_build_intent(), technique="dimensional", include_physical=True
        )
        # First builder call: original fanout, no_cache=False.
        # Second builder call: repair, no_cache=True.
        assert stub_pipeline["builder"].no_cache_during_call == [
            False,
            True,
        ], f"expected [False, True]; got {stub_pipeline['builder'].no_cache_during_call}"
        # After the whole run, caller's session is back to False.
        assert session.no_cache is False

    def test_no_cache_restored_even_if_caller_set_it_true(self, stub_pipeline, monkeypatch) -> None:
        """If the caller turned the bypass on before running us, the
        ``finally`` must restore that value, not naively set ``False``."""
        monkeypatch.delenv("FLUID_COPILOT_PARALLEL_PHYSICAL", raising=False)
        stub_pipeline["reports"].extend([_failing_report("exposes"), _passing_report()])

        session = _session()
        session.no_cache = True  # caller pre-configured bypass
        coordinator = StageCoordinator()
        coordinator.from_intent(
            session, intent=_build_intent(), technique="dimensional", include_physical=True
        )
        # Caller's pre-set value must survive the repair.
        assert session.no_cache is True


# ===========================================================================
# Module-level constants are part of the v1 contract
# ===========================================================================


class TestRepairModuleConstants:
    """These values are referenced by tests + docs; freezing them here
    makes a silent bump obvious."""

    def test_max_repair_attempts_is_one(self) -> None:
        assert _MAX_REPAIR_ATTEMPTS == 1

    def test_physical_repair_stages_are_builder_and_transformation(self) -> None:
        assert _PHYSICAL_REPAIR_STAGES == frozenset({"builder", "transformation"})
