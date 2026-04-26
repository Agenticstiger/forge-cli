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

"""Regression pins for v1.7 wirings (items 1, 4, 5, 6, 7 from the
"world-class delta" sprint)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.cost import (
    get_annotation_summary,
    reset_run_tracker,
    set_annotation_summary,
)
from fluid_build.copilot.scratchpad import Scratchpad
from fluid_build.copilot.store.backends.null import NullBackend


@pytest.fixture(autouse=True)
def _hermetic():
    reset_run_tracker()
    set_annotation_summary(None)
    yield
    reset_run_tracker()
    set_annotation_summary(None)


# ----------------------------------------------------------------------
# Item 1 — tool research phase
# ----------------------------------------------------------------------


class TestItem1ToolResearch:
    def test_research_invokes_semantic_memory_tool(self, tmp_path):
        """When the registry has ``search_semantic_memory``, the
        modeler invokes it during the research phase."""
        from fluid_build.copilot.agents.modeler_agent import (
            _ensure_tool_registry,
            _run_tool_research_phase,
        )

        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        _ensure_tool_registry(session)
        # The default registry includes ``search_semantic_memory``
        # because we passed a (Null) store.
        assert "search_semantic_memory" in session.tool_registry.tools

        summaries = _run_tool_research_phase(
            session,
            name="customer_orders",
            tables=None,
        )
        # At least one tool was actually invoked.
        assert len(session.tool_registry.invocations) >= 1
        assert any(
            inv.tool_name == "search_semantic_memory" for inv in session.tool_registry.invocations
        )
        # Summary records the tool name.
        assert any(s["tool"] == "search_semantic_memory" for s in summaries)

    def test_research_with_no_registry_returns_empty(self, tmp_path):
        """No tool_registry on session → empty summary list, no
        invocations. Best-effort contract."""
        from fluid_build.copilot.agents.modeler_agent import (
            _run_tool_research_phase,
        )

        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        # Don't construct a registry.
        assert getattr(session, "tool_registry", None) is None
        assert _run_tool_research_phase(session, name="x") == []

    def test_inspect_table_called_for_each_table(self, tmp_path):
        """When inspect_table is registered, it fires for each input table."""
        from fluid_build.copilot.agent_tools import Tool, ToolRegistry
        from fluid_build.copilot.agents.modeler_agent import (
            _run_tool_research_phase,
        )
        from fluid_build.forge_datamodel.from_ddl.parser import (
            ColumnDefinition,
            TableDefinition,
        )

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="inspect_table",
                description="x",
                input_schema={"type": "object", "properties": {"fqn": {"type": "string"}}},
                handler=lambda fqn: {"fqn": fqn, "columns": []},
            )
        )
        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        session.tool_registry = registry  # type: ignore[attr-defined]

        tables = [
            TableDefinition(name="customers", columns=[]),
            TableDefinition(name="orders", columns=[]),
        ]
        _run_tool_research_phase(session, name="x", tables=tables)
        invs = registry.invocations
        # 2 inspect_table calls, one per table.
        inspect_calls = [i for i in invs if i.tool_name == "inspect_table"]
        assert len(inspect_calls) == 2
        assert {i.arguments["fqn"] for i in inspect_calls} == {"customers", "orders"}


# ----------------------------------------------------------------------
# Item 4 — operator edits → modeler prompt
# ----------------------------------------------------------------------


class TestItem4OperatorEditsInPrompt:
    def test_corrections_injected_into_payload(self, tmp_path):
        from fluid_build.copilot.agents.modeler_agent import (
            _inject_operator_corrections,
        )
        from fluid_build.copilot.learning import (
            OperatorEdit,
            record_operator_edits,
        )
        from fluid_build.copilot.store.backends.file import FileBackend

        backend = FileBackend(root=tmp_path / "store", workspace_root=tmp_path)
        session = StageSession(store=backend, workspace_root=tmp_path)

        # Pre-populate the store with operator edits.
        record_operator_edits(
            store=backend,
            contract_name="orders",
            edits=[
                OperatorEdit(
                    path="metadata.domain",
                    kind="modified",
                    before="commerce",
                    after="retail",
                )
            ],
        )

        payload: dict = {}
        _inject_operator_corrections(
            session,
            payload=payload,
            contract_name="orders",
        )
        # Corrections landed in the payload.
        assert "operator_corrections" in payload
        assert any(
            "metadata.domain" in c.get("summary", "") for c in payload["operator_corrections"]
        )

    def test_no_edits_no_payload_key(self, tmp_path):
        """When the store has no edits, the payload stays clean
        (no empty list polluting the LLM prompt)."""
        from fluid_build.copilot.agents.modeler_agent import (
            _inject_operator_corrections,
        )

        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        payload: dict = {}
        _inject_operator_corrections(
            session,
            payload=payload,
            contract_name="x",
        )
        assert "operator_corrections" not in payload


# ----------------------------------------------------------------------
# Item 5 — confidence + provenance read by validator + cost summary
# ----------------------------------------------------------------------


class TestItem5ConfidenceConsumed:
    def test_validator_escalates_low_confidence_as_warning(self):
        """A claim with confidence < 0.50 lands as a
        ``severity="warning"`` finding in the validation report."""
        from fluid_build.copilot.agents.validator_agent import ValidatorAgent
        from fluid_build.copilot.confidence import (
            ClaimProvenance,
            Confidence,
        )

        scratchpad = Scratchpad()
        scratchpad.get_annotations().annotate(
            "metadata.domain",
            confidence=Confidence(score=0.30, rationale="modeler synthesis"),
            provenance=ClaimProvenance(kind="modeler_synthesis", ref="x"),
        )
        # Also a high-confidence claim that should NOT escalate.
        scratchpad.get_annotations().annotate(
            "metadata.owner.team",
            confidence=Confidence(score=0.95, rationale="catalog tag"),
            provenance=ClaimProvenance(kind="catalog_tag", ref="y"),
        )

        # Empty contract — validator emits its own findings, plus
        # we expect the low-confidence one.
        report = ValidatorAgent().run(
            logical=None,
            contract={"exposes": [{"name": "x"}]},
            scratchpad=scratchpad,
        )
        # At least one warning about low-confidence.
        low_conf_findings = [
            f
            for f in report.issues
            if "Low-confidence" in f.message and "metadata.domain" in f.message
        ]
        assert len(low_conf_findings) == 1
        assert low_conf_findings[0].severity == "warning"
        # The high-confidence claim does NOT escalate.
        assert not any(
            "metadata.owner.team" in f.message and "Low-confidence" in f.message
            for f in report.issues
        )

    def test_cost_summary_footer_renders_low_confidence_count(self):
        from fluid_build.copilot.cost import (
            CostBreakdown,
            CostRow,
            format_cost_summary,
            set_annotation_summary,
        )

        # Stamp the slot with a summary indicating 2 low-confidence
        # claims.
        set_annotation_summary(
            {
                "annotation_count": 5,
                "confidence_levels": {"high": 3, "medium": 0, "low": 2, "unknown": 0},
                "provenance_kinds": {},
            }
        )
        breakdown = CostBreakdown(
            rows=[
                CostRow(
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    input_tokens=100,
                    output_tokens=50,
                    calls=1,
                    usd=0.001,
                )
            ],
            total_input_tokens=100,
            total_output_tokens=50,
            total_calls=1,
            total_usd=0.001,
            annotation_summary={
                "annotation_count": 5,
                "confidence_levels": {"high": 3, "medium": 0, "low": 2, "unknown": 0},
                "provenance_kinds": {},
            },
        )
        text = format_cost_summary(breakdown)
        assert "2 low-confidence claims" in text


# ----------------------------------------------------------------------
# Item 6 — total_usd in episodic events
# ----------------------------------------------------------------------


class TestItem6TotalUsdInEpisodic:
    def test_total_usd_recorded(self, tmp_path):
        from fluid_build.copilot.agents.coordinator import StageCoordinator
        from fluid_build.copilot.cost import get_run_tracker
        from fluid_build.copilot.store.backends.file import FileBackend

        backend = FileBackend(root=tmp_path / "store", workspace_root=tmp_path)
        session = StageSession(store=backend, workspace_root=tmp_path)

        # Simulate some LLM cost.
        get_run_tracker().record_call(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=100_000,
            output_tokens=10_000,
            stage="modeler",
            agent_class="ModelerAgent",
        )

        StageCoordinator()._record_forge_episode(
            session,
            outcome="success",
            source_type="intent",
            logical=SimpleNamespace(
                name="x",
                technique="data_vault_2",
                dv2=None,
                dimensional=None,
            ),
        )

        records = backend.query("memory/episodic", limit=10)
        assert len(records) == 1
        value = records[0].value if hasattr(records[0], "value") else records[0]
        assert "total_usd" in value
        assert value["total_usd"] is not None
        assert value["total_usd"] > 0


# ----------------------------------------------------------------------
# Item 7 — provenance into Fluid contract
# ----------------------------------------------------------------------


class TestItem7ProvenanceInContract:
    def test_provenance_block_emitted_when_annotations_present(self, tmp_path):
        from fluid_build.copilot.confidence import (
            AnnotationLog,
            ClaimProvenance,
            Confidence,
        )
        from fluid_build.copilot.schemas.osi import (
            OSIAIContext,
            OSISemanticModel,
        )
        from fluid_build.copilot.schemas.stage_outputs import (
            ConceptualDraft,
            LogicalDraft,
        )
        from fluid_build.forge_datamodel.emit.fluid_contract import (
            build_contract_from_logical,
        )

        log = AnnotationLog()
        log.annotate(
            "metadata.domain",
            confidence=Confidence(score=0.95, rationale="catalog tag"),
            provenance=ClaimProvenance(
                kind="catalog_tag",
                ref="snowflake://domain_tag",
                snippet="commerce",
            ),
        )

        from fluid_build.copilot.schemas.data_model import DV2Model

        logical = LogicalDraft(
            name="orders",
            technique="data_vault_2",
            conceptual=ConceptualDraft(name="orders", description=""),
            osi=OSISemanticModel(
                name="orders",
                description="",
                ai_context=OSIAIContext(),
            ),
            dv2=DV2Model(hubs=[], links=[], satellites=[], pits=[], bridges=[]),
        )

        contract = build_contract_from_logical(logical, annotations=log)
        # Fluid 0.7.x: labels live at the top level of the contract.
        labels = contract.get("labels", {})
        assert "provenance" in labels
        # The block round-trips as JSON.
        import json

        block = json.loads(labels["provenance"])
        assert isinstance(block, list)
        assert len(block) == 1
        entry = block[0]
        assert entry["path"] == "metadata.domain"
        assert entry["confidence"] == 0.95
        assert any(s["kind"] == "catalog_tag" for s in entry["sources"])

    def test_no_annotations_no_provenance_label(self, tmp_path):
        from fluid_build.copilot.schemas.data_model import DV2Model
        from fluid_build.copilot.schemas.osi import (
            OSIAIContext,
            OSISemanticModel,
        )
        from fluid_build.copilot.schemas.stage_outputs import (
            ConceptualDraft,
            LogicalDraft,
        )
        from fluid_build.forge_datamodel.emit.fluid_contract import (
            build_contract_from_logical,
        )

        logical = LogicalDraft(
            name="orders",
            technique="data_vault_2",
            conceptual=ConceptualDraft(name="orders", description=""),
            osi=OSISemanticModel(
                name="orders",
                description="",
                ai_context=OSIAIContext(),
            ),
            dv2=DV2Model(hubs=[], links=[], satellites=[], pits=[], bridges=[]),
        )
        # No annotations passed.
        contract = build_contract_from_logical(logical)
        labels = contract.get("labels", {})
        assert "provenance" not in labels


# ----------------------------------------------------------------------
# Item 2 — cost-aware cooperation default
# ----------------------------------------------------------------------


class TestItem2CooperationCostAware:
    def test_default_on_when_flag_unset(self, tmp_path):
        """No flag set → cooperation runs (default ON in v1.6+)."""
        from fluid_build.copilot.agents.coordinator import StageCoordinator
        from fluid_build.copilot.scratchpad import Scratchpad

        coordinator = StageCoordinator()
        session = StageSession(store=NullBackend(), workspace_root=tmp_path)

        call_count = {"n": 0}

        def agent():
            call_count["n"] += 1
            return SimpleNamespace(dv2=None, dimensional=None, conceptual=None)

        coordinator._run_logical_with_cooperation(
            session,
            agent_invoke=agent,
        )
        # Default ON → at least 1 call (loop runs, may be 1 or 2
        # depending on critic-empty-progress detection).
        assert call_count["n"] >= 1

    def test_cost_ceiling_short_circuits_loop(self, tmp_path, monkeypatch):
        """When the running cost is at the ceiling, the loop
        should NOT run (saves operator's budget)."""
        from fluid_build.copilot.agents.coordinator import StageCoordinator
        from fluid_build.copilot.cost import get_run_tracker

        # Set ceiling at $0.01.
        monkeypatch.setenv("FLUID_COST_LIMIT_USD", "0.01")
        # Pre-populate tracker with $0.009 already spent.
        # claude-sonnet-4-6 is $3/$15 per 1M; 3000 in @ $3 = $0.009
        get_run_tracker().record_call(
            provider="anthropic",
            model="claude-sonnet-4-6",
            input_tokens=3000,
            output_tokens=0,
            stage="modeler",
            agent_class="ModelerAgent",
        )

        coordinator = StageCoordinator()
        session = StageSession(store=NullBackend(), workspace_root=tmp_path)

        call_count = {"n": 0}

        def agent():
            call_count["n"] += 1
            return SimpleNamespace(dv2=None, dimensional=None, conceptual=None)

        result = coordinator._run_logical_with_cooperation(
            session,
            agent_invoke=agent,
        )
        # Cost-aware short-circuit: loop skipped, single pass only.
        assert call_count["n"] == 1
        assert result is not None

    def test_explicit_opt_out_still_works(self, tmp_path):
        from fluid_build.copilot.agents.coordinator import StageCoordinator

        coordinator = StageCoordinator()
        session = StageSession(
            store=NullBackend(),
            workspace_root=tmp_path,
            capability_matrix={"critic_loop_enabled": False},
        )
        call_count = {"n": 0}

        def agent():
            call_count["n"] += 1
            return SimpleNamespace(dv2=None, dimensional=None, conceptual=None)

        coordinator._run_logical_with_cooperation(
            session,
            agent_invoke=agent,
        )
        assert call_count["n"] == 1
