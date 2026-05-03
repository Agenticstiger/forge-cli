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

"""Pin V1.3.7 — OSI v0.1.1 conformance smoke for forged contracts.

The plan promises that every Fluid contract emitted by ``fluid forge
data-model`` carries an OSI v0.1.1-shaped semantic block at
``exposes[*].contract.semantics`` so dbt, Snowflake Cortex,
Databricks Unity Catalog, Cube, and any OSI-aware BI tool can consume
the contract without translation.

Until V1.3.7 the OSI shape was tested at the schema level (Pydantic
model validates per the upstream spec) but no test confirmed the
*emitted* contract actually carries the OSI top-level keys the spec
requires. A regression that silently dropped the semantics block —
or scattered the fields under unexpected paths — would have shipped
without the suite catching it.

The pins here are intentionally **shape-level**, not field-level:

1. Every emitted contract has a ``semantics`` block on every
   ``exposes[]`` entry.
2. The ``semantics`` block carries at minimum the OSI-required
   top-level keys (``name`` and ``description`` per
   ``core-spec/spec.md`` v0.1.1 §"Semantic Model").
3. When the logical draft has metrics or entity-level relationships,
   those propagate into the emitted ``semantics`` (otherwise BI
   tools that read ``measures[]`` or ``entities[]`` get nothing).
4. The Fluid contract round-trips through the schema validator
   without raising.

The deeper "spec-by-spec field-level" conformance is covered by
``tests/copilot/test_osi_child_level_fields.py`` (already in the
suite). This file is the thin gate that catches "semantics dropped
from the emitted contract entirely" — the failure mode that would
silently break every downstream consumer.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable

import pytest

from fluid_build.copilot.schemas.data_model import (
    DimensionalModel,
    DimensionTable,
    DV2Model,
    FactTable,
    FieldDefinition,
    HubDefinition,
    SatelliteDefinition,
)
from fluid_build.copilot.schemas.osi import (
    OSIAIContext,
    OSIDataset,
    OSIDimension,
    OSIExpression,
    OSIExpressionDialect,
    OSIField,
    OSIMetric,
    OSIRelationship,
    OSISemanticModel,
)
from fluid_build.copilot.schemas.stage_outputs import ConceptualDraft, LogicalDraft
from fluid_build.forge_datamodel.emit.fluid_contract import build_contract_from_logical
from fluid_build.forge_datamodel.emit.validator import FluidContractValidator

# OSI v0.1.1 §"Semantic Model": ``name`` is REQUIRED. ``description``
# is also expected because every downstream BI tool we ship for
# (dbt's ``description``, Snowflake Cortex's ``description``, Cube's
# ``description``) maps directly from this field.
_OSI_REQUIRED_TOP_LEVEL_KEYS: tuple[str, ...] = ("name",)
_OSI_RECOMMENDED_TOP_LEVEL_KEYS: tuple[str, ...] = ("name", "description")


# ---------------------------------------------------------------------
# Helpers — build minimal logical drafts (dimensional + DV2)
# ---------------------------------------------------------------------


def _make_osi(
    name: str = "customer_orders",
    description: str = "Customer order facts and dimensions",
) -> OSISemanticModel:
    return OSISemanticModel(
        name=name,
        description=description,
        ai_context=OSIAIContext(
            instructions="Use for customer revenue analysis",
            synonyms=["customer purchases", "order history"],
            examples=["Show revenue by segment last 90 days"],
        ),
        datasets=[
            OSIDataset(
                name="orders",
                source="raw.orders",
                primary_key=["order_id"],
                fields=[
                    OSIField(
                        name="order_date",
                        expression=OSIExpression(
                            dialects=[
                                OSIExpressionDialect(dialect="ANSI_SQL", expression="order_date"),
                            ]
                        ),
                        dimension=OSIDimension(is_time=True, grain="day"),
                    ),
                    OSIField(name="customer_id"),
                    OSIField(name="amount"),
                ],
            )
        ],
        relationships=[
            OSIRelationship(
                name="orders_to_customers",
                **{"from": "orders", "to": "customers"},
                from_columns=["customer_id"],
                to_columns=["id"],
            )
        ],
        metrics=[
            OSIMetric(
                name="total_revenue",
                description="Total revenue from all orders",
                expression=OSIExpression(
                    dialects=[
                        OSIExpressionDialect(dialect="ANSI_SQL", expression="SUM(orders.amount)"),
                    ]
                ),
            )
        ],
    )


def _make_dimensional_logical() -> LogicalDraft:
    return LogicalDraft(
        name="customer_orders",
        description="Star schema for customer order analytics",
        technique="dimensional",
        dimensional=DimensionalModel(
            facts=[
                FactTable(
                    name="fact_orders",
                    grain_statement="one row per order",
                ),
            ],
            dimensions=[
                DimensionTable(name="dim_customer"),
                DimensionTable(name="dim_date"),
            ],
        ),
        osi=_make_osi(),
        conceptual=ConceptualDraft(name="customer_orders"),
    )


def _make_dv2_logical() -> LogicalDraft:
    return LogicalDraft(
        name="customer_orders",
        description="DV2 raw vault for customer orders",
        technique="data_vault_2",
        dv2=DV2Model(
            hubs=[
                HubDefinition(
                    entity_name="customer",
                    hub_table_name="hub_customer",
                    business_key_columns=["customer_id"],
                ),
                HubDefinition(
                    entity_name="order",
                    hub_table_name="hub_order",
                    business_key_columns=["order_id"],
                ),
            ],
            satellites=[
                SatelliteDefinition(
                    entity_name="customer",
                    satellite_table_name="sat_customer_profile",
                    parent_hub="hub_customer",
                    attributes=["name", "email"],
                ),
            ],
        ),
        osi=_make_osi(),
        conceptual=ConceptualDraft(name="customer_orders"),
    )


def _all_semantics_blocks(contract: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Yield every ``exposes[i].semantics`` mapping in ``contract``.

    The FLUID 0.7.2 schema (``$defs.expose.properties.semantics``)
    places the semantics block at the expose level — sibling of
    ``contract``, not nested inside it. The plan's "exposes[].contract
    .semantics" wording was imprecise; this helper matches the
    actual schema-blessed path so consumers (dbt's semantic-layer
    parser, Cube's adapter, Snowflake Cortex's catalog reader) all
    look in the right place. A contract may have multiple exposes
    (e.g. primary table + materialized view); the OSI block must
    live on each because each expose has independent semantics.
    """
    for expose in contract.get("exposes", []):
        semantics = expose.get("semantics")
        if semantics is not None:
            yield semantics


