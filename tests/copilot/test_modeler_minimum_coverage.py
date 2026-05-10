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

"""Coverage for ``ModelerAgent._ensure_minimum_coverage``.

The defense-in-depth hook exists because some providers (notably Gemini in
OpenAPI 3.0 mode) occasionally return ``facts: []`` and ``dimensions: []``
for thin intents even though ``dimensional.yaml`` requires at least one
fact and two dimensions. When that happens AND the session carries an
industry pack with a matching seed skeleton, the agent transplants the
skeleton into the vacuous branch so downstream stages have something real
to emit.

Pin-points tested here:

1. Vacuous dimensional result + retail skeleton → facts/dimensions
   replaced from skeleton (LLM's conceptual/osi/name preserved).
2. Vacuous DV2 result + telco skeleton → hubs replaced.
3. Non-vacuous dimensional → missing canonical pieces are appended.
4. Vacuous dimensional with NO pack on session → result passes through
   unchanged (no crash, no surprise).
5. Vacuous dimensional with a pack that has no ``seed_dimensional_skeleton``
   → result passes through unchanged.
6. Exactly one fact + exactly two dims is considered "covered" (the
   threshold is at the boundary, not above it).
"""

from __future__ import annotations

from pathlib import Path

from fluid_build.copilot.agents.base import StageSession
from fluid_build.copilot.agents.modeler_agent import ModelerAgent
from fluid_build.copilot.industry.compiler import IndustryPackCompiler
from fluid_build.copilot.industry.pack import IndustryPack
from fluid_build.copilot.schemas.data_model import (
    DimensionalModel,
    DimensionTable,
    DV2Model,
    FactTable,
    FieldDefinition,
)
from fluid_build.copilot.schemas.intent import (
    BusinessIntent,
    DataProduct,
    Dimensions,
    Grain,
    Metric,
)
from fluid_build.copilot.schemas.osi import OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import ConceptualDraft, LogicalDraft
from fluid_build.copilot.store.backends.null import NullBackend


def _vacuous_logical(technique: str, name: str = "demo") -> LogicalDraft:
    """A LogicalDraft shaped exactly like the failing Gemini retail output."""
    kwargs: dict = {
        "name": name,
        "technique": technique,
        "description": "vacuous LLM output",
        "conceptual": ConceptualDraft(name=name, entities=[]),
        "osi": OSISemanticModel(name=f"{name}_osi"),
        "source_summary": {"source_kind": "intent"},
    }
    if technique == "dimensional":
        kwargs["dimensional"] = DimensionalModel()  # facts=[] dimensions=[]
    else:
        kwargs["dv2"] = DV2Model()  # hubs=[]
    return LogicalDraft(**kwargs)


def _non_vacuous_dimensional(name: str = "demo") -> LogicalDraft:
    return LogicalDraft(
        name=name,
        technique="dimensional",
        description="real LLM output",
        conceptual=ConceptualDraft(name=name, entities=[]),
        osi=OSISemanticModel(name=f"{name}_osi"),
        dimensional=DimensionalModel(
            facts=[FactTable(name="fact_x", grain_statement="one row per x")],
            dimensions=[
                DimensionTable(
                    name="dim_a", attributes=[FieldDefinition(name="a", data_type="STRING")]
                ),
                DimensionTable(
                    name="dim_b", attributes=[FieldDefinition(name="b", data_type="STRING")]
                ),
            ],
        ),
        source_summary={"source_kind": "intent"},
    )


def _session_with_pack(pack: IndustryPack | None, tmp_path: Path) -> StageSession:
    return StageSession(
        store=NullBackend(),
        workspace_root=tmp_path,
        industry_pack=pack,
    )


