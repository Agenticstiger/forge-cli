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

"""Coverage for V2.4.13 — variant-specific dimensional lint rules.

D6 promoted ``DimensionalModel.variant`` to a typed Literal; V2.4.13
closes the loop by adding per-variant structural lint that warns when
a model's *declared* variant doesn't match its actual shape:

* **star** — one fact + ≥1 dim
* **snowflake** — at least one SCD2 dim
* **galaxy** — ≥2 facts + ≥1 conformed dim
* **flat** — ≤1 fact + zero separate dims

Findings are ``severity="warning"`` (not error) so a deviation
informs the operator without breaking the pipeline. Errors stay
reserved for schema / structural failures the validator agent can
auto-repair.

Tests cover:

1. **Per-variant happy path** — when the model matches its variant,
   zero findings fire. Defends against false positives.
2. **Per-variant violation** — each rule fires on the matching
   misshape, naming the right field path (so a CLI consumer can
   surface "field=dimensional.dimensions" in its UI).
3. **Validator integration** — the lint runs as part of the full
   :class:`FluidContractValidator.validate` call, alongside schema +
   skeleton linting.
"""

from __future__ import annotations

import pytest

from fluid_build.copilot.schemas.data_model import (
    DimensionalModel,
    DimensionTable,
    FactTable,
)
from fluid_build.copilot.schemas.osi import OSIAIContext, OSISemanticModel
from fluid_build.copilot.schemas.stage_outputs import ConceptualDraft, LogicalDraft
from fluid_build.forge_datamodel.emit.validator import (
    FluidContractValidator,
    lint_dimensional_variant,
)

# ---------------------------------------------------------------------
# Per-variant happy-path tests
# ---------------------------------------------------------------------


class TestVariantHappyPaths:
    def test_well_formed_star_emits_no_findings(self):
        model = DimensionalModel(
            facts=[FactTable(name="fact_orders", grain_statement="one row per order")],
            dimensions=[DimensionTable(name="dim_customer"), DimensionTable(name="dim_date")],
            variant="star",
        )
        assert lint_dimensional_variant(model) == []

    def test_well_formed_snowflake_emits_no_findings(self):
        model = DimensionalModel(
            facts=[FactTable(name="fact_orders", grain_statement="one row per order")],
            dimensions=[
                DimensionTable(name="dim_customer", slowly_changing_type="type2"),
                DimensionTable(name="dim_product"),
            ],
            variant="snowflake",
        )
        assert lint_dimensional_variant(model) == []

    def test_well_formed_galaxy_emits_no_findings(self):
        model = DimensionalModel(
            facts=[
                FactTable(name="fact_orders", grain_statement="one row per order"),
                FactTable(name="fact_returns", grain_statement="one row per return"),
            ],
            dimensions=[DimensionTable(name="dim_customer"), DimensionTable(name="dim_product")],
            conformed_dimensions=["dim_customer", "dim_product"],
            variant="galaxy",
        )
        assert lint_dimensional_variant(model) == []

    def test_well_formed_flat_emits_no_findings(self):
        """OBT/flat — exactly one fact, no separate dims."""
        model = DimensionalModel(
            facts=[FactTable(name="fact_orders_obt", grain_statement="one row per order")],
            variant="flat",
        )
        assert lint_dimensional_variant(model) == []


# ---------------------------------------------------------------------
# Per-variant violation tests
# ---------------------------------------------------------------------


class TestStarViolations:
    def test_star_with_zero_facts_warns(self):
        model = DimensionalModel(
            facts=[],
            dimensions=[DimensionTable(name="dim_x")],
            variant="star",
        )
        findings = lint_dimensional_variant(model)
        # No-fact star: warns about fact count. Dim warning may also
        # fire since fact_count==0; we just assert at least one fact
        # finding is present and it names the right field.
        fact_findings = [f for f in findings if f.field == "dimensional.facts"]
        assert fact_findings, f"expected fact-count warning, got {findings}"
        assert all(f.severity == "warning" for f in findings)

    def test_star_with_two_facts_warns(self):
        model = DimensionalModel(
            facts=[
                FactTable(name="fact_a", grain_statement="…"),
                FactTable(name="fact_b", grain_statement="…"),
            ],
            dimensions=[DimensionTable(name="dim_x")],
            variant="star",
        )
        findings = lint_dimensional_variant(model)
        assert any("exactly one fact" in f.message.lower() for f in findings), (
            "two-fact star must warn about the fact count"
        )

    def test_star_with_no_dims_warns(self):
        model = DimensionalModel(
            facts=[FactTable(name="fact_x", grain_statement="…")],
            variant="star",
        )
        findings = lint_dimensional_variant(model)
        dim_findings = [f for f in findings if f.field == "dimensional.dimensions"]
        assert dim_findings
        assert "switch" in dim_findings[0].message.lower()  # suggests flat


