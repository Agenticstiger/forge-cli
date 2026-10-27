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

"""Kafka / Confluent physicalType export — the Avro width contract.

Kafka had no physical-type table, so ``fluid_to_physical`` fell back to the
ODCS *logicalType*. FLUID ``bigint`` maps to logicalType ``integer``, so a
Kafka expose exported ``physicalType: integer`` for a 64-bit column. Every
tool that derives an Avro schema from an ODCS contract reads ``integer`` as
Avro ``int`` — 32-bit — while a producer such as Flink BIGINT writes a 64-bit
long. The registered subject and the bytes on the topic then disagree, and
consumers truncate silently. These tests pin the table that fixes it.

The expected Avro names below are a *second copy* of the mapping, written out
by hand on purpose: a future edit to ``_PHYSICAL_BY_PROVIDER`` has to be made
here too, which is what makes the change visible in review.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.providers.odcs import OdcsProvider
from fluid_build.providers.odcs.mappers.types import (
    _AVRO_DECIMAL,
    _FLUID_TYPE_TO_ODCS_LOGICAL,
    _PHYSICAL_BY_PROVIDER,
    fluid_to_physical,
    physical_type_custom_properties,
)

_KAFKA_TABLE_KEY = ("kafka", "confluent")

# Avro 1.12 primitive + complex type names (specification §"Primitive Types"
# and §"Complex Types"). ``fixed`` and ``union`` are unreachable from a flat
# FLUID column type, so they are listed for completeness only.
_AVRO_TYPE_NAMES = {
    "null",
    "boolean",
    "int",
    "long",
    "float",
    "double",
    "bytes",
    "string",
    "record",
    "enum",
    "array",
    "map",
    "fixed",
    "union",
}

# Spellings that are NOT Avro type names but are the tokens an ODCS→Avro
# consumer keys off to pick the right Avro type + logical type. See the comment
# block on the Kafka table in mappers/types.py for the per-token reasoning.
_ECOSYSTEM_TOKENS = {"decimal", "date", "time", "timestamp", "timestamp_ntz"}


# The whole table, FLUID type → expected physicalType.
_EXPECTED: Dict[str, str] = {
    # string family
    "string": "string",
    "text": "string",
    "varchar": "string",
    "varchar2": "string",
    "nvarchar": "string",
    "char": "string",
    "nchar": "string",
    "character": "string",
    "clob": "string",
    "uuid": "string",
    "uniqueidentifier": "string",
    "guid": "string",
    "enum": "string",
    # binary family → Avro bytes
    "binary": "bytes",
    "varbinary": "bytes",
    "bytes": "bytes",
    "blob": "bytes",
    "bytea": "bytes",
    "raw": "bytes",
    "hll": "bytes",
    # interval + geospatial serialize as text
    "interval": "string",
    "geography": "string",
    "geometry": "string",
    "geom": "string",
    "point": "string",
    # 32-bit integers
    "int": "int",
    "integer": "int",
    "int2": "int",
    "int4": "int",
    "int16": "int",
    "int32": "int",
    "tinyint": "int",
    "smallint": "int",
    "mediumint": "int",
    "serial": "int",
    "year": "int",
    # 64-bit integers — the defect this file exists for
    "int8": "long",
    "int64": "long",
    "bigint": "long",
    "long": "long",
    "longint": "long",
    "bigserial": "long",
    # floating point, by width
    "float": "float",
    "float4": "float",
    "float32": "float",
    "real": "float",
    "double": "double",
    "float8": "double",
    "float64": "double",
    # fixed point
    "decimal": "decimal",
    "dec": "decimal",
    "numeric": "decimal",
    "number": "decimal",
    "bignumeric": "decimal",
    "money": "decimal",
    # boolean
    "bool": "boolean",
    "boolean": "boolean",
    "bit": "boolean",
    # temporal
    "date": "date",
    "time": "time",
    "timestamp": "timestamp",
    "timestamptz": "timestamp",
    "timestamp_tz": "timestamp",
    "timestamp_ltz": "timestamp",
    "datetime": "timestamp_ntz",
    "datetime2": "timestamp_ntz",
    "smalldatetime": "timestamp_ntz",
    "timestamp_ntz": "timestamp_ntz",
    "timestampntz": "timestamp_ntz",
    # structured / semi-structured
    "json": "string",
    "jsonb": "string",
    "variant": "string",
    "super": "string",
    "object": "record",
    "struct": "record",
    "record": "record",
    "row": "record",
    "map": "map",
    "array": "array",
}


def _contract(platform: str, columns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A minimal valid FLUID contract with one expose on ``platform``."""
    return {
        "fluidVersion": "0.7.5",
        "kind": "DataContract",
        "id": "commerce.orders-product",
        "name": "orders-product",
        "metadata": {
            "version": "1.0.0",
            "name": "Orders Product",
            "description": "Orders stream",
            "status": "active",
            "domain": "commerce",
            "layer": "bronze",
        },
        "owner": {"team": "commerce-team", "email": "alice@acme.com"},
        "exposes": [
            {
                "id": "orders_stream",
                "exposeId": "orders_stream",
                "version": "1.0.0",
                "description": "Order events",
                "binding": {"platform": platform, "location": {"topic": "orders"}},
                "contract": {"schema": columns},
            }
        ],
    }


