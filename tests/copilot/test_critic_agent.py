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

"""Coverage for CriticAgent (Missing #2)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import List

import pytest

from fluid_build.copilot.agents.critic_agent import CriticAgent
from fluid_build.copilot.scratchpad import CriticFinding, Scratchpad


def _hub(name, business_keys):
    return SimpleNamespace(entity_name=name, business_key_columns=business_keys)


def _link(name, hubs, join_keys):
    return SimpleNamespace(
        link_name=name,
        hubs_involved=hubs,
        join_keys=join_keys,
    )


def _logical(*, hubs=None, links=None, conceptual_entities=None):
    dv2 = SimpleNamespace(
        hubs=hubs or [],
        links=links or [],
    )
    conceptual = SimpleNamespace(
        entities=[SimpleNamespace(name=n) for n in (conceptual_entities or [])],
    )
    return SimpleNamespace(dv2=dv2, conceptual=conceptual)


class TestReviewLogical:
    def test_hub_without_business_keys_flagged(self):
        pad = Scratchpad()
        logical = _logical(hubs=[_hub("customer", [])])

        findings = CriticAgent().review_logical(logical, scratchpad=pad)

        assert len(findings) == 1
        assert findings[0].stage == "logical"
        assert findings[0].severity == "warning"
        assert "business_key_columns" in findings[0].message
        # Side effect — added to the scratchpad too.
        assert len(pad.critic_findings) == 1

    def test_hub_with_business_keys_clean(self):
        pad = Scratchpad()
        logical = _logical(hubs=[_hub("customer", ["customer_id"])])
        findings = CriticAgent().review_logical(logical, scratchpad=pad)
        assert findings == []

    def test_link_with_one_hub_is_error(self):
        pad = Scratchpad()
        logical = _logical(
            hubs=[_hub("customer", ["customer_id"])],
            links=[_link("lnk_orphan", ["customer"], ["x"])],
        )
        findings = CriticAgent().review_logical(logical, scratchpad=pad)
        assert any(f.severity == "error" for f in findings)

    def test_link_without_join_keys_is_info_not_error(self):
        """Lineage-inferred links legitimately ship without
        join_keys — the critic surfaces them as INFO so they
        don't flood the repair loop."""
        pad = Scratchpad()
        logical = _logical(
            hubs=[
                _hub("customer", ["customer_id"]),
                _hub("order", ["order_id"]),
            ],
            links=[_link("lnk_customer_order", ["customer", "order"], [])],
        )
        findings = CriticAgent().review_logical(logical, scratchpad=pad)
        # No errors.
        assert all(f.severity != "error" for f in findings)
        # At least one info-level finding about the missing join_keys.
        assert any(f.severity == "info" and "join_keys" in f.message for f in findings)

    def test_conceptual_orphan_flagged_as_info(self):
        pad = Scratchpad()
        logical = _logical(
            hubs=[_hub("customer", ["customer_id"])],
            conceptual_entities=["customer", "order"],  # 'order' has no hub
        )
        findings = CriticAgent().review_logical(logical, scratchpad=pad)
        orphan_findings = [f for f in findings if "no matching" in f.message]
        assert len(orphan_findings) == 1
        assert orphan_findings[0].severity == "info"
        assert "order" in orphan_findings[0].message


