# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``validate_semantics_block``: the checks of the versioned members of schema 0.7.7.

RFC-versioned-relationships §2.4, the references a JSON Schema cannot check:

* ``validityParams.openEnd`` and ``chain`` sit on the ``isEnd`` dimension; ``openEnd`` is a
  literal of the end column's type; ``chain: dayBefore`` takes date columns.
* an entity's ``lifecycle`` sits on the natural entity of a model with a validity window, and
  its columns are declared and typed like the window.
* a relationship's ``asOf`` is a declared date or time column of the window's type, toward a
  target that declares a window and is joined on its natural key.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any, Dict, List

import jsonschema
import pytest

from fluid_build.forge_datamodel.semantics_builder import validate_semantics_block
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit

_SCHEMA = (
    Path(__file__).resolve().parents[2] / "fluid_build" / "schemas" / "fluid-schema-0.7.7.json"
)


def _expose(expose_id: str, columns: Dict[str, str], semantics: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "snowflake",
            "format": "snowflake_table",
            "location": {"database": "WEATHER", "schema": "CDP", "table": expose_id.upper()},
        },
        "contract": {"schema": [{"name": n, "type": t} for n, t in columns.items()]},
        "semantics": semantics,
    }


def _contract() -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "gold.weather.stations",
        "name": "Stations",
        "metadata": {
            "layer": "Gold",
            "owner": {"team": "weather-platform", "email": "weather-platform@example.org"},
        },
        "exposes": [
            _expose(
                "station_version",
                {
                    "station_id": "bigint",
                    "valid_from": "date",
                    "valid_to": "date",
                    "network_id": "bigint",
                    "sensor_model": "string",
                    "status": "string",
                    "commissioned_on": "date",
                    "is_current": "boolean",
                },
                {
                    "name": "station_version",
                    "entities": [
                        {
                            "name": "station",
                            "type": "natural",
                            "expr": "station_id",
                            "lifecycle": {
                                "activation": "commissioned_on",
                                "endMarker": {"column": "status", "value": "DECOMMISSIONED"},
                                "currentFlag": {"column": "is_current", "value": True},
                            },
                        }
                    ],
                    "dimensions": [
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
                                "validityParams": {
                                    "isEnd": True,
                                    "openEnd": None,
                                    "chain": "dayBefore",
                                },
                            },
                        },
                    ],
                },
            ),
            _expose(
                "reading",
                {
                    "reading_id": "bigint",
                    "station_id": "bigint",
                    "network_id": "bigint",
                    "sensor_model": "string",
                    "reading_date": "date",
                },
                {
                    "name": "reading",
                    "entities": [{"name": "station", "type": "foreign", "expr": "station_id"}],
                    "relationships": [
                        {
                            "name": "station",
                            "to": "station_version",
                            "fromColumns": ["station_id"],
                            "toColumns": ["station_id"],
                            "cardinality": "many_to_one",
                        },
                        {
                            "name": "station_on_date",
                            "to": "station_version",
                            "fromColumns": ["station_id"],
                            "toColumns": ["station_id"],
                            "cardinality": "many_to_one",
                            "asOf": "reading_date",
                        },
                    ],
                },
            ),
        ],
    }


def _version(contract: Dict[str, Any]) -> Dict[str, Any]:
    return contract["exposes"][0]


def _semantics(contract: Dict[str, Any]) -> Dict[str, Any]:
    return _version(contract)["semantics"]


def _end(contract: Dict[str, Any]) -> Dict[str, Any]:
    return _semantics(contract)["dimensions"][1]["typeParams"]["validityParams"]


def _lifecycle(contract: Dict[str, Any]) -> Dict[str, Any]:
    return _semantics(contract)["entities"][0]["lifecycle"]


def _as_of_relationship(contract: Dict[str, Any]) -> Dict[str, Any]:
    return contract["exposes"][1]["semantics"]["relationships"][1]


def _retype(contract: Dict[str, Any], expose: int, column: str, declared: str) -> None:
    for entry in contract["exposes"][expose]["contract"]["schema"]:
        if entry["name"] == column:
            entry["type"] = declared