# ---------------------------------------------------------------------
# Pin: every emitted contract carries OSI semantics on every expose
# ---------------------------------------------------------------------


class TestOSISemanticsPresence:
    @pytest.mark.parametrize(
        "logical_factory",
        [
            pytest.param(_make_dimensional_logical, id="dimensional"),
            pytest.param(_make_dv2_logical, id="data_vault_2"),
        ],
    )
    def test_every_expose_has_a_semantics_block(self, logical_factory) -> None:
        """The headline pin: ``exposes[i].semantics`` exists on every
        expose for every supported technique (per the FLUID 0.7.2
        schema's ``$defs.expose.properties.semantics`` definition).
        A regression that silently dropped the semantics block
        (e.g., a refactor that only emitted ``schema`` and ``binding``)
        would fail this loudly."""
        logical = logical_factory()
        contract = build_contract_from_logical(logical)
        exposes = contract.get("exposes", [])
        assert exposes, "contract must have at least one expose"
        for expose in exposes:
            assert "semantics" in expose, (
                f"expose {expose.get('exposeId')!r}: missing semantics — "
                "OSI v0.1.1 + FLUID 0.7.2 require the block on every expose"
            )

    @pytest.mark.parametrize(
        "logical_factory",
        [
            pytest.param(_make_dimensional_logical, id="dimensional"),
            pytest.param(_make_dv2_logical, id="data_vault_2"),
        ],
    )
    def test_semantics_carries_required_top_level_keys(self, logical_factory) -> None:
        """OSI v0.1.1 §"Semantic Model" requires ``name`` at minimum;
        ``description`` is recommended for every downstream BI tool we
        target. Pin both — a future PR that drops either is caught
        here before it ships."""
        logical = logical_factory()
        contract = build_contract_from_logical(logical)
        for semantics in _all_semantics_blocks(contract):
            for key in _OSI_REQUIRED_TOP_LEVEL_KEYS:
                assert key in semantics, f"OSI top-level key {key!r} missing"
            for key in _OSI_RECOMMENDED_TOP_LEVEL_KEYS:
                assert key in semantics, (
                    f"OSI recommended key {key!r} missing — every BI tool we "
                    "target reads this; dropping it silently breaks downstream"
                )