def _export(platform: str, columns: List[Dict[str, Any]]) -> Dict[str, Any]:
    return OdcsProvider().render(_contract(platform, columns), expose_id="orders_stream")


def _properties(odcs: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {p["name"]: p for p in odcs["schema"][0]["properties"]}


# ---------------------------------------------------------------------------
# The acceptance case
# ---------------------------------------------------------------------------


class TestKafkaBigintExportsAsAvroLong:
    """A Kafka expose with a bigint column must export ``physicalType: long``.

    Before the table existed this said ``integer``, which an ODCS→Avro
    consumer reads as a 32-bit Avro ``int``.
    """

    def test_bigint_is_long_not_integer(self) -> None:
        props = _properties(_export("kafka", [{"name": "order_id", "type": "bigint"}]))
        assert props["order_id"]["physicalType"] == "long"
        # The logicalType is unchanged — ODCS only has nine of them, and
        # ``integer`` is still the right one. The width lives in physicalType.
        assert props["order_id"]["logicalType"] == "integer"

    def test_a_32_bit_column_is_still_int(self) -> None:
        """The fix must not widen everything: a real ``int`` stays 32-bit."""
        props = _properties(_export("kafka", [{"name": "seq", "type": "int"}]))
        assert props["seq"]["physicalType"] == "int"

    def test_confluent_platform_gets_the_same_table(self) -> None:
        """``confluent`` is a FLUID binding.platform of its own and is Kafka."""
        props = _properties(_export("confluent", [{"name": "order_id", "type": "bigint"}]))
        assert props["order_id"]["physicalType"] == "long"


# ---------------------------------------------------------------------------
# The whole table, driven from the expectation above
# ---------------------------------------------------------------------------


class TestEveryFluidTypeHasAnAvroPhysicalType:
    @pytest.mark.parametrize("fluid_type,expected", sorted(_EXPECTED.items()))
    def test_type_exports_the_expected_avro_name(self, fluid_type: str, expected: str) -> None:
        assert fluid_to_physical(fluid_type, "kafka") == expected

    @pytest.mark.parametrize("fluid_type,expected", sorted(_EXPECTED.items()))
    def test_type_is_case_insensitive(self, fluid_type: str, expected: str) -> None:
        assert fluid_to_physical(fluid_type.upper(), "kafka") == expected

    def test_table_is_exhaustive_against_the_fluid_type_map(self) -> None:
        """Drift guard. A FLUID type missing from the Kafka table makes
        ``fluid_to_physical`` return None, which emits no ``physicalType`` at
        all — the contract silently stops stating the width."""
        table = _PHYSICAL_BY_PROVIDER[_KAFKA_TABLE_KEY]
        missing = sorted(set(_FLUID_TYPE_TO_ODCS_LOGICAL) - set(table))
        assert not missing, (
            f"FLUID types missing from the Kafka/Avro physical table: {missing} — add "
            "them to _PHYSICAL_BY_PROVIDER[('kafka', 'confluent')] in "
            "fluid_build/providers/odcs/mappers/types.py."
        )

    def test_expectation_covers_the_whole_table(self) -> None:
        """The hand-written ``_EXPECTED`` above must track the table exactly,
        so editing the table without editing this file fails the suite."""
        table = _PHYSICAL_BY_PROVIDER[_KAFKA_TABLE_KEY]
        assert _EXPECTED == table

    def test_every_physical_name_is_an_avro_type_or_a_documented_token(self) -> None:
        allowed = _AVRO_TYPE_NAMES | _ECOSYSTEM_TOKENS
        bad = {k: v for k, v in _PHYSICAL_BY_PROVIDER[_KAFKA_TABLE_KEY].items() if v not in allowed}
        assert not bad, f"physical names that are neither Avro types nor documented tokens: {bad}"

    def test_no_avro_name_carries_a_parameter_suffix(self) -> None:
        """Avro type names are not parameterized — there is no ``long(18)``."""
        for fluid_type in ("varchar(255)", "decimal(18,4)", "timestamp_ntz(9)", "char(3)"):
            physical = fluid_to_physical(fluid_type, "kafka")
            assert physical is not None and "(" not in physical, fluid_type


# ---------------------------------------------------------------------------
# Decimals: the one type whose physicalType cannot carry its own parameters
# ---------------------------------------------------------------------------


class TestKafkaDecimalCarriesPrecisionAndScale:
    def test_precision_and_scale_ride_in_custom_properties(self) -> None:
        props = _properties(_export("kafka", [{"name": "total", "type": "decimal(18,4)"}]))
        total = props["total"]
        assert total["physicalType"] == _AVRO_DECIMAL
        assert total["customProperties"] == [
            {"property": "precision", "value": 18},
            {"property": "scale", "value": 4},
        ]

    def test_precision_only_decimal_omits_scale(self) -> None:
        """Avro requires ``precision``; ``scale`` is optional and defaults to 0."""
        assert physical_type_custom_properties("decimal(18)", "kafka") == [
            {"property": "precision", "value": 18}
        ]

    def test_unparameterized_decimal_adds_nothing(self) -> None:
        props = _properties(_export("kafka", [{"name": "total", "type": "decimal"}]))
        assert props["total"]["physicalType"] == _AVRO_DECIMAL
        assert "customProperties" not in props["total"]

    @pytest.mark.parametrize("fluid_type", ["bigint", "string", "timestamp", "double"])
    def test_non_decimal_types_add_nothing(self, fluid_type: str) -> None:
        assert physical_type_custom_properties(fluid_type, "kafka") == []

    @pytest.mark.parametrize("provider", ["snowflake", "gcp", None])
    def test_only_the_avro_providers_use_custom_properties(self, provider: Optional[str]) -> None:
        """Snowflake and BigQuery put the parameters in the physical name
        itself (``DECIMAL(18,4)``), so they must not also get the pair."""
        assert physical_type_custom_properties("decimal(18,4)", provider) == []

    def test_the_derived_avro_decimal_is_a_valid_schema(self) -> None:
        """Replay the ODCS→Avro mapping datacontract-cli performs
        (``to_avro_type``: physicalType ``decimal`` → bytes + the ``decimal``
        logical type, precision/scale read from ``customProperties``) and parse
        the result with Apache Avro's own parser."""
        avro_schema = pytest.importorskip("avro.schema")
        props = _properties(_export("kafka", [{"name": "total", "type": "decimal(18,4)"}]))
        total = props["total"]
        assert total["physicalType"] == _AVRO_DECIMAL
        config = {cp["property"]: cp["value"] for cp in total["customProperties"]}
        field_type = {
            "type": "bytes",
            "logicalType": "decimal",
            "precision": config["precision"],
            "scale": config["scale"],
        }
        parsed = avro_schema.parse(
            json.dumps(
                {
                    "type": "record",
                    "name": "orders_stream",
                    "fields": [{"name": "total", "type": field_type}],
                }
            )
        )
        rendered = json.loads(str(parsed))["fields"][0]["type"]
        assert rendered["logicalType"] == "decimal"
        assert (rendered["precision"], rendered["scale"]) == (18, 4)

    def test_a_bigint_derives_a_64_bit_avro_long(self) -> None:
        """The same replay for the headline case: physicalType ``long`` is in
        datacontract-cli's ``["long", "bigint"]`` branch → Avro ``long``."""
        avro_schema = pytest.importorskip("avro.schema")
        props = _properties(_export("kafka", [{"name": "order_id", "type": "bigint"}]))
        assert props["order_id"]["physicalType"] in ("long", "bigint")
        parsed = avro_schema.parse(
            json.dumps(
                {
                    "type": "record",
                    "name": "orders_stream",
                    "fields": [{"name": "order_id", "type": "long"}],
                }
            )
        )
        assert json.loads(str(parsed))["fields"][0]["type"] == "long"


# ---------------------------------------------------------------------------
# No leakage onto providers that already had a table
# ---------------------------------------------------------------------------


class TestProvidersWithATableAreUnchanged:
    SNOWFLAKE = {
        "bigint": "NUMBER",
        "int": "NUMBER",
        "decimal(18,4)": "DECIMAL(18,4)",
        "double": "DOUBLE",
        "timestamp": "TIMESTAMP_NTZ",
        "date": "DATE",
        "binary": "BINARY",
        "varchar(255)": "VARCHAR(255)",
    }
    BIGQUERY = {
        "bigint": "INT64",
        "int": "INT64",
        "decimal(18,4)": "NUMERIC(18,4)",
        "double": "FLOAT64",
        "timestamp": "TIMESTAMP",
        "date": "DATE",
        "binary": "BYTES",
        "varchar(255)": "STRING(255)",
    }

    @pytest.mark.parametrize("fluid_type,expected", sorted(SNOWFLAKE.items()))
    def test_snowflake_physical_types(self, fluid_type: str, expected: str) -> None:
        assert fluid_to_physical(fluid_type, "snowflake") == expected

    @pytest.mark.parametrize("fluid_type,expected", sorted(BIGQUERY.items()))
    def test_bigquery_physical_types(self, fluid_type: str, expected: str) -> None:
        assert fluid_to_physical(fluid_type, "gcp") == expected

    def test_snowflake_export_adds_no_custom_properties(self) -> None:
        props = _properties(
            _export(
                "snowflake",
                [
                    {"name": "order_id", "type": "bigint"},
                    {"name": "total", "type": "decimal(18,4)"},
                ],
            )
        )
        assert props["order_id"]["physicalType"] == "NUMBER"
        assert props["total"]["physicalType"] == "DECIMAL(18,4)"
        assert "customProperties" not in props["total"]

    def test_a_provider_with_no_table_still_falls_back_to_the_logical_type(self) -> None:
        """Unchanged behaviour, pinned deliberately: giving every table-less
        provider the FLUID type instead would change aws/azure/databricks/
        postgres/local exports, which is a separate decision."""
        assert fluid_to_physical("bigint", "postgres") == "integer"
        assert fluid_to_physical("bigint", "aws") == "integer"


# ---------------------------------------------------------------------------
# The exported document is still a valid ODCS v3.1.0 contract
# ---------------------------------------------------------------------------


class TestExportedKafkaContractIsValidOdcs:
    def test_validates_against_the_vendored_odcs_schema(self) -> None:
        pytest.importorskip("jsonschema")
        from fluid_build.providers.odcs.validation import collect_errors, load_schema

        schema = load_schema()
        assert schema is not None, "vendored odcs-schema-v3.1.0.json is missing"
        columns = [
            {"name": name, "type": fluid_type, "description": f"{name} column"}
            for name, fluid_type in (
                ("order_id", "bigint"),
                ("seq", "int"),
                ("total", "decimal(18,4)"),
                ("rate", "double"),
                ("event_ts", "timestamp"),
                ("booked_on", "date"),
                ("payload", "binary"),
                ("attrs", "json"),
                ("is_paid", "boolean"),
            )
        ]
        odcs = _export("kafka", columns)
        errors = collect_errors(odcs, schema)
        assert not errors, f"exported Kafka ODCS is not schema-valid: {errors}"

    def test_the_vendored_schema_is_the_one_we_claim(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "fluid_build"
            / "providers"
            / "odcs"
            / "odcs-schema-v3.1.0.json"
        )
        assert schema_path.exists()

    def test_export_import_export_is_still_a_fixed_point(self) -> None:
        """The new ``customProperties`` must round-trip: on re-export the
        imported pass-through reproduces them, it does not synthesise a second
        copy."""
        odcs = _export("kafka", [{"name": "total", "type": "decimal(18,4)"}])
        result = OdcsProvider().roundtrip_check(odcs)
        assert result["equal"], (
            f"missing: {result['missing']}\n"
            f"extra  : {result['extra']}\n"
            f"changed: {result['changed']}"
        )
