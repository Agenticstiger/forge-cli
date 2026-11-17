# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0
"""An ``enum`` level in the 0.7.7 preview may hold any character but a control character.

A level is a quoted string with its own quote doubled, as SQL writes a string literal
(``enum('Dr.', '>10000', 'O''Brien', 'a, b')``), or an unquoted word list as before
(``enum(IE, United Kingdom)``). Each quoted level must be followed by a comma or the
closing parenthesis, so a level with an unescaped quote is refused, and so the pattern
never backtracks through the ways to read a run of quotes.

The wider text is for enum levels only: every other type keeps the argument characters
it had, and no emitter writes an enum's levels into a SQL type. 0.7.6 is unchanged.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

import fluid_build
from fluid_build.engines.dbt._types import split_parameterized as dbt_split
from fluid_build.engines.dbt._types import sql_type
from fluid_build.iac.providers.aws import _hive_outcome
from fluid_build.iac.providers.gcp import _bq_outcome
from fluid_build.providers.odcs.mappers.types import (
    fluid_to_logical,
    fluid_to_physical,
    logical_type_options,
)
from fluid_build.providers.odcs.mappers.types import split_parameterized as odcs_split
from fluid_build.schema_manager import FluidSchemaManager

pytestmark = pytest.mark.unit

_SCHEMAS = Path(fluid_build.__file__).parent / "schemas"


def _type_pattern(version: str) -> "re.Pattern[str]":
    schema = json.loads((_SCHEMAS / f"fluid-schema-{version}.json").read_text(encoding="utf-8"))
    return re.compile(schema["$defs"]["column"]["properties"]["type"]["anyOf"][1]["pattern"])


def _contract(column_type: str, version: str = "0.7.7") -> Dict[str, Any]:
    return {
        "fluidVersion": version,
        "kind": "DataProduct",
        "id": "gold.crm.customers",
        "name": "Customers",
        "metadata": {
            "layer": "Gold",
            "owner": {"team": "crm-platform", "email": "crm-platform@example.org"},
        },
        "exposes": [
            {
                "exposeId": "customers",
                "kind": "table",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {"database": "CRM", "schema": "GOLD", "table": "CUSTOMERS"},
                },
                "contract": {
                    "schema": [
                        {"name": "customer_id", "type": "string"},
                        {"name": "salutation", "type": column_type},
                    ]
                },
            }
        ],
    }


@pytest.fixture(scope="module")
def manager() -> FluidSchemaManager:
    return FluidSchemaManager()


ACCEPTED = [
    "enum('Dr.','>10000')",
    "enum('Dr.', 'Cir.', 'Mr. & Mrs.')",
    "enum('O''Brien')",
    "enum('a, b', 'c)d', '(e)')",
    'enum("say ""hi""", "it\'s")',
    "enum('a\"b')",
    "enum('€', 'naïve', '日本')",
    "enum('')",
    "ENUM ( 'x' , \"y\" )",
    "enum(IE, United Kingdom)",
    "enum(-1, 0, 1)",
    "enum()",
    "enum",
]

REFUSED = [
    "enum('it's')",
    "enum('a)",
    "enum('a'b')",
    "enum(a'b')",
    "enum('a' 'b')",
    "enum(a,,b)",
    "enum('a',)",
    "enum(,a)",
    "enum(a.b)",
    "enum('a'))",
    "enum(('a'))",
    "enum('a\tb')",
    "enum('a\nb')",
    "enum('\x85')",
]


def _refusals(manager: FluidSchemaManager, column_type: str) -> List[Any]:
    """The schema's errors for a contract whose second column is ``column_type``."""
    result = manager.validate_contract(_contract(column_type), offline_only=True)
    return [] if result.is_valid else (list(result.errors) or ["refused, with no message"])


def test_the_fixture_is_valid(manager):
    assert _refusals(manager, "string") == []


@pytest.mark.parametrize("column_type", ACCEPTED)
def test_a_quoted_level_may_hold_any_printable_character(manager, column_type):
    assert _refusals(manager, column_type) == []


@pytest.mark.parametrize("column_type", REFUSED)
def test_an_unescaped_quote_or_a_malformed_list_is_refused(manager, column_type):
    assert _refusals(manager, column_type)


@pytest.mark.parametrize(
    "column_type",
    ["decimal(18,4)", "varchar(255)", "number(12, 2)", "varchar('x')", "timestamp with time zone"],
)
def test_other_types_keep_their_arguments(manager, column_type):
    assert _refusals(manager, column_type) == []