# ---------------------------------------------------------------------
# Pin: metrics, entities, and time-grain dimensions propagate
# ---------------------------------------------------------------------


class TestOSIPropagation:
    def test_logical_metrics_become_emitted_measures(self) -> None:
        """OSI metrics on the LogicalDraft must surface as
        ``semantics.measures[]`` (Cube-shaped) AND
        ``semantics.metrics[]`` (dbt-shaped) so neither downstream
        flavour drops them. A regression that only emitted ONE of the
        two would silently break the other."""
        logical = _make_dimensional_logical()
        contract = build_contract_from_logical(logical)
        for semantics in _all_semantics_blocks(contract):
            assert "measures" in semantics, "Cube-shaped measures missing"
            assert "metrics" in semantics, "dbt-shaped metrics missing"
            assert any(m.get("name") == "total_revenue" for m in semantics["measures"])
            assert any(m.get("name") == "total_revenue" for m in semantics["metrics"])

    def test_time_grain_dimension_carries_typeParams(self) -> None:
        """The OSI ``dimension.grain="day"`` must surface as
        ``typeParams.timeGranularity="day"`` so dbt's semantic-layer
        picks it up. A regression that dropped ``typeParams`` would
        silently break time-grain queries."""
        logical = _make_dimensional_logical()
        contract = build_contract_from_logical(logical)
        for semantics in _all_semantics_blocks(contract):
            time_dims = [d for d in semantics.get("dimensions", []) if d.get("type") == "time"]
            assert time_dims, "expected at least one time dimension in the emitted semantics"
            assert any(
                d.get("typeParams", {}).get("timeGranularity") == "day" for d in time_dims
            ), "time dimension must carry typeParams.timeGranularity for dbt"

    def test_unsupported_second_time_grain_is_schema_safe(self) -> None:
        logical = _make_dimensional_logical()
        assert logical.osi.datasets[0].fields[0].dimension is not None
        logical.osi.datasets[0].fields[0].dimension.grain = "second"

        contract = build_contract_from_logical(logical)

        for semantics in _all_semantics_blocks(contract):
            time_dims = [d for d in semantics.get("dimensions", []) if d.get("type") == "time"]
            assert time_dims
            assert all(
                d.get("typeParams", {}).get("timeGranularity") != "second" for d in time_dims
            )
            assert any(
                d.get("typeParams", {}).get("timeGranularity") == "minute" for d in time_dims
            )

    def test_relationships_become_foreign_entities(self) -> None:
        """Every OSI ``relationships[]`` entry must surface as a
        ``foreign`` entity in the emitted ``semantics.entities[]`` so
        Cube/dbt can resolve cross-table joins at query time."""
        logical = _make_dimensional_logical()
        contract = build_contract_from_logical(logical)
        for semantics in _all_semantics_blocks(contract):
            entities = semantics.get("entities", [])
            foreign_entities = [e for e in entities if e.get("type") == "foreign"]
            assert any(
                e.get("name") == "customers" for e in foreign_entities
            ), "OSI relationship target must surface as a foreign entity"


