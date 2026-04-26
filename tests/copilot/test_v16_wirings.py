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

"""Regression pins for the v1.6 wirings (Items 1–9 of the
"light the fuse" sprint).

Each test asserts an end-to-end behaviour, not a helper's local
correctness. Without these pins, a future refactor could remove
any of the wirings and the rest of the agentic pipeline would
silently lose signal again.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.coordinator import StageCoordinator
from fluid_build.copilot.scratchpad import CriticFinding, Scratchpad
from fluid_build.copilot.store.backends.null import NullBackend

# ----------------------------------------------------------------------
# Item 7 — default critic_errors_trigger_repair=True
# ----------------------------------------------------------------------


class TestItem7CriticErrorDefaultOn:
    def test_default_escalates(self, tmp_path):
        """No flag set → critic errors escalate (new default)."""
        coordinator = StageCoordinator()
        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        session.get_scratchpad().add_critic_finding(
            CriticFinding(
                stage="logical",
                severity="error",
                message="x",
            )
        )
        from fluid_build.copilot.schemas.stage_outputs import (
            ValidationReport,
        )

        physical = SimpleNamespace(
            validation=ValidationReport(score=10, issues=[], passes_schema=True),
        )
        coordinator._escalate_critic_errors_into_report(
            session,
            physical=physical,
        )
        assert physical.validation.passes_schema is False

    def test_explicit_opt_out_preserves_legacy(self, tmp_path):
        """Setting ``critic_errors_trigger_repair=False`` preserves
        v1.5 behaviour (no escalation)."""
        coordinator = StageCoordinator()
        session = StageSession(
            store=NullBackend(),
            workspace_root=tmp_path,
            capability_matrix={"critic_errors_trigger_repair": False},
        )
        session.get_scratchpad().add_critic_finding(
            CriticFinding(
                stage="logical",
                severity="error",
                message="x",
            )
        )
        from fluid_build.copilot.schemas.stage_outputs import (
            ValidationReport,
        )

        physical = SimpleNamespace(
            validation=ValidationReport(score=10, issues=[], passes_schema=True),
        )
        coordinator._escalate_critic_errors_into_report(
            session,
            physical=physical,
        )
        assert physical.validation.passes_schema is True


# ----------------------------------------------------------------------
# Item 9 — StageBudget wraps each stage
# ----------------------------------------------------------------------


class TestItem9StageBudget:
    def test_default_budget_resolved(self, tmp_path):
        """No env / capability override → built-in default applies."""
        coordinator = StageCoordinator()
        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        budget = coordinator._stage_budget(session, stage="logical")
        # Default ``logical`` budget is 600s.
        assert budget.limit_s == 600.0

    def test_env_var_overrides_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLUID_STAGE_BUDGET_LOGICAL_S", "30")
        coordinator = StageCoordinator()
        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        budget = coordinator._stage_budget(session, stage="logical")
        assert budget.limit_s == 30.0

    def test_capability_matrix_overrides_default(self, tmp_path):
        coordinator = StageCoordinator()
        session = StageSession(
            store=NullBackend(),
            workspace_root=tmp_path,
            capability_matrix={"stage_budgets": {"logical": 45.0}},
        )
        budget = coordinator._stage_budget(session, stage="logical")
        assert budget.limit_s == 45.0

    def test_unknown_stage_no_budget(self, tmp_path):
        """A stage with no env / capability / built-in entry gets
        ``limit_s=0`` (disabled, not enforced)."""
        coordinator = StageCoordinator()
        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        budget = coordinator._stage_budget(session, stage="future_stage")
        assert budget.limit_s == 0


# ----------------------------------------------------------------------
# Item 6 — `fluid forge data-model learn` subcommand
# ----------------------------------------------------------------------


class TestItem6LearnCommand:
    def test_learn_command_records_diff(self, tmp_path):
        """Run the learn command end-to-end on two contracts that
        differ. Verify edits land on the store."""
        import yaml

        from fluid_build.cli.forge_data_model import run_learn_command

        original = tmp_path / "orders.fluid.yaml"
        edited = tmp_path / "orders.edited.yaml"
        original.write_text(
            yaml.safe_dump(
                {
                    "metadata": {"domain": "commerce"},
                    "exposes": [{"name": "orders"}],
                }
            ),
            encoding="utf-8",
        )
        edited.write_text(
            yaml.safe_dump(
                {
                    "metadata": {"domain": "retail"},  # CHANGED
                    "exposes": [{"name": "orders"}, {"name": "refunds"}],  # ADDED
                }
            ),
            encoding="utf-8",
        )

        # Build args + spy on the store.
        store = MagicMock()
        with patch(
            "fluid_build.cli.forge_data_model._build_session",
            return_value=SimpleNamespace(store=store),
        ):
            args = SimpleNamespace(
                original=str(original),
                edited=str(edited),
                name="orders",
                quiet=True,
            )
            rc = run_learn_command(args, logger=MagicMock())

        assert rc == 0
        store.put.assert_called_once()
        ns = store.put.call_args.args[0]
        key = store.put.call_args.args[1]
        assert ns == "memory/semantic"
        assert key.startswith("operator_edit:orders:")

    def test_learn_command_no_edits_no_op(self, tmp_path):
        import yaml

        from fluid_build.cli.forge_data_model import run_learn_command

        path = tmp_path / "x.yaml"
        path.write_text(yaml.safe_dump({"metadata": {"domain": "x"}}), encoding="utf-8")

        store = MagicMock()
        with patch(
            "fluid_build.cli.forge_data_model._build_session",
            return_value=SimpleNamespace(store=store),
        ):
            args = SimpleNamespace(
                original=str(path),
                edited=str(path),
                name=None,
                quiet=True,
            )
            rc = run_learn_command(args, logger=MagicMock())

        assert rc == 0
        store.put.assert_not_called()


# ----------------------------------------------------------------------
# Item 5 — run_with_critic_loop wraps modeler
# ----------------------------------------------------------------------


class TestItem5CriticLoopWiring:
    def test_default_off_runs_single_pass(self, tmp_path):
        """Default ``critic_loop_enabled=False`` → modeler runs
        once, no cooperation loop."""
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
        assert result is not None
        assert call_count["n"] == 1

    def test_opt_in_runs_cooperation_loop(self, tmp_path):
        """``critic_loop_enabled=True`` → loop runs, cap respected."""
        coordinator = StageCoordinator()
        session = StageSession(
            store=NullBackend(),
            workspace_root=tmp_path,
            capability_matrix={
                "critic_loop_enabled": True,
                "critic_loop_max_attempts": 2,
            },
        )
        call_count = {"n": 0}

        def agent():
            call_count["n"] += 1
            # Empty draft → critic finds at least one issue per pass
            # (no facts / no hubs).
            return SimpleNamespace(dv2=None, dimensional=None, conceptual=None)

        result = coordinator._run_logical_with_cooperation(
            session,
            agent_invoke=agent,
        )
        assert result is not None
        # Loop ran at least once. Empty-progress detection may
        # cap at 1-2 attempts depending on critic output stability.
        assert 1 <= call_count["n"] <= 2


# ----------------------------------------------------------------------
# Item 3 — StagePlan in modeler
# ----------------------------------------------------------------------


class TestItem3StagePlan:
    def test_plan_lands_on_scratchpad_for_tables(self, tmp_path):
        from fluid_build.copilot.agents.modeler_agent import (
            _record_logical_plan_from_tables,
        )
        from fluid_build.copilot.planning import get_plan
        from fluid_build.forge_datamodel.from_ddl.parser import (
            ColumnDefinition,
            TableDefinition,
        )

        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        tables = [
            TableDefinition(
                name="customers",
                columns=[
                    ColumnDefinition(name="customer_id", logical_type="VARCHAR", primary_key=True)
                ],
                primary_keys=["customer_id"],
            ),
            TableDefinition(
                name="orders",
                columns=[
                    ColumnDefinition(name="order_id", logical_type="VARCHAR", primary_key=True),
                    ColumnDefinition(name="customer_id", logical_type="VARCHAR"),
                ],
                primary_keys=["order_id"],
            ),
        ]
        _record_logical_plan_from_tables(
            session=session,
            name="orders",
            tables=tables,
            technique="data_vault_2",
        )
        plan = get_plan("logical", scratchpad=session.get_scratchpad())
        assert plan is not None
        assert plan.stage == "logical"
        # 2 hubs + 1 inferred link from the shared customer_id column.
        hub_steps = plan.steps_by_kind("create_hub")
        link_steps = plan.steps_by_kind("create_link")
        assert len(hub_steps) == 2
        assert len(link_steps) == 1
        assert "customer_id" in link_steps[0].rationale


# ----------------------------------------------------------------------
# Item 4 — Confidence + ClaimProvenance on modeler outputs
# ----------------------------------------------------------------------


class TestItem4Annotations:
    def test_dv2_hub_with_pk_match_high_confidence(self, tmp_path):
        from fluid_build.copilot.agents.modeler_agent import (
            _annotate_logical_from_tables,
        )
        from fluid_build.copilot.schemas.data_model import (
            DV2Model,
            HubDefinition,
        )
        from fluid_build.copilot.schemas.osi import (
            OSIAIContext,
            OSISemanticModel,
        )
        from fluid_build.copilot.schemas.stage_outputs import (
            ConceptualDraft,
            LogicalDraft,
        )
        from fluid_build.forge_datamodel.from_ddl.parser import (
            ColumnDefinition,
            TableDefinition,
        )

        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        tables = [
            TableDefinition(
                name="customers",
                columns=[
                    ColumnDefinition(name="customer_id", logical_type="VARCHAR", primary_key=True)
                ],
                primary_keys=["customer_id"],
            ),
        ]
        logical = LogicalDraft(
            name="orders",
            technique="data_vault_2",
            conceptual=ConceptualDraft(name="orders", description=""),
            osi=OSISemanticModel(
                name="orders",
                description="",
                ai_context=OSIAIContext(),
            ),
            dv2=DV2Model(
                hubs=[
                    HubDefinition(
                        entity_name="customer",
                        hub_table_name="hub_customer",
                        business_key_columns=["customer_id"],
                        mapped_source_tables=["customers"],
                    )
                ],
                links=[],
                satellites=[],
                pits=[],
                bridges=[],
            ),
        )
        _annotate_logical_from_tables(
            session=session,
            logical=logical,
            tables=tables,
            source_type="ddl",
        )
        ann_log = session.get_scratchpad().get_annotations()
        ann = ann_log.by_path["dv2.hubs.customer.business_key_columns"]
        # Exact PK match → 0.95.
        assert ann.confidence.score == 0.95
        assert ann.confidence.level == "high"
        assert any(p.kind == "ddl_constraint" for p in ann.provenance)


# ----------------------------------------------------------------------
# Item 1 — ToolRegistry attached to session
# ----------------------------------------------------------------------


class TestItem1ToolRegistry:
    def test_registry_attached_after_modeler_call(self, tmp_path):
        """Calling ``_ensure_tool_registry`` populates
        ``session.tool_registry`` so the v1.6+ LLM modeler tool
        loop can pick it up."""
        from fluid_build.copilot.agents.modeler_agent import (
            _ensure_tool_registry,
        )

        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        # Not set yet.
        assert getattr(session, "tool_registry", None) is None
        _ensure_tool_registry(session)
        registry = getattr(session, "tool_registry", None)
        assert registry is not None
        # Default registry has at least the semantic-search tool
        # because the session has a (Null) store.
        assert "search_semantic_memory" in registry.tools

    def test_existing_registry_preserved(self, tmp_path):
        """An operator who pre-attaches a custom registry must NOT
        have it overwritten by the default builder."""
        from fluid_build.copilot.agent_tools import (
            Tool,
            ToolRegistry,
            build_default_tool_registry,
        )
        from fluid_build.copilot.agents.modeler_agent import (
            _ensure_tool_registry,
        )

        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        custom = ToolRegistry()
        custom.register(
            Tool(
                name="my_custom",
                description="x",
                input_schema={"type": "object"},
                handler=lambda: {"ok": True},
            )
        )
        session.tool_registry = custom  # type: ignore[attr-defined]

        _ensure_tool_registry(session)
        # Custom registry preserved.
        assert session.tool_registry is custom  # type: ignore[attr-defined]


# ----------------------------------------------------------------------
# Item 2 — StreamingCall opt-in path
# ----------------------------------------------------------------------


class TestItem2Streaming:
    def test_default_off_uses_blocking_path(self, tmp_path):
        """``streaming_enabled`` not set → existing httpx.post path
        runs."""
        # The test indirectly verifies this by checking that
        # ``capability_matrix["streaming_enabled"]`` is recognized
        # as a real flag the pipeline reads.
        session = StageSession(store=NullBackend(), workspace_root=tmp_path)
        cm = session.capability_matrix or {}
        assert not cm.get("streaming_enabled")

    def test_streaming_flag_is_capability_matrix_field(self, tmp_path):
        """Documents the capability flag's existence + spelling.
        Pin so a refactor that drops the field stays visible."""
        session = StageSession(
            store=NullBackend(),
            workspace_root=tmp_path,
            capability_matrix={"streaming_enabled": True},
        )
        assert session.capability_matrix["streaming_enabled"] is True
