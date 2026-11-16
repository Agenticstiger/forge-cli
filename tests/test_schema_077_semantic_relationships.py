# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Schema pins for the two semantics fields the 0.7.7 preview adds (RFC-semantics-tier1 §4, §5).

* ``exposes[].semantics.relationships[]`` (``$defs.semanticRelationship``): a named foreign key
  from the declaring expose (the many side) to an expose of the same contract, its key columns
  ordered and paired by position, so a composite key is one relationship. The shape is Apache
  Ossie's ``Relationship`` (``from_columns`` / ``to_columns``) plus the cardinality and join type
  Ossie leaves out.
* ``exposes[].semantics.dimensions[].typeParams.validityParams``: one bound of the validity window
  of a type II slowly changing dimension, field for field dbt's ``validity_params``.

Both are optional and preview-gated: a contract that sets neither validates as before, a 0.7.6
contract that sets one is told to declare 0.7.7, and untagged contracts never resolve to 0.7.7.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.schema_manager import FluidSchemaManager, ValidationResult

pytestmark = pytest.mark.unit

_RELATIONSHIP = {
    "name": "orders_to_customers",
    "to": "customers",
    "fromColumns": ["customer_id"],
    "toColumns": ["customer_id"],
    "cardinality": "many_to_one",
    "joinType": "left",
}


def _expose(expose_id: str, columns: List[str], semantics: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "snowflake",
            "format": "snowflake_table",
            "location": {"database": "SALES", "schema": "CDP", "table": expose_id.upper()},
        },
        "contract": {"schema": [{"name": c, "type": "string"} for c in columns]},
        "semantics": semantics,
    }


def _contract(version: str = "0.7.7") -> Dict[str, Any]:
    """Orders (with a relationship) and an SCD customer history (with a validity window)."""
    return {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": "gold.sales.orders",
        "name": "Orders",
        "metadata": {
            "layer": "Gold",
            "owner": {"team": "sales-platform", "email": "sales-platform@example.org"},
        },
        "exposes": [
            _expose(
                "orders",
                ["order_id", "customer_id", "region"],
                {
                    "name": "orders",
                    "entities": [
                        {"name": "order", "type": "primary", "expr": "order_id"},
                        {"name": "customer", "type": "foreign", "expr": "customer_id"},
                    ],
                    "relationships": [copy.deepcopy(_RELATIONSHIP)],
                },
            ),
            _expose(
                "customers",
                ["customer_id", "segment", "valid_from", "valid_to"],
                {
                    "name": "customers",
                    "entities": [{"name": "customer", "type": "natural", "expr": "customer_id"}],
                    "dimensions": [
                        {"name": "segment", "type": "categorical"},
                        {
                            "name": "valid_from",
                            "type": "time",
                            "typeParams": {
                                "timeGranularity": "day",
                                "validityParams": {"isStart": True},
                            },
                        },
                        {
                            "name": "valid_to",
                            "type": "time",
                            "typeParams": {
                                "timeGranularity": "day",
                                "validityParams": {"isEnd": True},
                            },
                        },
                    ],
                },
            ),
        ],
    }


def _without_new_fields(version: str) -> Dict[str, Any]:
    contract = _contract(version)
    del contract["exposes"][0]["semantics"]["relationships"]
    for dimension in contract["exposes"][1]["semantics"]["dimensions"]:
        dimension.get("typeParams", {}).pop("validityParams", None)
    contract["exposes"][1]["semantics"]["entities"][0]["type"] = "primary"
    return contract


@pytest.fixture(scope="module")
def manager() -> FluidSchemaManager:
    return FluidSchemaManager()


def _validate(manager: FluidSchemaManager, contract: Dict[str, Any], version: str):
    return manager.validate_contract(contract, schema_version=version, offline_only=True)


def _messages(result: ValidationResult) -> List[str]:
    return [getattr(e, "message", str(e)) for e in result.errors]


def _relationship(contract: Dict[str, Any]) -> Dict[str, Any]:
    return contract["exposes"][0]["semantics"]["relationships"][0]


