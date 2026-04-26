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

"""Coverage for Gap 6 — catalog lineage → DV2 link inference.

The catalog adapters surface ``CatalogLineage`` (upstream / downstream
FQNs) but until V1.5 Sprint E, that signal landed in the audit trail
and disappeared. Gap 6 wires it into a deterministic post-processor
on ``LogicalAgent.from_catalog`` that appends DV2 ``LinkDefinition``s
the modeler missed.

The post-processor must be:

1. **Deterministic.** Same lineage map in → same link list out,
   regardless of dict iteration order.
2. **Non-destructive.** Never modify or remove modeler-emitted links;
   only append.
3. **Idempotent.** Running twice produces the same set of links (no
   duplicates).
4. **Correctly named.** Link names use the existing
   ``forge_datamodel.dv2.naming.link_name`` helper.
5. **Lossy-safe.** When a downstream / upstream table has no hub
   mapping, the edge is silently skipped (we don't synthesize hubs
   from lineage signal alone — too risky).
"""

from __future__ import annotations

import pytest

from fluid_build.copilot.agents.logical_agent import (
    infer_dv2_links_from_lineage,
)
from fluid_build.copilot.schemas.data_model import (
    DV2Model,
    HubDefinition,
    LinkDefinition,
)


def _make_dv2(hubs, links=None):
    """Build a minimal :class:`DV2Model` for testing."""
    return DV2Model(
        hubs=hubs,
        links=links or [],
        satellites=[],
        pits=[],
        bridges=[],
    )


class TestBasicInference:
    def test_single_lineage_edge_produces_one_link(self):
        """Two hubs, one lineage edge between their source tables →
        one inferred link."""
        dv2 = _make_dv2(
            hubs=[
                HubDefinition(
                    entity_name="customer",
                    hub_table_name="hub_customer",
                    mapped_source_tables=["raw.customers"],
                ),
                HubDefinition(
                    entity_name="order",
                    hub_table_name="hub_order",
                    mapped_source_tables=["raw.orders"],
                ),
            ],
        )
        # raw.orders depends on raw.customers (catalog lineage).
        lineage = {"raw.orders": ["raw.customers"]}

        inferred = infer_dv2_links_from_lineage(dv2, lineage)
        assert len(inferred) == 1
        link = inferred[0]
        assert set(link.hubs_involved) == {"customer", "order"}
        # link_name uses the helper — encodes directionality
        # (upstream, downstream) order.
        assert link.link_name == "lnk_customer_order"

    def test_no_join_keys_or_relationships_on_inferred_link(self):
        """Lineage signal alone doesn't tell us the FK columns —
        emit an empty join_keys and let the downstream emitter
        flag the link as 'lineage-derived, FK unknown'."""
        dv2 = _make_dv2(
            hubs=[
                HubDefinition(
                    entity_name="a", hub_table_name="hub_a", mapped_source_tables=["t_a"]
                ),
                HubDefinition(
                    entity_name="b", hub_table_name="hub_b", mapped_source_tables=["t_b"]
                ),
            ],
        )
        inferred = infer_dv2_links_from_lineage(dv2, {"t_a": ["t_b"]})
        assert inferred[0].join_keys == []
        assert inferred[0].relationships == []


class TestDeduplication:
    def test_existing_link_blocks_duplicate(self):
        """Modeler already emitted a link between customer + order;
        lineage edge between the same two hubs MUST NOT add a
        duplicate."""
        dv2 = _make_dv2(
            hubs=[
                HubDefinition(
                    entity_name="customer",
                    hub_table_name="hub_customer",
                    mapped_source_tables=["raw.customers"],
                ),
                HubDefinition(
                    entity_name="order",
                    hub_table_name="hub_order",
                    mapped_source_tables=["raw.orders"],
                ),
            ],
            links=[
                LinkDefinition(
                    link_name="lnk_customer_order",
                    link_table_name="lnk_customer_order",
                    hubs_involved=["customer", "order"],
                ),
            ],
        )
        inferred = infer_dv2_links_from_lineage(dv2, {"raw.orders": ["raw.customers"]})
        assert inferred == []

    def test_idempotent(self):
        """Running the inference twice with the same input produces
        the same output. (Defends against a future PR that
        accidentally mutates ``dv2`` in place during inference.)"""
        dv2 = _make_dv2(
            hubs=[
                HubDefinition(
                    entity_name="a", hub_table_name="hub_a", mapped_source_tables=["t_a"]
                ),
                HubDefinition(
                    entity_name="b", hub_table_name="hub_b", mapped_source_tables=["t_b"]
                ),
            ],
        )
        first = infer_dv2_links_from_lineage(dv2, {"t_a": ["t_b"]})
        second = infer_dv2_links_from_lineage(dv2, {"t_a": ["t_b"]})
        assert len(first) == len(second) == 1
        assert first[0].hubs_involved == second[0].hubs_involved

    def test_single_lineage_pair_produces_only_one_link_when_bidirectional(self):
        """If lineage records BOTH directions (a → b AND b → a — rare
        but possible with poorly-curated catalogs), only one link
        is inferred — keyed by frozenset(hubs)."""
        dv2 = _make_dv2(
            hubs=[
                HubDefinition(
                    entity_name="a", hub_table_name="hub_a", mapped_source_tables=["t_a"]
                ),
                HubDefinition(
                    entity_name="b", hub_table_name="hub_b", mapped_source_tables=["t_b"]
                ),
            ],
        )
        # Both directions in the lineage map.
        lineage = {"t_a": ["t_b"], "t_b": ["t_a"]}
        inferred = infer_dv2_links_from_lineage(dv2, lineage)
        assert len(inferred) == 1


