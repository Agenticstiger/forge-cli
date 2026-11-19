# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A BigQuery decimal carries the precision and scale its contract declares.

``decimal(18,4)`` was written as a bare NUMERIC, which is NUMERIC(38,9), while the dbt
engine wrote ``numeric(18,4)``: each dbt rebuild and each apply changed the column
back. It is now NUMERIC(18,4), or BIGNUMERIC(P,S) when a NUMERIC cannot hold the
range, with ``precision`` and ``scale`` as the strings the API returns. BigQuery
cannot add a precision to a live NUMERIC (a narrowing), and the provider plans it in
place, so each contract with a table carries one ``terraform_data`` marker, named by
the tables that declare a parameterised decimal: created in a state written before,
it plans their replacement once, which the data-loss gate refuses without
``--allow-data-loss`` (``test_iac_gcp_decimal_parameters_plan.py`` runs that against
the real provider).
"""

from __future__ import annotations

import copy
import json

import pytest

from fluid_build.iac import build_module, get_iac_plugin
from fluid_build.iac.providers import gcp
from fluid_build.iac.providers.gcp import _bq_decimal, _bq_field, _bq_sql_type

pytestmark = [pytest.mark.unit, pytest.mark.provider, pytest.mark.gcp]


def _contract(schema, **location):
    return {
        "fluidVersion": "0.7.7",
        "id": "sales.decimals",
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {
                    "platform": "gcp",
                    "format": location.pop("fmt", "bigquery_table"),
                    "location": {
                        "project": "acme",
                        "dataset": "sales",
                        "table": "orders",
                        **location,
                    },
                },
                "contract": {"schema": copy.deepcopy(schema)},
            }
        ],
    }


def _resources(contract):
    return get_iac_plugin("gcp").emit(contract)


@pytest.mark.parametrize(
    "declared, field",
    [
        ("decimal(18,4)", {"type": "NUMERIC", "precision": "18", "scale": "4"}),
        ("numeric(10,2)", {"type": "NUMERIC", "precision": "10", "scale": "2"}),
        ("Decimal (12, 2)", {"type": "NUMERIC", "precision": "12", "scale": "2"}),
        ("decimal(10)", {"type": "NUMERIC", "precision": "10"}),
        ("decimal(10,0)", {"type": "NUMERIC", "precision": "10"}),
        ("decimal(29,0)", {"type": "NUMERIC", "precision": "29"}),
        ("decimal(38,9)", {"type": "NUMERIC", "precision": "38", "scale": "9"}),
        ("decimal(30,0)", {"type": "BIGNUMERIC", "precision": "30"}),
        ("decimal(38,10)", {"type": "BIGNUMERIC", "precision": "38", "scale": "10"}),
        ("bignumeric(40,20)", {"type": "BIGNUMERIC", "precision": "40", "scale": "20"}),
        ("decimal", {"type": "NUMERIC"}),
        ("money", {"type": "NUMERIC"}),
        ("decimal(60,2)", {"type": "BIGNUMERIC"}),
    ],
)
def test_a_decimal_is_written_with_the_precision_and_scale_bigquery_holds(declared, field):
    emitted = _bq_field({"name": "amt", "type": declared})
    assert {k: v for k, v in emitted.items() if k in ("type", "precision", "scale")} == field


def test_the_limits_are_bigquerys():
    """NUMERIC(P,S): S <= 9, P <= S + 29; BIGNUMERIC(P,S): S <= 38, P <= S + 38."""
    assert _bq_decimal("decimal(38,9)") == ("NUMERIC", 38, 9)
    assert _bq_decimal("decimal(39,9)") == ("BIGNUMERIC", 39, 9)
    assert _bq_decimal("decimal(10,10)") == ("BIGNUMERIC", 10, 10)
    assert _bq_decimal("decimal(76,38)") == ("BIGNUMERIC", 76, 38)
    assert _bq_decimal("decimal(77,38)") is None
    assert _bq_decimal("decimal(2,5)") is None
    assert _bq_decimal("decimal") is None
    assert _bq_decimal("string(10)") is None


@pytest.mark.parametrize(
    "declared, status",
    [("decimal(" + "9" * 5000 + ")", "lossy"), ("decimal(2," + "9" * 5000 + ")", "refused")],
    ids=["huge-precision", "huge-scale"],
)
def test_a_precision_past_int_s_digit_limit_is_classified_not_raised(declared, status):
    """``int()`` refuses a string of more than 4300 digits; such a decimal is out of any
    BigQuery range, so validate warns or refuses it instead of failing with a ValueError."""
    from fluid_build.engines.dbt import _types as dbt_types

    assert gcp._bq_outcome(declared).status == status
    assert "precision" not in _bq_field({"name": "amt", "type": declared})
    assert dbt_types.sql_type(declared, "bigquery")


@pytest.mark.parametrize(
    "declared",
    ["decimal(1;drop table x)", "decimal(18;4)", "numeric(18,4,2)", "bignumeric(a)"],
)
def test_arguments_that_are_no_precision_and_scale_are_refused(declared):
    """They were read as a bare NUMERIC, and dbt's BigQuery types are this table's, so a
    typo such as ``decimal(18;4)`` lost its scale without a word on both sides."""
    outcome = gcp._bq_outcome(declared)
    assert outcome.target is None
    assert outcome.why == "whose arguments are not a precision and an optional scale in digits"
    assert _bq_sql_type(declared) is None


@pytest.mark.parametrize("declared", ["decimal()", "money(19,4)", "money(1;x)"])
def test_empty_arguments_and_moneys_digits_stay_a_bare_numeric(declared):
    assert gcp._bq_outcome(declared).target == "NUMERIC"
    assert _bq_sql_type(declared) == "numeric"


def test_the_dbt_spelling_matches_the_table():
    assert _bq_sql_type("decimal(18,4)") == "numeric(18,4)"
    assert _bq_sql_type("decimal(10)") == "numeric(10)"
    assert _bq_sql_type("decimal(38,10)") == "bignumeric(38,10)"
    assert _bq_sql_type("decimal(60,2)") == "bignumeric"
    assert _bq_sql_type("array") is None


MARKER = "sales_decimals_decimal_parameters"


def test_a_table_with_a_parameterised_decimal_names_the_contracts_marker():
    resources = _resources(_contract([{"name": "amt", "type": "decimal(18,4)"}]))
    table = next(iter(resources["google_bigquery_table"].values()))
    assert resources["terraform_data"] == {MARKER: {"input": {"decimals": "parameterised"}}}
    assert table["lifecycle"] == {"replace_triggered_by": [f"terraform_data.{MARKER}"]}


@pytest.mark.parametrize("declared", ["decimal", "numeric", "money", "integer", "string"])
def test_a_table_without_one_has_the_marker_and_does_not_name_it(declared):
    """The marker is there before the table's first parameterised decimal, so that
    column does not create it, which would replace the table."""
    resources = _resources(_contract([{"name": "amt", "type": declared}]))
    table = next(iter(resources["google_bigquery_table"].values()))
    assert resources["terraform_data"] == {MARKER: {"input": {"decimals": "parameterised"}}}
    assert "lifecycle" not in table


def test_a_decimal_inside_a_record_counts():
    schema = [
        {
            "name": "money",
            "type": "struct",
            "fields": [{"name": "amount", "type": "decimal(12,2)"}],
        }
    ]
    table = next(iter(_resources(_contract(schema))["google_bigquery_table"].values()))
    assert table["lifecycle"] == {"replace_triggered_by": [f"terraform_data.{MARKER}"]}


def test_one_marker_serves_every_table_of_the_contract():
    contract = _contract([{"name": "amt", "type": "decimal(18,4)"}])
    refunds = copy.deepcopy(contract["exposes"][0])
    refunds["exposeId"] = "refunds"
    refunds["binding"]["location"]["table"] = "refunds"
    refunds["contract"]["schema"] = [{"name": "note", "type": "string"}]
    contract["exposes"].append(refunds)
    resources = _resources(contract)
    assert list(resources["terraform_data"]) == [MARKER]
    tables = resources["google_bigquery_table"]
    assert tables["sales_decimals_orders"]["lifecycle"] == {
        "replace_triggered_by": [f"terraform_data.{MARKER}"]
    }
    assert "lifecycle" not in tables["sales_decimals_refunds"]


def test_partitioning_and_decimal_parameters_both_trigger_the_replacement():
    contract = _contract(
        [{"name": "day", "type": "date"}, {"name": "amt", "type": "decimal(18,4)"}],
        partitionBy=["day"],
    )
    resources = _resources(contract)
    table = next(iter(resources["google_bigquery_table"].values()))
    assert table["lifecycle"]["replace_triggered_by"] == [
        "terraform_data.sales_decimals_orders_partitioning",
        f"terraform_data.{MARKER}",
    ]


def test_a_contract_with_only_a_view_has_no_marker():
    contract = _contract([], fmt="bigquery_view", view="orders_v", query="SELECT 1")
    resources = _resources(contract)
    assert "terraform_data" not in resources


def test_the_policy_tagged_schema_carries_the_parameters_too():
    contract = _contract(
        [
            {"name": "id", "type": "string", "required": True},
            {"name": "amt", "type": "decimal(18,4)"},
        ]
    )
    contract["accessPolicy"] = {
        "grants": [
            {"principal": "group:platform@acme.example", "permissions": ["read"]},
            {"principal": "group:analysts@acme.example", "permissions": ["read"]},
        ]
    }
    expose = contract["exposes"][0]
    expose["binding"]["principals"] = {
        "group:platform@acme.example": "group:platform@acme.com",
        "group:analysts@acme.example": "group:analysts@acme.com",
    }
    expose["policy"] = {
        "authz": {
            "columnRestrictions": [
                {"principal": "group:analysts@acme.example", "columns": ["amt"], "access": "deny"}
            ]
        }
    }
    module = json.loads(build_module(get_iac_plugin("gcp"), contract))
    [table] = module["resource"]["google_bigquery_table"].values()
    assert '"precision": "18"' in table["schema"] and '"scale": "4"' in table["schema"]
    assert '"policyTags"' in table["schema"]
