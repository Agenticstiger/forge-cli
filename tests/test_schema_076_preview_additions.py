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

"""Schema pins for the five optional 0.7.6 preview fields.

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

The field names are Bitol's (ODCS v3.1.0 / ODPS v1.1.0) so import and export
are a pass-through rather than a translation.

All five are optional and 0.7.6 is preview-gated: a contract that sets none
of them validates exactly as it does today, and a 0.7.5 contract cannot
reach them at all.
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
# Preview gating: 0.7.5 cannot reach any of them
# ---------------------------------------------------------------------------


class TestPreviewGated:
    def test_076_is_the_preview_version(self):
        assert "0.7.6" in FluidSchemaManager.PREVIEW_VERSIONS
        assert FluidSchemaManager.latest_bundled_version() != "0.7.6"

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
    " — give each expose its own contract.id"
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
