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

"""StageCoordinator targeted-repair mixin.

Lifted from ``copilot/agents/coordinator.py`` (host file was 1406
LOC). The 5 repair methods below are pure ``self``-method calls that
mutate ``StageSession`` / ``PhysicalDraft`` / ``LogicalDraft`` —
they do NOT introduce new dependencies, just route already-built
agents through one targeted re-run.

Methods:

* :meth:`_maybe_repair_physical` — single physical-stage re-run when
  the validator implicates ``builder`` / ``transformation``.
* :meth:`_maybe_repair_logical` — logical-stage repair via the
  LogicalAgent's "revise with feedback" entry point.
* :meth:`_rerun_logical_stage` / :meth:`_rerun_physical_stage` —
  scratchpad-aware single-stage replays.
* :meth:`_emit_validator_feedback` — structured ``StageFeedback``
  on the session scratchpad.

The class stays a mixin (``class StageCoordinator(_RepairLoopMixin)``)
so existing ``coord._maybe_repair_physical(...)`` calls keep
resolving through MRO without behaviour change.
"""

from __future__ import annotations

import logging

from fluid_build.copilot.agents._coordinator_helpers import (
    LOGICAL_REPAIR_STAGES as _LOGICAL_REPAIR_STAGES,
)
from fluid_build.copilot.agents._coordinator_helpers import (
    MAX_REPAIR_ATTEMPTS as _MAX_REPAIR_ATTEMPTS,
)
from fluid_build.copilot.agents._coordinator_helpers import (
    PHYSICAL_REPAIR_STAGES as _PHYSICAL_REPAIR_STAGES,
)
from fluid_build.copilot.agents._coordinator_helpers import (
    diagnose_failing_stage as _diagnose_failing_stage,
)
from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.schemas.stage_outputs import (
    LogicalDraft,
    PhysicalDraft,
    ValidationReport,
)
from fluid_build.copilot.scratchpad import StageFeedback
from fluid_build.observability.tracing import traced_span

_log = logging.getLogger("fluid.copilot.coordinator.repair")


