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

"""Schema pins for the optional fields 0.7.6 added.

Each one closes a hole where a valid contract could not say something its
own exporters already read:

* ``metadata.team`` — ``metadata.owner`` holds one team name and one mailbox
  and carries no role, so the exported ODCS/ODPS team block was always empty;
* the root ``description`` as an object — as a string it could only ever
  export as ``description.purpose``, leaving ``usage`` and ``limitations``
  with no home a contract could declare;
* ``consumes[].contractId`` — the canonical-port builder already prefers an
  explicit upstream contract id, but ``consumes[]`` was closed;
* ``exposes[].contract.id`` — every ODCS id was derived as
  ``<product id>.<exposeId>``, so a rename silently broke every consumer
  reference, and a catalog with an id format of its own could not be served;
* ``binding.location.host`` plus ``binding.serialization`` — the ODCS server
  mapper already reads ``host``, and a Kafka binding declaring only
  ``format: kafka_topic`` tells a consumer nothing about whether the values
  are Avro or JSON.
* ``exposes[].contract.schema[].quality`` — the ODCS exporter has always
  replayed a column's quality array verbatim and the importer has always read
  it back, but ``$defs.column`` is closed and did not declare it, so a column
  written the way the exporter supports was refused by ``fluid validate``.

The field names are Bitol's (ODCS v3.1.0 / ODPS v1.1.0) so import and export
are a pass-through rather than a translation.

All of them are optional and version-gated: a contract that sets none of them
validates exactly as it does today, and a 0.7.5 contract cannot reach them at
all. 0.7.6 opened as a preview and is stable since #752 promoted it, so the
default version an untagged contract gets now carries every one of them.
"""

from __future__ import annotations

import copy
import logging
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.cli.validate import _run_contract_rules
from fluid_build.schema_manager import FluidSchemaManager, ValidationResult

pytestmark = pytest.mark.unit


def _base_contract(version: str = "0.7.6") -> Dict[str, Any]:
    """A minimal valid contract that sets none of the new fields."""
    return {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": "gold.sales.orders",
        "name": "Orders",
        "description": "Orders, one row per order line.",
        "metadata": {
            "layer": "Gold",
            "owner": {"team": "sales-platform", "email": "sales-platform@example.com"},
        },
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"database": "SALES", "schema": "CDP", "table": "ORDERS"},
                },
                "contract": {"schema": [{"name": "ID", "type": "integer", "required": True}]},
            }
        ],
    }


def _kafka_expose() -> Dict[str, Any]:
    return {
        "exposeId": "order_events",
        "kind": "stream",
        "binding": {
            "platform": "kafka",
            "format": "kafka_topic",
            "location": {"topic": "sales.orders.v1"},
        },
        "contract": {"schema": [{"name": "ID", "type": "integer"}]},
    }


@pytest.fixture(scope="module")
def manager() -> FluidSchemaManager:
    return FluidSchemaManager()


def _messages(result: ValidationResult) -> List[str]:
    return [getattr(e, "message", str(e)) for e in result.errors]


def _validate(manager: FluidSchemaManager, contract: Dict[str, Any], version: str):
    return manager.validate_contract(contract, schema_version=version, offline_only=True)


def _assert_valid(manager, contract, version="0.7.6"):
    result = _validate(manager, contract, version)
    assert result.is_valid, f"expected valid against {version}, got: {_messages(result)}"


def _assert_invalid(manager, contract, version="0.7.6") -> ValidationResult:
    result = _validate(manager, contract, version)
    assert not result.is_valid, f"expected INVALID against {version}, but it validated"
    return result


# ---------------------------------------------------------------------------
# Absent: the five fields change nothing for a contract that sets none
# ---------------------------------------------------------------------------


class TestAbsent:
    def test_base_contract_sets_none_of_them_and_validates(self, manager):
        _assert_valid(manager, _base_contract())

    def test_a_075_contract_is_unaffected(self, manager):
        _assert_valid(manager, _base_contract(version="0.7.5"), version="0.7.5")


# ---------------------------------------------------------------------------
# Present: each addition validates
# ---------------------------------------------------------------------------