def _errors(contract: Dict[str, Any]) -> List[str]:
    errors, _ = validate_semantics_block(contract)
    return errors


def _only(contract: Dict[str, Any], *needles: str) -> str:
    errors = _errors(contract)
    assert len(errors) == 1, errors
    for needle in needles:
        assert needle in errors[0], errors[0]
    return errors[0]


def _timestamp_window(contract: Dict[str, Any], declared: str) -> None:
    _retype(contract, 0, "valid_from", declared)
    _retype(contract, 0, "valid_to", declared)
    _retype(contract, 0, "commissioned_on", declared)
    _retype(contract, 1, "reading_date", declared)


class TestBase:
    def test_the_base_contract_has_no_findings(self):
        assert validate_semantics_block(_contract()) == ([], [])

    def test_without_the_new_members_the_findings_are_as_before(self):
        contract = _contract()
        del _end(contract)["openEnd"]
        del _end(contract)["chain"]
        del _semantics(contract)["entities"][0]["lifecycle"]
        del _as_of_relationship(contract)["asOf"]
        assert validate_semantics_block(contract) == ([], [])


class TestWindowMembers:
    @pytest.mark.parametrize("member,value", [("openEnd", "9999-12-31"), ("chain", "none")])
    def test_member_on_the_start_dimension(self, member, value):
        contract = _contract()
        _semantics(contract)["dimensions"][0]["typeParams"]["validityParams"][member] = value
        _only(
            contract,
            "exposes[station_version].semantics.dimensions[0].typeParams.validityParams." + member,
            "isEnd",
        )

    def test_open_end_accepts_a_date(self):
        contract = _contract()
        _end(contract)["openEnd"] = "9999-12-31"
        assert _errors(contract) == []

    @pytest.mark.parametrize("literal", ["9999-13-01", "31.12.9999", "9999-02-30"])
    def test_open_end_rejects_a_bad_date(self, literal):
        contract = _contract()
        _end(contract)["openEnd"] = literal
        _only(contract, "validityParams.openEnd", repr(literal), "date")

    def test_open_end_timestamp(self):
        contract = _contract()
        _timestamp_window(contract, "timestamp")
        _end(contract)["chain"] = "none"
        _end(contract)["openEnd"] = "9999-12-31T23:59:59"
        assert _errors(contract) == []
        _end(contract)["openEnd"] = "9999-12-31T23:59:59Z"
        _only(contract, "validityParams.openEnd", "timestamp")

    def test_open_end_timestamptz(self):
        contract = _contract()
        _timestamp_window(contract, "timestamp with time zone")
        _end(contract)["chain"] = "none"
        _end(contract)["openEnd"] = "9999-12-31T23:59:59Z"
        assert _errors(contract) == []
        _end(contract)["openEnd"] = "9999-12-31T23:59:59+02:00"
        assert _errors(contract) == []
        _end(contract)["openEnd"] = "9999-12-31T23:59:59"
        _only(contract, "validityParams.openEnd", "timestamptz")

    def test_open_end_with_no_column_type_to_match(self):
        contract = _contract()
        _retype(contract, 0, "valid_to", "string")
        _end(contract)["chain"] = "none"
        _end(contract)["openEnd"] = "9999-12-31"
        errors = _errors(contract)
        assert any("validityParams.openEnd" in e and "no column type" in e for e in errors), errors
        # undeclared end column too
        contract = _contract()
        _end(contract)["chain"] = "none"
        _end(contract)["openEnd"] = "9999-12-31"
        _semantics(contract)["dimensions"][1]["expr"] = "gone"
        errors = _errors(contract)
        assert any("openEnd" in e and "no column type" in e and "gone" in e for e in errors)

    def test_open_end_null_is_skipped(self):
        contract = _contract()
        _retype(contract, 0, "valid_to", "string")
        _end(contract)["chain"] = "none"
        errors = _errors(contract)
        assert not any("openEnd" in e for e in errors), errors

    @pytest.mark.parametrize("declared", ["timestamp", "timestamp with time zone"])
    def test_day_before_on_a_time_window(self, declared):
        contract = _contract()
        _timestamp_window(contract, declared)
        _only(contract, "validityParams.chain", "dayBefore", "date columns")

    def test_day_before_on_an_untyped_window(self):
        contract = _contract()
        _retype(contract, 0, "valid_from", "date")
        _retype(contract, 0, "valid_to", "timestamp")
        errors = _errors(contract)
        assert any("validityParams.chain" in e and "dayBefore" in e for e in errors), errors

    def test_other_chains_take_a_time_window(self):
        contract = _contract()
        _timestamp_window(contract, "timestamp")
        _end(contract)["chain"] = "nextStart"
        assert _errors(contract) == []


