# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A declared type's arguments carry over only within its family, and only when well formed.

The dbt engine's table is also the Snowflake and Databricks IaC emitters' (and BigQuery's
for a type its emitter refuses), so an argument passed through where the warehouse
rejects it failed a dbt build and an apply alike: ``varchar(max)`` was ``VARCHAR(max)``
on Snowflake, and a ``tinyint(1)`` display width became ``NUMBER(1)``, a column too
narrow for the integers it was declared to hold. A length keeps one integer of at least
1 (``MAX`` on Redshift, which has it), a time at most 9 fractional-second digits, and a
decimal what it declares, so an emitter that checks decimals can still refuse them.
"""

from __future__ import annotations

import pytest

from fluid_build.engines.dbt import _types
from fluid_build.iac.providers import snowflake

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize(
    "declared, adapter, expected",
    [
        ("varchar(max)", "snowflake", "varchar"),
        ("nvarchar(MAX)", "duckdb", "varchar"),
        ("varchar(max)", "redshift", "varchar(max)"),
        ("binary(max)", "snowflake", "binary"),
        ("varchar(0)", "snowflake", "varchar"),
        ("string(1,2)", "redshift", "varchar"),
        ("tinyint(1)", "snowflake", "number"),
        ("int(3)", "snowflake", "number"),
        ("bigint(20)", "snowflake", "number"),
        ("varchar2(20)", "duckdb", "varchar(20)"),
        ("nvarchar2(20)", "snowflake", "varchar(20)"),
        ("nchar(1000)", "redshift", "varchar(1000)"),
        ("bytea(10)", "redshift", "varchar(10)"),
        ("binary(16)", "snowflake", "binary(16)"),
        ("varchar(25)", "snowflake", "varchar(25)"),
        ("timestamp(3)", "snowflake", "timestamp_ntz(3)"),
        ("timestamp(12)", "snowflake", "timestamp_ntz"),
        ("time(9)", "snowflake", "time(9)"),
        ("number(12, 2)", "snowflake", "number(12,2)"),
        ("decimal(18,4)", "databricks", "decimal(18,4)"),
        ("decimal(1,x)", "databricks", "decimal(1,x)"),
        ("bignumeric(40,20)", "snowflake", "varchar"),
        ("enum('a','b')", "snowflake", "varchar"),
    ],
)
def test_arguments_carry_over_only_within_their_family(declared, adapter, expected):
    assert _types.sql_type(declared, adapter) == expected


def test_a_length_too_long_for_int_is_not_parsed():
    huge = "9" * 5000
    assert _types.sql_type(f"varchar({huge})", "snowflake") == f"varchar({huge})"


@pytest.mark.parametrize(
    "declared, dbt, iac",
    [
        ("json", "variant", "VARIANT"),
        ("jsonb", "variant", "VARIANT"),
        ("money", "number(19,4)", "NUMBER(19,4)"),
    ],
)
def test_snowflake_holds_any_json_value_and_the_cents_of_money(declared, dbt, iac):
    """OBJECT holds only a key-value map, and a bare NUMBER is NUMBER(38,0)."""
    assert _types.sql_type(declared, "snowflake") == dbt
    assert snowflake._sf_type(declared) == iac


def _build_contract(*types: str):
    schema = [{"name": f"c{i}", "type": t} for i, t in enumerate(types)]
    build = {"engine": "dbt", "execution": {"runtime": {"platform": "snowflake"}}}
    contract = {"exposes": [{"exposeId": "e", "contract": {"schema": schema}}], "builds": [build]}
    return contract, build


def test_an_integer_column_gets_no_precision_warning_on_snowflake():
    contract, build = _build_contract("int64", "int32", "int16", "bigint", "integer", "number")
    warnings = _types.precision_warnings(contract, build)
    assert len(warnings) == 1 and warnings[0].startswith("e.c5: type 'number'")