class TestPresent:
    def test_metadata_team(self, manager):
        contract = _base_contract()
        contract["metadata"]["team"] = {
            "name": "Sales Platform",
            "members": [
                {
                    "username": "ada",
                    "name": "Ada Lovelace",
                    "role": "owner",
                    "email": "ada@example.com",
                },
                {"username": "grace", "role": "steward"},
            ],
        }
        _assert_valid(manager, contract)

    def test_metadata_team_name_only(self, manager):
        contract = _base_contract()
        contract["metadata"]["team"] = {"name": "Sales Platform"}
        _assert_valid(manager, contract)

    def test_description_object(self, manager):
        contract = _base_contract()
        contract["description"] = {
            "purpose": "Order lines for revenue reporting.",
            "usage": "Join to dim_customer on customer_id.",
            "limitations": "Not a financial system of record; excludes cancelled orders.",
        }
        _assert_valid(manager, contract)

    def test_description_object_partial(self, manager):
        contract = _base_contract()
        contract["description"] = {"purpose": "Order lines."}
        _assert_valid(manager, contract)

    def test_description_as_a_string_still_validates(self, manager):
        # The legacy form keeps its meaning: it becomes `purpose` on export.
        contract = _base_contract()
        contract["description"] = "Orders, one row per order line."
        _assert_valid(manager, contract)

    def test_consumes_contract_id(self, manager):
        contract = _base_contract()
        contract["consumes"] = [
            {
                "productId": "silver.crm.customers_v2",
                "exposeId": "customers",
                "contractId": "urn:acme:contract:crm-customers:3",
            }
        ]
        _assert_valid(manager, contract)

    def test_expose_contract_id(self, manager):
        contract = _base_contract()
        contract["exposes"][0]["contract"]["id"] = "550e8400-e29b-41d4-a716-446655440000"
        _assert_valid(manager, contract)

    def test_binding_location_host_and_serialization(self, manager):
        contract = _base_contract()
        expose = _kafka_expose()
        expose["binding"]["location"]["host"] = "pkc-abc12.eu-central-1.aws.confluent.cloud:9092"
        expose["binding"]["serialization"] = {"value": "avro-confluent", "key": "json"}
        contract["exposes"].append(expose)
        _assert_valid(manager, contract)

    def test_serialization_value_only(self, manager):
        contract = _base_contract()
        expose = _kafka_expose()
        expose["binding"]["serialization"] = {"value": "protobuf"}
        contract["exposes"].append(expose)
        _assert_valid(manager, contract)

    @pytest.mark.parametrize(
        "serialization",
        [
            {"value": "json"},
            {"value": "avro"},
            {"value": "protobuf"},
            {"value": "xml"},
            {"value": "avro-confluent"},
            {"value": "json-confluent"},
        ],
        ids=["json", "avro", "protobuf", "xml", "avro-confluent", "json-confluent"],
    )
    def test_every_serialization_value_in_the_enum_is_accepted(self, manager, serialization):
        contract = _base_contract()
        expose = _kafka_expose()
        expose["binding"]["serialization"] = serialization
        contract["exposes"].append(expose)
        _assert_valid(manager, contract)


# ---------------------------------------------------------------------------
# Version gating: 0.7.5 cannot reach any of them; the stable default can
# ---------------------------------------------------------------------------


class TestVersionGated:
    def test_076_is_stable_and_no_preview_is_the_default(self, manager):
        # This pinned the preview gate while 0.7.6 was behind it. #752 promoted
        # 0.7.6, so the gate now reads the other way: the version an untagged
        # contract is checked against takes every field this file is about.
        # Asked of the default rather than of "0.7.6", so a later promotion,
        # which only ever adds, does not have to come back here;
        # test_schema_manager.py pins the default itself.
        assert "0.7.6" not in FluidSchemaManager.PREVIEW_VERSIONS
        default = FluidSchemaManager.latest_bundled_version()
        contract = _with_column_quality(_ODCS_RULE)
        contract["fluidVersion"] = default
        contract["metadata"]["team"] = {"name": "Sales Platform"}
        contract["description"] = {"purpose": "Order lines."}
        contract["consumes"] = [
            {
                "productId": "silver.crm.customers_v2",
                "exposeId": "customers",
                "contractId": "urn:acme:contract:crm-customers:3",
            }
        ]
        contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:orders:1"
        expose = _kafka_expose()
        expose["binding"]["location"]["host"] = "broker:9092"
        expose["binding"]["serialization"] = {"value": "avro"}
        contract["exposes"].append(expose)
        _assert_valid(manager, contract, version=default)
        # The gate itself, version-agnostic (the invariant #431 wrote): whatever
        # is in PREVIEW_VERSIONS stays opt-in, as 0.7.6 did until it graduated.
        for preview in FluidSchemaManager.PREVIEW_VERSIONS:
            assert default != preview

    def test_metadata_team_rejected_on_075(self, manager):
        contract = _base_contract(version="0.7.5")
        contract["metadata"]["team"] = {"name": "Sales Platform"}
        _assert_invalid(manager, contract, version="0.7.5")

    def test_description_object_rejected_on_075(self, manager):
        contract = _base_contract(version="0.7.5")
        contract["description"] = {"purpose": "Order lines."}
        _assert_invalid(manager, contract, version="0.7.5")

    def test_consumes_contract_id_rejected_on_075(self, manager):
        contract = _base_contract(version="0.7.5")
        contract["consumes"] = [
            {
                "productId": "silver.crm.customers_v2",
                "exposeId": "customers",
                "contractId": "urn:acme:contract:crm-customers:3",
            }
        ]
        _assert_invalid(manager, contract, version="0.7.5")

    def test_expose_contract_id_rejected_on_075(self, manager):
        contract = _base_contract(version="0.7.5")
        contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:orders:1"
        _assert_invalid(manager, contract, version="0.7.5")

    def test_location_host_rejected_on_075(self, manager):
        contract = _base_contract(version="0.7.5")
        expose = _kafka_expose()
        expose["binding"]["location"]["host"] = "broker:9092"
        contract["exposes"].append(expose)
        _assert_invalid(manager, contract, version="0.7.5")

    def test_serialization_rejected_on_075(self, manager):
        contract = _base_contract(version="0.7.5")
        expose = _kafka_expose()
        expose["binding"]["serialization"] = {"value": "avro"}
        contract["exposes"].append(expose)
        _assert_invalid(manager, contract, version="0.7.5")