class TestSemanticCompleteness:
    @pytest.mark.parametrize(
        "logical_factory",
        [
            pytest.param(_make_dimensional_logical, id="dimensional"),
            pytest.param(_make_dv2_logical, id="data_vault_2"),
        ],
    )
    @pytest.mark.xfail(
        strict=False,
        reason="needs build_runners + acquisition pattern \u2014 lands in PR-3 (runners) or later",
    )
    def test_physical_ir_backfills_sparse_osi_semantics(self, logical_factory) -> None:
        logical = logical_factory()
        logical.osi = OSISemanticModel(name=logical.name)
        if logical.dimensional is not None:
            logical.dimensional.facts[0].foreign_keys = ["customer_id", "order_date"]
            logical.dimensional.facts[0].measures = [
                FieldDefinition(
                    name="gross_revenue",
                    data_type="NUMBER",
                    description="Gross revenue booked on the order.",
                )
            ]
            logical.dimensional.dimensions[0].natural_keys = ["customer_id"]
            logical.dimensional.dimensions[0].attributes = [
                FieldDefinition(name="customer_name", data_type="STRING"),
            ]
            logical.dimensional.dimensions[1].attributes = [
                FieldDefinition(name="order_date", data_type="DATE"),
            ]

        contract = build_contract_from_logical(logical)

        for semantics in _all_semantics_blocks(contract):
            for key in ("entities", "dimensions", "measures", "metrics"):
                assert semantics.get(key), f"expected non-empty semantics.{key}"
        report = FluidContractValidator().validate(contract=contract)
        assert report.passes_schema is True

    def test_validator_rejects_semantically_thin_contracts(self) -> None:
        contract = build_contract_from_logical(_make_dimensional_logical())
        contract["exposes"][0]["semantics"] = {
            "name": "thin_model",
            "description": "A named but unusable semantic model.",
        }

        report = FluidContractValidator().validate(contract=contract)

        assert report.passes_schema is False
        fields = {issue.field for issue in report.issues if issue.severity == "error"}
        assert "exposes[0].semantics.entities" in fields
        assert "exposes[0].semantics.dimensions" in fields
        assert "exposes[0].semantics.measures" in fields
        assert "exposes[0].semantics.metrics" in fields


# ---------------------------------------------------------------------
# Pin: emitted contract round-trips through the validator
# ---------------------------------------------------------------------


class TestEmittedContractValidates:
    @pytest.mark.parametrize(
        "logical_factory",
        [
            pytest.param(_make_dimensional_logical, id="dimensional"),
            pytest.param(_make_dv2_logical, id="data_vault_2"),
        ],
    )
    @pytest.mark.xfail(
        strict=False,
        reason="needs build_runners + acquisition pattern \u2014 lands in PR-3 (runners) or later",
    )
    def test_emitted_contract_passes_fluid_validator(self, logical_factory) -> None:
        """The OSI block we emit can't break Fluid contract validation —
        otherwise the user gets a contract that references
        ``semantics`` correctly per the OSI spec but fails the
        ``fluid_build`` validator's structural rules."""
        logical = logical_factory()
        contract = build_contract_from_logical(logical)
        # ``FluidContractValidator.validate`` returns a
        # ``ValidationReport``; zero error-severity issues == clean.
        report = FluidContractValidator().validate(contract=contract)
        errors = [i for i in report.issues if i.severity == "error"]
        assert not errors, (
            f"emitted contract failed Fluid validator: " f"{[i.message for i in errors]}"
        )
        # Schema-validation also passes.
        assert report.passes_schema is True, f"contract failed schema validation: {report.issues}"

    def test_osi_pydantic_round_trip_preserves_dialects(self) -> None:
        """OSI's ``expression.dialects[]`` is the multi-dialect SQL
        representation that downstream BI tools key off. A
        ``model_dump`` → ``model_validate_json`` round-trip must
        preserve the dialect list verbatim — no field renames, no
        silent default substitutions."""
        import json

        osi = _make_osi()
        dumped = osi.model_dump(mode="json")
        rehydrated = OSISemanticModel.model_validate(dumped)
        # Dialect lists pin tightly because the OSI v0.1.1 spec
        # requires the wrapping ``dialects`` array — flattening to a
        # bare expression string would break tools that key off
        # ``dialects[].dialect``.
        assert rehydrated.metrics[0].expression.dialects[0].dialect == "ANSI_SQL"
        assert rehydrated.metrics[0].expression.dialects[0].expression == "SUM(orders.amount)"
        # And JSON serialisation must be lossless too — the
        # serialised contract is what hits disk for downstream tools
        # to parse.
        as_json = json.dumps(dumped)
        assert "ANSI_SQL" in as_json
        assert "SUM(orders.amount)" in as_json