class TestLifecycle:
    def test_on_an_entity_that_is_not_natural(self):
        contract = _contract()
        entity = _semantics(contract)["entities"][0]
        entity["type"] = "primary"
        errors = _errors(contract)
        assert any("entities[0].lifecycle" in e and "natural" in e for e in errors), errors

    def test_in_a_model_without_validity_params(self):
        contract = _contract()
        for dimension in _semantics(contract)["dimensions"]:
            del dimension["typeParams"]["validityParams"]
        del _as_of_relationship(contract)["asOf"]
        _only(contract, "entities[0].lifecycle", "no validityParams")

    def test_a_malformed_window_adds_nothing(self):
        contract = _contract()
        del _semantics(contract)["dimensions"][0]["typeParams"]["validityParams"]
        errors = _errors(contract)
        assert not any("no validityParams" in e for e in errors), errors
        assert any("exactly two time dimensions" in e for e in errors), errors

    @pytest.mark.parametrize(
        "path,where",
        [
            (("activation",), "lifecycle.activation"),
            (("endMarker", "column"), "lifecycle.endMarker.column"),
            (("currentFlag", "column"), "lifecycle.currentFlag.column"),
        ],
    )
    def test_column_not_declared(self, path, where):
        contract = _contract()
        target = _lifecycle(contract)
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = "nope"
        _only(contract, where, "'nope' is not a column of exposes[station_version]")

    def test_column_did_you_mean(self):
        contract = _contract()
        _lifecycle(contract)["activation"] = "COMMISSIONED_ON"
        _only(contract, "lifecycle.activation", "did you mean 'commissioned_on'")

    def test_activation_of_another_type(self):
        contract = _contract()
        _retype(contract, 0, "commissioned_on", "timestamp")
        _only(contract, "lifecycle.activation", "timestamp", "date")

    @pytest.mark.parametrize(
        "member,value",
        [
            ("endMarker", 5),
            ("endMarker", True),
            ("currentFlag", "yes"),
        ],
    )
    def test_value_of_another_type(self, member, value):
        contract = _contract()
        _lifecycle(contract)[member]["value"] = value
        _only(contract, f"lifecycle.{member}.value", repr(value))

    def test_value_kinds_accepted(self):
        contract = _contract()
        _retype(contract, 0, "status", "int")
        _lifecycle(contract)["endMarker"]["value"] = 3
        assert _errors(contract) == []
        _lifecycle(contract)["endMarker"]["value"] = 2.5
        assert _errors(contract) == []

    def test_date_value(self):
        contract = _contract()
        _retype(contract, 0, "status", "date")
        _lifecycle(contract)["endMarker"]["value"] = "2024-02-30"
        _only(contract, "lifecycle.endMarker.value", "date")
        _lifecycle(contract)["endMarker"]["value"] = "2024-02-29"
        assert _errors(contract) == []


