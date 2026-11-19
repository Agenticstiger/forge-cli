# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A declared type's arguments carry over only within its family, and only when well formed.

The dbt engine's table is also the Snowflake and Databricks IaC emitters' (and BigQuery's
for a type its emitter refuses), so an argument passed through where the warehouse
rejects it failed a dbt build and an apply alike: ``varchar(max)`` was ``VARCHAR(max)``
on Snowflake, and a ``tinyint(1)`` display width became ``NUMBER(1)``, a column too
narrow for the integers it was declared to hold. A length keeps one integer of at least
1 (``MAX`` on Redshift, which has it), a time at most 9 fractional-second digits, and a
decimal a precision and scale in digits that its adapter holds. A decimal with any other
arguments was written into the SQL as declared (``decimal(1;drop table x)`` became
``NUMBER(1;drop table x)`` on Snowflake); the dbt engine and the emitters now refuse it.
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
        ("decimal(1,x)", "databricks", "decimal"),
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


@pytest.mark.parametrize(
    "declared, adapter, expected",
    [
        ("decimal(38,37)", "snowflake", "decimal(38,37)"),
        ("decimal(38,37)", "redshift", "decimal(38,37)"),
        ("decimal(38,38)", "databricks", "decimal(38,38)"),
        ("decimal(38,38)", "duckdb", "decimal(38,38)"),
        ("numeric(18, 4)", "redshift", "numeric(18,4)"),
        ("decimal(12)", "snowflake", "decimal(12)"),
        ("decimal(1000,2)", None, "numeric(1000,2)"),
        ("decimal(1;drop table x)", "snowflake", "decimal"),
        ("decimal(1;drop table x)", "duckdb", "decimal"),
        ("decimal(1;drop table x)", None, "numeric"),
        ("decimal(1;drop table x)", "bigquery", "numeric"),
        ("number(18,4,2)", "snowflake", "number"),
        ("decimal(40,20)", "snowflake", "decimal"),
        ("decimal(38,38)", "snowflake", "decimal"),
        ("decimal(38,38)", "redshift", "decimal"),
        ("decimal(39,2)", "databricks", "decimal"),
        ("decimal(10,12)", "duckdb", "decimal"),
        ("decimal(0)", "snowflake", "decimal"),
    ],
)
def test_a_decimal_keeps_only_a_precision_and_scale_its_adapter_holds(declared, adapter, expected):
    assert _types.sql_type(declared, adapter) == expected


@pytest.mark.parametrize(
    "declared, adapter, why",
    [
        ("decimal(1;drop table x)", "snowflake", "arguments are not a precision and an optional"),
        ("number(18,4,2)", "snowflake", "arguments are not a precision and an optional"),
        ("decimal(10,12)", "duckdb", "precision and scale are not a decimal's"),
        ("decimal(0)", "redshift", "precision and scale are not a decimal's"),
        ("decimal(40,20)", "snowflake", "precision and scale a Snowflake NUMBER cannot hold"),
        ("decimal(38,38)", "redshift", "precision and scale a Redshift DECIMAL cannot hold"),
        ("decimal(39)", "databricks", "precision and scale a Databricks DECIMAL cannot hold"),
        ("decimal(39)", "duckdb", "precision and scale a DuckDB DECIMAL cannot hold"),
        ("decimal(" + "9" * 5000 + ")", "snowflake", "a Snowflake NUMBER cannot hold"),
    ],
)
def test_a_decimal_its_adapter_cannot_take_is_named_with_the_reason(declared, adapter, why):
    problem = _types.decimal_argument_problem(declared, adapter)
    assert problem is not None
    assert why in problem[0] and problem[1].startswith("Declare decimal(p")


@pytest.mark.parametrize(
    "declared, adapter",
    [
        ("decimal(18,4)", "snowflake"),
        ("decimal", "snowflake"),
        ("decimal()", "snowflake"),
        ("varchar(1;x)", "snowflake"),
        ("decimal(1000,2)", None),
        ("decimal(60,2)", "bigquery"),
        (None, "snowflake"),
        # Their type takes no precision on the adapter, so the arguments are dropped.
        ("bignumeric(40,20)", "snowflake"),
        ("bignumeric(76,38)", "duckdb"),
        ("money(abc)", "snowflake"),
        ("money(1;x)", "bigquery"),
    ],
)
def test_a_held_decimal_a_bare_one_or_another_type_is_no_problem(declared, adapter):
    assert _types.decimal_argument_problem(declared, adapter) is None


def test_the_bigquery_fallback_drops_what_the_emitter_refuses():
    """``decimal(2,5)`` was ``numeric(2,5)`` in dbt's BigQuery SQL, where the emitter
    refused it: dbt now checks the emitter's rule and gives its reason."""
    from fluid_build.iac.providers.gcp import _bq_outcome

    outcome = _bq_outcome("decimal(2,5)")
    assert outcome.target is None
    assert _types.sql_type("decimal(2,5)", "bigquery") == "numeric"
    assert _types.decimal_argument_problem("decimal(2,5)", "bigquery") == (outcome.why, outcome.fix)


def test_dbt_refuses_a_column_type_it_would_write_into_a_skeleton_model():
    from fluid_build.engines.dbt import DbtEngine

    contract, build = _build_contract("decimal(18,4)", "decimal(1;drop table x)", "decimal(40,20)")
    with pytest.raises(ValueError) as caught:
        DbtEngine().generate(contract, build)
    message = str(caught.value)
    assert message.startswith("dbt cannot type 2 columns of expose 'e' on snowflake as declared:")
    assert "column 'c1' is declared 'decimal(1;drop table x)', whose arguments" in message
    assert (
        "column 'c2' is declared 'decimal(40,20)', whose precision and scale a Snowflake" in message
    )
    assert "'c0'" not in message


def test_a_build_that_writes_no_column_types_is_not_refused_until_it_does():
    """An embedded-logic build's own SQL sets the column types; a model contract writes
    them into schema.yml, and only then does the declared decimal reach dbt."""
    from fluid_build.engines.dbt import DbtEngine

    contract, build = _build_contract("decimal(40,20)")
    build.update(pattern="embedded-logic", properties={"sql": "select 1 as c0"})
    assert "models/marts/main.sql" in DbtEngine().generate(contract, build)
    with pytest.raises(ValueError, match="dbt cannot type 1 column of expose 'e'"):
        DbtEngine().generate(contract, build, model_contracts=True)