class TestSnowflakeViolations:
    def test_snowflake_without_scd2_warns(self):
        model = DimensionalModel(
            facts=[FactTable(name="fact_x", grain_statement="…")],
            dimensions=[DimensionTable(name="dim_customer")],  # no SCD2
            variant="snowflake",
        )
        findings = lint_dimensional_variant(model)
        assert any("SCD2" in f.message for f in findings)
        assert any(f.field == "dimensional.dimensions" for f in findings)

    def test_snowflake_with_scd2_passes(self):
        model = DimensionalModel(
            facts=[FactTable(name="fact_x", grain_statement="…")],
            dimensions=[
                DimensionTable(name="dim_customer", slowly_changing_type="type2"),
            ],
            variant="snowflake",
        )
        assert lint_dimensional_variant(model) == []


class TestGalaxyViolations:
    def test_galaxy_with_one_fact_warns(self):
        model = DimensionalModel(
            facts=[FactTable(name="fact_only", grain_statement="…")],
            dimensions=[DimensionTable(name="dim_x")],
            conformed_dimensions=["dim_x"],
            variant="galaxy",
        )
        findings = lint_dimensional_variant(model)
        assert any("≥ 2 fact tables" in f.message for f in findings), (
            "single-fact galaxy must warn about the fact count"
        )

    def test_galaxy_without_conformed_dims_warns(self):
        model = DimensionalModel(
            facts=[
                FactTable(name="fact_a", grain_statement="…"),
                FactTable(name="fact_b", grain_statement="…"),
            ],
            dimensions=[DimensionTable(name="dim_x")],
            conformed_dimensions=[],  # empty
            variant="galaxy",
        )
        findings = lint_dimensional_variant(model)
        conformed_findings = [f for f in findings if f.field == "dimensional.conformed_dimensions"]
        assert conformed_findings
        assert "structural signature" in conformed_findings[0].message.lower()


class TestFlatViolations:
    def test_flat_with_two_facts_warns(self):
        model = DimensionalModel(
            facts=[
                FactTable(name="fact_a", grain_statement="…"),
                FactTable(name="fact_b", grain_statement="…"),
            ],
            variant="flat",
        )
        findings = lint_dimensional_variant(model)
        assert any("≤ 1 fact" in f.message for f in findings)

    def test_flat_with_dimensions_warns(self):
        """OBT shouldn't have separate dim tables — that's just a star
        with extras."""
        model = DimensionalModel(
            facts=[FactTable(name="fact_obt", grain_statement="…")],
            dimensions=[DimensionTable(name="dim_x")],
            variant="flat",
        )
        findings = lint_dimensional_variant(model)
        dim_findings = [f for f in findings if f.field == "dimensional.dimensions"]
        assert dim_findings
        assert "denormalised" in dim_findings[0].message.lower()


# ---------------------------------------------------------------------
# Validator integration — variant lint fires alongside other rules
# ---------------------------------------------------------------------


def _make_logical(model: DimensionalModel) -> LogicalDraft:
    return LogicalDraft(
        name="orders",
        technique="dimensional",
        dimensional=model,
        osi=OSISemanticModel(name="orders", ai_context=OSIAIContext()),
        conceptual=ConceptualDraft(name="orders"),
    )


class TestVariantLintInValidator:
    def test_validator_includes_variant_warnings(self):
        """A snowflake-without-SCD2 logical draft must surface the
        variant warning when run through ``FluidContractValidator``."""
        logical = _make_logical(
            DimensionalModel(
                facts=[FactTable(name="fact_x", grain_statement="…")],
                dimensions=[DimensionTable(name="dim_customer")],
                variant="snowflake",
            )
        )
        report = FluidContractValidator().validate(logical=logical)
        warning_messages = [i.message for i in report.issues if i.severity == "warning"]
        assert any("SCD2" in msg for msg in warning_messages)

    def test_validator_no_variant_warning_for_clean_model(self):
        """A well-formed model produces zero variant findings even when
        the validator runs other lint paths."""
        logical = _make_logical(
            DimensionalModel(
                facts=[FactTable(name="fact_orders", grain_statement="…")],
                dimensions=[DimensionTable(name="dim_customer")],
                variant="star",
            )
        )
        report = FluidContractValidator().validate(logical=logical)
        # No SCD2/galaxy/flat-related warnings — passes_schema is also True.
        for issue in report.issues:
            assert "SCD2" not in issue.message
            assert "galaxy" not in issue.message.lower()

    def test_dv2_drafts_skip_variant_lint(self):
        """Variant lint only applies to dimensional drafts. A DV2 draft
        must not trigger any variant warning even though it has a
        ``dimensional`` field set to None."""
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
        report = FluidContractValidator().validate(logical=logical)
        # No variant-related findings.
        for issue in report.issues:
            assert "variant=" not in (issue.message or "")