class TestRobustness:
    def test_no_hub_mapping_skips_edge(self):
        """Lineage references a table the modeler didn't promote to
        a hub — the edge is silently skipped (we don't fabricate
        hubs)."""
        dv2 = _make_dv2(
            hubs=[
                HubDefinition(
                    entity_name="customer",
                    hub_table_name="hub_customer",
                    mapped_source_tables=["raw.customers"],
                ),
            ],
        )
        # raw.orders has no corresponding hub.
        lineage = {"raw.orders": ["raw.customers"]}
        inferred = infer_dv2_links_from_lineage(dv2, lineage)
        assert inferred == []

    def test_self_edge_skipped(self):
        """Lineage that resolves to the same hub on both ends (a
        recursive table; or a downstream view of the same hub) is
        skipped — DV2 self-links are exotic and shouldn't be
        synthesized from lineage alone."""
        dv2 = _make_dv2(
            hubs=[
                HubDefinition(
                    entity_name="customer",
                    hub_table_name="hub_customer",
                    mapped_source_tables=["raw.customers", "raw.customers_v2"],
                ),
            ],
        )
        lineage = {"raw.customers_v2": ["raw.customers"]}
        inferred = infer_dv2_links_from_lineage(dv2, lineage)
        assert inferred == []

    def test_empty_inputs_produce_empty_output(self):
        """No DV2 OR no lineage → empty output. No exceptions, no
        nonsense links."""
        empty_dv2 = _make_dv2(hubs=[])
        assert infer_dv2_links_from_lineage(empty_dv2, {"t_a": ["t_b"]}) == []
        full_dv2 = _make_dv2(
            hubs=[
                HubDefinition(entity_name="a", hub_table_name="hub_a", mapped_source_tables=["t_a"])
            ],
        )
        assert infer_dv2_links_from_lineage(full_dv2, {}) == []
        assert infer_dv2_links_from_lineage(None, {"x": ["y"]}) == []

    def test_case_insensitive_match(self):
        """Snowflake reports FQNs in uppercase; the modeler typically
        stores ``mapped_source_tables`` in lowercase. The matcher
        case-folds both sides so they line up."""
        dv2 = _make_dv2(
            hubs=[
                HubDefinition(
                    entity_name="customer",
                    hub_table_name="hub_customer",
                    mapped_source_tables=["raw.customers"],
                ),
                HubDefinition(
                    entity_name="order",
                    hub_table_name="hub_order",
                    mapped_source_tables=["raw.orders"],
                ),
            ],
        )
        # Snowflake-style uppercase FQN.
        lineage = {"DB.SCHEMA.ORDERS": ["DB.SCHEMA.CUSTOMERS"]}
        inferred = infer_dv2_links_from_lineage(dv2, lineage)
        # Bare-table-name match wins via _table_token's case-fold +
        # tail extraction.
        assert len(inferred) == 1
        assert set(inferred[0].hubs_involved) == {"customer", "order"}


class TestDeterminism:
    def test_output_order_stable_across_dict_orders(self):
        """Two equivalent lineage maps with different insertion order
        produce identical link lists — required for cache-key
        stability and deterministic forge runs."""
        dv2 = _make_dv2(
            hubs=[
                HubDefinition(
                    entity_name=name,
                    hub_table_name=f"hub_{name}",
                    mapped_source_tables=[f"t_{name}"],
                )
                for name in ("a", "b", "c", "d")
            ],
        )

        # Same edges, different insertion order.
        lineage_1 = {"t_d": ["t_a"], "t_b": ["t_a"], "t_c": ["t_b"]}
        lineage_2 = {"t_c": ["t_b"], "t_d": ["t_a"], "t_b": ["t_a"]}

        out_1 = infer_dv2_links_from_lineage(dv2, lineage_1)
        out_2 = infer_dv2_links_from_lineage(dv2, lineage_2)
        # Same number of links, same names in same order.
        assert [l.link_name for l in out_1] == [l.link_name for l in out_2]
        assert len(out_1) == 3
