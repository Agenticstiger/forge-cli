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

"""Smoke tests for E13–E18 primitives (plan, tools, streaming,
learning, projections)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from fluid_build.copilot.scratchpad import Scratchpad

# ----------------------------------------------------------------------
# E13 — plan-then-execute
# ----------------------------------------------------------------------


class TestPlanning:
    def test_plan_steps_filterable_by_kind(self):
        from fluid_build.copilot.planning import (
            PlanStep,
            StagePlan,
            get_plan,
            record_plan,
        )

        plan = StagePlan(
            stage="logical",
            summary="Build 3 hubs from raw catalog",
            steps=[
                PlanStep(kind="create_hub", target="hub_customer", rationale="..."),
                PlanStep(kind="create_hub", target="hub_order", rationale="..."),
                PlanStep(kind="create_link", target="lnk_co", rationale="..."),
            ],
        )
        assert plan.step_count() == 3
        hubs = plan.steps_by_kind("create_hub")
        assert len(hubs) == 2
        assert {s.target for s in hubs} == {"hub_customer", "hub_order"}

    def test_record_and_get_plan(self):
        from fluid_build.copilot.planning import (
            StagePlan,
            get_plan,
            record_plan,
        )

        pad = Scratchpad()
        plan = StagePlan(stage="logical", summary="x")
        record_plan(plan, scratchpad=pad)
        assert get_plan("logical", scratchpad=pad) is plan
        assert get_plan("builder", scratchpad=pad) is None


# ----------------------------------------------------------------------
# E14 — tool-use scaffolding
# ----------------------------------------------------------------------


class TestToolRegistry:
    def test_register_and_invoke(self):
        from fluid_build.copilot.agent_tools import Tool, ToolRegistry

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="echo",
                description="Echo input",
                input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                handler=lambda text: {"echoed": text},
            )
        )
        result = registry.invoke("echo", {"text": "hi"})
        assert result == {"echoed": "hi"}
        assert len(registry.invocations) == 1
        assert registry.invocations[0].success is True

    def test_unknown_tool_returns_error(self):
        from fluid_build.copilot.agent_tools import ToolRegistry

        registry = ToolRegistry()
        result = registry.invoke("nope", {})
        assert "error" in result
        assert registry.invocations[0].success is False

    def test_handler_exception_caught(self):
        from fluid_build.copilot.agent_tools import Tool, ToolRegistry

        def boom():
            raise RuntimeError("simulated handler crash")

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="bad",
                description="fails",
                input_schema={"type": "object"},
                handler=boom,
            )
        )
        result = registry.invoke("bad", {})
        assert "error" in result
        assert registry.invocations[0].success is False

    def test_invalid_tool_name_rejected(self):
        from fluid_build.copilot.agent_tools import Tool, ToolRegistry

        registry = ToolRegistry()
        with pytest.raises(ValueError):
            registry.register(
                Tool(
                    name="bad name with spaces",
                    description="x",
                    input_schema={},
                    handler=lambda: {},
                )
            )

    def test_list_for_llm_returns_mcp_shape(self):
        from fluid_build.copilot.agent_tools import Tool, ToolRegistry

        registry = ToolRegistry()
        registry.register(
            Tool(
                name="t",
                description="d",
                input_schema={"type": "object"},
                handler=lambda: None,
            )
        )
        out = registry.list_for_llm()
        assert out == [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]


# ----------------------------------------------------------------------
# E15 — streaming
# ----------------------------------------------------------------------


class TestStreaming:
    def test_streaming_call_concatenates_chunks(self):
        from fluid_build.copilot.streaming import (
            NullStreamHandler,
            StreamingCall,
        )

        chunks = iter(["hello", " ", "world"])
        with StreamingCall(chunks, NullStreamHandler()) as call:
            for _ in call:
                pass
        assert call.full_text == "hello world"

    def test_handler_on_chunk_called_per_chunk(self):
        from fluid_build.copilot.streaming import StreamingCall

        seen = []

        class H:
            def on_chunk(self, chunk):
                seen.append(chunk)

            def on_complete(self, text):
                seen.append(("done", text))

        with StreamingCall(iter(["a", "b"]), H()) as call:
            for _ in call:
                pass
        assert seen == ["a", "b", ("done", "ab")]

    def test_null_handler_swallows_silently(self):
        from fluid_build.copilot.streaming import (
            NullStreamHandler,
            StreamingCall,
        )

        h = NullStreamHandler()
        h.on_chunk("anything")
        h.on_complete("anything")  # No raise, no output.


# ----------------------------------------------------------------------
# E16 — continuous learning
# ----------------------------------------------------------------------


class TestContinuousLearning:
    def test_compute_edits_detects_modified_added_removed(self):
        from fluid_build.copilot.learning import compute_edits

        before = {
            "metadata": {"domain": "commerce", "owner": "team-a"},
            "exposes": [{"name": "orders"}],
        }
        after = {
            "metadata": {"domain": "retail", "deprecated": True},
            "exposes": [{"name": "orders"}, {"name": "refunds"}],
        }
        edits = compute_edits(before=before, after=after)
        kinds = {e.kind for e in edits}
        # Some modified, some added, some removed.
        assert "modified" in kinds
        assert "added" in kinds
        assert "removed" in kinds

    def test_compute_edits_identical_returns_empty(self):
        from fluid_build.copilot.learning import compute_edits

        before = {"metadata": {"domain": "commerce"}}
        assert compute_edits(before=before, after=before) == []

    def test_record_operator_edits_writes_to_store(self):
        from fluid_build.copilot.learning import (
            OperatorEdit,
            record_operator_edits,
        )

        store = MagicMock()
        record_operator_edits(
            store=store,
            contract_name="orders",
            edits=[
                OperatorEdit(
                    path="metadata.domain", kind="modified", before="commerce", after="retail"
                )
            ],
        )
        store.put.assert_called_once()
        # Key is prefixed correctly.
        args = store.put.call_args
        ns = args.args[0] if args.args else args.kwargs.get("namespace")
        key = args.args[1] if len(args.args) > 1 else args.kwargs.get("key")
        assert ns == "memory/semantic"
        assert key.startswith("operator_edit:orders:")

    def test_record_operator_edits_empty_no_op(self):
        from fluid_build.copilot.learning import record_operator_edits

        store = MagicMock()
        record_operator_edits(store=store, contract_name="x", edits=[])
        store.put.assert_not_called()

    def test_fetch_recent_edits_handles_no_store(self):
        from fluid_build.copilot.learning import fetch_recent_edits

        assert fetch_recent_edits(store=None, contract_name="x") == []


# ----------------------------------------------------------------------
# E18 — projections + budgets
# ----------------------------------------------------------------------


class TestProjections:
    def test_no_history_returns_no_confidence(self):
        from fluid_build.copilot.projections import project_run_cost

        store = MagicMock()
        store.query.return_value = []
        proj = project_run_cost(
            store=store,
            technique="data_vault_2",
            source_type="intent",
        )
        assert proj.samples == 0
        assert proj.confidence == "none"

    def test_single_sample_low_confidence(self):
        from fluid_build.copilot.projections import project_run_cost

        store = MagicMock()
        store.query.return_value = [
            SimpleNamespace(
                value={
                    "technique": "data_vault_2",
                    "source_type": "intent",
                    "total_usd": 0.30,
                }
            ),
        ]
        proj = project_run_cost(
            store=store,
            technique="data_vault_2",
            source_type="intent",
        )
        assert proj.samples == 1
        assert proj.confidence == "low"
        assert proj.low_usd == 0.30
        assert proj.high_usd == 0.30

    def test_five_samples_high_confidence(self):
        from fluid_build.copilot.projections import project_run_cost

        store = MagicMock()
        costs = [0.10, 0.20, 0.30, 0.40, 0.50]
        store.query.return_value = [
            SimpleNamespace(
                value={
                    "technique": "data_vault_2",
                    "source_type": "intent",
                    "total_usd": c,
                }
            )
            for c in costs
        ]
        proj = project_run_cost(
            store=store,
            technique="data_vault_2",
            source_type="intent",
        )
        assert proj.samples == 5
        assert proj.confidence == "high"
        assert proj.low_usd <= proj.high_usd

    def test_summary_renders_when_no_history(self):
        from fluid_build.copilot.projections import CostProjection

        proj = CostProjection(low_usd=0, high_usd=0, samples=0, confidence="none")
        assert "no prior runs" in proj.summary()

    def test_summary_renders_with_samples(self):
        from fluid_build.copilot.projections import CostProjection

        proj = CostProjection(
            low_usd=0.10,
            high_usd=0.50,
            samples=4,
            confidence="medium",
        )
        text = proj.summary()
        assert "$0.1" in text
        assert "$0.5" in text


class TestStageBudget:
    def test_under_budget_no_raise(self):
        from fluid_build.copilot.projections import StageBudget

        budget = StageBudget(stage="modeler", limit_s=60)
        budget.start()
        budget.check()  # No raise — well under 60s.

    def test_over_budget_raises_with_actual_numbers(self):
        from fluid_build.copilot.projections import (
            StageBudget,
            StageBudgetExceeded,
        )

        budget = StageBudget(stage="modeler", limit_s=0.001)
        budget.start()
        # Sleep enough to blow the 1ms budget.
        import time

        time.sleep(0.01)
        with pytest.raises(StageBudgetExceeded) as exc_info:
            budget.check()
        assert exc_info.value.stage == "modeler"
        assert exc_info.value.budget_s == 0.001
        assert exc_info.value.elapsed_s >= 0.001

    def test_zero_limit_disabled(self):
        """``limit_s=0`` means no enforcement (operator hasn't set
        a budget for this stage)."""
        from fluid_build.copilot.projections import StageBudget

        budget = StageBudget(stage="x", limit_s=0)
        budget.start()
        import time

        time.sleep(0.01)
        budget.check()  # No raise.