# ---------------------------------------------------------------------------
# Rejected shapes — every new object is additionalProperties: false
# ---------------------------------------------------------------------------


class TestRejectedShapes:
    def test_team_member_without_username(self, manager):
        # Bitol's own required list for TeamMember is ["username"].
        contract = _base_contract()
        contract["metadata"]["team"] = {"members": [{"name": "Ada", "role": "owner"}]}
        _assert_invalid(manager, contract)

    def test_unknown_key_in_metadata_team(self, manager):
        contract = _base_contract()
        contract["metadata"]["team"] = {"name": "Sales Platform", "headcount": 4}
        _assert_invalid(manager, contract)

    def test_unknown_key_in_team_member(self, manager):
        contract = _base_contract()
        contract["metadata"]["team"] = {"members": [{"username": "ada", "github": "ada"}]}
        _assert_invalid(manager, contract)

    def test_unknown_key_in_description_object(self, manager):
        contract = _base_contract()
        contract["description"] = {"purpose": "Order lines.", "summary": "Orders."}
        _assert_invalid(manager, contract)

    def test_unknown_key_in_serialization(self, manager):
        contract = _base_contract()
        expose = _kafka_expose()
        expose["binding"]["serialization"] = {"value": "avro", "registry": "https://sr.example"}
        contract["exposes"].append(expose)
        _assert_invalid(manager, contract)

    def test_serialization_value_outside_the_enum_names_the_allowed_set(self, manager):
        contract = _base_contract()
        expose = _kafka_expose()
        expose["binding"]["serialization"] = {"value": "msgpack"}
        contract["exposes"].append(expose)
        result = _assert_invalid(manager, contract)
        joined = " ".join(_messages(result))
        for allowed in ("json", "avro", "protobuf", "xml", "avro-confluent", "json-confluent"):
            assert allowed in joined, f"the error does not name {allowed!r}: {joined}"


# ---------------------------------------------------------------------------
# One contract id per expose — the code check JSON Schema cannot express
# ---------------------------------------------------------------------------

_DUPLICATE_MESSAGE = (
    "contract id 'urn:acme:contract:orders:1' is used by exposes 'orders' and 'orders_api'"
    " — two output ports under one contract id publish two different schemas under a single"
    " identity, so a consumer resolving it gets one of the two arbitrarily and the other port's"
    " schema is lost; give each expose its own contract.id"
)


def _two_expose_contract(first_id: str, second_id: str) -> Dict[str, Any]:
    contract = _base_contract()
    contract["exposes"][0]["contract"]["id"] = first_id
    second = copy.deepcopy(contract["exposes"][0])
    second["exposeId"] = "orders_api"
    second["contract"]["id"] = second_id
    contract["exposes"].append(second)
    return contract


def _rule_errors(contract: Dict[str, Any]) -> List[str]:
    """Run the post-JSON-Schema contract rules the way ``fluid validate`` does."""
    result = ValidationResult(is_valid=True)
    args = SimpleNamespace(verbose=False, quiet=True, strict=False, offline=True)
    _run_contract_rules(contract, None, result, args, logging.getLogger("test"))
    return [str(e) for e in result.errors]


