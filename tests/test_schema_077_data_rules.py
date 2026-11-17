# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Schema pins for the rules about valid data the 0.7.7 preview adds (RFC-data-rules.md).

* ``exposes[].contract.invariants[]`` (``$defs.invariant``): one form each (check; derive with
  expr; derive with aggregate; dependency), an optional ``when``, and one ODCS share operator
  with ``unit``.
* ``exposes[].contract.schema[]``: ``identifierFormat``, ``placeholders``, ``sharedAttribute``.
* ``exposes[].semantics.relationships[].sharedColumns``.

All optional and preview-gated: a contract without them validates as before, a 0.7.6 contract
that uses one is told to declare 0.7.7, and every touched object keeps
``additionalProperties: false``. The lint behind the schema is ``tests/data_rules``.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List

import pytest

from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit


def _expose(expose_id: str, schema: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "snowflake",
            "format": "snowflake_table",
            "location": {"database": "BILLING", "schema": "CDP", "table": expose_id.upper()},
        },
        "contract": {"schema": schema, **extra.pop("contract", {})},
        **extra,
    }


def _contract(version: str = "0.7.7") -> Dict[str, Any]:
    return {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": "gold.billing.invoices",
        "name": "Invoices",
        "metadata": {"layer": "Gold", "owner": {"team": "billing-platform"}},
        "exposes": [
            _expose(
                "invoices",
                [
                    {"name": "invoice_id", "type": "string", "required": True},
                    {"name": "contract_id", "type": "string"},
                    {"name": "net", "type": "decimal(12,2)"},
                    {"name": "gross", "type": "decimal(12,2)"},
                    {"name": "status", "type": "string"},
                    {
                        "name": "tower_id",
                        "type": "string",
                        "identifierFormat": [
                            {"prefixes": ["MSC", "TWR"], "mask": "99999"},
                            {"mask": "A9-99AA"},
                        ],
                        "placeholders": {
                            "values": ["XXXXX"],
                            "prefixes": ["TBD_"],
                            "mustBeLessOrEqualTo": 1,
                            "unit": "percent",
                        },
                        "sharedAttribute": "tower",
                    },
                ],
                contract={
                    "invariants": [
                        {"id": "gross_from_net", "derive": "gross", "expr": "ROUND(net * 1.19, 2)"},
                        {
                            "id": "net_positive",
                            "check": "net > 0",
                            "when": "status = 'active'",
                            "severity": "warning",
                            "mustBeBetween": [1.5, 2.5],
                            "unit": "percent",
                        },
                        {
                            "id": "status_by_contract",
                            "dependency": {"from": ["contract_id"], "to": ["status"]},
                            "mustBe": 0,
                        },
                    ]
                },
                semantics={
                    "relationships": [
                        {
                            "name": "contract",
                            "to": "contracts",
                            "fromColumns": ["contract_id"],
                            "toColumns": ["contract_id"],
                            "cardinality": "many_to_one",
                            "sharedColumns": ["status"],
                        }
                    ]
                },
            ),
            _expose(
                "contracts",
                [
                    {"name": "contract_id", "type": "string", "required": True},
                    {"name": "status", "type": "string"},
                    {"name": "lines_total", "type": "decimal(12,2)"},
                ],
                contract={
                    "invariants": [
                        {
                            "id": "lines_total_from_invoices",
                            "derive": "lines_total",
                            "aggregate": {
                                "function": "sum",
                                "column": "net",
                                "from": "invoices",
                                "via": "contract",
                                "filter": "status = 'active'",
                                "ifNone": 0,
                            },
                        }
                    ]
                },
            ),
        ],
    }


def _without_new_fields(version: str) -> Dict[str, Any]:
    contract = _contract(version)
    for expose in contract["exposes"]:
        expose["contract"].pop("invariants", None)
        for column in expose["contract"]["schema"]:
            for key in ("identifierFormat", "placeholders", "sharedAttribute"):
                column.pop(key, None)
        expose.pop("semantics", None)
    return contract


@pytest.fixture(scope="module")
def manager() -> FluidSchemaManager:
    return FluidSchemaManager()


def _validate(manager: FluidSchemaManager, contract: Dict[str, Any], version: str):
    return manager.validate_contract(contract, schema_version=version, offline_only=True)


def _messages(result) -> List[str]:
    return list(result.errors)


def _invariant(contract: Dict[str, Any], index: int = 0) -> Dict[str, Any]:
    return contract["exposes"][0]["contract"]["invariants"][index]


