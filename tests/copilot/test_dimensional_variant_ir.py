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

"""Coverage for the typed dimensional-variant IR (D6).

Before D6, ``DimensionalModel`` had no typed variant field; the per-flavor
choice lived as a plain string under ``source_summary.dimensional_variant``
that was only set inside ``emit_dimensional_variants``. That meant:

* The modeler couldn't declare "this is a snowflake" at forge time.
* The validator had no typed hook to lint variant-specific rules
  against.
* Tooling read a free-form string from a discovery sidecar rather than
  a canonical IR field.

D6 promoted the choice to a first-class ``Literal`` on the IR with
``"star"`` as the default. These tests pin:

* The ``DimensionalVariant`` Literal and the ``DIMENSIONAL_VARIANTS``
  tuple agree — adding a new variant requires editing both.
* ``DimensionalModel`` defaults to ``"star"``.
* Invalid variants are rejected at parse time.
* ``recommend_dimensional_variant`` returns a variant that matches the
  shape of the model (multiple facts → galaxy, SCD2 → snowflake, etc.).
* ``emit_dimensional_variants`` emits both the **typed** IR field and
  the legacy ``source_summary.dimensional_variant`` string for
  backward compat (the existing e2e test still passes).
* Round-trip through ``model_dump_json`` / ``model_validate_json``
  preserves the typed variant.
"""

from __future__ import annotations

import json
import typing

import pytest
from pydantic import ValidationError

from fluid_build.copilot.schemas.data_model import (
    DIMENSIONAL_VARIANTS,
    DimensionalModel,
    DimensionalVariant,
    DimensionTable,
    FactTable,
    recommend_dimensional_variant,
)
from fluid_build.copilot.schemas.osi import OSIAIContext, OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import ConceptualDraft, LogicalDraft
from fluid_build.forge_datamodel.emit.variants import emit_dimensional_variants

# ----------------------------------------------------------------------
# Literal ↔ tuple lockstep
# ----------------------------------------------------------------------


class TestDimensionalVariantConstants:
    def test_tuple_matches_literal_order_and_contents(self):
        literal_values = set(typing.get_args(DimensionalVariant))
        assert set(DIMENSIONAL_VARIANTS) == literal_values
        # Order is load-bearing for emit loops; pin it too.
        assert DIMENSIONAL_VARIANTS == ("star", "snowflake", "galaxy", "flat")

    def test_tuple_is_immutable(self):
        assert isinstance(DIMENSIONAL_VARIANTS, tuple)


# ----------------------------------------------------------------------
# DimensionalModel typed field
# ----------------------------------------------------------------------


class TestDimensionalModelVariantField:
    def test_defaults_to_star(self):
        model = DimensionalModel()
        assert model.variant == "star"

    def test_accepts_every_variant(self):
        for variant in DIMENSIONAL_VARIANTS:
            model = DimensionalModel(variant=variant)
            assert model.variant == variant

    def test_rejects_unknown_variant(self):
        with pytest.raises(ValidationError):
            DimensionalModel(variant="cube")  # type: ignore[arg-type]

    def test_round_trip_preserves_variant(self):
        model = DimensionalModel(
            facts=[FactTable(name="fact_sales", grain_statement="one row per line")],
            variant="galaxy",
        )
        as_json = model.model_dump_json()
        parsed = json.loads(as_json)
        assert parsed["variant"] == "galaxy"
        loaded = DimensionalModel.model_validate_json(as_json)
        assert loaded.variant == "galaxy"
        assert loaded == model

    def test_legacy_caller_without_variant_still_validates(self):
        """Existing test harnesses / fixtures that predate D6 don't
        pass ``variant`` — those must keep working via the default."""
        model = DimensionalModel(
            facts=[FactTable(name="fact_x", grain_statement="…")],
            dimensions=[DimensionTable(name="dim_customer")],
        )
        assert model.variant == "star"


# ----------------------------------------------------------------------
# recommend_dimensional_variant heuristic
# ----------------------------------------------------------------------