class TestPresent:
    def test_relationship_and_validity_window_validate_on_077(self, manager):
        result = _validate(manager, _contract(), "0.7.7")
        assert result.is_valid, _messages(result)

    def test_composite_key_and_default_join_type(self, manager):
        contract = _contract()
        relationship = _relationship(contract)
        relationship.update(
            fromColumns=["customer_id", "region"], toColumns=["customer_id", "segment"]
        )
        del relationship["joinType"]
        assert _validate(manager, contract, "0.7.7").is_valid

    @pytest.mark.parametrize("cardinality", ["many_to_one", "one_to_one"])
    @pytest.mark.parametrize("join_type", ["left", "inner"])
    def test_every_enum_value_is_accepted(self, manager, cardinality, join_type):
        contract = _contract()
        _relationship(contract).update(cardinality=cardinality, joinType=join_type)
        assert _validate(manager, contract, "0.7.7").is_valid

    def test_the_schema_documents_the_ossie_shape(self):
        schema = FluidSchemaManager()._bundled_schemas["0.7.7"]
        relationship = schema["$defs"]["semanticRelationship"]
        assert relationship["required"] == ["name", "to", "fromColumns", "toColumns", "cardinality"]
        assert relationship["properties"]["to"]["$ref"] == "#/$defs/identifier"
        assert {"fromColumns", "toColumns"} <= set(relationship["properties"])
        assert relationship["properties"]["fromColumns"]["minItems"] == 1
        assert relationship["properties"]["joinType"]["default"] == "left"
        assert relationship["additionalProperties"] is False


class TestAbsent:
    def test_076_and_077_contracts_without_them_still_validate(self, manager):
        for version in ("0.7.6", "0.7.7"):
            result = _validate(manager, _without_new_fields(version), version)
            assert result.is_valid, (version, _messages(result))

    def test_077_is_still_a_preview_and_not_the_default(self):
        assert "0.7.7" in FluidSchemaManager.PREVIEW_VERSIONS
        assert FluidSchemaManager.latest_bundled_version() == "0.7.6"


class TestRejected:
    def test_a_076_contract_is_told_to_declare_077(self, manager):
        result = _validate(manager, _contract("0.7.6"), "0.7.6")
        messages = _messages(result)
        assert any(
            "'relationships' (named relationships between exposes) is a field of the 0.7.7 "
            "preview schema: set the contract's fluidVersion to 0.7.7" in m
            for m in messages
        ), messages
        assert any(
            "'validityParams' (the validity window of a slowly changing dimension) is a field of "
            "the 0.7.7 preview schema" in m
            for m in messages
        ), messages

    def test_a_typo_next_to_a_preview_field_keeps_the_plain_message(self, manager):
        contract = _contract("0.7.6")
        contract["exposes"][0]["semantics"]["relationshipz"] = []
        messages = _messages(_validate(manager, contract, "0.7.6"))
        assert any(
            "'relationships', 'relationshipz' were unexpected" in m for m in messages
        ), messages

    @pytest.mark.parametrize("field", ["name", "to", "fromColumns", "toColumns", "cardinality"])
    def test_each_required_field(self, manager, field):
        contract = _contract()
        del _relationship(contract)[field]
        result = _validate(manager, contract, "0.7.7")
        assert not result.is_valid
        assert any(f"'{field}' is a required property" in m for m in _messages(result))

    @pytest.mark.parametrize("field", ["fromColumns", "toColumns"])
    def test_empty_column_list(self, manager, field):
        contract = _contract()
        _relationship(contract)[field] = []
        assert not _validate(manager, contract, "0.7.7").is_valid

    @pytest.mark.parametrize(
        "patch",
        [
            {"cardinality": "one_to_many"},
            {"joinType": "outer"},
            {"to": "not a valid id"},
            {"onDelete": "cascade"},
        ],
    )
    def test_values_outside_the_shape(self, manager, patch):
        contract = _contract()
        _relationship(contract).update(patch)
        assert not _validate(manager, contract, "0.7.7").is_valid

    def test_unknown_key_in_validity_params(self, manager):
        contract = _contract()
        dimension = contract["exposes"][1]["semantics"]["dimensions"][1]
        dimension["typeParams"]["validityParams"]["inclusive"] = True
        assert not _validate(manager, contract, "0.7.7").is_valid


class TestFluidValidate:
    """The schema field and the lint reach the command an operator runs."""

    def _run(self, tmp_path, contract, capsys) -> tuple[int, str]:
        from fluid_build.cli import build_parser
        from fluid_build.cli.validate import run

        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
        args = build_parser().parse_args(["validate", str(path), "--offline"])
        rc = run(args, logging.getLogger("test"))
        captured = capsys.readouterr()
        return rc, captured.out + captured.err

    def test_a_valid_contract_passes(self, tmp_path, capsys):
        rc, output = self._run(tmp_path, _contract(), capsys)
        assert rc == 0, output

    def test_a_relationship_to_a_missing_expose_fails(self, tmp_path, capsys):
        contract = _contract()
        _relationship(contract)["to"] = "customer_profiles"
        rc, output = self._run(tmp_path, contract, capsys)
        assert rc == 1
        assert "'customer_profiles' is not an exposeId of this contract" in output