class TestAsOf:
    def test_target_without_a_window(self):
        contract = _contract()
        for dimension in _semantics(contract)["dimensions"]:
            del dimension["typeParams"]["validityParams"]
        del _semantics(contract)["entities"][0]["lifecycle"]
        _only(contract, "relationships[1].asOf", "no validity window")

    def test_target_with_a_malformed_window_adds_nothing(self):
        contract = _contract()
        del _semantics(contract)["dimensions"][0]["typeParams"]["validityParams"]
        errors = _errors(contract)
        assert not any("asOf" in e for e in errors), errors

    def test_to_columns_are_not_the_natural_key(self):
        contract = _contract()
        relationship = _as_of_relationship(contract)
        relationship["fromColumns"] = ["network_id"]
        relationship["toColumns"] = ["network_id"]
        _only(contract, "relationships[1].asOf", "toColumns", "'station_id'", "station")

    def test_column_not_declared(self):
        contract = _contract()
        _as_of_relationship(contract)["asOf"] = "READING_DATE"
        _only(
            contract,
            "relationships[1].asOf",
            "not a column of exposes[reading]",
            "did you mean 'reading_date'",
        )

    def test_column_not_a_date_or_time(self):
        contract = _contract()
        _retype(contract, 1, "reading_date", "string")
        _only(contract, "relationships[1].asOf", "string", "date, timestamp or timestamptz")

    def test_untyped_column(self):
        contract = _contract()
        _retype(contract, 1, "reading_date", "json")
        _only(contract, "relationships[1].asOf", "untyped")

    def test_a_date_never_meets_a_timestamp(self):
        contract = _contract()
        _retype(contract, 1, "reading_date", "timestamp")
        _only(contract, "relationships[1].asOf", "timestamp", "date", "never meets")

    def test_target_window_with_no_type(self):
        contract = _contract()
        _retype(contract, 0, "valid_to", "string")
        _end(contract)["chain"] = "none"
        _only(contract, "relationships[1].asOf", "one date or time type")

    def test_matching_timestamps(self):
        contract = _contract()
        _timestamp_window(contract, "timestamp")
        _end(contract)["chain"] = "none"
        assert _errors(contract) == []


class TestAsOfNames:
    def test_to_columns_are_compared_in_nfc(self):
        key = "st\u00e9"  # one code point
        contract = _contract()
        for expose in contract["exposes"]:
            for column in expose["contract"]["schema"]:
                if column["name"] == "station_id":
                    column["name"] = key
        for relationship in contract["exposes"][1]["semantics"]["relationships"]:
            relationship["fromColumns"] = relationship["toColumns"] = [key]
        contract["exposes"][1]["semantics"]["entities"][0]["expr"] = key
        # the natural entity names the column in NFD (e + combining acute)
        _semantics(contract)["entities"][0]["expr"] = unicodedata.normalize("NFD", key)
        assert _errors(contract) == []


class TestJsonSchema:
    @pytest.fixture(scope="class")
    def schema(self) -> Dict[str, Any]:
        return json.loads(_SCHEMA.read_text(encoding="utf-8"))

    @pytest.fixture(scope="class")
    def manager(self) -> FluidSchemaManager:
        return FluidSchemaManager()

    def _messages(self, manager: FluidSchemaManager, contract: Dict[str, Any]) -> List[str]:
        result = manager.validate_contract(contract, schema_version="0.7.7", offline_only=True)
        return [getattr(e, "message", str(e)) for e in result.errors]

    def test_the_schema_is_a_valid_schema(self, schema):
        jsonschema.Draft202012Validator.check_schema(schema)

    def test_the_base_contract_validates(self, manager):
        assert self._messages(manager, _contract()) == []

    def test_an_unknown_lifecycle_key_is_refused(self, manager):
        contract = _contract()
        _lifecycle(contract)["unknown"] = "x"
        messages = self._messages(manager, contract)
        assert any("unknown" in m for m in messages), messages

    def test_an_unknown_chain_is_refused(self, manager):
        contract = _contract()
        _end(contract)["chain"] = "sideways"
        messages = self._messages(manager, contract)
        assert any("sideways" in m for m in messages), messages

    def test_as_of_must_be_a_string(self, manager):
        contract = _contract()
        _as_of_relationship(contract)["asOf"] = 5
        assert self._messages(manager, contract)

    def test_a_076_contract_is_told_to_declare_077_for_lifecycle(self, manager):
        contract = _contract()
        contract["fluidVersion"] = "0.7.6"
        for dimension in _semantics(contract)["dimensions"]:
            dimension.get("typeParams", {}).pop("validityParams", None)
        del contract["exposes"][1]["semantics"]
        result = manager.validate_contract(contract, schema_version="0.7.6", offline_only=True)
        text = " ".join(getattr(e, "message", str(e)) for e in result.errors)
        assert "lifecycle" in text and "0.7.7" in text, text