class TestRecommendDimensionalVariant:
    def test_multiple_facts_and_conformed_dims_recommends_galaxy(self):
        model = DimensionalModel(
            facts=[
                FactTable(name="fact_orders", grain_statement="one row per order"),
                FactTable(name="fact_returns", grain_statement="one row per return"),
            ],
            conformed_dimensions=["dim_customer", "dim_product"],
        )
        assert recommend_dimensional_variant(model) == "galaxy"

    def test_single_fact_no_dims_recommends_flat(self):
        model = DimensionalModel(
            facts=[FactTable(name="fact_orders_obt", grain_statement="one row per order")],
        )
        assert recommend_dimensional_variant(model) == "flat"

    def test_no_facts_no_dims_recommends_flat(self):
        assert recommend_dimensional_variant(DimensionalModel()) == "flat"

    def test_scd2_dim_recommends_snowflake(self):
        model = DimensionalModel(
            facts=[FactTable(name="fact_sales", grain_statement="one row per line")],
            dimensions=[
                DimensionTable(
                    name="dim_customer",
                    slowly_changing_type="type2",
                ),
            ],
        )
        assert recommend_dimensional_variant(model) == "snowflake"

    def test_default_single_fact_plain_dims_recommends_star(self):
        model = DimensionalModel(
            facts=[FactTable(name="fact_sales", grain_statement="one row per line")],
            dimensions=[
                DimensionTable(name="dim_customer"),
                DimensionTable(name="dim_product"),
            ],
        )
        assert recommend_dimensional_variant(model) == "star"

    def test_multi_fact_without_conformed_dims_still_recommends_star(self):
        """Two facts that DON'T share conformed dims — not a galaxy
        yet. Guards against false positives if a user has two
        independent domains temporarily in the same draft."""
        model = DimensionalModel(
            facts=[
                FactTable(name="fact_a", grain_statement="…"),
                FactTable(name="fact_b", grain_statement="…"),
            ],
            dimensions=[DimensionTable(name="dim_x")],
        )
        assert recommend_dimensional_variant(model) == "star"


# ----------------------------------------------------------------------
# emit_dimensional_variants integration
# ----------------------------------------------------------------------


def _make_dim_logical(name: str = "sales_domain") -> LogicalDraft:
    return LogicalDraft(
        name=name,
        technique="dimensional",
        dimensional=DimensionalModel(
            facts=[
                FactTable(name="fact_order_line", grain_statement="one row per order line"),
            ],
            dimensions=[DimensionTable(name="dim_customer")],
            variant="snowflake",
        ),
        osi=OSISemanticModel(name=name, ai_context=OSIAIContext()),
        conceptual=ConceptualDraft(name=name),
    )


class TestEmitDimensionalVariantsRespectsTypedIR:
    def test_emits_one_document_per_registered_variant(self):
        logical = _make_dim_logical()
        variants = emit_dimensional_variants(logical)
        assert set(variants) == {f"{logical.name}.{v}.model.json" for v in DIMENSIONAL_VARIANTS}

    def test_each_document_sets_typed_variant_field(self):
        """The typed IR field inside ``dimensional`` must reflect
        THIS canvas's flavor — not the flavor the modeler originally
        chose."""
        logical = _make_dim_logical()
        variants = emit_dimensional_variants(logical)
        for filename, content in variants.items():
            doc = json.loads(content)
            flavor = filename.split(".")[1]
            assert doc["dimensional"]["variant"] == flavor

    def test_legacy_source_summary_hint_still_emitted(self):
        """Backward-compat guarantee — tooling that reads
        ``source_summary.dimensional_variant`` keeps working."""
        logical = _make_dim_logical()
        variants = emit_dimensional_variants(logical)
        for filename, content in variants.items():
            doc = json.loads(content)
            flavor = filename.split(".")[1]
            assert doc["source_summary"]["dimensional_variant"] == flavor

    def test_non_dimensional_drafts_emit_nothing(self):
        """A DV2 draft shouldn't accidentally produce dimensional
        canvases — mirrors the existing contract."""
        from fluid_build.copilot.schemas.data_model import DV2Model, HubDefinition

        logical = LogicalDraft(
            name="orders",
            technique="data_vault_2",
            dv2=DV2Model(
                hubs=[
                    HubDefinition(
                        entity_name="customer",
                        hub_table_name="hub_customer",
                        business_key_columns=["customer_id"],
                    )
                ]
            ),
            osi=OSISemanticModel(name="orders", ai_context=OSIAIContext()),
            conceptual=ConceptualDraft(name="orders"),
        )
        assert emit_dimensional_variants(logical) == {}

    def test_source_IR_variant_is_not_mutated_by_emit(self):
        """``emit_dimensional_variants`` deep-copies before writing, so
        the forged IR the caller still holds must keep its original
        variant choice."""
        logical = _make_dim_logical()
        assert logical.dimensional is not None
        assert logical.dimensional.variant == "snowflake"
        emit_dimensional_variants(logical)
        assert logical.dimensional.variant == "snowflake"
