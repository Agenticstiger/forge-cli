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

"""Pin D7 — auto-write ``memory/semantic`` on a successful forge.

Prior to D7 the store had a one-way semantic contract: ModelerAgent
*read* from ``memory/semantic`` but nothing ever *wrote* to it outside
explicit test seeds. D7 closes the loop by hooking the coordinator's
successful-forge return points so a slim OSI-centric payload is
persisted under ``memory/semantic/<slug>.<hash>`` for subsequent runs.

The write is opt-in (``FLUID_COPILOT_SEMANTIC_MEMORY=1``) because the
payload includes natural-language business context from OSI — names,
descriptions, synonyms. Auto-accumulating that on shared / multi-tenant
workstations without explicit consent would be a privacy regression.

These pins cover:

* The env-var gate (accepted truthy tokens, rejected tokens, whitespace
  / case tolerance).
* The write helper in isolation (idempotency, payload shape, metadata
  shape, key format).
* The coordinator's two success paths (``from_intent`` and
  ``from_tables``) — default off, opt-in on.
* Safety — broken store, missing store, ModelerAgent direct use still
  read-only.
* Round-trip — an auto-written record is actually retrievable by the
  modeler on a subsequent forge.
"""

from __future__ import annotations

import json

import pytest

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.coordinator import StageCoordinator
from fluid_build.copilot.agents.modeler_agent import ModelerAgent
from fluid_build.copilot.schemas.data_model import (
    DimensionalModel,
    FactTable,
    FieldDefinition,
)
from fluid_build.copilot.schemas.intent import (
    BusinessIntent,
    DataProduct,
    Dimensions,
    Grain,
)
from fluid_build.copilot.schemas.osi import OSIAIContext, OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import LogicalDraft
from fluid_build.copilot.store.backends.file import FileBackend
from fluid_build.copilot.store.backends.null import NullBackend
from fluid_build.copilot.store.semantic_writer import (
    _ENV_VAR,
    _NAMESPACE,
    auto_semantic_write_enabled,
    write_semantic_record,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logical(
    name: str = "customer_orders",
    technique: str = "dimensional",
    description: str = "retail loyalty point-of-sale analytics",
) -> LogicalDraft:
    """Minimal-but-realistic LogicalDraft for unit testing the writer.

    The writer only touches ``name``, ``technique``, ``description``,
    and ``osi`` — the physical fields stay ``None`` so the fixture
    remains small and the payload-shape pins are unambiguous.
    """
    return LogicalDraft(
        name=name,
        description=description,
        technique=technique,
        osi=OSISemanticModel(
            name=name,
            description=description,
            ai_context=OSIAIContext(
                instructions="Use for lifetime value and churn",
                synonyms=["loyalty", "POS"],
            ),
        ),
        dimensional=(
            DimensionalModel(
                facts=[
                    FactTable(
                        name="fact_order_line",
                        grain_statement="one row per order line",
                        measures=[FieldDefinition(name="amount", data_type="decimal")],
                    )
                ],
            )
            if technique == "dimensional"
            else None
        ),
    )


def _simple_intent() -> BusinessIntent:
    return BusinessIntent(
        data_product=DataProduct(
            name="customer_orders",
            domain="retail",
            description="loyalty point-of-sale analytics",
        ),
        grain=Grain(entity="order_line", time_dimension="order_date"),
        dimensions=Dimensions(entities=["customer", "product"]),
    )


# ---------------------------------------------------------------------------
# Env-var gate
# ---------------------------------------------------------------------------


class TestEnvGate:
    def test_unset_is_disabled(self, monkeypatch):
        monkeypatch.delenv(_ENV_VAR, raising=False)
        assert auto_semantic_write_enabled() is False

    @pytest.mark.parametrize(
        "value", ["1", "true", "yes", "on", "TRUE", "Yes", "ON", "  1  ", "\tyes\n"]
    )
    def test_truthy_tokens_enable(self, monkeypatch, value):
        monkeypatch.setenv(_ENV_VAR, value)
        assert auto_semantic_write_enabled() is True, f"{value!r} should enable auto-write"

    @pytest.mark.parametrize(
        "value",
        ["", "0", "false", "no", "off", "FALSE", "No", "OFF", "garbage", "-1", "null"],
    )
    def test_non_truthy_tokens_disable(self, monkeypatch, value):
        monkeypatch.setenv(_ENV_VAR, value)
        assert auto_semantic_write_enabled() is False, f"{value!r} should leave auto-write disabled"


# ---------------------------------------------------------------------------
# write_semantic_record — direct unit tests
# ---------------------------------------------------------------------------


class TestWriteSemanticRecord:
    def test_no_store_returns_none(self, monkeypatch):
        """Guards the coordinator path where a caller builds a
        ``StageSession`` without wiring a store at all."""
        monkeypatch.setenv(_ENV_VAR, "1")
        assert write_semantic_record(None, _make_logical()) is None

    def test_disabled_flag_skips_write(self, tmp_path, monkeypatch):
        monkeypatch.delenv(_ENV_VAR, raising=False)
        store = FileBackend(root=tmp_path)
        key = write_semantic_record(store, _make_logical())
        assert key is None
        assert store.query(_NAMESPACE, limit=100) == []

    def test_enabled_flag_writes_record(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_ENV_VAR, "1")
        store = FileBackend(root=tmp_path)
        key = write_semantic_record(store, _make_logical(), source_type="intent")
        assert key is not None
        records = store.query(_NAMESPACE, limit=100)
        assert len(records) == 1
        assert records[0].key == key

    def test_key_is_slug_dot_16_hex(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_ENV_VAR, "1")
        store = FileBackend(root=tmp_path)
        key = write_semantic_record(store, _make_logical(name="Customer Orders V2!"))
        assert key is not None
        slug, _, digest = key.partition(".")
        # Slug strips punctuation and lowercases:
        assert slug == "customer_orders_v2"
        # Digest is 16 lowercase hex chars:
        assert len(digest) == 16
        assert all(c in "0123456789abcdef" for c in digest)

    def test_payload_shape(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_ENV_VAR, "1")
        store = FileBackend(root=tmp_path)
        write_semantic_record(store, _make_logical())
        record = store.query(_NAMESPACE, limit=100)[0]
        value = record.value
        assert set(value.keys()) == {"name", "technique", "description", "osi"}
        assert value["name"] == "customer_orders"
        assert value["technique"] == "dimensional"
        # OSI round-trips as JSON so the retrieval-side ranker can
        # search over ai_context text just like hand-seeded records.
        assert value["osi"]["name"] == "customer_orders"
        assert "loyalty" in value["osi"]["ai_context"]["synonyms"]

    def test_metadata_shape(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_ENV_VAR, "1")
        store = FileBackend(root=tmp_path)
        write_semantic_record(store, _make_logical(), source_type="intent")
        record = store.query(_NAMESPACE, limit=100)[0]
        meta = record.metadata
        assert meta["source_type"] == "intent"
        assert meta["technique"] == "dimensional"
        assert meta["written_by"] == "coordinator.auto_semantic_write"
        assert "timestamp" in meta and "T" in meta["timestamp"]

    def test_idempotent_same_input_same_key(self, tmp_path, monkeypatch):
        """Re-forging the same intent shouldn't proliferate records —
        the content hash collapses identical models to a single key."""
        monkeypatch.setenv(_ENV_VAR, "1")
        store = FileBackend(root=tmp_path)
        logical = _make_logical()
        k1 = write_semantic_record(store, logical)
        k2 = write_semantic_record(store, logical)
        assert k1 == k2
        # Second write upserts onto the same key, so the record count
        # stays at 1 — not two copies of the same semantic model.
        records = store.query(_NAMESPACE, limit=100)
        assert len(records) == 1

    def test_different_names_produce_different_keys(self, tmp_path, monkeypatch):
        """Two forges with different names must not collide on a key —
        content-addressable hashing is only meaningful if distinct
        semantic models land in distinct records."""
        monkeypatch.setenv(_ENV_VAR, "1")
        store = FileBackend(root=tmp_path)
        k_retail = write_semantic_record(store, _make_logical(name="retail_pos"))
        k_ecomm = write_semantic_record(
            store,
            _make_logical(
                name="ecommerce_cart",
                description="online shopping cart abandonment",
            ),
        )
        assert k_retail != k_ecomm
        assert len(store.query(_NAMESPACE, limit=100)) == 2

    def test_same_name_different_content_produces_different_keys(self, tmp_path, monkeypatch):
        """Two forges with the same name but different semantic content
        must not collapse — the hash portion distinguishes them."""
        monkeypatch.setenv(_ENV_VAR, "1")
        store = FileBackend(root=tmp_path)
        k1 = write_semantic_record(store, _make_logical(description="point-of-sale loyalty"))
        k2 = write_semantic_record(store, _make_logical(description="ecommerce cart abandonment"))
        assert k1 != k2


# ---------------------------------------------------------------------------
# Coordinator integration — both success paths
# ---------------------------------------------------------------------------


class TestCoordinatorIntegration:
    def test_from_intent_writes_when_flag_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv(_ENV_VAR, "1")
        store = FileBackend(root=tmp_path)
        session = StageSession(store=store, workspace_root=tmp_path)
        result = StageCoordinator().from_intent(
            session, intent=_simple_intent(), technique="dimensional"
        )
        assert result.logical.name == "customer_orders"
        records = store.query(_NAMESPACE, limit=100)
        assert len(records) == 1
        value = records[0].value
        assert value["technique"] == "dimensional"
        assert records[0].metadata["source_type"] == "intent"

    def test_from_intent_does_not_write_when_flag_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv(_ENV_VAR, raising=False)
        store = FileBackend(root=tmp_path)
        session = StageSession(store=store, workspace_root=tmp_path)
        StageCoordinator().from_intent(session, intent=_simple_intent(), technique="dimensional")
        assert store.query(_NAMESPACE, limit=100) == []

    def test_from_tables_writes_when_flag_on(self, tmp_path, monkeypatch):
        from fluid_build.forge_datamodel.from_ddl.parser import DDLParser

        monkeypatch.setenv(_ENV_VAR, "1")
        store = FileBackend(root=tmp_path)
        session = StageSession(store=store, workspace_root=tmp_path)
        ddl = """
        CREATE TABLE orders (
            order_id VARCHAR(64) PRIMARY KEY,
            customer_id VARCHAR(64),
            amount DECIMAL(18,2)
        );
        CREATE TABLE customers (
            customer_id VARCHAR(64) PRIMARY KEY,
            customer_name STRING
        );
        """
        tables = DDLParser().parse_ddl_content(ddl)
        StageCoordinator().from_tables(
            session, name="orders", tables=tables, technique="data_vault_2"
        )
        records = store.query(_NAMESPACE, limit=100)
        assert len(records) == 1
        assert records[0].metadata["source_type"] == "tables"
        assert records[0].value["technique"] == "data_vault_2"

    def test_null_backend_does_not_crash(self, monkeypatch):
        """NullBackend accepts puts as no-ops — auto-write on a null
        store succeeds silently (no record is actually retained)."""
        monkeypatch.setenv(_ENV_VAR, "1")
        session = StageSession(store=NullBackend())
        # Must not raise even with the flag enabled.
        result = StageCoordinator().from_intent(
            session, intent=_simple_intent(), technique="dimensional"
        )
        assert result.logical.name == "customer_orders"


# ---------------------------------------------------------------------------
# Safety — never poison a successful forge
# ---------------------------------------------------------------------------


class TestSafety:
    def test_exploding_store_does_not_crash_forge(self, tmp_path, monkeypatch):
        """A backend that raises on ``put`` must not retroactively
        break a forge that already succeeded."""
        monkeypatch.setenv(_ENV_VAR, "1")

        real_store = FileBackend(root=tmp_path)

        class _ExplodingOnPut:
            """Wraps the real store, lets reads pass through, but
            makes every ``put`` raise. The coordinator's auto-write
            must swallow the error — the read-side LLM cache the
            agents use is untouched because that goes through
            ``get`` / ``put`` on different namespaces where we
            deliberately *don't* inject the failure mode we're pinning.
            """

            def __getattr__(self, name):
                return getattr(real_store, name)

            def put(self, ns, *args, **kwargs):
                if ns == _NAMESPACE:
                    raise RuntimeError("disk full")
                return real_store.put(ns, *args, **kwargs)

        session = StageSession(store=_ExplodingOnPut(), workspace_root=tmp_path)
        # Must not raise — the exception is swallowed with a warning.
        result = StageCoordinator().from_intent(
            session, intent=_simple_intent(), technique="dimensional"
        )
        assert result.logical.name == "customer_orders"

    def test_modeler_agent_direct_use_still_read_only(self, tmp_path, monkeypatch):
        """D7 only writes via the coordinator. Tests and power-users
        who call ``ModelerAgent._llm_from_intent`` directly must still
        see a read-only ``memory/semantic`` contract — otherwise
        ``tests/copilot/test_modeler_semantic_retrieval.py::test_retrieval_is_read_only``
        would regress."""
        monkeypatch.setenv(_ENV_VAR, "1")
        store = FileBackend(root=tmp_path)
        session = StageSession(
            store=store,
            workspace_root=tmp_path,
            llm_config=None,  # no LLM → agent returns a stub deterministically
        )
        # Direct agent call — coordinator is intentionally NOT involved.
        agent = ModelerAgent()
        # The agent has a deterministic non-LLM path when llm_config is
        # None; we only care that it doesn't side-effect the store.
        try:
            agent._llm_from_intent(session, intent=_simple_intent(), technique="dimensional")
        except Exception:
            # Some code paths require an LLM stub; the point is that no
            # write to memory/semantic happened regardless of outcome.
            pass
        assert store.query(_NAMESPACE, limit=100) == [], (
            "ModelerAgent direct use must never write to memory/semantic — "
            "only the coordinator's success path performs auto-writes."
        )


# ---------------------------------------------------------------------------
# Round-trip — auto-written records fuel the next forge's retrieval
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_auto_written_record_is_retrievable(self, tmp_path, monkeypatch):
        """End-to-end pin: forge A's auto-write lands in the exact
        namespace the modeler's retrieval pipeline queries, with a
        JSON-safe payload that survives a ``query`` round-trip via
        :class:`VectorBackend`. Ranking *quality* — whether a intent-
        shaped query surfaces A's record above noise — is a separate
        concern pinned by ``test_modeler_semantic_retrieval.py``;
        D7's contract stops at "the record is there, with a usable
        shape, under the correct namespace."
        """
        from fluid_build.copilot.store.backends.vector import VectorBackend

        monkeypatch.setenv(_ENV_VAR, "1")
        store = FileBackend(root=tmp_path)
        session = StageSession(store=store, workspace_root=tmp_path)

        # First forge — populates memory/semantic automatically.
        StageCoordinator().from_intent(session, intent=_simple_intent(), technique="dimensional")

        # Payload is present, under the right namespace, JSON-safe
        # end-to-end, and has the fields the retrieval side looks at.
        records = store.query(_NAMESPACE, limit=100)
        assert len(records) == 1
        value = json.loads(json.dumps(records[0].value))
        assert value["name"] == "customer_orders"
        assert value["technique"] == "dimensional"
        # The coordinator's OSI derives synonyms from intent.name +
        # intent.domain, so at a minimum the domain token ("retail")
        # reaches the persisted ai_context — which is exactly the text
        # the modeler's retrieval query includes.
        assert "retail" in value["osi"]["ai_context"]["synonyms"]

        # Retrieval API compatibility: the same ``VectorBackend.query``
        # call the modeler uses to list candidates for ranking (see
        # ``modeler_agent._llm_from_intent``) surfaces the auto-written
        # record without errors and with a structurally-valid record
        # object.
        ranker = VectorBackend(store)
        listed = ranker.query(_NAMESPACE, limit=100)
        assert len(listed) == 1
        assert listed[0].namespace == _NAMESPACE
        assert listed[0].value["name"] == "customer_orders"
