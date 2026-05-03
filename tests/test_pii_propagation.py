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

"""Pin tests for PII / sensitivity classification propagation through
the composition pipeline (Plan 2.2).

When an ADP / CDP composes from an SDP whose schema declares ``pii``
on a column, the same tag must flow onto matched downstream columns
by name. Without this, an SDP correctly tagged as
``classification: pii`` produces a downstream contract whose ``email``
column LOOKS unclassified — and the marketplace / catalog / policy
engine all silently lose the constraint.

This module pins the rule:

* match by column name,
* preserve operator overrides on the downstream side (don't downgrade
  ``confidential`` to nothing because the upstream said nothing),
* accept either ``sensitivity`` (FLUID schema) or ``classification``
  (catalog-style alias) on the upstream,
* surface a propagation log line per stamped column for receipt /
  agent feedback.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from fluid_build.forge_datamodel.from_data_products.pipeline import (
    UpstreamProduct,
    propagate_pii_classifications,
)


def _upstream(
    product_id: str,
    expose_id: str,
    columns: List[Dict[str, Any]],
    *,
    layer: str = "Bronze",
    product_type: str = "SDP",
) -> UpstreamProduct:
    """Build an UpstreamProduct for the propagation tests."""
    return UpstreamProduct(
        id=product_id,
        name=product_id.split(".")[-1],
        product_type=product_type,
        layer=layer,
        domain="customer",
        contract_path=f"/tmp/{product_id}/contract.fluid.yaml",
        exposes=(
            {
                "exposeId": expose_id,
                "kind": "Table",
                "schema": columns,
            },
        ),
    )


def _downstream(columns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a minimal downstream contract with one expose."""
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "demo.customer_360_adp",
        "name": "Customer 360 ADP",
        "domain": "customer",
        "metadata": {"layer": "Silver", "productType": "ADP"},
        "exposes": [
            {
                "exposeId": "joined",
                "name": "joined",
                "contract": {"schema": columns},
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1. Happy path — a single ``pii`` column flows through.
# ---------------------------------------------------------------------------


class TestPropagationHappyPath:
    """SDP says ``email`` is pii; downstream picks it up automatically."""

    def test_pii_propagates_onto_matching_column(self):
        upstream = _upstream(
            "demo.customers_sdp",
            "customers",
            [
                {"name": "customer_id", "type": "integer"},
                {"name": "email", "type": "string", "sensitivity": "pii"},
            ],
        )
        downstream = _downstream(
            [
                {"name": "customer_id", "type": "integer"},
                {"name": "email", "type": "string"},
            ]
        )

        log = propagate_pii_classifications(downstream, [upstream])

        cols = downstream["exposes"][0]["contract"]["schema"]
        email_col = next(c for c in cols if c["name"] == "email")
        assert email_col["sensitivity"] == "pii"
        # Non-pii column is left untouched.
        cust_col = next(c for c in cols if c["name"] == "customer_id")
        assert "sensitivity" not in cust_col
        # Log shape — one entry per stamped column.
        assert any("email" in entry and "pii" in entry for entry in log)

    def test_propagation_log_is_human_readable(self):
        upstream = _upstream(
            "demo.customers_sdp",
            "customers",
            [{"name": "ssn", "type": "string", "sensitivity": "phi"}],
        )
        downstream = _downstream([{"name": "ssn", "type": "string"}])

        log = propagate_pii_classifications(downstream, [upstream])

        assert len(log) == 1
        # Log line should mention column path + tag for receipt rendering.
        line = log[0]
        assert "ssn" in line
        assert "phi" in line
        assert "exposes[0]" in line


# ---------------------------------------------------------------------------
# 2. Operator overrides win.
# ---------------------------------------------------------------------------


class TestOperatorOverrideWins:
    """Downstream operator decisions are NOT silently overwritten."""

    def test_existing_sensitivity_is_preserved(self):
        upstream = _upstream(
            "demo.customers_sdp",
            "customers",
            [{"name": "email", "type": "string", "sensitivity": "pii"}],
        )
        downstream = _downstream(
            [
                {
                    "name": "email",
                    "type": "string",
                    # Operator already classified this as more strict.
                    "sensitivity": "confidential",
                },
            ]
        )

        log = propagate_pii_classifications(downstream, [upstream])

        col = downstream["exposes"][0]["contract"]["schema"][0]
        # Operator override stays.
        assert col["sensitivity"] == "confidential"
        # Nothing was propagated.
        assert log == []

    def test_existing_classification_alias_is_also_preserved(self):
        """Downstream contracts that used the legacy ``classification``
        key (catalog-style alias) keep that as an override."""
        upstream = _upstream(
            "demo.customers_sdp",
            "customers",
            [{"name": "email", "type": "string", "sensitivity": "pii"}],
        )
        downstream = _downstream(
            [{"name": "email", "type": "string", "classification": "internal"}]
        )

        log = propagate_pii_classifications(downstream, [upstream])

        col = downstream["exposes"][0]["contract"]["schema"][0]
        # The override must survive — even when the alias key was used.
        assert col.get("classification") == "internal"
        # And we didn't write a competing ``sensitivity``.
        assert "sensitivity" not in col
        assert log == []


# ---------------------------------------------------------------------------
# 3. Upstream-side aliases (sensitivity vs classification).
# ---------------------------------------------------------------------------


class TestUpstreamAliases:
    """Upstream tags written under either key flow downstream."""

    def test_classification_alias_on_upstream_is_recognised(self):
        upstream = _upstream(
            "demo.customers_sdp",
            "customers",
            [
                {
                    "name": "email",
                    "type": "string",
                    # Upstream contract used the legacy alias key.
                    "classification": "pii",
                }
            ],
        )
        downstream = _downstream([{"name": "email", "type": "string"}])

        log = propagate_pii_classifications(downstream, [upstream])

        col = downstream["exposes"][0]["contract"]["schema"][0]
        # Always written under the canonical key, regardless of which
        # one the upstream carried.
        assert col["sensitivity"] == "pii"
        assert len(log) == 1


# ---------------------------------------------------------------------------
# 4. Multi-upstream and multi-expose mesh.
# ---------------------------------------------------------------------------


class TestMultiUpstreamMesh:
    """Two SDPs feed an ADP. Each upstream's pii columns flow onto the
    matching downstream columns; non-matched columns stay untouched."""

    def test_multiple_upstreams_each_contribute_tags(self):
        customers = _upstream(
            "demo.customers_sdp",
            "customers",
            [
                {"name": "customer_id", "type": "integer"},
                {"name": "email", "type": "string", "sensitivity": "pii"},
            ],
        )
        orders = _upstream(
            "demo.orders_sdp",
            "orders",
            [
                {"name": "order_id", "type": "integer"},
                {"name": "billing_address", "type": "string", "sensitivity": "pii"},
            ],
        )
        downstream = _downstream(
            [
                {"name": "customer_id", "type": "integer"},
                {"name": "email", "type": "string"},
                {"name": "order_id", "type": "integer"},
                {"name": "billing_address", "type": "string"},
                # Brand new column that doesn't exist upstream — no
                # tag should be invented for it.
                {"name": "computed_at", "type": "timestamp"},
            ]
        )

        log = propagate_pii_classifications(downstream, [customers, orders])

        cols = {c["name"]: c for c in downstream["exposes"][0]["contract"]["schema"]}
        assert cols["email"]["sensitivity"] == "pii"
        assert cols["billing_address"]["sensitivity"] == "pii"
        assert "sensitivity" not in cols["customer_id"]
        assert "sensitivity" not in cols["order_id"]
        assert "sensitivity" not in cols["computed_at"]
        # Two propagation log entries, one per stamped column.
        assert len(log) == 2

    def test_first_upstream_match_wins_for_same_named_column(self):
        """When two upstreams both have a column with the same name,
        the first matched tag wins. Predictable and deterministic;
        operators can disambiguate by renaming downstream columns."""
        first = _upstream(
            "demo.upstream_a",
            "rows",
            [{"name": "email", "type": "string", "sensitivity": "pii"}],
        )
        second = _upstream(
            "demo.upstream_b",
            "rows",
            [{"name": "email", "type": "string", "sensitivity": "phi"}],
        )
        downstream = _downstream([{"name": "email", "type": "string"}])

        propagate_pii_classifications(downstream, [first, second])

        col = downstream["exposes"][0]["contract"]["schema"][0]
        # First-write-wins is the documented rule.
        assert col["sensitivity"] == "pii"


# ---------------------------------------------------------------------------
# 5. Edge cases — malformed inputs don't crash.
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Defensive: malformed upstream / downstream shapes get swallowed
    silently rather than crashing the composition pipeline."""

    def test_no_upstream_tags_is_a_clean_noop(self):
        upstream = _upstream(
            "demo.customers_sdp",
            "customers",
            [{"name": "email", "type": "string"}],  # no sensitivity.
        )
        downstream = _downstream([{"name": "email", "type": "string"}])

        log = propagate_pii_classifications(downstream, [upstream])

        assert log == []
        assert "sensitivity" not in downstream["exposes"][0]["contract"]["schema"][0]

    def test_downstream_without_exposes_is_a_clean_noop(self):
        upstream = _upstream(
            "demo.customers_sdp",
            "customers",
            [{"name": "email", "type": "string", "sensitivity": "pii"}],
        )
        downstream = {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": "demo.empty_adp",
            # No ``exposes`` key.
        }

        log = propagate_pii_classifications(downstream, [upstream])

        assert log == []

    def test_malformed_schema_is_skipped_gracefully(self):
        upstream = _upstream(
            "demo.customers_sdp",
            "customers",
            [{"name": "email", "type": "string", "sensitivity": "pii"}],
        )
        downstream = {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": "demo.bad_adp",
            "exposes": [
                # First expose is broken (string instead of dict).
                "not a dict",
                # Second expose has a string schema.
                {
                    "exposeId": "bad_schema",
                    "contract": {"schema": "not a list"},
                },
                # Third expose is well-formed and should still get the tag.
                {
                    "exposeId": "good",
                    "contract": {"schema": [{"name": "email", "type": "string"}]},
                },
            ],
        }

        log = propagate_pii_classifications(downstream, [upstream])

        # The malformed exposes were skipped; the well-formed one got
        # the tag.
        assert len(log) == 1
        good = downstream["exposes"][2]["contract"]["schema"][0]
        assert good["sensitivity"] == "pii"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