class _RepairLoopMixin:
    """Mixin holding the targeted-repair surface for :class:`StageCoordinator`.

    Methods rely on self-state populated by the host coordinator
    (``self._stage_budget``, ``self._record_agent_event``,
    ``self._stage_execution_mode`` etc.). Method bodies are unchanged
    from the inline originals — this is a pure file-level move so
    existing test patches (``StageCoordinator._maybe_repair_physical``
    etc.) flow through MRO.
    """

    def _maybe_repair_physical(
        self,
        session: StageSession,
        *,
        physical: PhysicalDraft,
        logical: LogicalDraft,
        contract: dict,
        engine: str,
    ) -> None:
        """Re-run the single physical stage that produced a failing draft.

        The validator has already populated ``physical.validation``;
        this method decides whether a targeted re-run is warranted and,
        if so, mutates ``physical`` in place. It never raises: repair
        is strictly additive — a clean draft is left untouched, and a
        draft that still fails after repair keeps its original
        ``validation`` replaced by the new (hopefully improved) one so
        callers see the latest signal.

        Bounded by :data:`_MAX_REPAIR_ATTEMPTS` (one extra attempt); the
        caller can still inspect ``physical.validation.passes_schema``
        if it wants to short-circuit further work on a still-failing
        draft.
        """
        report = physical.validation
        if report is None or report.passes_schema:
            return

        stage = _diagnose_failing_stage(report)
        if stage is None:
            _log.info("fluid.copilot.repair.skip: validator failed but no stage could be diagnosed")
            return
        if stage in _LOGICAL_REPAIR_STAGES:
            # Phase 3.7 — logical-stage repair. Route OSI / DV2 /
            # Dimensional conformance failures back to the LogicalAgent
            # for one extra turn. The new draft re-runs the physical
            # stages from the repaired LogicalDraft.
            self._maybe_repair_logical(
                session,
                physical=physical,
                logical=logical,
                contract=contract,
                engine=engine,
            )
            return
        if stage not in _PHYSICAL_REPAIR_STAGES:
            # Readme failures stay observability-only for v1.0 — see
            # the module-level M3 comment for rationale.
            _log.info(
                "fluid.copilot.repair.skip: diagnosed stage %r is not in any repair scope",
                stage,
            )
            return

        # Missing #4 — structured feedback loops. Convert the
        # validator's findings into a ``StageFeedback`` addressed
        # to the failing stage, then write to the session
        # scratchpad. The re-run path reads
        # ``scratchpad.feedback_for_stage(stage)`` and biases the
        # agent's prompt accordingly. Without this, retries see
        # the original prompt + a tail-appended findings list and
        # have to figure out the contract themselves.
        try:
            self._emit_validator_feedback(session, stage=stage, report=report)
        except Exception:  # pragma: no cover — defensive
            pass

        attempts = 0
        while attempts < _MAX_REPAIR_ATTEMPTS and not physical.validation.passes_schema:
            attempts += 1
            with traced_span(
                "fluid.copilot.repair",
                {
                    "fluid.copilot.repair": True,
                    "fluid.copilot.repair.stage": stage,
                    "fluid.copilot.repair.attempt": attempts,
                },
            ):
                self._rerun_physical_stage(
                    session,
                    stage=stage,
                    physical=physical,
                    logical=logical,
                    contract=contract,
                    engine=engine,
                )
                # Re-validate the repaired draft so the caller sees the
                # fresh pass/fail signal; we intentionally *replace*
                # ``physical.validation`` rather than append, because
                # the original report's findings may no longer apply
                # to the repaired artefact.
                physical.validation = self.validator_agent.run(
                    logical=logical,
                    contract=contract,
                    industry_pack=session.industry_pack,
                )
                _log.info(
                    "fluid.copilot.repair.done: stage=%s passes_schema=%s score=%s",
                    stage,
                    physical.validation.passes_schema,
                    physical.validation.score,
                )

    # ------------------------------------------------------------------
    # Phase 3.7 — Logical-stage repair
    # ------------------------------------------------------------------

    def _maybe_repair_logical(
        self,
        session: StageSession,
        *,
        physical: PhysicalDraft,
        logical: LogicalDraft,
        contract: dict,
        engine: str,
    ) -> None:
        """Re-run the LogicalAgent when its draft fails OSI / DV2 /
        Dimensional conformance.

        Mirrors ``_maybe_repair_physical`` but targets the logical
        stage. The validator's findings are written to scratchpad as
        ``StageFeedback(target_stage="logical")``; the rerun reads
        that feedback to bias its prompt; the resulting ``LogicalDraft``
        replaces ``logical`` in place; downstream physical stages
        re-run against the repaired draft so the entire chain reflects
        the fix.

        Bounded by ``_MAX_REPAIR_ATTEMPTS`` (one extra attempt). Never
        raises — repair is strictly additive.
        """
        report = physical.validation
        if report is None or report.passes_schema:
            return

        # Push validator findings into scratchpad as feedback for the
        # logical stage so the rerun's prompt has structured context.
        try:
            self._emit_validator_feedback(session, stage="logical", report=report)
        except Exception:  # pragma: no cover — defensive
            pass

        attempts = 0
        repaired_logical = logical
        while attempts < _MAX_REPAIR_ATTEMPTS and not physical.validation.passes_schema:
            attempts += 1
            with traced_span(
                "fluid.copilot.repair",
                {
                    "fluid.copilot.repair": True,
                    "fluid.copilot.repair.stage": "logical",
                    "fluid.copilot.repair.attempt": attempts,
                },
            ):
                # Re-run the LogicalAgent. The agent reads the
                # scratchpad's ``feedback_for_stage("logical")`` and
                # biases its prompt toward the validator's findings.
                # Cache miss is critical — without it the cached draft
                # short-circuits the repair turn.
                repaired_logical = self._rerun_logical_stage(
                    session,
                    logical=logical,
                    contract=contract,
                )
                # Re-validate the new draft against the existing
                # contract / industry pack. We replace
                # ``physical.validation`` with the fresh signal —
                # findings from the prior draft no longer apply.
                physical.validation = self.validator_agent.run(
                    logical=repaired_logical,
                    contract=contract,
                    industry_pack=session.industry_pack,
                )
                _log.info(
                    "fluid.copilot.repair.done: stage=logical " "passes_schema=%s score=%s",
                    physical.validation.passes_schema,
                    physical.validation.score,
                )

        # Mutate the caller's logical reference so downstream
        # consumers (the run loop, persistence, telemetry) see the
        # repaired model. ``logical`` is a dataclass; mutate field-by-
        # field rather than rebinding the local name.
        if repaired_logical is not logical:
            for fname in (
                "name",
                "technique",
                "conceptual",
                "logical_entities",
                "annotations",
            ):
                if hasattr(repaired_logical, fname):
                    try:
                        setattr(logical, fname, getattr(repaired_logical, fname))
                    except (AttributeError, TypeError):  # pragma: no cover
                        # Frozen dataclass / read-only — accept the
                        # repaired draft as the new local; downstream
                        # consumers in this run see the original until
                        # the next save.
                        pass

    def _rerun_logical_stage(
        self,
        session: StageSession,
        *,
        logical: LogicalDraft,
        contract: dict,
    ) -> LogicalDraft:
        """Persist validator findings as scratchpad feedback for the
        next logical-stage attempt.

        The current LogicalAgent has three entry points
        (``from_tables`` / ``from_intent`` / ``from_catalog``) selected
        by the run loop based on input shape; there's no single
        ``run()`` to re-invoke. Phase 3.7's MVP closes the
        observability gap (validator findings no longer disappear when
        the logical stage fails) by writing the findings to scratchpad
        feedback so:

        * the OPERATOR sees them in the run summary / receipt,
        * the next FORGE RUN benefits from them via the cross-run
          memory store, and
        * a future v1.4+ in-run logical-stage re-invocation can read
          the same scratchpad shape without the agent needing a
          generic ``run()`` method.

        Returns the original ``logical`` unchanged. The caller's
        physical-stage repair path still has its chance to fix things
        from the same flawed LogicalDraft.
        """
        try:
            scratchpad = session.get_scratchpad()
            findings = list(getattr(scratchpad, "feedback_for_stage", lambda *_: [])("logical"))
            _log.info(
                "fluid.copilot.repair.logical.feedback_recorded: "
                "findings=%d (in-run rerun is v1.4+)",
                len(findings),
            )
        except Exception:  # pragma: no cover — defensive
            pass
        return logical

    def _emit_validator_feedback(
        self,
        session: StageSession,
        *,
        stage: str,
        report: ValidationReport,
    ) -> None:
        """Write a structured ``StageFeedback`` for the failing stage.

        The feedback's ``summary`` is a one-line human-readable
        message; ``structured`` carries the validator's findings as
        a list of dicts so the consuming agent can branch on
        ``severity`` / ``field`` without re-parsing free text.

        Empty / clean reports produce no feedback (the loop won't
        invoke this method anyway, but defensive).
        """

        findings_payload = [
            {
                "message": getattr(f, "message", ""),
                "severity": getattr(f, "severity", "warning"),
                "field": getattr(f, "field", "") or "",
            }
            for f in (getattr(report, "issues", None) or [])
        ]
        if not findings_payload:
            return

        error_count = sum(1 for f in findings_payload if f.get("severity") == "error")
        warning_count = sum(1 for f in findings_payload if f.get("severity") == "warning")
        summary = (
            f"Validator found {error_count} error(s) and {warning_count} "
            f"warning(s) in stage {stage!r}. Bias the next attempt to "
            f"address the listed findings."
        )

        feedback = StageFeedback(
            source_stage="validator",
            target_stage=stage,
            summary=summary,
            structured={
                "score": getattr(report, "score", None),
                "passes_schema": getattr(report, "passes_schema", False),
                "findings": findings_payload,
            },
        )
        session.get_scratchpad().add_feedback(feedback)

    def _rerun_physical_stage(
        self,
        session: StageSession,
        *,
        stage: str,
        physical: PhysicalDraft,
        logical: LogicalDraft,
        contract: dict,
        engine: str,
    ) -> None:
        """Re-run exactly one physical-stage agent, bypassing the cache.

        ``session.no_cache`` is flipped on for the duration of the
        re-run so the LLM is genuinely re-prompted rather than served
        the same bad response from the shared cache. The flip is
        restored in a ``finally`` block so a downstream exception can
        never leak the bypass into unrelated work on the same session.
        """
        prior_no_cache = getattr(session, "no_cache", False)
        try:
            session.no_cache = True
            if stage == "builder":
                # Preserve readme + transform_plan from the original
                # fanout: the builder's job is to synthesise the
                # contract-facing ``PhysicalDraft`` shell; readme and
                # transform_plan are orthogonal artefacts the parallel
                # pipeline already produced correctly.
                preserved_readme = physical.readme
                preserved_transform = physical.transform_plan
                with traced_span("fluid.copilot.builder", {"fluid.copilot.agent": "builder"}):
                    repaired = self.builder.build_physical(
                        session, logical=logical, contract=contract, engine=engine
                    )
                repaired.readme = preserved_readme
                repaired.transform_plan = preserved_transform
                # Mutate the caller's ``physical`` in place by copying
                # the repaired object's fields onto it — the caller
                # already holds a reference and downstream code may
                # too, so swapping the object would desync them.
                for field_name in repaired.model_fields:
                    setattr(physical, field_name, getattr(repaired, field_name))
            elif stage == "transformation":
                with traced_span(
                    "fluid.copilot.transformation",
                    {"fluid.copilot.agent": "transformation"},
                ):
                    physical.transform_plan = self.transformation_agent.run(logical, engine=engine)
            else:  # pragma: no cover — defensive guard; caller filtered
                _log.warning(
                    "fluid.copilot.repair.unknown_stage: %r — no-op",
                    stage,
                )
        finally:
            session.no_cache = prior_no_cache