class TestDuplicateContractId:
    def test_two_exposes_sharing_a_contract_id_are_rejected(self, manager):
        contract = _two_expose_contract("urn:acme:contract:orders:1", "urn:acme:contract:orders:1")
        # The duplicate is schema-valid: uniqueItems compares whole array
        # items, so JSON Schema cannot see it.
        _assert_valid(manager, contract)
        assert _DUPLICATE_MESSAGE in _rule_errors(contract)

    def test_two_exposes_with_distinct_contract_ids_are_accepted(self, manager):
        contract = _two_expose_contract(
            "urn:acme:contract:orders:1", "urn:acme:contract:orders-api:1"
        )
        _assert_valid(manager, contract)
        assert _rule_errors(contract) == []

    def test_no_contract_id_at_all_is_accepted(self):
        assert _rule_errors(_base_contract()) == []

    def test_fluid_validate_offline_reaches_the_check(self, tmp_path, capsys):
        # The rule has to fire on the path the operator actually runs, not
        # only when the helper is called directly.
        from fluid_build.cli import build_parser
        from fluid_build.cli.validate import run

        contract_path = tmp_path / "contract.fluid.yaml"
        contract_path.write_text(
            yaml.safe_dump(
                _two_expose_contract("urn:acme:contract:orders:1", "urn:acme:contract:orders:1"),
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        args = build_parser().parse_args(["validate", str(contract_path), "--offline"])
        rc = run(args, logging.getLogger("test"))
        captured = capsys.readouterr()

        assert rc == 1
        assert _DUPLICATE_MESSAGE in captured.out + captured.err


# ---------------------------------------------------------------------------
# ``column.quality`` — the sixth field
# ---------------------------------------------------------------------------


_ODCS_RULE = {
    "id": "ID-nullValues",
    "type": "library",
    "metric": "nullValues",
    "mustBe": 0,
    "dimension": "completeness",
    "severity": "error",
}


def _with_column_quality(rule: Dict[str, Any]) -> Dict[str, Any]:
    contract = _base_contract()
    contract["exposes"][0]["contract"]["schema"][0]["quality"] = [copy.deepcopy(rule)]
    return contract


class TestColumnQuality:
    def test_a_rule_in_odcs_shape_validates(self, manager):
        _assert_valid(manager, _with_column_quality(_ODCS_RULE))

    def test_a_bare_text_rule_validates(self, manager):
        # ODCS ``$defs.DataQuality`` requires nothing but a non-empty object,
        # and the FLUID copy deliberately re-declares no required key.
        _assert_valid(manager, _with_column_quality({"type": "text", "description": "Hourly."}))

    def test_rejected_on_075(self, manager):
        contract = _with_column_quality(_ODCS_RULE)
        contract["fluidVersion"] = "0.7.5"
        result = _assert_invalid(manager, contract, version="0.7.5")
        assert any("quality" in m for m in _messages(result)), _messages(result)

    def test_an_empty_rule_object_is_rejected(self, manager):
        # ``minProperties: 1``: a rule that says nothing is not a rule, and a
        # DQ engine handed one has no check to run.
        _assert_invalid(manager, _with_column_quality({}))

    def test_a_metric_outside_the_odcs_five_is_rejected(self, manager):
        result = _assert_invalid(
            manager, _with_column_quality({"type": "library", "metric": "freshness"})
        )
        assert any("nullValues" in m for m in _messages(result)), _messages(result)

    def test_a_rule_id_outside_the_stable_id_pattern_is_rejected(self, manager):
        # ODCS ``$defs.StableId``. An id with a dot or a space is what a DQ
        # engine keys its results on, so it has to be the spelling ODCS allows.
        _assert_invalid(manager, _with_column_quality({**_ODCS_RULE, "id": "ID.nullValues"}))


# ---------------------------------------------------------------------------
# ``exposes[].contract.id`` is deliberately unconstrained
# ---------------------------------------------------------------------------


class TestContractIdShape:
    """Why the importer has to tolerate an id FLUID's root cannot spell.

    The alternative to teaching the ODCS importer about such an id was to
    constrain this field with the root's own ``$defs.identifier`` pattern, which
    would have made ``fluid validate`` reject exactly the ids the field exists
    to carry — the catalog-supplied URN or UUID that cannot be derived as
    ``<product id>.<exposeId>``. These pin that choice: the field takes any
    non-empty string, and the round-trip is the ODCS importer's problem (see
    tests/providers/test_pinned_id_portability.py).
    """

    def test_a_catalog_urn_validates(self, manager):
        contract = _base_contract()
        contract["exposes"][0]["contract"]["id"] = "urn:acme:contract:orders:v1"
        _assert_valid(manager, contract)

    def test_the_field_declares_no_pattern(self, manager):
        field = manager.get_schema("0.7.6", offline_only=True)["$defs"]["exposeContract"][
            "properties"
        ]["id"]
        assert "pattern" not in field
        assert field["minLength"] == 1

    def test_an_empty_contract_id_is_rejected(self, manager):
        # ``minLength: 1``: an empty string would publish a contract with no
        # id at all while reading as a pin, which the derived-id fallback
        # would then not supply.
        contract = _base_contract()
        contract["exposes"][0]["contract"]["id"] = ""
        _assert_invalid(manager, contract)