@pytest.mark.parametrize("column_type", ["varchar('Dr.')", "decimal(1.5)", "varchar('a;b')"])
def test_any_character_is_for_enum_levels_only(manager, column_type):
    assert _refusals(manager, column_type)


def test_0_7_6_is_unchanged():
    stable = _type_pattern("0.7.6")
    assert stable.search("enum('a', 'b')")
    assert not stable.search("enum('Dr.')")


@pytest.mark.parametrize(
    "probe",
    [
        "enum(" + "'" * 20001 + "x",
        "enum(" + "'a' ," * 20000 + "x",
        "enum(" + "a ," * 20000 + "x",
        "enum(" + "a " * 20000 + ",x",
        "enum(" + " " * 20000 + "x",
    ],
)
def test_the_pattern_refuses_long_hostile_input_in_linear_time(probe):
    pattern = _type_pattern("0.7.7")
    started = time.perf_counter()
    assert not pattern.search(probe)
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    "adapter", [None, "bigquery", "databricks", "duckdb", "redshift", "snowflake"]
)
@pytest.mark.parametrize(
    "column_type", ["enum('a','b')", "enum('Dr.','>10000')", "enum('x'' ; --')"]
)
def test_dbt_never_writes_enum_levels_into_a_sql_type(adapter, column_type):
    assert sql_type(column_type, adapter) == sql_type("enum", adapter)


def test_dbt_keeps_the_arguments_of_other_types():
    assert sql_type("number(12, 2)", "snowflake") == "number(12,2)"
    assert sql_type("varchar(25)") == "varchar(25)"


@pytest.mark.parametrize("provider", ["snowflake", "bigquery", "aws", "databricks"])
def test_odcs_physical_type_of_an_enum_takes_no_parameters(provider):
    assert fluid_to_physical("enum('Dr.','>10000')", provider) == fluid_to_physical(
        "enum", provider
    )
    assert fluid_to_physical("enum('a)b', 'c')", provider) == fluid_to_physical("enum", provider)


def test_odcs_keeps_the_parameters_of_other_types():
    assert fluid_to_physical("decimal(18,4)", "snowflake") == "DECIMAL(18,4)"


def test_a_quoted_parenthesis_is_still_an_enum(caplog):
    with caplog.at_level(logging.WARNING):
        assert fluid_to_logical("enum('a)b')") == "string"
    assert "No ODCS logicalType mapping" not in caplog.text
    assert odcs_split("enum('a)b', 'c, d')") == ("enum", ("'a)b'", "'c, d'"))
    assert dbt_split("enum('a)b', 'c, d')") == ("enum", "'a)b','c, d'")


@pytest.mark.parametrize(
    "fluid_type, odcs, dbt",
    [
        ("decimal(18,4)", ("decimal", ("18", "4")), ("decimal", "18,4")),
        ("VARCHAR( 255 )", ("varchar", ("255",)), ("VARCHAR", "255")),
        ("number(12, 2)", ("number", ("12", "2")), ("number", "12,2")),
        (
            "timestamp with time zone",
            ("timestamp with time zone", ()),
            ("timestamp with time zone", None),
        ),
        ("varchar()", ("varchar", ()), ("varchar", None)),
        ("array<int>", ("array<int>", ()), ("array<int>", None)),
        (
            "map(string, array(int))",
            ("map(string, array(int))", ()),
            ("map(string, array(int))", None),
        ),
        ("decimal(10,2) extra", ("decimal(10,2) extra", ()), ("decimal(10,2) extra", None)),
    ],
)
def test_the_splitters_read_unquoted_types_as_before(fluid_type, odcs, dbt):
    assert odcs_split(fluid_type) == odcs
    assert dbt_split(fluid_type) == dbt


@pytest.mark.parametrize("outcome", [_hive_outcome, _bq_outcome])
def test_glue_and_bigquery_take_a_level_in_any_script(outcome):
    assert outcome("enum('Z\u00fcrich', 'caf\u00e9', '\u65e5\u672c')") == outcome("enum")


def test_an_enum_level_is_never_a_length():
    assert logical_type_options("enum(10)") == {}
    assert logical_type_options("varchar(10)") == {"maxLength": 10}


def test_an_unclosed_quote_in_another_type_reads_as_before():
    assert odcs_split("varchar(a'b)") == ("varchar", ("a'b",))
    assert dbt_split("varchar(a'b)") == ("varchar", "a'b")
    assert fluid_to_physical("varchar(a'b)", "snowflake") == "VARCHAR(a'b)"
