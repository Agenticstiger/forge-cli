# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid forge data-model``: OSI relationships land in the contract 1:1 (schema 0.7.7).

The OSI IR keeps a relationship whole (``from``, ``to``, ordered ``from_columns`` /
``to_columns``) and the Ossie sidecar emits it so. The contract used to keep only a
foreign entity on its first column. Where both datasets are exposes of the contract
(the one-expose-per-dataset shape), the relationship is now written on the ``from``
expose, composite keys included, and only such a contract moves to 0.7.7.
"""

from __future__ import annotations

from typing import List

import pytest

from fluid_build.copilot.schemas.osi import (
    OSIDataset,
    OSIField,
    OSIRelationship,
    OSISemanticModel,
)
from fluid_build.copilot.schemas.stage_outputs import ConceptualDraft, LogicalDraft
from fluid_build.forge_datamodel.emit.fluid_contract import build_contract_from_logical
from fluid_build.forge_datamodel.emit.osi_sidecar import build_osi_document
from fluid_build.forge_datamodel.semantics_builder import validate_semantics_block
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit


def _dataset(name: str, primary_key: List[str], columns: List[str]) -> OSIDataset:
    return OSIDataset(
        name=name,
        source=name,
        primary_key=primary_key,
        fields=[OSIField(name=column) for column in columns],
    )


def _relationship(name: str, source: str, target: str, from_columns, to_columns):
    return OSIRelationship(
        name=name,
        **{"from": source, "to": target},
        from_columns=list(from_columns),
        to_columns=list(to_columns),
    )


def _logical(relationships, technique: str = "flat", **branches) -> LogicalDraft:
    osi = OSISemanticModel(
        name="shop",
        description="Shop orders",
        datasets=[
            _dataset("orders", ["order_id"], ["order_id", "customer_id", "region", "amount"]),
            _dataset("customers", ["customer_id"], ["customer_id", "segment"]),
            _dataset("order_lines", ["line_id"], ["line_id", "order_id", "region"]),
        ],
        relationships=relationships,
    )
    return LogicalDraft(
        name="shop",
        description="Shop orders",
        technique=technique,
        osi=osi,
        conceptual=ConceptualDraft(name="shop"),
        **branches,
    )


_ORDERS_TO_CUSTOMERS = _relationship(
    "orders_to_customers", "orders", "customers", ["customer_id"], ["customer_id"]
)
_LINES_TO_ORDERS = _relationship(
    "order_lines_to_orders", "order_lines", "orders", ["order_id", "region"], ["order_id", "region"]
)


def _semantics(contract, expose_id: str):
    return next(e for e in contract["exposes"] if e["exposeId"] == expose_id)["semantics"]


def test_relationships_land_on_the_from_expose_one_to_one():
    contract = build_contract_from_logical(_logical([_ORDERS_TO_CUSTOMERS, _LINES_TO_ORDERS]))
    assert _semantics(contract, "orders")["relationships"] == [
        {
            "name": "orders_to_customers",
            "to": "customers",
            "fromColumns": ["customer_id"],
            "toColumns": ["customer_id"],
            "cardinality": "many_to_one",
        }
    ]
    assert _semantics(contract, "order_lines")["relationships"][0]["fromColumns"] == [
        "order_id",
        "region",
    ]
    assert "relationships" not in _semantics(contract, "customers")


def test_contract_and_sidecar_hold_the_same_joins():
    logical = _logical([_ORDERS_TO_CUSTOMERS, _LINES_TO_ORDERS])
    contract = build_contract_from_logical(logical)
    in_contract = sorted(
        (e["exposeId"], r["to"], tuple(r["fromColumns"]), tuple(r["toColumns"]))
        for e in contract["exposes"]
        for r in e["semantics"].get("relationships", [])
    )
    sidecar = build_osi_document(logical)["semantic_model"][0]["relationships"]
    in_sidecar = sorted(
        (r["from"], r["to"], tuple(r["from_columns"]), tuple(r["to_columns"])) for r in sidecar
    )
    assert in_contract == in_sidecar


def test_only_a_contract_with_relationships_moves_to_077():
    manager = FluidSchemaManager()
    with_joins = build_contract_from_logical(_logical([_ORDERS_TO_CUSTOMERS]))
    assert with_joins["fluidVersion"] == "0.7.7"
    result = manager.validate_contract(with_joins, schema_version="0.7.7", offline_only=True)
    assert result.is_valid, [getattr(e, "message", str(e)) for e in result.errors]

    without = build_contract_from_logical(_logical([]))
    assert without["fluidVersion"] == FluidSchemaManager.latest_bundled_version()


def test_the_foreign_entity_is_named_after_the_target_key():
    """dbt MetricFlow joins by entity name, so the emitted pair must match."""
    contract = build_contract_from_logical(_logical([_ORDERS_TO_CUSTOMERS]))
    orders = {e["name"]: e for e in _semantics(contract, "orders")["entities"]}
    customers = {e["name"]: e for e in _semantics(contract, "customers")["entities"]}
    assert orders["customer"] == {"name": "customer", "type": "foreign", "expr": "customer_id"}
    assert customers["customer"]["type"] == "primary"
    assert validate_semantics_block(contract) == ([], [])


def test_a_relationship_on_undeclared_columns_is_not_written():
    ghost = _relationship(
        "orders_to_customers", "orders", "customers", ["buyer_id"], ["customer_id"]
    )
    contract = build_contract_from_logical(_logical([ghost]))
    assert "relationships" not in _semantics(contract, "orders")
    assert contract["fluidVersion"] == FluidSchemaManager.latest_bundled_version()


def test_a_single_expose_shape_keeps_foreign_entities_only():
    """The dimensional emitter collapses to one expose: no target to name."""
    from fluid_build.copilot.schemas.data_model import (
        DimensionalModel,
        DimensionTable,
        FactTable,
    )

    logical = _logical(
        [_ORDERS_TO_CUSTOMERS],
        technique="dimensional",
        dimensional=DimensionalModel(
            facts=[FactTable(name="fact_orders", grain_statement="one row per order")],
            dimensions=[DimensionTable(name="dim_customer")],
        ),
    )
    contract = build_contract_from_logical(logical)
    assert all("relationships" not in e["semantics"] for e in contract["exposes"])
    assert contract["fluidVersion"] == FluidSchemaManager.latest_bundled_version()


def test_the_rename_never_takes_a_dimension_s_name():
    """MetricFlow names share one namespace; the bridge would drop the dimension."""
    osi = _logical([]).osi.model_copy(deep=True)
    osi.datasets[0].fields.append(OSIField(name="customer"))
    osi.relationships = [_ORDERS_TO_CUSTOMERS]
    logical = _logical([_ORDERS_TO_CUSTOMERS]).model_copy(update={"osi": osi})
    contract = build_contract_from_logical(logical)
    orders = _semantics(contract, "orders")
    assert "customer" in [d["name"] for d in orders["dimensions"]]
    assert [e["name"] for e in orders["entities"] if e["type"] == "foreign"][0] == "customers"
    assert orders["relationships"][0]["to"] == "customers"


def test_datasets_that_slug_to_one_expose_id_get_no_relationship():
    osi = OSISemanticModel(
        name="shop",
        description="Shop orders",
        datasets=[
            _dataset("Orders", ["id"], ["id", "c"]),
            _dataset("orders", ["id"], ["id", "c"]),
        ],
        relationships=[_relationship("r", "Orders", "orders", ["c"], ["id"])],
    )
    logical = LogicalDraft(
        name="shop",
        description="Shop orders",
        technique="flat",
        osi=osi,
        conceptual=ConceptualDraft(name="shop"),
    )
    contract = build_contract_from_logical(logical)
    assert all("relationships" not in e["semantics"] for e in contract["exposes"])


def _foreign_entities(contract):
    return {
        expose["exposeId"]: [
            (entity["name"], entity["expr"])
            for entity in expose["semantics"].get("entities", [])
            if entity["type"] == "foreign"
        ]
        for expose in contract["exposes"]
    }


def test_a_foreign_entity_lands_on_its_from_expose_only():
    """One expose per dataset: a relationship's foreign entity is its ``from`` dataset's.

    Every expose used to get every relationship's foreign entity, the target keyed on its own
    key and the others on a column they lack, and the dbt export carried them all.
    """
    lines_to_orders = _relationship(
        "order_lines_to_orders", "order_lines", "orders", ["order_id"], ["order_id"]
    )
    contract = build_contract_from_logical(_logical([_ORDERS_TO_CUSTOMERS, lines_to_orders]))
    assert _foreign_entities(contract) == {
        "orders": [("customer", "customer_id")],
        "customers": [],
        "order_lines": [("order", "order_id")],
    }
    assert validate_semantics_block(contract) == ([], [])


def test_no_entity_keys_on_a_column_its_expose_lacks():
    ghost = _relationship(
        "orders_to_customers", "orders", "customers", ["buyer_id"], ["customer_id"]
    )
    contract = build_contract_from_logical(_logical([ghost, _LINES_TO_ORDERS]))
    for expose in contract["exposes"]:
        columns = {column["name"] for column in expose["contract"]["schema"]}
        for entity in expose["semantics"]["entities"]:
            assert entity["expr"] in columns, (expose["exposeId"], entity)
    assert _foreign_entities(contract) == {
        "orders": [],
        "customers": [],
        "order_lines": [("orders", "order_id")],
    }
