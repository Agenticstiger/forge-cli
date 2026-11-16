# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``validate_semantics_block``: entity and dimension names as dbt MetricFlow sees them.

MetricFlow gives the entities and dimensions of one semantic model one namespace, and the dbt
export (``engines/dbt/semantic_models``) keeps the first of two that share a name, entities
before dimensions, and drops the other without a word: a dimension that loses its name warns. Across semantic models, dbt gives a name
one element type, so a dimension in one model named like an entity of another fails
``dbt parse`` (dbt-semantic-interfaces' ``ElementConsistencyRule``). Both are compared by the
MetricFlow name the export writes (``metricflow_name``) and both WARN.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from fluid_build.forge_datamodel.semantics_builder import validate_semantics_block

pytestmark = pytest.mark.unit


def _expose(expose_id: str, **semantics: Any) -> Dict[str, Any]:
    return {"exposeId": expose_id, "kind": "table", "semantics": semantics}


def _contract(*exposes: Dict[str, Any]) -> Dict[str, Any]:
    return {"fluidVersion": "0.7.7", "exposes": list(exposes)}


def _entity(name: str, entity_type: str = "foreign", expr: str = "") -> Dict[str, Any]:
    return {"name": name, "type": entity_type, "expr": expr or f"{name}_id"}


def _dimensions(*names: str) -> List[Dict[str, Any]]:
    return [{"name": name, "type": "categorical"} for name in names]


class TestOneModel:
    def test_an_entity_and_a_dimension_of_one_name(self):
        contract = _contract(
            _expose("orders", entities=[_entity("region")], dimensions=_dimensions("region"))
        )
        assert validate_semantics_block(contract) == (
            [],
            [
                "exposes[orders].semantics.dimensions[0]: dimension 'region' and entity "
                "'region' are one dbt MetricFlow name, 'region'. Entities and dimensions share "
                "one namespace in a semantic model, so the dbt export keeps entity 'region' and "
                "drops this dimension. Rename one of them."
            ],
        )

    def test_two_dimensions_of_one_metricflow_name(self):
        contract = _contract(_expose("orders", dimensions=_dimensions("order_date", "order date")))
        _, warnings = validate_semantics_block(contract)
        assert warnings == [
            "exposes[orders].semantics.dimensions[1]: dimension 'order date' and dimension "
            "'order_date' are one dbt MetricFlow name, 'order_date'. Entities and dimensions "
            "share one namespace in a semantic model, so the dbt export keeps dimension "
            "'order_date' and drops this dimension. Rename one of them."
        ]

    def test_two_entities_of_one_name_are_not_reported(self):
        """The export keeps the first; forge's one-expose shape repeats a foreign entity's name."""
        contract = _contract(
            _expose(
                "orders",
                entities=[_entity("customer", expr="buyer_id"), _entity("customer")],
            )
        )
        assert validate_semantics_block(contract) == ([], [])

    def test_an_entity_the_export_drops_takes_no_name(self):
        """The export drops a natural entity without a validity window, so dbt sees no clash."""
        contract = _contract(
            _expose(
                "customers",
                entities=[_entity("customer", "primary"), _entity("segment", "natural")],
                dimensions=_dimensions("segment"),
            )
        )
        assert validate_semantics_block(contract) == ([], [])

    def test_a_window_bound_is_reported_once(self):
        """The validity window check already makes a bound's clash an error."""
        bound = {
            "type": "time",
            "typeParams": {"timeGranularity": "day", "validityParams": {"isStart": True}},
        }
        contract = _contract(
            _expose(
                "customer_history",
                entities=[_entity("customer", "natural")],
                dimensions=[
                    {"name": "valid-from", "type": "categorical"},
                    {"name": "valid_from", **bound},
                    {
                        "name": "valid_to",
                        "type": "time",
                        "typeParams": {"timeGranularity": "day", "validityParams": {"isEnd": True}},
                    },
                ],
            )
        )
        errors, warnings = validate_semantics_block(contract)
        assert len(errors) == 1 and "the validity window bound 'valid_from'" in errors[0]
        assert warnings == []

    def test_collections_that_are_not_lists_hold_no_names(self):
        """The schema refuses them; the check reads them as empty and still checks the rest."""
        contract = _contract(
            _expose("orders", entities=5, dimensions="region"),
            _expose("stores", entities=[_entity("region")], dimensions=_dimensions("region")),
        )
        _, warnings = validate_semantics_block(contract)
        assert [warning.split(":")[0] for warning in warnings] == [
            "exposes[stores].semantics.dimensions[0]"
        ]


class TestAcrossModels:
    def test_a_dimension_named_like_an_entity_of_another_expose(self):
        contract = _contract(
            _expose(
                "orders", entities=[_entity("order", "primary")], dimensions=_dimensions("region")
            ),
            _expose("stores", entities=[_entity("store", "primary"), _entity("region")]),
            _expose("regions", entities=[_entity("region", "primary")]),
        )
        assert validate_semantics_block(contract) == (
            [],
            [
                "exposes[orders].semantics.dimensions[0]: the dbt MetricFlow name 'region' is a "
                "dimension of exposes[orders] and an entity of exposes[stores], "
                "exposes[regions]. dbt MetricFlow gives a name one element type across the "
                "semantic models of a project, so `dbt parse` rejects the export whenever the "
                "project declares a metric. Rename one of them."
            ],
        )

    def test_repeats_of_one_element_type_are_clean(self):
        """Entities of one name join models; dimensions of one name are one dimension."""
        contract = _contract(
            _expose(
                "orders",
                entities=[_entity("order", "primary"), _entity("customer")],
                dimensions=_dimensions("region"),
            ),
            _expose(
                "customers",
                entities=[_entity("customer", "primary")],
                dimensions=_dimensions("region"),
            ),
        )
        assert validate_semantics_block(contract) == ([], [])

    def test_the_element_the_export_keeps_is_the_one_compared(self):
        """``orders`` keeps the entity 'region' and drops its dimension, so ``stores`` clashes."""
        contract = _contract(
            _expose("orders", entities=[_entity("region")], dimensions=_dimensions("region")),
            _expose(
                "stores", entities=[_entity("store", "primary")], dimensions=_dimensions("region")
            ),
        )
        _, warnings = validate_semantics_block(contract)
        assert [warning.split(":")[0] for warning in warnings] == [
            "exposes[orders].semantics.dimensions[0]",
            "exposes[stores].semantics.dimensions[0]",
        ]
        assert "a dimension of exposes[stores] and an entity of exposes[orders]" in warnings[1]

    def test_an_expose_the_export_writes_no_model_for_is_not_compared(self):
        """``a-b`` and ``a_b`` are one model name: the export writes the first only."""
        contract = _contract(
            _expose("a-b", entities=[_entity("x", "primary")], dimensions=_dimensions("region")),
            _expose("a_b", entities=[_entity("region", "primary")]),
        )
        assert validate_semantics_block(contract) == ([], [])
