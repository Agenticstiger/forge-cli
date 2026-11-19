# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A Snowflake decimal is a precision and scale a NUMBER holds, or it is refused.

The emitter types its columns through the dbt engine's table, which passed a decimal's
arguments through as declared: ``decimal(1;drop table x)`` went into the module as
``NUMBER(1;drop table x)``, the column type of the table's CREATE TABLE, and
``decimal(40,20)`` as a NUMBER Snowflake rejects at apply (precision 1 to 38, scale 0 to
37). ``fluid validate`` and the emitter now refuse both, with one message, and a
decimal a NUMBER holds is written as before. The contract schema's type pattern already
refuses the first at validate; ``fluid generate iac`` reads a contract without it.
"""

from __future__ import annotations

import pytest

from fluid_build.iac import get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.governance_validation import validate_governance
from fluid_build.iac.providers.snowflake import _sf_type, validate_snowflake_column_types

from .test_iac_column_type_gate import _validate

pytestmark = [pytest.mark.unit, pytest.mark.provider, pytest.mark.snowflake]

_LOC = {"database": "ANALYTICS", "schema": "SALES", "table": "ORDERS"}


def _contract(*types, platform="snowflake", fmt="snowflake_table"):
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "sales.orders",
        "name": "Orders",
        "description": "decimal arguments",
        "domain": "sales",
        "metadata": {"layer": "Bronze", "owner": {"team": "data"}},
        "exposes": [
            {
                "exposeId": "orders",
                "version": "1.0.0",
                "kind": "table",
                "binding": {"platform": platform, "format": fmt, "location": dict(_LOC)},
                "contract": {"schema": [{"name": f"c{i}", "type": t} for i, t in enumerate(types)]},
            }
        ],
    }


def test_validate_and_emit_refuse_it_with_one_message():
    contract = _contract("decimal(18,4)", "decimal(1;drop table x)", "decimal(40,20)")
    errors, warnings = validate_snowflake_column_types(contract)
    with pytest.raises(UnsupportedBindingError) as caught:
        get_iac_plugin("snowflake").emit(contract)
    refusal = caught.value
    assert refusal.kind == "column-type" and warnings == []
    assert errors == [f"{refusal} {' '.join(refusal.remediation)}"]
    assert (
        "'c1' is declared 'decimal(1;drop table x)', whose arguments are not a precision"
        in errors[0]
    )
    assert (
        "'c2' is declared 'decimal(40,20)', whose precision and scale a Snowflake NUMBER "
        "cannot hold" in errors[0]
    )
    assert "'c0'" not in errors[0]


def test_fluid_validate_runs_the_gate():
    errors, _warnings = validate_governance(_contract("decimal(38,38)"))
    assert [e for e in errors if "Snowflake cannot hold its columns as declared" in e]


def test_fluid_validate_exits_1_for_a_precision_the_schema_lets_through(tmp_path, capsys):
    assert _validate(tmp_path, _contract("decimal(40,20)")) == 1
    out = capsys.readouterr()
    assert "a Snowflake NUMBER cannot hold" in out.out + out.err


@pytest.mark.parametrize(
    "declared, written",
    [
        ("decimal(18,4)", "NUMBER(18,4)"),
        ("decimal(38,37)", "NUMBER(38,37)"),
        ("number(12, 2)", "NUMBER(12,2)"),
        ("decimal(10)", "NUMBER(10)"),
        ("decimal", "NUMBER(38,0)"),
        ("money", "NUMBER(19,4)"),
    ],
)
def test_a_decimal_a_number_holds_is_written_as_before(declared, written):
    resources = get_iac_plugin("snowflake").emit(_contract(declared))
    [table] = resources["snowflake_table"].values()
    assert table["column"][0]["type"] == written == _sf_type(declared)


def test_a_type_that_is_no_decimal_on_snowflake_still_drops_its_arguments():
    """``bignumeric`` is text on Snowflake and ``money`` a fixed NUMBER(19,4): neither
    carries the declared arguments, so neither is judged by them."""
    contract = _contract("bignumeric(40,20)", "bignumeric(38,10)", "money(abc)")
    assert validate_snowflake_column_types(contract) == ([], [])
    [table] = get_iac_plugin("snowflake").emit(contract)["snowflake_table"].values()
    assert [c["type"] for c in table["column"]] == ["VARCHAR", "VARCHAR", "NUMBER(19,4)"]


@pytest.mark.parametrize(
    "contract",
    [
        _contract("decimal(1;drop table x)", fmt="snowflake_view"),
        _contract("decimal(40,20)", platform="gcp", fmt="bigquery_table"),
    ],
    ids=["a-view", "another-platform"],
)
def test_a_binding_with_no_snowflake_table_has_no_column_types_to_gate(contract):
    assert validate_snowflake_column_types(contract) == ([], [])
