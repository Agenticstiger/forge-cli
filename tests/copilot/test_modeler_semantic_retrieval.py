# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Pin the logical-stage prompt the modeler sends.

Semantic-memory retrieval ("prior similar models") was removed with the store's vector and
semantic-writer memory; the prompt carries no ``prior_similar_models`` key. The pins here
stub ``ModelerAgent.call`` so the captured ``user_prompt`` is inspected directly: fast,
hermetic, unaffected by provider churn.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.modeler_agent import ModelerAgent
from fluid_build.copilot.schemas.data_model import DimensionalModel, FactTable, FieldDefinition
from fluid_build.copilot.schemas.intent import BusinessIntent, DataProduct
from fluid_build.copilot.schemas.osi import OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
from fluid_build.copilot.store.backends.file import FileBackend

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _DummyLlmConfig:
    """Minimum shape the ModelerAgent checks for before taking the LLM
    branch (``session.llm_config is not None``)."""

    provider = "anthropic"
    model = "claude-sonnet-4-6"


def _make_session_with_store(tmp_path, *, llm_enabled: bool = True) -> StageSession:
    store = FileBackend(root=tmp_path)
    return StageSession(
        store=store,
        workspace_root=tmp_path,
        llm_config=_DummyLlmConfig() if llm_enabled else None,
        active_provider="anthropic",
    )


def _stub_modeler_that_captures_prompts(agent: ModelerAgent) -> List[Dict[str, Any]]:
    """Monkey-patch ``agent.call`` to capture the user_prompt without
    touching any real provider. Returns a list captured calls accrue
    onto — inspect after running the agent."""
    captured: List[Dict[str, Any]] = []

    def fake_call(
        session,
        *,
        system_prompt,
        user_prompt,
        output_schema,
        params,
        retry_schema_errors=True,
    ):
        captured.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "output_schema": output_schema,
                "params": params,
            }
        )
        # Return a minimally-valid LogicalDraft so the caller's
        # downstream assembly doesn't choke. The semantic-retrieval
        # code runs BEFORE this, so the return shape doesn't matter to
        # what we're pinning.
        return LogicalDraft(
            name="captured",
            description="",
            technique="dimensional",
            osi=OSISemanticModel(name="captured"),
            source_summary={},
            conceptual=None,
            dv2=None,
            # Minimum shape that passes the cross-technique validator:
            # a non-empty ``dimensional`` field for technique="dimensional".
            dimensional=DimensionalModel(
                facts=[
                    FactTable(
                        name="fact_stub",
                        grain_statement="stub",
                        measures=[FieldDefinition(name="value", data_type="int")],
                    )
                ]
            ),
        )

    agent.call = fake_call  # type: ignore[method-assign]
    return captured


def _simple_intent() -> BusinessIntent:
    return BusinessIntent(
        data_product=DataProduct(
            name="customer_orders",
            domain="retail",
            description="loyalty point-of-sale analytics",
        ),
    )


def test_dv2_prompt_carries_machine_readable_join_key_contract(tmp_path) -> None:
    session = _make_session_with_store(tmp_path)
    agent = ModelerAgent()
    captured = _stub_modeler_that_captures_prompts(agent)
    agent._llm_from_intent(session, intent=_simple_intent(), technique="data_vault_2")

    payload = json.loads(captured[0]["user_prompt"])
    join_key_contract = payload["schema_constraints"]["dv2.links[].join_keys[]"]
    assert join_key_contract["type"] == "object"
    assert join_key_contract["required"] == ["table1", "column1", "table2", "column2"]
    assert "string" in join_key_contract["forbidden_shapes"]
