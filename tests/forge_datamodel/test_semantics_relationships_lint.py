# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``validate_semantics_block``: the checks of relationships and validity windows (schema 0.7.7).

RFC-semantics-tier1's v1 lint, the references a JSON Schema cannot check:

* §4 relationships: ``to`` is an expose of this contract; ``fromColumns`` and ``toColumns`` have
  the same length; every column is one its own expose declares; names differ within an expose.
  Errors. A one-column relationship with no matching entity pair warns: the dbt MetricFlow
  export joins by entity name and would not follow it.
* §5 validity windows: exactly two time dimensions, one ``isStart`` and the other ``isEnd``, and
  one natural entity (dbt-semantic-interfaces' ``SemanticModelValidityWindowRule``). Errors.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

import pytest

from fluid_build.forge_datamodel.semantics_builder import (
    RELATIONSHIPS_VERSION,
    validate_semantics_block,
    validity_window,
    version_for_relationship_keys,
)

pytestmark = pytest.mark.unit


def _expose(expose_id: str, columns: List[str], semantics: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "contract": {"schema": [{"name": c, "type": "string"} for c in columns]},
        "semantics": semantics,
    }


def _contract() -> Dict[str, Any]:
    """Orders -> customers, with a matching entity pair: lint-clean."""
    return {
        "fluidVersion": "0.7.7",
        "exposes": [
            _expose(
                "orders",
                ["order_id", "customer_id", "region"],
                {
                    "entities": [
                        {"name": "order", "type": "primary", "expr": "order_id"},
                        {"name": "customer", "type": "foreign", "expr": "customer_id"},
                    ],
                    "relationships": [
                        {
                            "name": "orders_to_customers",
                            "to": "customers",
                            "fromColumns": ["customer_id"],
                            "toColumns": ["id"],
                            "cardinality": "many_to_one",
                        }
                    ],
                },
            ),
            _expose(
                "customers",
                ["id", "region", "segment"],
                {"entities": [{"name": "customer", "type": "primary", "expr": "id"}]},
            ),
        ],
    }


def _relationship(contract: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    return contract["exposes"][0]["semantics"]["relationships"][index]


def _scd(**overrides: Any) -> Dict[str, Any]:
    semantics: Dict[str, Any] = {
        "entities": [{"name": "customer", "type": "natural", "expr": "customer_id"}],
        "dimensions": [
            {"name": "segment", "type": "categorical"},
            {
                "name": "valid_from",
                "type": "time",
                "typeParams": {"timeGranularity": "day", "validityParams": {"isStart": True}},
            },
            {
                "name": "valid_to",
                "type": "time",
                "typeParams": {"timeGranularity": "day", "validityParams": {"isEnd": True}},
            },
        ],
    }
    semantics.update(overrides)
    return {
        "exposes": [
            _expose(
                "customer_history", ["customer_id", "segment", "valid_from", "valid_to"], semantics
            )
        ]
    }


class TestRelationshipReferences:
    def test_a_well_formed_relationship_is_clean(self):
        assert validate_semantics_block(_contract()) == ([], [])

    def test_unknown_target_names_the_exposes_of_the_contract(self):
        contract = _contract()
        _relationship(contract)["to"] = "customer_profiles"
        errors, _ = validate_semantics_block(contract)
        assert errors == [
            "exposes[orders].semantics.relationships[0].to: 'customer_profiles' is not an "
            "exposeId of this contract (customers, orders). A relationship joins two exposes of "
            "one contract."
        ]

    def test_column_lists_of_different_lengths(self):
        contract = _contract()
        _relationship(contract)["fromColumns"] = ["customer_id", "region"]
        errors, _ = validate_semantics_block(contract)
        assert any(
            "fromColumns has 2 columns and toColumns 1. They pair up by position" in e
            for e in errors
        ), errors

    def test_a_from_column_the_expose_lacks_suggests_the_near_match(self):
        contract = _contract()
        _relationship(contract)["fromColumns"] = ["Customer_ID"]
        errors, _ = validate_semantics_block(contract)
        assert errors == [
            "exposes[orders].semantics.relationships[0].fromColumns: 'Customer_ID' is not a column "
            "of exposes[orders] (did you mean 'customer_id'?). Name a column its contract.schema "
            "declares."
        ]

    def test_a_to_column_is_checked_against_the_target(self):
        contract = _contract()
        _relationship(contract)["toColumns"] = ["order_id"]
        errors, _ = validate_semantics_block(contract)
        assert errors == [
            "exposes[orders].semantics.relationships[0].toColumns: 'order_id' is not a column of "
            "exposes[customers]. Name a column its contract.schema declares."
        ]

    def test_a_target_without_a_schema_cannot_be_checked(self):
        contract = _contract()
        del contract["exposes"][1]["contract"]
        errors, _ = validate_semantics_block(contract)
        assert errors == [
            "exposes[orders].semantics.relationships[0].toColumns: exposes[customers] declares no "
            "contract.schema columns, so 'id' cannot be checked. Declare the expose's columns."
        ]

    def test_duplicate_names_in_one_expose(self):
        contract = _contract()
        relationships = contract["exposes"][0]["semantics"]["relationships"]
        relationships.append(copy.deepcopy(relationships[0]))
        errors, _ = validate_semantics_block(contract)
        assert errors == [
            "exposes[orders].semantics.relationships[1]: the name 'orders_to_customers' is "
            "relationships[0]'s too. A relationship is referred to by its name, so the names in "
            "one expose must differ."
        ]

    def test_composite_keys_and_self_joins_are_valid(self):
        contract = _contract()
        _relationship(contract).update(
            fromColumns=["customer_id", "region"], toColumns=["id", "region"]
        )
        contract["exposes"][0]["semantics"]["relationships"].append(
            {
                "name": "orders_to_orders",
                "to": "orders",
                "fromColumns": ["order_id"],
                "toColumns": ["order_id"],
                "cardinality": "one_to_one",
            }
        )
        assert validate_semantics_block(contract) == ([], [])

    def test_a_malformed_column_list_is_left_to_the_schema(self):
        contract = _contract()
        _relationship(contract).update(fromColumns=[], toColumns="id")
        assert validate_semantics_block(contract) == ([], [])


class TestEntityDrift:
    """dbt MetricFlow joins by entity name, so a relationship with no matching pair warns."""

    def test_no_entity_pair_warns_with_the_fix(self):
        contract = _contract()
        contract["exposes"][1]["semantics"]["entities"][0]["name"] = "client"
        errors, warnings = validate_semantics_block(contract)
        assert errors == []
        assert warnings == [
            "exposes[orders].semantics.relationships[0]: no entity pair matches this "
            "relationship, and the dbt MetricFlow export joins by entity name, so it will not "
            "follow the relationship. Declare an entity with expr 'customer_id' on exposes[orders] "
            "(type foreign) and one of the same name with expr 'id' on exposes[customers] (type "
            "primary or unique)."
        ]

    def test_a_target_entity_on_another_column_does_not_match(self):
        contract = _contract()
        contract["exposes"][1]["semantics"]["entities"][0]["expr"] = "region"
        assert len(validate_semantics_block(contract)[1]) == 1

    def test_a_foreign_target_entity_does_not_match(self):
        contract = _contract()
        contract["exposes"][1]["semantics"]["entities"][0]["type"] = "foreign"
        assert len(validate_semantics_block(contract)[1]) == 1

    @pytest.mark.parametrize("target_type", ["primary", "unique"])
    def test_a_key_entity_of_the_same_name_matches(self, target_type):
        contract = _contract()
        contract["exposes"][1]["semantics"]["entities"][0]["type"] = target_type
        assert validate_semantics_block(contract)[1] == []

    def test_a_natural_target_counts_only_beside_a_validity_window(self):
        """The dbt export drops a natural entity without a window, so it joins nothing."""
        contract = _contract()
        target = contract["exposes"][1]
        target["semantics"]["entities"][0]["type"] = "natural"
        assert len(validate_semantics_block(contract)[1]) == 1
        target["contract"]["schema"] += [
            {"name": "valid_from", "type": "timestamp"},
            {"name": "valid_to", "type": "timestamp"},
        ]
        target["semantics"]["dimensions"] = _scd()["exposes"][0]["semantics"]["dimensions"][1:]
        assert validate_semantics_block(contract) == ([], [])

    def test_a_primary_target_beside_a_validity_window_does_not_count(self):
        """dbt rejects a primary entity beside a window, so the export drops it."""
        contract = _contract()
        target = contract["exposes"][1]
        target["semantics"]["entities"].append(
            {"name": "customer_version", "type": "natural", "expr": "region"}
        )
        target["semantics"]["dimensions"] = _scd()["exposes"][0]["semantics"]["dimensions"][1:]
        errors, warnings = validate_semantics_block(contract)
        assert errors == []
        assert len(warnings) == 1 and "(type natural)" in warnings[0]

    def test_an_entity_without_expr_keys_on_its_name(self):
        contract = _contract()
        contract["exposes"][1]["semantics"]["entities"] = [{"name": "id", "type": "primary"}]
        contract["exposes"][0]["semantics"]["entities"][1] = {
            "name": "id",
            "type": "foreign",
            "expr": "customer_id",
        }
        assert validate_semantics_block(contract)[1] == []

    def test_composite_keys_and_broken_references_are_not_cross_checked(self):
        contract = _contract()
        contract["exposes"][1]["semantics"]["entities"] = []
        _relationship(contract).update(
            fromColumns=["customer_id", "region"], toColumns=["id", "region"]
        )
        assert validate_semantics_block(contract) == ([], [])
        _relationship(contract)["to"] = "missing"
        errors, warnings = validate_semantics_block(contract)
        assert len(errors) == 1 and warnings == []


class TestValidityWindow:
    def test_a_well_formed_window_is_clean(self):
        assert validate_semantics_block(_scd()) == ([], [])

    def test_it_requires_a_natural_entity(self):
        contract = _scd(entities=[{"name": "customer", "type": "primary", "expr": "customer_id"}])
        assert validate_semantics_block(contract)[0] == [
            "exposes[customer_history].semantics: valid_from, valid_to declare a validity window, "
            "but the model declares no natural entity. The window separates the versions of one "
            "natural key: declare it, e.g. {name: customer, type: natural, expr: customer_id}."
        ]

    def test_only_one_natural_entity(self):
        contract = _scd()
        contract["exposes"][0]["semantics"]["entities"].append(
            {"name": "segment_key", "type": "natural", "expr": "segment"}
        )
        errors, _ = validate_semantics_block(contract)
        assert errors == [
            "exposes[customer_history].semantics: a validity window separates the versions of one "
            "natural key, but the model declares 2 natural entities (customer, segment_key). Keep "
            "one."
        ]

    def test_a_start_without_an_end(self):
        contract = _scd()
        del contract["exposes"][0]["semantics"]["dimensions"][2]["typeParams"]["validityParams"]
        errors, _ = validate_semantics_block(contract)
        assert errors == [
            "exposes[customer_history].semantics: a validity window is exactly two time "
            "dimensions, one with validityParams.isStart and the other with "
            "validityParams.isEnd; this model has valid_from (isStart)."
        ]

    @pytest.mark.parametrize(
        "flags, described",
        [
            ({"isStart": True}, "valid_to (isStart)"),
            ({"isStart": True, "isEnd": True}, "valid_to (isStart and isEnd)"),
            ({}, "valid_to (neither isStart nor isEnd)"),
            ({"isEnd": False}, "valid_to (neither isStart nor isEnd)"),
        ],
    )
    def test_bounds_that_do_not_form_one_window(self, flags, described):
        contract = _scd()
        contract["exposes"][0]["semantics"]["dimensions"][2]["typeParams"]["validityParams"] = flags
        errors, _ = validate_semantics_block(contract)
        assert len(errors) == 1 and described in errors[0], errors

    def test_a_bound_on_a_categorical_dimension(self):
        contract = _scd()
        dimension = contract["exposes"][0]["semantics"]["dimensions"][2]
        dimension["type"] = "categorical"
        errors, _ = validate_semantics_block(contract)
        assert errors == [
            "exposes[customer_history].semantics.dimensions[2].typeParams.validityParams: "
            "dimension 'valid_to' is 'categorical', but a validity window is bounded by times. "
            "Make it type: time, or remove validityParams."
        ]

    def test_validity_window_returns_the_bounds_and_the_key(self):
        semantics = _scd()["exposes"][0]["semantics"]
        assert validity_window(semantics) == ("valid_from", "valid_to", "customer")

    def test_a_malformed_entities_list_does_not_hide_the_other_findings(self):
        """The schema refuses ``entities: 5``; the lint must still report the rest."""
        contract = _scd(entities=5)
        contract["exposes"][0]["semantics"]["measures"] = [
            {"name": "versions", "agg": "sum", "expr": "SUM(segment)"}
        ]
        errors, _ = validate_semantics_block(contract)
        assert any("itself an aggregate" in e for e in errors), errors

    def test_validity_window_is_none_for_a_malformed_or_absent_window(self):
        broken = _scd(entities=[])["exposes"][0]["semantics"]
        assert validity_window(broken) is None
        assert validity_window({"dimensions": [{"name": "d", "type": "time"}]}) is None


class TestVersionForRelationshipKeys:
    """Producers move a contract to the version that holds these fields only when it uses them."""

    def test_a_076_contract_with_a_relationship_needs_077(self):
        contract = _contract()
        contract["fluidVersion"] = "0.7.6"
        assert version_for_relationship_keys(contract) == RELATIONSHIPS_VERSION == "0.7.7"

    def test_a_076_contract_with_a_validity_window_needs_077(self):
        contract = _scd()
        contract["fluidVersion"] = "0.7.6"
        assert version_for_relationship_keys(contract) == "0.7.7"

    def test_nothing_changes_without_them_or_on_a_newer_version(self):
        contract = _contract()
        contract["fluidVersion"] = "0.7.6"
        del contract["exposes"][0]["semantics"]["relationships"]
        assert version_for_relationship_keys(contract) is None
        for version in ("0.7.7", "0.7.10", "0.8.0"):
            assert version_for_relationship_keys({**_contract(), "fluidVersion": version}) is None
