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

"""Staged orchestration for modeler + builder flows.

The coordinator orchestrates the five fine-grained v1.3 agents
(``LogicalAgent`` → {``BuilderAgent``, ``ReadmeAgent``,
``TransformationAgent``} → ``ValidatorAgent``). The physical-stage
fanout between Builder / Readme / Transformation runs on a
``ThreadPoolExecutor`` (M1): readme and transformation depend only on
the ``LogicalDraft`` and neither reads the other's output, so the
wall-clock cost of readme generation overlaps the builder's work and
comes off the critical path entirely on a cold cache. Warm-cache runs
(all three agent calls short-circuit through the shared LLM cache)
pay the thread-pool spin-up as their only extra cost — still net-zero
because the cached calls return in microseconds.

A single env-var escape hatch, ``FLUID_COPILOT_PARALLEL_PHYSICAL=0``,
forces serial execution when a user's custom store backend turns out
not to be thread-safe in their environment. The default is parallel.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional, Sequence

import yaml

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.builder_agent import BuilderAgent
from fluid_build.copilot.agents.contract_forge_agent import ContractForgeAgent
from fluid_build.copilot.agents.logical_agent import LogicalAgent
from fluid_build.copilot.agents.readme_agent import ReadmeAgent
from fluid_build.copilot.agents.transformation_agent import TransformationAgent
from fluid_build.copilot.agents.validator_agent import ValidatorAgent
from fluid_build.copilot.checkpoint import (
    CheckpointStore,
    JsonStageSerializer,
    get_default_saver,
)
from fluid_build.copilot.schemas.intent import BusinessIntent
from fluid_build.copilot.schemas.stage_outputs import (
    LogicalDraft,
    PhysicalDraft,
    ValidationReport,
)
from fluid_build.copilot.store.semantic_writer import write_semantic_record
from fluid_build.forge_datamodel.from_ddl.parser import TableDefinition
from fluid_build.forge_datamodel.logical_canonicalizer import canonicalize_logical_draft
from fluid_build.llm.providers import get_catalog_tier_model
from fluid_build.observability.tracing import traced_span

_log = logging.getLogger(__name__)
# StageCoordinator helpers — physically extracted to
# ``copilot/agents/_coordinator_helpers.py``. Pure functions /
# constants that don't depend on coordinator state. Re-exported
# under their original ``_``-prefixed names so existing call sites
# (``self._diagnose_failing_stage(...)`` etc.) keep resolving.
from fluid_build.copilot.agents._coordinator_helpers import (  # noqa: F401
    LOGICAL_REPAIR_STAGES as _LOGICAL_REPAIR_STAGES,
)
from fluid_build.copilot.agents._coordinator_helpers import (  # noqa: F401
    MAX_REPAIR_ATTEMPTS as _MAX_REPAIR_ATTEMPTS,
)
from fluid_build.copilot.agents._coordinator_helpers import (  # noqa: F401
    PHYSICAL_REPAIR_STAGES as _PHYSICAL_REPAIR_STAGES,
)
from fluid_build.copilot.agents._coordinator_helpers import (  # noqa: E402,F401
    CoordinatorResult,
)
from fluid_build.copilot.agents._coordinator_helpers import (  # noqa: F401
    diagnose_failing_stage as _diagnose_failing_stage,
)
from fluid_build.copilot.agents._coordinator_helpers import (  # noqa: F401
    new_run_id as _new_run_id,
)
from fluid_build.copilot.agents._coordinator_helpers import (  # noqa: F401
    parallel_physical_enabled as _parallel_physical_enabled,
)
from fluid_build.copilot.agents._coordinator_repair import _RepairLoopMixin


class StageCoordinator(_RepairLoopMixin):
    """Coordinate staged data-model and physical planning flows."""

    def __init__(
        self,
        *,
        checkpoint_store: Optional[CheckpointStore] = None,
    ) -> None:
        # Coordinator routes through the fine-grained v1.3 split
        # (LogicalAgent + BuilderAgent + ReadmeAgent + TransformationAgent +
        # ValidatorAgent). ``ModelerAgent`` and ``ConceptualAgent`` remain
        # available as classes — LogicalAgent and ConceptualAgent compose
        # ModelerAgent internally — but are intentionally *not* instantiated
        # here; direct use was deprecated in favour of the LogicalAgent entry
        # point.
        self.builder = BuilderAgent()
        self.contract_forge_agent = ContractForgeAgent()
        self.logical_agent = LogicalAgent()
        self.readme_agent = ReadmeAgent()
        self.transformation_agent = TransformationAgent()
        self.validator_agent = ValidatorAgent()
        # V1.5 Sprint E — pre-emit conformance lint. The agent runs
        # the same FluidContractValidator + OSI checks the post-emit
        # ValidatorAgent uses, but BEFORE the contract reaches disk.
        # Imported lazily so callers that never construct a
        # StageCoordinator don't pay for the import cost (e.g.
        # ``fluid validate <existing-contract>`` doesn't need it).
        from fluid_build.copilot.agents.conformance_agent import (
            ConformanceAgent,
        )
        from fluid_build.copilot.agents.critic_agent import CriticAgent

        self.conformance_agent = ConformanceAgent()
        # Missing #2 — proactive heuristic critic. Reviews each
        # stage's output and writes findings to the session
        # scratchpad so the repair loop can read them on retry.
        self.critic_agent = CriticAgent()
        # Checkpointing — resume the staged pipeline on Ctrl-C / OOM.
        # ``None`` means "lazy-resolve via get_default_saver() on first
        # use". Tests pass an explicit ``checkpoint_store=`` (mock or
        # ``NullCheckpointStore``) to bypass the env-var dispatch.
        self._explicit_saver = checkpoint_store
        self._saver: Optional[CheckpointStore] = None
        # Cost tracker reading is process-wide; the coordinator computes
        # the per-stage USD delta by remembering the running total at
        # the last checkpoint write. Lock protects parallel-physical
        # writes that race for the same delta-base.
        self._cost_at_last_checkpoint: float = 0.0
        self._cost_lock = threading.Lock()
        # Hash of the contract once contract_forge lands. Stamped onto
        # every later stage's checkpoint so the CLI's stale-detector
        # can spot edits between the user pausing and resuming.
        self._contract_hash: Optional[str] = None

    # ------------------------------------------------------------------
    # Checkpoint helpers
    # ------------------------------------------------------------------

    def _get_saver(self) -> CheckpointStore:
        """Lazy-resolve the saver.

        Honours the explicit ``checkpoint_store=`` kwarg first;
        otherwise falls back to ``get_default_saver()`` (which dispatches
        on ``FLUID_COPILOT_CHECKPOINT``). Caches the result so every
        stage uses the same backend within one run.
        """
        if self._saver is not None:
            return self._saver
        self._saver = self._explicit_saver or get_default_saver()
        return self._saver

    def _resolve_run_id(self, session: StageSession) -> str:
        """Pick — and stamp onto the session — the run-id used for
        checkpoint keys.

        Idempotent: a session that already carries ``run_id`` keeps it
        (so a resume call deliberately constructed with the prior id
        finds its prior cursors). Otherwise we mint a new short id and
        stamp it back so observability spans share the same id.
        """
        run_id = getattr(session, "run_id", "") or _new_run_id()
        try:
            session.run_id = run_id
        except Exception:  # pragma: no cover — defensive (frozen dataclass)
            pass
        return run_id

    def _stage_cost_delta(self) -> float:
        """Compute USD spent since the last checkpoint write.

        Reads the cost-tracker's ``breakdown().total_usd`` and subtracts
        the running base. Updates the base under a lock so two parallel
        workers don't double-count or skip cost. When the tracker
        returns ``None`` (unknown model in the price table) the delta
        falls back to ``0.0`` so the checkpoint write doesn't fail.
        """
        try:
            from fluid_build.copilot.cost import get_run_tracker

            total = get_run_tracker().breakdown().total_usd
        except Exception:  # pragma: no cover — defensive
            total = None
        with self._cost_lock:
            base = self._cost_at_last_checkpoint
            if total is None:
                # Unknown total — don't move the base, return 0 so the
                # write still happens with an honest "I don't know"
                # cost figure rather than a misleading delta.
                return 0.0
            delta = max(0.0, float(total) - base)
            self._cost_at_last_checkpoint = float(total)
        return delta

    def _save_stage(
        self,
        run_id: str,
        stage: str,
        payload: Any,
    ) -> None:
        """Write one stage's cursor.

        ``cost_usd`` is the per-stage delta computed from the global
        cost tracker; ``contract_hash`` is the canonical hash stamped
        after ``contract_forge`` (None for the logical stage).

        Best-effort: a checkpoint write failure logs at WARNING and
        returns. Checkpointing must NEVER poison a forge that
        otherwise succeeded.
        """
        try:
            saver = self._get_saver()
            saver.put(
                run_id,
                stage,
                payload,
                cost_usd=self._stage_cost_delta(),
                contract_hash=self._contract_hash,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            _log.warning(
                "checkpoint write for stage %r failed (%s); continuing",
                stage,
                exc,
            )

    @staticmethod
    def _hash_contract(contract: Dict[str, Any]) -> str:
        """Canonical sha256 of a contract dict.

        Recipe — ``yaml.safe_dump(contract, sort_keys=True)`` →
        ``encode("utf-8")`` → ``sha256().hexdigest()``. ``sort_keys=True``
        is load-bearing: Python dict iteration order differs between
        runs, and any drift in the serialised bytes would make every
        resume look stale. ``yaml.safe_dump`` (not ``json.dumps``) is
        chosen because the contracts that hit disk are YAML and the
        operator-facing hash should match what's written.
        """
        try:
            blob = yaml.safe_dump(contract, sort_keys=True, default_flow_style=False)
        except Exception:  # pragma: no cover — defensive
            # Fall back to json — same byte-deterministic property.
            blob = json.dumps(contract, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _run_contract_forge_stage(
        self,
        session: StageSession,
        *,
        run_id: str,
        logical: LogicalDraft,
        engine: str,
    ) -> Dict[str, Any]:
        """Run (or restore) the contract_forge stage.

        Centralised so the three entry methods (from_tables / from_intent
        / from_catalog) all share the same checkpoint shape. The forged
        contract's canonical hash is stamped on ``self._contract_hash``
        so every later stage's checkpoint carries it — that's what the
        CLI's stale-detector compares against.
        """
        saver = self._get_saver()
        with traced_span(
            "fluid.copilot.contract_forge",
            {"fluid.copilot.agent": "contract_forge"},
        ):
            with saver.skip_if_done(run_id, "contract_forge") as restored:
                if restored is not None:
                    # The contract is a plain dict; deserialize without
                    # an expected_type. ``restored.payload`` was stored
                    # as a dict, so json.dumps→deserialize is the cheap
                    # round-trip path.
                    contract = JsonStageSerializer.deserialize(
                        restored.payload_kind,
                        json.dumps(restored.payload, default=str),
                        expected_type=None,
                    )
                    # Restore the hash from the cursor so later stages
                    # see the same value — avoids a re-hash that could
                    # diverge on contract dict ordering.
                    self._contract_hash = restored.contract_hash or self._hash_contract(contract)
                else:
                    contract = self.contract_forge_agent.forge_contract(
                        session, logical=logical, engine=engine
                    )
                    self._contract_hash = self._hash_contract(contract)
                    self._save_stage(run_id, "contract_forge", contract)
        return contract

    def _maybe_run_post_synthesis_stages(
        self,
        *,
        run_id: str,
        contract: Dict[str, Any],
        physical: Optional[PhysicalDraft],
    ) -> None:
        """Run the final two checkpointed stages: enrichment + judge.

        Both are fail-open — a failure in either logs at DEBUG and
        the run still returns a CoordinatorResult to the caller. The
        purpose of running them here (rather than in the runtime as
        before) is to bring them inside the checkpoint envelope so
        a Ctrl-C between validator and judge doesn't lose 30 seconds
        of work.

        Skipped entirely when ``physical`` is None — without a
        validated physical artifact there's no enrichment / judge
        signal to write.
        """
        if physical is None:
            return
        # Gate: only run when post-synthesis is configured by the
        # runtime. The legacy runtime path still runs enrichment/judge
        # directly; the staged path through the coordinator gets them
        # under the checkpoint envelope. Operators who want to skip
        # entirely set FLUID_COPILOT_POST_SYNTHESIS=0.
        if os.environ.get("FLUID_COPILOT_POST_SYNTHESIS") == "0":
            return
        saver = self._get_saver()
        # ----- enrichment -------------------------------------------
        with saver.skip_if_done(run_id, "enrichment") as restored:
            if restored is not None:
                enrichment_artifacts: Optional[Dict[str, Any]] = (
                    restored.payload if isinstance(restored.payload, dict) else None
                )
            else:
                enrichment_artifacts = self._run_enrichment_stage(contract)
                # Always save — the dict-or-None payload IS the cursor.
                # ``None`` means "we tried and got nothing"; the resume
                # path treats it the same as a hit so we don't re-run.
                self._save_stage(
                    run_id,
                    "enrichment",
                    enrichment_artifacts if enrichment_artifacts is not None else {},
                )
        # Stash on the physical so callers see the enrichment in the
        # provenance block without a separate read.
        try:
            physical.provenance = dict(physical.provenance or {})
            physical.provenance["enrichment_artifacts"] = enrichment_artifacts
        except Exception:  # pragma: no cover — defensive
            pass
        # ----- judge -------------------------------------------------
        with saver.skip_if_done(run_id, "judge") as restored:
            if restored is not None:
                judge_result: Optional[Dict[str, Any]] = (
                    restored.payload if isinstance(restored.payload, dict) else None
                )
            else:
                judge_result = self._run_judge_stage(contract, build_artifacts=enrichment_artifacts)
                self._save_stage(
                    run_id,
                    "judge",
                    judge_result if judge_result is not None else {},
                )
        try:
            physical.provenance = dict(physical.provenance or {})
            physical.provenance["judge_result"] = judge_result
        except Exception:  # pragma: no cover — defensive
            pass

    def _run_enrichment_stage(
        self,
        contract: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Best-effort enrichment pass. Returns ``None`` on any failure
        — checkpointing is orthogonal to enrichment success."""
        try:
            from fluid_build.copilot.enrichment import enrich_contract

            return enrich_contract(contract)
        except Exception as exc:  # noqa: BLE001 — fail-open
            _log.debug("enrichment stage failed (skipping): %s", exc)
            return None

    def _run_judge_stage(
        self,
        contract: Dict[str, Any],
        *,
        build_artifacts: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Best-effort LLM-as-judge pass. Returns ``None`` on any
        failure or when the judge is disabled by env-var."""
        try:
            from fluid_build.copilot.agents.judge_agent import JudgeAgent

            result = JudgeAgent().judge(contract, build_artifacts=build_artifacts)
            return {
                "score": getattr(result, "total", None),
                "axes": {
                    axis: getattr(score, "score", None)
                    for axis, score in (getattr(result, "axes", {}) or {}).items()
                },
                "model": getattr(result, "model", None),
            }
        except Exception as exc:  # noqa: BLE001 — fail-open
            _log.debug("judge stage failed (skipping): %s", exc)
            return None

    def from_tables(
        self,
        session: StageSession,
        *,
        name: str,
        tables: Sequence[TableDefinition],
        technique: str,
        source_type: Optional[str] = None,
        engine: str = "dbt",
        include_physical: bool = False,
    ) -> CoordinatorResult:
        # Phase 3.8 — outermost ``fluid.copilot.staged.invocation`` span
        # ties every nested span (coordinator, logical, builder, ...)
        # under one run_id so observability dashboards can group the
        # full staged-forge lifecycle. No-op when OTEL is disabled.
        run_id = self._resolve_run_id(session)
        # Per-product cost attribution — push the project name onto
        # the cost tracker's product stack so every LLM call inside
        # the run credits the right product. Pops in the ``finally``
        # below so a mid-run exception still releases the slot.
        from fluid_build.copilot.cost import get_run_tracker

        _cost_tracker = get_run_tracker()
        _cost_tracker.push_product(name)
        try:
            with (
                traced_span(
                    "fluid.copilot.staged.invocation",
                    {
                        "fluid.copilot.run_id": run_id,
                        "fluid.copilot.entry": "tables",
                        "fluid.copilot.technique": technique,
                        "fluid.copilot.engine": engine,
                        "fluid.copilot.include_physical": include_physical,
                        "fluid.copilot.product_id": name,
                    },
                ),
                traced_span(
                    "fluid.copilot.coordinator.from_tables",
                    {
                        "fluid.copilot.entry": "tables",
                        "fluid.copilot.technique": technique,
                        "fluid.copilot.engine": engine,
                        "fluid.copilot.table_count": len(tables),
                        "fluid.copilot.include_physical": include_physical,
                    },
                ),
            ):
                with traced_span("fluid.copilot.logical", {"fluid.copilot.agent": "logical"}):
                    logical_budget = self._stage_budget(session, stage="logical")
                    saver = self._get_saver()
                    with saver.skip_if_done(run_id, "logical") as restored:
                        if restored is not None:
                            logical = JsonStageSerializer.deserialize(
                                restored.payload_kind,
                                json.dumps(restored.payload, default=str),
                                expected_type=LogicalDraft,
                            )
                        else:
                            logical = self._run_logical_with_cooperation(
                                session,
                                agent_invoke=lambda: self.logical_agent.from_tables(
                                    session,
                                    name=name,
                                    tables=list(tables),
                                    technique=technique,
                                    source_type=source_type,
                                ),
                            )
                            self._save_stage(run_id, "logical", logical)
                    self._check_stage_budget(logical_budget)
                logical = canonicalize_logical_draft(logical)
                self._record_agent_event(session, stage="logical", agent=self.logical_agent)
                self._run_logical_critic(session, logical=logical)
                self._stamp_annotation_summary(session)
                contract = self._run_contract_forge_stage(
                    session, run_id=run_id, logical=logical, engine=engine
                )
                self._record_agent_event(
                    session,
                    stage="contract_forge",
                    agent=self.contract_forge_agent,
                )
                physical = None
                if include_physical:
                    physical = self._run_physical_stages(
                        session,
                        logical=logical,
                        contract=contract,
                        engine=engine,
                        run_id=run_id,
                    )
                    contract = physical.contract
                self._maybe_run_post_synthesis_stages(
                    run_id=run_id, contract=contract, physical=physical
                )
                # D7 — auto-write memory/semantic on successful forge (opt-in).
                # Gated by FLUID_COPILOT_SEMANTIC_MEMORY; swallows errors so
                # a broken store never poisons a successful forge result.
                write_semantic_record(session.store, logical, source_type="tables")
                # A2 — episodic memory writer. Records a "forge.success"
                # event so future runs can resume / branch on prior
                # outcomes. Best-effort: a store error MUST NOT poison
                # a successful forge result.
                self._record_forge_episode(
                    session,
                    outcome="success",
                    source_type="tables",
                    logical=logical,
                )
                return CoordinatorResult(logical=logical, contract=contract, physical=physical)
        finally:
            _cost_tracker.pop_product()

    def from_intent(
        self,
        session: StageSession,
        *,
        intent: BusinessIntent,
        technique: str,
        engine: str = "dbt",
        include_physical: bool = False,
    ) -> CoordinatorResult:
        # Phase 3.8 — outermost ``fluid.copilot.staged.invocation`` span.
        run_id = self._resolve_run_id(session)
        # Per-product cost attribution — derive product_id from the
        # business intent (the entity name in the intent IS the
        # product). Pops in the matching ``finally`` so a mid-run
        # exception releases the slot.
        from fluid_build.copilot.cost import get_run_tracker

        _cost_tracker = get_run_tracker()
        product_id = (
            getattr(getattr(intent, "grain", None), "entity", None)
            or getattr(intent, "name", None)
            or "intent_product"
        )
        _cost_tracker.push_product(str(product_id))
        try:
            with (
                traced_span(
                    "fluid.copilot.staged.invocation",
                    {
                        "fluid.copilot.run_id": run_id,
                        "fluid.copilot.entry": "intent",
                        "fluid.copilot.technique": technique,
                        "fluid.copilot.engine": engine,
                        "fluid.copilot.include_physical": include_physical,
                        "fluid.copilot.product_id": str(product_id),
                    },
                ),
                traced_span(
                    "fluid.copilot.coordinator.from_intent",
                    {
                        "fluid.copilot.entry": "intent",
                        "fluid.copilot.technique": technique,
                        "fluid.copilot.engine": engine,
                        "fluid.copilot.include_physical": include_physical,
                    },
                ),
            ):
                with traced_span("fluid.copilot.logical", {"fluid.copilot.agent": "logical"}):
                    logical_budget = self._stage_budget(session, stage="logical")
                    saver = self._get_saver()
                    with saver.skip_if_done(run_id, "logical") as restored:
                        if restored is not None:
                            logical = JsonStageSerializer.deserialize(
                                restored.payload_kind,
                                json.dumps(restored.payload, default=str),
                                expected_type=LogicalDraft,
                            )
                        else:
                            logical = self._run_logical_with_cooperation(
                                session,
                                agent_invoke=lambda: self.logical_agent.from_intent(
                                    session,
                                    intent=intent,
                                    technique=technique,
                                ),
                            )
                            self._save_stage(run_id, "logical", logical)
                    self._check_stage_budget(logical_budget)
                logical = canonicalize_logical_draft(logical)
                self._record_agent_event(session, stage="logical", agent=self.logical_agent)
                self._run_logical_critic(session, logical=logical)
                self._stamp_annotation_summary(session)
                contract = self._run_contract_forge_stage(
                    session, run_id=run_id, logical=logical, engine=engine
                )
                self._record_agent_event(
                    session,
                    stage="contract_forge",
                    agent=self.contract_forge_agent,
                )
                physical = None
                if include_physical:
                    physical = self._run_physical_stages(
                        session,
                        logical=logical,
                        contract=contract,
                        engine=engine,
                        run_id=run_id,
                    )
                    contract = physical.contract
                self._maybe_run_post_synthesis_stages(
                    run_id=run_id, contract=contract, physical=physical
                )
                # D7 — auto-write memory/semantic on successful forge (opt-in).
                # See module-level comment in ``store.semantic_writer`` for
                # the privacy / predictability rationale behind the opt-in.
                write_semantic_record(session.store, logical, source_type="intent")
                # A2 — episodic memory writer.
                self._record_forge_episode(
                    session,
                    outcome="success",
                    source_type="intent",
                    logical=logical,
                )
                return CoordinatorResult(logical=logical, contract=contract, physical=physical)
        finally:
            _cost_tracker.pop_product()

    def from_catalog(
        self,
        session: StageSession,
        *,
        name: str,
        adapter: Any,
        scope: Any,
        technique: str,
        engine: str = "dbt",
        include_physical: bool = False,
    ) -> CoordinatorResult:
        """V1.5 — forge a model from a metadata-source catalog scope.

        Parallel structure to :meth:`from_intent` and
        :meth:`from_tables`; the only difference is the input feed
        (``adapter`` + ``scope``). Internally the catalog is
        enumerated, each table's full metadata fetched, then
        :meth:`LogicalAgent.from_catalog` translates the
        :class:`CatalogTable` list into the modeler's input shape.

        After the logical stage, the same Builder + Readme +
        Transformation + Validator agents run as for intent / DDL —
        no special-case code paths downstream. That keeps the
        contract and dbt project byte-identical regardless of how
        the user supplied input.

        ``adapter`` must implement
        :class:`fluid_build.copilot.catalog.base.CatalogAdapter`;
        ``scope`` is a
        :class:`fluid_build.copilot.catalog.models.CatalogScope`.
        Typed as ``Any`` here so this module avoids a hard import
        of the catalog subpackage (catalog imports are lazy at the
        CLI / MCP-tool layer).
        """
        # Phase 3.8 — outermost ``fluid.copilot.staged.invocation`` span.
        run_id = self._resolve_run_id(session)
        # Per-product cost attribution — the catalog scope's name is
        # the product being forged.
        from fluid_build.copilot.cost import get_run_tracker

        _cost_tracker = get_run_tracker()
        _cost_tracker.push_product(name)
        try:
            with (
                traced_span(
                    "fluid.copilot.staged.invocation",
                    {
                        "fluid.copilot.run_id": run_id,
                        "fluid.copilot.entry": "catalog",
                        "fluid.copilot.technique": technique,
                        "fluid.copilot.engine": engine,
                        "fluid.copilot.include_physical": include_physical,
                        "fluid.copilot.product_id": name,
                    },
                ),
                traced_span(
                    "fluid.copilot.coordinator.from_catalog",
                    {
                        "fluid.copilot.entry": "catalog",
                        "fluid.copilot.technique": technique,
                        "fluid.copilot.engine": engine,
                        "fluid.copilot.catalog_name": getattr(adapter, "name", "unknown"),
                        "fluid.copilot.include_physical": include_physical,
                    },
                ),
            ):
                with traced_span("fluid.copilot.logical", {"fluid.copilot.agent": "logical"}):
                    logical_budget = self._stage_budget(session, stage="logical")
                    saver = self._get_saver()
                    with saver.skip_if_done(run_id, "logical") as restored:
                        if restored is not None:
                            logical = JsonStageSerializer.deserialize(
                                restored.payload_kind,
                                json.dumps(restored.payload, default=str),
                                expected_type=LogicalDraft,
                            )
                        else:
                            logical = self._run_logical_with_cooperation(
                                session,
                                agent_invoke=lambda: self.logical_agent.from_catalog(
                                    session,
                                    name=name,
                                    adapter=adapter,
                                    scope=scope,
                                    technique=technique,
                                ),
                            )
                            self._save_stage(run_id, "logical", logical)
                    self._check_stage_budget(logical_budget)
                logical = canonicalize_logical_draft(logical)
                self._record_agent_event(session, stage="logical", agent=self.logical_agent)
                self._run_logical_critic(session, logical=logical)
                self._stamp_annotation_summary(session)
                contract = self._run_contract_forge_stage(
                    session, run_id=run_id, logical=logical, engine=engine
                )
                self._record_agent_event(
                    session,
                    stage="contract_forge",
                    agent=self.contract_forge_agent,
                )
                physical = None
                if include_physical:
                    physical = self._run_physical_stages(
                        session,
                        logical=logical,
                        contract=contract,
                        engine=engine,
                        run_id=run_id,
                    )
                    contract = physical.contract
                self._maybe_run_post_synthesis_stages(
                    run_id=run_id, contract=contract, physical=physical
                )
                # D7 — auto-write memory/semantic on successful forge (opt-in).
                write_semantic_record(
                    session.store,
                    logical,
                    source_type=f"catalog:{getattr(adapter, 'name', 'unknown')}",
                )
                # A2 — episodic memory writer.
                self._record_forge_episode(
                    session,
                    outcome="success",
                    source_type=f"catalog:{getattr(adapter, 'name', 'unknown')}",
                    logical=logical,
                )
                return CoordinatorResult(logical=logical, contract=contract, physical=physical)
        finally:
            _cost_tracker.pop_product()

    def _run_physical_stages(
        self,
        session: StageSession,
        *,
        logical: LogicalDraft,
        contract: dict,
        engine: str,
        run_id: Optional[str] = None,
    ) -> PhysicalDraft:
        """Run builder ∥ readme ∥ transformation, then validator, under spans.

        Builder, readme, and transformation all depend only on
        ``logical`` (and ``contract`` for builder); none of them reads
        another's output. So the three fan out concurrently across a
        small ``ThreadPoolExecutor`` and the coordinator re-attaches the
        parallel-produced ``readme`` and ``transform_plan`` onto the
        builder's ``PhysicalDraft`` before handing the combined result
        to the validator.

        The existing sequential code mutated ``physical.readme`` and
        ``physical.transform_plan`` after construction; we preserve that
        mutation pattern so callers see an identically-shaped
        ``PhysicalDraft`` whether parallel or serial execution ran.

        Thread-safety: ``logical`` is a Pydantic ``BaseModel`` — read
        access from multiple threads is safe (Pydantic v2 stores state
        as ordinary attrs; no shared-mutable state). The only shared
        writable resource is ``session.store``; the default
        ``FileBackend`` uses ``os.replace()`` for atomic writes and each
        agent's cache key is scoped by stage name, so concurrent puts
        hit disjoint paths. Non-default backends (Sqlite with WAL,
        Postgres via psycopg) rely on their drivers' thread-safety;
        the ``FLUID_COPILOT_PARALLEL_PHYSICAL=0`` escape hatch is the
        last-resort fallback when that assumption breaks.

        Checkpointing: each of builder/readme/transformation gets its
        own ``skip_if_done`` block inside the worker closure. A partial
        completion (e.g. readme landed, transformation crashed) means
        the next ``fluid forge`` run skips readme and re-runs
        transformation — borrowed from LangGraph's super-step / partial
        node writes pattern.
        """
        if not _parallel_physical_enabled():
            return self._run_physical_stages_serial(
                session,
                logical=logical,
                contract=contract,
                engine=engine,
                run_id=run_id,
            )

        # Resolve a run-id so the worker closures can call
        # ``skip_if_done`` without threading the value through.
        # When the caller didn't pass one (legacy callers), mint a
        # session-stamped id so the entire run shares one key space.
        run_id_resolved = run_id or self._resolve_run_id(session)
        saver = self._get_saver()

        def _run_builder() -> PhysicalDraft:
            with traced_span("fluid.copilot.builder", {"fluid.copilot.agent": "builder"}):
                with saver.skip_if_done(run_id_resolved, "builder") as restored:
                    if restored is not None:
                        return JsonStageSerializer.deserialize(
                            restored.payload_kind,
                            json.dumps(restored.payload, default=str),
                            expected_type=PhysicalDraft,
                        )
                    result = self.builder.build_physical(
                        session, logical=logical, contract=contract, engine=engine
                    )
                    self._save_stage(run_id_resolved, "builder", result)
                    return result

        def _run_readme():
            with traced_span("fluid.copilot.readme", {"fluid.copilot.agent": "readme"}):
                from fluid_build.copilot.schemas.stage_outputs import ReadmeDraft

                with saver.skip_if_done(run_id_resolved, "readme") as restored:
                    if restored is not None:
                        return JsonStageSerializer.deserialize(
                            restored.payload_kind,
                            json.dumps(restored.payload, default=str),
                            expected_type=ReadmeDraft,
                        )
                    result = self.readme_agent.run(logical, engine=engine)
                    self._save_stage(run_id_resolved, "readme", result)
                    return result

        def _run_transformation():
            with traced_span(
                "fluid.copilot.transformation", {"fluid.copilot.agent": "transformation"}
            ):
                from fluid_build.copilot.schemas.stage_outputs import TransformPlan

                with saver.skip_if_done(run_id_resolved, "transformation") as restored:
                    if restored is not None:
                        return JsonStageSerializer.deserialize(
                            restored.payload_kind,
                            json.dumps(restored.payload, default=str),
                            expected_type=TransformPlan,
                        )
                    result = self.transformation_agent.run(logical, engine=engine)
                    self._save_stage(run_id_resolved, "transformation", result)
                    return result

        # ``max_workers=3`` is a hard ceiling: three physical-stage
        # agents, no benefit to more threads, and a named prefix so
        # the threads are visible in ``py-spy``/``threading.enumerate()``
        # during debugging.
        #
        # We propagate the coordinator's contextvars to each worker so
        # OpenTelemetry's ``start_as_current_span`` parents the worker's
        # child span under the coordinator's span — not under the
        # thread's empty root context. Each submission gets its OWN
        # ``copy_context()`` because ``Context.run()`` raises
        # ``RuntimeError: cannot enter context: ... is already entered``
        # if two threads try to enter the same context object. Creating
        # the snapshot per submission gives every worker an independent,
        # isolated copy that can still see the coordinator's span as
        # parent at the moment of capture.
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="fluid-physical") as executor:
            builder_future = executor.submit(contextvars.copy_context().run, _run_builder)
            readme_future = executor.submit(contextvars.copy_context().run, _run_readme)
            transformation_future = executor.submit(
                contextvars.copy_context().run, _run_transformation
            )
            # Block on all three in turn. Any exception raised inside a
            # worker surfaces here via ``.result()`` — we propagate it
            # up the stack; the executor's ``__exit__`` waits for the
            # remaining workers to wind down before the exception
            # finishes bubbling out.
            physical = builder_future.result()
            physical.readme = readme_future.result()
            physical.transform_plan = transformation_future.result()
        self._record_agent_event(session, stage="builder", agent=self.builder)
        self._record_agent_event(session, stage="readme", agent=self.readme_agent)
        self._record_agent_event(session, stage="transformation", agent=self.transformation_agent)

        # Missing-#2 critic review of the contract — heuristic
        # findings land on the session scratchpad so the repair
        # loop can bias the BuilderAgent's prompt on retry.
        try:
            self.critic_agent.review_contract(
                contract,
                scratchpad=session.get_scratchpad(),
            )
            self.critic_agent.review_transform(
                physical.transform_plan,
                logical,
                scratchpad=session.get_scratchpad(),
            )
        except Exception:  # pragma: no cover — defensive
            pass

        # Pre-emit conformance lint (V1.5 Sprint E). Runs the
        # FluidContractValidator + OSI checks in-memory BEFORE the
        # contract is written to disk. Findings are surfaced through
        # the regular validation report; severity-error findings
        # block the post-emit validator's "passes_schema" gate so
        # the repair loop has a precise hook to act on.
        self._run_pre_emit_conformance(
            session,
            logical=logical,
            contract=contract,
        )

        with traced_span("fluid.copilot.validator", {"fluid.copilot.agent": "validator"}):
            with saver.skip_if_done(run_id_resolved, "validator") as restored:
                if restored is not None:
                    physical.validation = JsonStageSerializer.deserialize(
                        restored.payload_kind,
                        json.dumps(restored.payload, default=str),
                        expected_type=ValidationReport,
                    )
                else:
                    physical.validation = self.validator_agent.run(
                        logical=logical,
                        contract=contract,
                        industry_pack=session.industry_pack,
                        scratchpad=session.get_scratchpad(),
                    )
                    self._save_stage(run_id_resolved, "validator", physical.validation)
        self._record_agent_event(session, stage="validator", agent=self.validator_agent)
        # C8 — escalate critic-error findings into the validation
        # report so ``_maybe_repair_physical`` fires when the
        # critic finds errors the validator missed.
        self._escalate_critic_errors_into_report(session, physical=physical)
        self._maybe_repair_physical(
            session, physical=physical, logical=logical, contract=contract, engine=engine
        )
        return physical

    def _escalate_critic_errors_into_report(
        self,
        session: StageSession,
        *,
        physical: PhysicalDraft,
    ) -> None:
        """C8 — promote ``severity="error"`` critic findings into
        the validation report so the repair loop actually triggers.

        **Opt-in** via ``session.capability_matrix["critic_errors_trigger_repair"]``
        (default off for backwards compat). When enabled:

        1. Reads error-severity findings from the scratchpad.
        2. Appends each as a ``ValidationFinding(severity="error")``
           on ``physical.validation``.
        3. Flips ``passes_schema=False`` so
           ``_maybe_repair_physical`` enters the repair loop.

        Best-effort: any failure logs DEBUG and leaves the report
        unchanged — critic escalation must NEVER break a forge
        that the validator considers clean.
        """
        # Default: ON in v1.6+ — critic errors trigger the repair
        # loop. Operators who want the legacy "critic findings are
        # observability-only" behaviour can opt OUT via
        # ``capability_matrix["critic_errors_trigger_repair"] = False``.
        # Tests that don't want the repair-loop side effect should
        # set the flag to False explicitly.
        cm = session.capability_matrix or {}
        if cm.get("critic_errors_trigger_repair") is False:
            return
        try:
            scratchpad = session.get_scratchpad()
        except Exception:  # pragma: no cover — defensive
            return
        report = physical.validation
        if report is None:
            return
        from fluid_build.copilot.schemas.stage_outputs import ValidationFinding

        # Pull error-severity findings from every stage the critic
        # reviewed. The repair loop's diagnoser keys off the
        # ``field`` to pick which stage to rerun; we surface the
        # critic stage alongside so the diagnoser can branch.
        for stage in ("logical", "builder", "transformation"):
            for finding in scratchpad.critic_findings_for_stage(stage):
                if finding.severity != "error":
                    continue
                report.issues.append(
                    ValidationFinding(
                        message=f"[critic:{stage}] {finding.message}",
                        severity="error",
                        field=finding.target or stage,
                    )
                )
                report.passes_schema = False

    def _stage_budget(
        self,
        session: StageSession,
        *,
        stage: str,
    ) -> Any:
        """Item 9 — return a started :class:`StageBudget` for ``stage``.

        Resolution order for the limit:

        1. ``$FLUID_STAGE_BUDGET_<STAGE>_S`` env var (per-invocation).
        2. ``session.capability_matrix["stage_budgets"][<stage>]``.
        3. Built-in default per stage (``logical=600s``,
           ``builder=300s``, ``readme=120s``, ``transformation=300s``,
           ``validator=120s``).

        Returns a budget with ``start()`` already called so the
        caller wraps work in a ``with`` block via the matching
        :meth:`_check_stage_budget`.
        """
        from fluid_build.copilot.projections import StageBudget

        env_key = f"FLUID_STAGE_BUDGET_{stage.upper()}_S"
        env_value = os.environ.get(env_key)
        limit_s: float = 0.0
        if env_value:
            try:
                limit_s = float(env_value)
            except (TypeError, ValueError):
                limit_s = 0.0
        if limit_s <= 0:
            cm = session.capability_matrix or {}
            cfg_budgets = cm.get("stage_budgets") or {}
            if isinstance(cfg_budgets, dict):
                cfg_value = cfg_budgets.get(stage)
                if cfg_value is not None:
                    try:
                        limit_s = float(cfg_value)
                    except (TypeError, ValueError):
                        limit_s = 0.0
        if limit_s <= 0:
            defaults = {
                "logical": 600.0,
                "builder": 300.0,
                "readme": 120.0,
                "transformation": 300.0,
                "validator": 120.0,
            }
            limit_s = defaults.get(stage, 0.0)
        budget = StageBudget(stage=stage, limit_s=limit_s)
        budget.start()
        return budget

    def _record_agent_event(
        self,
        session: StageSession,
        *,
        stage: str,
        agent: Any,
    ) -> None:
        """Record an accountable owner for a completed stage."""
        mode = self._stage_execution_mode(session, stage=stage)
        tier = str(getattr(agent, "tier", "") or "")
        model = ""
        notes = ""
        if stage == "logical" and session.llm_config is not None:
            if session.tiered:
                tier = tier or "deep"
                provider_name = str(getattr(session.llm_config, "provider", "") or "")
                primary_model = str(getattr(session.llm_config, "model", "") or "")
                model = (
                    getattr(session.llm_config, "tier_models", {}).get(tier)
                    or (get_catalog_tier_model(provider_name, tier) if provider_name else "")
                    or primary_model
                )
            else:
                tier = "primary"
                model = str(getattr(session.llm_config, "model", "") or "")
        elif mode == "deterministic":
            notes = "deterministic stage; no LLM call"
        session.record_agent_event(
            stage=stage,
            agent=type(agent).__name__,
            mode=mode,
            tier=tier,
            model=model,
            notes=notes,
        )

    def _stage_execution_mode(self, session: StageSession, *, stage: str) -> str:
        if stage == "logical":
            if session.require_llm:
                return "strict_llm"
            if session.llm_config is not None and session.fallback_used:
                return "llm_with_fallback"
            if session.llm_config is not None:
                return "llm"
            return "heuristic"
        return "deterministic"

    def _check_stage_budget(self, budget: Any) -> None:
        """Item 9 — call after every staged operation. Best-effort:
        when the budget exceeds, raises ``StageBudgetExceeded``;
        otherwise no-op. ``budget.limit_s == 0`` disables
        enforcement (see :meth:`_stage_budget`)."""
        try:
            budget.check()
        except Exception:
            raise

    def _record_forge_episode(
        self,
        session: StageSession,
        *,
        outcome: str,
        source_type: str,
        logical: LogicalDraft,
    ) -> None:
        """A2 — write a ``forge.<outcome>`` event to ``memory/episodic``.

        Captures the forge's headline metadata (name, technique,
        source_type, basic counters) so future runs can:

        * Resume an interview with "last time you forged X with
          technique Y"; offer to repeat or change.
        * Down-rank repeated failures so the modeler doesn't keep
          suggesting a hub the operator has explicitly removed.
        * Provide an audit trail of what the operator forged when.

        Best-effort: any error in the store path is swallowed so a
        store failure can never poison a successful forge result.
        Honors ``FLUID_COPILOT_EPISODIC_MEMORY=0`` for opt-out
        symmetry with the semantic writer.
        """
        if os.environ.get("FLUID_COPILOT_EPISODIC_MEMORY") == "0":
            return
        try:
            from fluid_build.copilot.store.episodic import (
                record_episodic_event,
            )

            payload: Dict[str, Any] = {
                "outcome": outcome,
                "source_type": source_type,
                "model_name": getattr(logical, "name", "") or "",
                "technique": getattr(logical, "technique", "") or "",
            }
            # Item 6 — record cost so future runs' cost projections
            # have data to project from. ``total_usd`` may be ``None``
            # when an unknown model was used or when the run was
            # heuristic-only (no LLM calls). Issue 7 — for
            # heuristic-only runs we tag ``mode: heuristic`` and omit
            # the cost fields entirely so the projection table isn't
            # polluted with bogus $0/0-token rows.
            try:
                from fluid_build.copilot.cost import get_run_tracker

                breakdown = get_run_tracker().breakdown()
                if breakdown.total_calls > 0:
                    payload["mode"] = "llm"
                    payload["total_usd"] = breakdown.total_usd
                    payload["total_input_tokens"] = breakdown.total_input_tokens
                    payload["total_output_tokens"] = breakdown.total_output_tokens
                    payload["total_calls"] = breakdown.total_calls
                else:
                    payload["mode"] = "heuristic"
            except Exception:  # pragma: no cover — defensive
                pass
            # Light counters — useful for ranking without bloating
            # the store with full IR copies (memory/semantic stores
            # the full record; episodic is a slim event log).
            dv2 = getattr(logical, "dv2", None)
            if dv2 is not None:
                payload["dv2_counts"] = {
                    "hubs": len(getattr(dv2, "hubs", []) or []),
                    "links": len(getattr(dv2, "links", []) or []),
                    "satellites": len(getattr(dv2, "satellites", []) or []),
                }
            dimensional = getattr(logical, "dimensional", None)
            if dimensional is not None:
                payload["dimensional_counts"] = {
                    "facts": len(getattr(dimensional, "facts", []) or []),
                    "dimensions": len(getattr(dimensional, "dimensions", []) or []),
                }
            record_episodic_event(
                session.store,
                event_type=f"forge.{outcome}",
                payload=payload,
            )
        except Exception:  # pragma: no cover — defensive
            pass

    def _run_logical_with_cooperation(
        self,
        session: StageSession,
        *,
        agent_invoke: Callable[[], Any],
        max_attempts: int = 2,
    ) -> Any:
        """Item 5 — multi-turn modeler ↔ critic cooperation.

        **v1.6+ default: ON** with cost-aware short-circuit. The
        loop runs unless ``capability_matrix["critic_loop_enabled"]``
        is explicitly False OR the projected next-pass cost would
        exceed the cost ceiling.

        Capped at 2 attempts by default to bound cost; operators
        can raise via ``capability_matrix["critic_loop_max_attempts"]``.
        """
        cm = session.capability_matrix or {}
        # Default ON; explicit False opts out for a single-pass run.
        if cm.get("critic_loop_enabled") is False:
            return agent_invoke()
        # Cost-aware short-circuit: if a configured cost ceiling
        # would be exceeded by an extra pass, skip the loop and
        # save the operator's budget.
        if self._cooperation_would_exceed_budget(session):
            return agent_invoke()

        attempts_cap = int(cm.get("critic_loop_max_attempts") or max_attempts)
        from fluid_build.copilot.agents.cooperation_loop import (
            run_with_critic_loop,
        )

        def _agent(_feedback):
            # The modeler's prompt-builder reads scratchpad
            # feedback via ``_inject_scratchpad_signals`` so we
            # don't need to thread the feedback object through;
            # ``run_with_critic_loop`` writes it to the scratchpad
            # already.
            return agent_invoke()

        def _critic(output):
            # Re-run the heuristic critic against each pass's
            # output. Findings land on the scratchpad
            # automatically; we return the list so the loop can
            # decide whether to iterate.
            try:
                return self.critic_agent.review_logical(
                    output,
                    scratchpad=session.get_scratchpad(),
                )
            except Exception:
                return []

        outcome = run_with_critic_loop(
            stage="logical",
            agent_callable=_agent,
            critic_callable=_critic,
            scratchpad=session.get_scratchpad(),
            max_attempts=attempts_cap,
        )
        return outcome.output

    def _cooperation_would_exceed_budget(
        self,
        session: StageSession,
    ) -> bool:
        """Return True when running the cooperation loop would
        push past the configured cost ceiling.

        Conservative estimate: assume the next pass costs the
        average of all calls so far (or 5¢ baseline if no calls
        yet). Operators who explicitly set the ceiling expect
        the system to enforce it; the loop is the right place
        to skip rather than blow past at the next ``check_cost_ceiling``.
        """
        try:
            from fluid_build.copilot.cost import (
                _resolve_cost_limit_usd,
                get_run_tracker,
            )

            limit = _resolve_cost_limit_usd()
            if limit is None:
                return False
            breakdown = get_run_tracker().breakdown()
            running = breakdown.total_usd
            if running is None:
                return False  # unknown model — can't project
            calls = breakdown.total_calls or 0
            avg = (running / calls) if calls else 0.05
            return (running + avg) > limit
        except Exception:  # pragma: no cover — defensive
            return False

    def _stamp_annotation_summary(self, session: StageSession) -> None:
        """Item 5 — copy the scratchpad's :class:`AnnotationLog`
        summary onto the cost-tracker's module-level slot so the
        cost-summary footer can render the low-confidence count
        without threading the scratchpad through the print site.
        """
        try:
            from fluid_build.copilot.cost import set_annotation_summary

            log = session.get_scratchpad().get_annotations()
            set_annotation_summary(log.summary())
        except Exception:  # pragma: no cover — defensive
            pass

    def _run_logical_critic(
        self,
        session: StageSession,
        *,
        logical: LogicalDraft,
    ) -> None:
        """Sprint #8 — run :meth:`CriticAgent.review_logical` immediately
        after the LogicalAgent emits its draft.

        Findings land on the session scratchpad (severity error /
        warning / info) so the modeler can read them on a repair-
        loop retry — :func:`_inject_scratchpad_signals` in
        ``modeler_agent.py`` pulls
        ``critic_findings_for_stage("logical")`` into the next
        prompt.

        Best-effort: any agent failure is swallowed because the
        logical critic is observability + retry signal, NOT a hard
        gate. A clean modeler output should never be blocked by a
        critic crash.
        """
        try:
            self.critic_agent.review_logical(
                logical,
                scratchpad=session.get_scratchpad(),
            )
        except Exception:  # pragma: no cover — defensive
            pass

    def _run_pre_emit_conformance(
        self,
        session: StageSession,
        *,
        logical: LogicalDraft,
        contract: dict,
    ) -> None:
        """Run pre-emit conformance lint over ``logical`` + ``contract``.

        Two sub-passes (V1.5 Sprint E + Gap 10):

        1. **Dialect back-fill.** ``ConformanceAgent.apply_dialect_mapper``
           walks every OSI ``expression.dialects[]`` array, fills
           in missing dialects from the deterministic mapper, and
           flags drift. Runs against the OSI-supported dialect set
           (``ANSI_SQL | SNOWFLAKE | DATABRICKS``) so the back-fill
           can be safely written back into the model.
        2. **Standards lint.** ``ConformanceAgent.run`` validates
           the contract against Fluid 0.7.2 + OSI v0.1.1
           schemas. Findings are appended to
           ``session.discovery_report`` (or logged at INFO when
           no discovery report is attached) so operators see
           the conformance report alongside the usual validation
           output.

        Defensive: any exception in the agent path is swallowed
        and logged. Pre-emit conformance is observability + repair
        signal; it must NEVER block a forge that the rest of the
        pipeline considers valid.
        """
        try:
            # Pass 1 — dialect back-fill. The agent's default
            # targets are the OSI-validated subset of
            # ``DEFAULT_DIALECTS`` so the mutated OSI passes
            # Pydantic. (Gap 4 reconciled this — no need to pass
            # an explicit ``targets=`` list anymore.)
            self.conformance_agent.apply_dialect_mapper(logical)
            # Pass 2 — standards lint. Default standards
            # (``["fluid", "osi"]``) cover the two implemented
            # specs; ODCS / DCS placeholders stay opt-in.
            report = self.conformance_agent.run(
                logical=logical,
                contract=contract,
            )
            # Surface the report on the session so callers
            # (forge_data_model.py, mcp.py) can attach it to the
            # final receipt without rerunning the agent.
            summary = report.summary()
            session.capability_matrix.setdefault("pre_emit_conformance_summary", summary)
            # Sprint #5 — also stamp the process-wide slot so the
            # CLI's cost-summary print site can render the
            # conformance line in its receipt block. The session
            # ref isn't visible from that print site; the slot is
            # the bridge.
            try:
                from fluid_build.copilot.cost import (
                    set_pre_emit_conformance_summary,
                )

                set_pre_emit_conformance_summary(summary)
            except Exception:  # pragma: no cover — defensive
                pass
        except Exception as exc:  # pragma: no cover — defensive
            _log.debug(
                "pre-emit conformance skipped due to %s",
                exc,
                exc_info=True,
            )

    def _run_physical_stages_serial(
        self,
        session: StageSession,
        *,
        logical: LogicalDraft,
        contract: dict,
        engine: str,
        run_id: Optional[str] = None,
    ) -> PhysicalDraft:
        """Sequential fallback used when parallel fanout is disabled.

        Kept as a separate method so the code path is unambiguous when
        a user reports a threading-related bug: they can flip
        ``FLUID_COPILOT_PARALLEL_PHYSICAL=0`` and immediately land on
        this codepath with no other behavioural difference. Production
        default is the parallel path above. Checkpointing is applied
        identically here so the escape hatch doesn't lose resume.
        """
        from fluid_build.copilot.schemas.stage_outputs import ReadmeDraft, TransformPlan

        run_id_resolved = run_id or self._resolve_run_id(session)
        saver = self._get_saver()
        with traced_span("fluid.copilot.builder", {"fluid.copilot.agent": "builder"}):
            builder_budget = self._stage_budget(session, stage="builder")
            with saver.skip_if_done(run_id_resolved, "builder") as restored:
                if restored is not None:
                    physical = JsonStageSerializer.deserialize(
                        restored.payload_kind,
                        json.dumps(restored.payload, default=str),
                        expected_type=PhysicalDraft,
                    )
                else:
                    physical = self.builder.build_physical(
                        session, logical=logical, contract=contract, engine=engine
                    )
                    self._save_stage(run_id_resolved, "builder", physical)
            self._check_stage_budget(builder_budget)
        self._record_agent_event(session, stage="builder", agent=self.builder)
        with traced_span("fluid.copilot.readme", {"fluid.copilot.agent": "readme"}):
            readme_budget = self._stage_budget(session, stage="readme")
            with saver.skip_if_done(run_id_resolved, "readme") as restored:
                if restored is not None:
                    physical.readme = JsonStageSerializer.deserialize(
                        restored.payload_kind,
                        json.dumps(restored.payload, default=str),
                        expected_type=ReadmeDraft,
                    )
                else:
                    physical.readme = self.readme_agent.run(logical, engine=engine)
                    self._save_stage(run_id_resolved, "readme", physical.readme)
            self._check_stage_budget(readme_budget)
        self._record_agent_event(session, stage="readme", agent=self.readme_agent)
        with traced_span("fluid.copilot.transformation", {"fluid.copilot.agent": "transformation"}):
            tx_budget = self._stage_budget(session, stage="transformation")
            with saver.skip_if_done(run_id_resolved, "transformation") as restored:
                if restored is not None:
                    physical.transform_plan = JsonStageSerializer.deserialize(
                        restored.payload_kind,
                        json.dumps(restored.payload, default=str),
                        expected_type=TransformPlan,
                    )
                else:
                    physical.transform_plan = self.transformation_agent.run(logical, engine=engine)
                    self._save_stage(run_id_resolved, "transformation", physical.transform_plan)
            self._check_stage_budget(tx_budget)
        self._record_agent_event(session, stage="transformation", agent=self.transformation_agent)
        # Pre-emit conformance lint — same as the parallel path.
        self._run_pre_emit_conformance(
            session,
            logical=logical,
            contract=contract,
        )
        with traced_span("fluid.copilot.validator", {"fluid.copilot.agent": "validator"}):
            with saver.skip_if_done(run_id_resolved, "validator") as restored:
                if restored is not None:
                    physical.validation = JsonStageSerializer.deserialize(
                        restored.payload_kind,
                        json.dumps(restored.payload, default=str),
                        expected_type=ValidationReport,
                    )
                else:
                    physical.validation = self.validator_agent.run(
                        logical=logical,
                        contract=contract,
                        industry_pack=session.industry_pack,
                        scratchpad=session.get_scratchpad(),
                    )
                    self._save_stage(run_id_resolved, "validator", physical.validation)
        self._record_agent_event(session, stage="validator", agent=self.validator_agent)
        # C8 — escalate critic-error findings (serial path).
        self._escalate_critic_errors_into_report(session, physical=physical)
        self._maybe_repair_physical(
            session, physical=physical, logical=logical, contract=contract, engine=engine
        )
        return physical

    # ------------------------------------------------------------------
    # M3 — Targeted repair helpers
    # ------------------------------------------------------------------