class TestTheShape:
    def test_every_new_field_validates_in_077(self, manager):
        result = _validate(manager, _contract(), "0.7.7")
        assert result.is_valid, _messages(result)

    @pytest.mark.parametrize(
        "where, key",
        [
            ("invariant", "language"),
            ("aggregate", "groupBy"),
            ("dependency", "when"),
            ("format", "regex"),
            ("placeholders", "pattern"),
            ("relationship", "sharedWith"),
        ],
    )
    def test_every_touched_object_keeps_additional_properties_false(self, manager, where, key):
        contract = _contract()
        expose = contract["exposes"][0]
        target = {
            "invariant": _invariant(contract),
            "aggregate": contract["exposes"][1]["contract"]["invariants"][0]["aggregate"],
            "dependency": _invariant(contract, 2)["dependency"],
            "format": expose["contract"]["schema"][5]["identifierFormat"][0],
            "placeholders": expose["contract"]["schema"][5]["placeholders"],
            "relationship": expose["semantics"]["relationships"][0],
        }[where]
        target[key] = "x"
        assert not _validate(manager, contract, "0.7.7").is_valid

    def test_the_definitions_carry_the_preview_marker(self, manager):
        defs = manager._bundled_schemas["0.7.7"]["$defs"]
        for name in ("invariant", "invariantAggregate", "invariantDependency", "fluidSqlText"):
            assert defs[name]["description"].startswith("NEW in v0.7.7 (preview)"), name
        column = defs["column"]["properties"]
        for key in ("identifierFormat", "placeholders", "sharedAttribute"):
            assert column[key]["description"].startswith("NEW in v0.7.7 (preview)"), key
        shared = defs["semanticRelationship"]["properties"]["sharedColumns"]
        assert shared["description"].startswith("NEW in v0.7.7 (preview)")
        contract = defs["exposeContract"]["properties"]["invariants"]
        assert contract["description"].startswith("NEW in v0.7.7 (preview)")
        assert defs["column"]["additionalProperties"] is False
        assert defs["invariant"]["additionalProperties"] is False

    def test_the_schema_examples_validate(self, manager):
        import jsonschema

        schema = manager._bundled_schemas["0.7.7"]
        defs = schema["$defs"]
        for name in ("invariant",):
            for example in defs[name]["examples"]:
                jsonschema.validate(example, {"$ref": f"#/$defs/{name}", "$defs": defs})


class TestOneForm:
    @pytest.mark.parametrize(
        "entry, fragment",
        [
            ({"id": "a"}, "no form"),
            ({"id": "a", "check": "net > 0", "derive": "gross", "expr": "1"}, "together"),
            ({"id": "a", "derive": "gross"}, "derive needs expr"),
            (
                {"id": "a", "derive": "gross", "expr": "1", "aggregate": {}},
                "not both",
            ),
            ({"id": "a", "expr": "1"}, "go with derive"),
            ({"id": "a", "check": "net > 0", "expr": "1"}, "go with derive"),
            (
                {"id": "a", "dependency": {"from": ["net"], "to": ["gross"]}, "when": "net > 0"},
                "takes no when",
            ),
        ],
    )
    def test_a_bad_form_says_what_is_wrong(self, manager, entry, fragment):
        contract = _contract()
        contract["exposes"][0]["contract"]["invariants"] = [entry]
        result = _validate(manager, contract, "0.7.7")
        assert not result.is_valid
        joined = " | ".join(_messages(result))
        assert fragment in joined, joined
        assert "{" not in joined  # the entry itself is never printed

    def test_a_type_error_inside_a_form_names_its_key(self, manager):
        contract = _contract()
        contract["exposes"][0]["contract"]["invariants"] = [{"id": "a", "check": 123}]
        joined = " | ".join(_messages(_validate(manager, contract, "0.7.7")))
        assert "check" in joined and "is not of type 'string'" in joined

    def test_ids_follow_the_identifier_grammar(self, manager):
        contract = _contract()
        _invariant(contract)["id"] = "1-bad"
        assert not _validate(manager, contract, "0.7.7").is_valid

    def test_share_values_are_not_negative(self, manager):
        contract = _contract()
        _invariant(contract, 1)["mustBeBetween"] = [-1, 2]
        assert not _validate(manager, contract, "0.7.7").is_valid

    def test_mask_is_at_most_64_characters(self, manager):
        contract = _contract()
        contract["exposes"][0]["contract"]["schema"][5]["identifierFormat"] = [{"mask": "9" * 65}]
        assert not _validate(manager, contract, "0.7.7").is_valid


class TestPreviewGate:
    def test_076_contract_is_told_to_declare_077(self, manager):
        result = _validate(manager, _contract("0.7.6"), "0.7.6")
        joined = " | ".join(_messages(result))
        assert (
            "'invariants' (rules about valid data) is a field of the 0.7.7 preview schema" in joined
        )
        assert "'identifierFormat' (the column's identifier formats)" in joined
        assert "'sharedAttribute' (an attribute shared across exposes)" in joined

    def test_a_typo_beside_a_preview_field_keeps_jsonschemas_message(self, manager):
        contract = _contract("0.7.6")
        contract["exposes"][0]["contract"]["schema"][5]["identifierFormatt"] = []
        joined = " | ".join(_messages(_validate(manager, contract, "0.7.6")))
        assert "identifierFormatt" in joined

    def test_contracts_without_them_still_validate(self, manager):
        for version in ("0.7.6", "0.7.7"):
            result = _validate(manager, _without_new_fields(version), version)
            assert result.is_valid, (version, _messages(result))

    def test_077_is_still_a_preview_and_not_the_default(self):
        assert "0.7.7" in FluidSchemaManager.PREVIEW_VERSIONS
        assert FluidSchemaManager.latest_bundled_version() != "0.7.7"

    def test_076_is_untouched(self, manager):
        defs = manager._bundled_schemas["0.7.6"]["$defs"]
        assert "invariant" not in defs
        assert "identifierFormat" not in defs["column"]["properties"]
        assert "invariants" not in defs["exposeContract"]["properties"]

    def test_the_hint_does_not_mutate_anything(self, manager):
        contract = _contract("0.7.6")
        before = copy.deepcopy(contract)
        _validate(manager, contract, "0.7.6")
        assert contract == before
