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

"""Pin ModelerAgent's semantic-memory retrieval on cache-miss prompts.

The plan (B1) requires ModelerAgent to inject the top-3
``memory/semantic`` matches into the user-prompt payload on every
LLM call, so prior forged models inform the new generation. The
design also requires **strict graceful degradation**:

* Empty store  → no injection, LLM call succeeds unchanged.
* Search error → no injection, LLM call succeeds unchanged.
* No store on the session → no injection, no crash.

If any of those three paths crashed or silently dropped the LLM call,
a regression in the store subsystem would cascade into forge failures
— which is the opposite of what retrieval is supposed to deliver.

The pins here don't run real LLM calls. They stub ``ModelerAgent.call``
so the captured ``user_prompt`` is inspected directly. That keeps the
tests fast (<50ms), hermetic, and unaffected by provider churn.
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


# ---------------------------------------------------------------------------
# Happy path — prior models present → injected into user_prompt
# ---------------------------------------------------------------------------


def test_prior_similar_models_injected_when_semantic_memory_populated(tmp_path) -> None:
    session = _make_session_with_store(tmp_path)
    # Seed semantic memory with two past forged models so retrieval
    # has something token-overlapping with the new intent.
    session.store.put(
        "memory/semantic",
        "prior_retail_loyalty",
        {"intent": "retail loyalty point-of-sale analytics", "technique": "dimensional"},
    )
    session.store.put(
        "memory/semantic",
        "prior_telco_churn",
        {"intent": "subscriber attrition mobile voice plan", "technique": "data_vault_2"},
    )

    agent = ModelerAgent()
    captured = _stub_modeler_that_captures_prompts(agent)
    agent._llm_from_intent(session, intent=_simple_intent(), technique="dimensional")

    assert len(captured) == 1
    payload = json.loads(captured[0]["user_prompt"])
    assert (
        "prior_similar_models" in payload
    ), "ModelerAgent must inject prior_similar_models when memory/semantic is non-empty"
    hits = payload["prior_similar_models"]
    # At least one hit, no more than the documented cap of 3.
    assert 1 <= len(hits) <= ModelerAgent._SEMANTIC_RETRIEVAL_LIMIT
    # Hits carry key + value so the LLM can cite / reference them.
    for hit in hits:
        assert "key" in hit
        assert "value" in hit


def test_retail_intent_ranks_retail_prior_first(tmp_path) -> None:
    """Sanity check the ranking direction: a retail-loyalty intent should
    surface the retail-loyalty prior above the telco-churn prior."""
    session = _make_session_with_store(tmp_path)
    session.store.put(
        "memory/semantic",
        "prior_retail_loyalty",
        {"intent": "retail loyalty point-of-sale analytics", "technique": "dimensional"},
    )
    session.store.put(
        "memory/semantic",
        "prior_telco_churn",
        {"intent": "subscriber attrition mobile voice plan", "technique": "data_vault_2"},
    )

    agent = ModelerAgent()
    captured = _stub_modeler_that_captures_prompts(agent)
    agent._llm_from_intent(session, intent=_simple_intent(), technique="dimensional")

    hits = json.loads(captured[0]["user_prompt"])["prior_similar_models"]
    assert (
        hits[0]["key"] == "prior_retail_loyalty"
    ), f"expected retail prior ranked first for a retail intent; got {hits[0]['key']}"


# ---------------------------------------------------------------------------
# Graceful degradation — all three "no retrieval" paths
# ---------------------------------------------------------------------------


def test_empty_semantic_memory_omits_key_without_error(tmp_path) -> None:
    session = _make_session_with_store(tmp_path)
    # No seed — the store is empty.
    agent = ModelerAgent()
    captured = _stub_modeler_that_captures_prompts(agent)
    agent._llm_from_intent(session, intent=_simple_intent(), technique="dimensional")

    payload = json.loads(captured[0]["user_prompt"])
    # Empty retrieval must leave the payload pristine — no empty-list
    # key, no "null" placeholder. The v1.0 prompt shape is preserved
    # byte-for-byte when retrieval adds nothing.
    assert "prior_similar_models" not in payload


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


def test_search_failure_falls_through_silently(tmp_path) -> None:
    """Simulate a broken search backend. The forge MUST continue and
    MUST NOT surface the error — retrieval is strictly additive."""
    session = _make_session_with_store(tmp_path)

    class _ExplodingStore:
        def search(self, *args, **kwargs):
            raise RuntimeError("index corrupted")

    session.store = _ExplodingStore()  # type: ignore[assignment]
    agent = ModelerAgent()
    captured = _stub_modeler_that_captures_prompts(agent)
    # Must NOT raise.
    agent._llm_from_intent(session, intent=_simple_intent(), technique="dimensional")
    payload = json.loads(captured[0]["user_prompt"])
    assert "prior_similar_models" not in payload


def test_session_without_store_skips_retrieval(tmp_path) -> None:
    """If a caller builds a StageSession without a store at all, we
    must NOT crash on attribute access — retrieval is opt-in by
    presence of the store."""
    session = _make_session_with_store(tmp_path)
    session.store = None  # type: ignore[assignment]
    agent = ModelerAgent()
    captured = _stub_modeler_that_captures_prompts(agent)
    agent._llm_from_intent(session, intent=_simple_intent(), technique="dimensional")
    payload = json.loads(captured[0]["user_prompt"])
    assert "prior_similar_models" not in payload


# ---------------------------------------------------------------------------
# Query construction — token-rich queries drive ranking quality
# ---------------------------------------------------------------------------


def test_query_from_intent_contains_technique_and_entity_tokens() -> None:
    intent = _simple_intent()
    query = ModelerAgent._build_semantic_query_from_intent(intent=intent, technique="dimensional")
    # Technique + domain + name + description all must show up so
    # ranking uses the full business-context signal.
    assert "dimensional" in query
    assert "customer_orders" in query
    assert "retail" in query
    assert "loyalty" in query


def test_query_from_tables_contains_table_and_column_names() -> None:
    from fluid_build.forge_datamodel.from_ddl.parser import (
        ColumnDefinition,
        TableDefinition,
    )

    tables = [
        TableDefinition(
            name="orders",
            primary_keys=["order_id"],
            columns=[
                ColumnDefinition(name="order_id", logical_type="BIGINT"),
                ColumnDefinition(name="customer_id", logical_type="BIGINT"),
                ColumnDefinition(name="gross_amount", logical_type="DECIMAL(18,4)"),
            ],
        ),
    ]
    query = ModelerAgent._build_semantic_query_from_tables(
        name="customer_orders", tables=tables, technique="dimensional"
    )
    assert "dimensional" in query
    assert "customer_orders" in query
    assert "orders" in query
    assert "gross_amount" in query


# ---------------------------------------------------------------------------
# Read-only contract — retrieval must NOT write to memory/semantic
# ---------------------------------------------------------------------------


def test_retrieval_is_read_only(tmp_path) -> None:
    """B1 is strictly additive retrieval. Auto-writing forged models
    back into memory/semantic is a separate v1.1+ decision (privacy
    implications, tenancy, etc.) — the B1 wiring must not do it
    pre-emptively."""
    session = _make_session_with_store(tmp_path)
    # Baseline: semantic namespace starts empty.
    assert session.store.query("memory/semantic", limit=100) == []

    agent = ModelerAgent()
    _stub_modeler_that_captures_prompts(agent)
    agent._llm_from_intent(session, intent=_simple_intent(), technique="dimensional")

    # After the forge runs, memory/semantic is still empty — retrieval
    # did not secretly write the new model back.
    assert session.store.query("memory/semantic", limit=100) == []