class TestEnsureMinimumCoverage:
    # ------------------------------------------------------------------
    # 1. Happy path: vacuous dimensional + real skeleton → transplanted.
    # ------------------------------------------------------------------
    def test_vacuous_dimensional_is_backfilled_from_skeleton(self, tmp_path: Path):
        pack = IndustryPackCompiler().compile("retail", technique="dimensional")
        assert (
            pack.seed_dimensional_skeleton is not None
        ), "retail pack must carry a dimensional skeleton — fixture precondition"
        result = _vacuous_logical("dimensional", name="retail_sales")
        session = _session_with_pack(pack, tmp_path)

        out = ModelerAgent()._ensure_minimum_coverage(
            result=result, session=session, technique="dimensional"
        )

        assert out.dimensional is not None
        assert len(out.dimensional.facts) >= 1
        assert len(out.dimensional.dimensions) >= 2
        # LLM-provided scalars are preserved — only the vacuous branch flipped.
        assert out.name == "retail_sales"
        assert out.osi.name == "retail_sales_osi"

    # ------------------------------------------------------------------
    # 2. DV2 path.
    # ------------------------------------------------------------------
    def test_vacuous_dv2_is_backfilled_from_skeleton(self, tmp_path: Path):
        pack = IndustryPackCompiler().compile("telecommunications", technique="data_vault_2")
        assert (
            pack.seed_dv2_skeleton is not None
        ), "telco pack must carry a DV2 skeleton — fixture precondition"
        result = _vacuous_logical("data_vault_2", name="telco_subs")
        session = _session_with_pack(pack, tmp_path)

        out = ModelerAgent()._ensure_minimum_coverage(
            result=result, session=session, technique="data_vault_2"
        )

        assert out.dv2 is not None
        assert len(out.dv2.hubs) >= 1
        assert out.name == "telco_subs"

    # ------------------------------------------------------------------
    # 3. Non-vacuous LLM output is preserved, with missing canonical pieces appended.
    # ------------------------------------------------------------------
    def test_non_vacuous_result_gets_canonical_repair(self, tmp_path: Path):
        pack = IndustryPackCompiler().compile("retail", technique="dimensional")
        result = _non_vacuous_dimensional(name="boutique_orders")
        session = _session_with_pack(pack, tmp_path)

        out = ModelerAgent()._ensure_minimum_coverage(
            result=result, session=session, technique="dimensional"
        )

        assert out.dimensional is not None
        fact_names = {fact.name for fact in out.dimensional.facts}
        dim_names = {dim.name for dim in out.dimensional.dimensions}
        # The LLM's own tables are preserved, and canonical essentials
        # are appended so coverage warnings become actionable repair.
        assert {"fact_x", "fact_sales_line", "fact_transaction"}.issubset(fact_names)
        assert {"dim_a", "dim_b", "dim_customer", "dim_product", "dim_store", "dim_date"}.issubset(
            dim_names
        )

    # ------------------------------------------------------------------
    # 4. No pack → no seeding, no crash.
    # ------------------------------------------------------------------
    def test_no_industry_pack_yields_no_backfill(self, tmp_path: Path):
        result = _vacuous_logical("dimensional")
        session = _session_with_pack(None, tmp_path)

        out = ModelerAgent()._ensure_minimum_coverage(
            result=result, session=session, technique="dimensional"
        )

        assert out.dimensional is not None
        assert out.dimensional.facts == []
        assert out.dimensional.dimensions == []

    # ------------------------------------------------------------------
    # 5. Pack present but no skeleton for this technique → no seeding.
    # ------------------------------------------------------------------
    def test_pack_without_matching_skeleton_is_noop(self, tmp_path: Path):
        # Finance does not ship a data-vault-2 skeleton — the finance
        # pack is dimensional/OBT-first because ISO 20022 message shapes
        # slot naturally into fact tables, not hubs/links. (F3 filled in
        # telco/dimensional, retail/data_vault_2, healthcare/dimensional,
        # and finance/one_big_table, so the remaining gap in the skeleton
        # matrix is exactly finance × data_vault_2.)
        pack = IndustryPackCompiler().compile("finance", technique="data_vault_2")
        assert pack.seed_dv2_skeleton is None

        result = _vacuous_logical("data_vault_2")
        session = _session_with_pack(pack, tmp_path)

        out = ModelerAgent()._ensure_minimum_coverage(
            result=result, session=session, technique="data_vault_2"
        )

        assert out.dv2 is not None
        assert out.dv2.hubs == []

    # ------------------------------------------------------------------
    # 6. Boundary: exactly 1 fact + 2 dims is non-vacuous but still repaired.
    # ------------------------------------------------------------------
    def test_one_fact_two_dims_is_considered_covered(self, tmp_path: Path):
        pack = IndustryPackCompiler().compile("retail", technique="dimensional")
        result = _non_vacuous_dimensional()  # 1 fact + 2 dims exactly
        session = _session_with_pack(pack, tmp_path)

        out = ModelerAgent()._ensure_minimum_coverage(
            result=result, session=session, technique="dimensional"
        )

        # LLM output survives and missing canonical facts/dims are appended.
        assert len(out.dimensional.facts) >= 3
        assert len(out.dimensional.dimensions) >= 6
        assert out.dimensional.facts[0].name == "fact_x"

    def test_intent_llm_output_uses_deterministic_backbone_before_skeleton_repair(
        self, tmp_path: Path
    ):
        pack = IndustryPackCompiler().compile("retail", technique="dimensional")
        session = _session_with_pack(pack, tmp_path)
        intent = BusinessIntent(
            data_product=DataProduct(name="provider_matrix_sales", domain="retail"),
            grain=Grain(entity="sales_line", description="One row per sales line."),
            dimensions=Dimensions(
                entities=["customer", "product", "store", "date"],
                attributes=["segment", "category"],
            ),
            metrics=[
                Metric(name="gross_revenue", description="Sum of revenue."),
                Metric(name="units_sold", description="Sum of quantity."),
            ],
        )
        provider_result = LogicalDraft(
            name="provider_matrix_sales",
            technique="dimensional",
            conceptual=ConceptualDraft(name="provider_matrix_sales"),
            dimensional=DimensionalModel(
                facts=[FactTable(name="fact_provider_specific", grain_statement="drifty")],
                dimensions=[DimensionTable(name="dim_provider_specific")],
            ),
            osi=OSISemanticModel(name="provider_matrix_sales"),
            source_summary={"provider_specific": "ignored_for_backbone"},
        )
        agent = ModelerAgent()

        backbone = agent._deterministic_intent_backbone(
            provider_result=provider_result,
            intent=intent,
            technique="dimensional",
        )
        out = agent._ensure_minimum_coverage(
            result=backbone,
            session=session,
            technique="dimensional",
        )

        assert out.dimensional is not None
        fact_names = {fact.name for fact in out.dimensional.facts}
        dim_names = {dimension.name for dimension in out.dimensional.dimensions}
        assert "fact_provider_specific" not in fact_names
        assert "dim_provider_specific" not in dim_names
        assert {"fact_sales_line", "fact_transaction"}.issubset(fact_names)
        assert {"dim_customer", "dim_product", "dim_store", "dim_date"}.issubset(dim_names)
        assert out.source_summary["logical_backbone"] == "deterministic_intent"

    def test_dv2_intent_builder_skips_self_links_when_grain_is_also_dimension(self):
        intent = BusinessIntent(
            data_product=DataProduct(name="telco_usage", domain="telecommunications"),
            grain=Grain(entity="usage_event"),
            dimensions=Dimensions(
                entities=["party", "usage_event", "service"],
                attributes=["status"],
            ),
        )

        out = ModelerAgent()._dv2_from_intent(intent)

        link_names = {link.link_table_name for link in out.links}
        assert "lnk_usage_event_usage_event" not in link_names
        assert {"lnk_usage_event_party", "lnk_usage_event_service"}.issubset(link_names)