class TestReviewLogicalDimensional:
    """C7 — Critic now reviews dimensional models, not just DV2."""

    def _dim_logical(
        self,
        *,
        facts=None,
        dimensions=None,
        variant=None,
    ):
        dimensional = SimpleNamespace(
            facts=facts or [],
            dimensions=dimensions or [],
            variant=variant,
        )
        return SimpleNamespace(
            dv2=None,
            dimensional=dimensional,
            conceptual=SimpleNamespace(entities=[]),
        )

    def test_no_facts_is_error(self):
        pad = Scratchpad()
        logical = self._dim_logical(facts=[])
        findings = CriticAgent().review_logical(logical, scratchpad=pad)
        assert any(f.severity == "error" and "fact tables" in f.message for f in findings)

    def test_facts_without_dimensions_is_warning(self):
        pad = Scratchpad()
        logical = self._dim_logical(
            facts=[SimpleNamespace(name="fact_orders", measures=[1], foreign_keys=[1])],
            dimensions=[],
        )
        findings = CriticAgent().review_logical(logical, scratchpad=pad)
        assert any(f.severity == "warning" and "no dimensions" in f.message for f in findings)

    def test_fact_without_measures_warns(self):
        pad = Scratchpad()
        logical = self._dim_logical(
            facts=[SimpleNamespace(name="fact_x", measures=[], foreign_keys=[1])],
            dimensions=[SimpleNamespace(name="dim_x", attributes=[1])],
        )
        findings = CriticAgent().review_logical(logical, scratchpad=pad)
        assert any(f.severity == "warning" and "no measures" in f.message for f in findings)

    def test_fact_without_foreign_keys_warns_when_dims_exist(self):
        pad = Scratchpad()
        logical = self._dim_logical(
            facts=[SimpleNamespace(name="fact_x", measures=[1], foreign_keys=[])],
            dimensions=[SimpleNamespace(name="dim_x", attributes=[1])],
        )
        findings = CriticAgent().review_logical(logical, scratchpad=pad)
        assert any(f.severity == "warning" and "foreign_keys" in f.message for f in findings)

    def test_dimension_without_attributes_warns(self):
        pad = Scratchpad()
        logical = self._dim_logical(
            facts=[SimpleNamespace(name="fact_x", measures=[1], foreign_keys=[1])],
            dimensions=[SimpleNamespace(name="dim_empty", attributes=[])],
        )
        findings = CriticAgent().review_logical(logical, scratchpad=pad)
        assert any(f.severity == "warning" and "no attributes" in f.message for f in findings)

    def test_galaxy_with_one_fact_is_info_not_error(self):
        pad = Scratchpad()
        logical = self._dim_logical(
            facts=[SimpleNamespace(name="fact_x", measures=[1], foreign_keys=[1])],
            dimensions=[SimpleNamespace(name="dim_x", attributes=[1])],
            variant="galaxy",
        )
        findings = CriticAgent().review_logical(logical, scratchpad=pad)
        # No errors.
        assert all(f.severity != "error" for f in findings)
        # Info-level note about variant + fact count.
        assert any(f.severity == "info" and "galaxy" in f.message for f in findings)


class TestReviewContract:
    def test_empty_exposes_is_error(self):
        pad = Scratchpad()
        findings = CriticAgent().review_contract(
            {"exposes": [], "metadata": {"domain": "x"}},
            scratchpad=pad,
        )
        assert any(f.severity == "error" and "exposes" in f.message for f in findings)

    def test_missing_domain_is_warning(self):
        pad = Scratchpad()
        findings = CriticAgent().review_contract(
            {"exposes": [{"name": "x", "description": "x"}]},
            scratchpad=pad,
        )
        assert any(f.severity == "warning" and "domain" in f.message for f in findings)

    def test_expose_without_description_is_info(self):
        pad = Scratchpad()
        findings = CriticAgent().review_contract(
            {
                "metadata": {"domain": "commerce"},
                "exposes": [{"name": "orders"}],
            },
            scratchpad=pad,
        )
        assert any(f.severity == "info" and "description" in f.message for f in findings)

    def test_clean_contract_no_findings(self):
        pad = Scratchpad()
        findings = CriticAgent().review_contract(
            {
                "metadata": {"domain": "commerce"},
                "exposes": [{"name": "orders", "description": "Order events"}],
            },
            scratchpad=pad,
        )
        assert findings == []

    def test_non_dict_contract_no_crash(self):
        pad = Scratchpad()
        assert CriticAgent().review_contract(None, scratchpad=pad) == []
        assert CriticAgent().review_contract("not a dict", scratchpad=pad) == []  # type: ignore[arg-type]


class TestReviewTransform:
    def test_acyclic_plan_clean(self):
        pad = Scratchpad()
        plan = SimpleNamespace(
            builds=[
                SimpleNamespace(name="raw_orders", ref_models=[]),
                SimpleNamespace(name="stg_orders", ref_models=["raw_orders"]),
                SimpleNamespace(name="dim_customer", ref_models=["raw_orders"]),
            ]
        )
        findings = CriticAgent().review_transform(plan, None, scratchpad=pad)
        assert findings == []

    def test_cycle_flagged_as_error(self):
        pad = Scratchpad()
        plan = SimpleNamespace(
            builds=[
                SimpleNamespace(name="a", ref_models=["b"]),
                SimpleNamespace(name="b", ref_models=["c"]),
                SimpleNamespace(name="c", ref_models=["a"]),
            ]
        )
        findings = CriticAgent().review_transform(plan, None, scratchpad=pad)
        assert any(f.severity == "error" and "circular" in f.message for f in findings)

    def test_empty_plan_no_findings(self):
        pad = Scratchpad()
        plan = SimpleNamespace(builds=[])
        assert CriticAgent().review_transform(plan, None, scratchpad=pad) == []
