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

"""Unit tests for the dbt schema.yml test generator."""

from __future__ import annotations

import pytest

from fluid_build.copilot.tools.dbt_test_generator import generate_dbt_tests


@pytest.fixture
def empty_schema() -> dict:
    return {"model_name": "blank_model", "columns": []}


def _column(name: str, **extra) -> dict:
    base = {"name": name, "type": "VARCHAR"}
    base.update(extra)
    return base


def _get_column(out: dict, name: str) -> dict:
    cols = out["models"][0]["columns"]
    for c in cols:
        if c["name"] == name:
            return c
    raise AssertionError(f"column {name!r} not in output: {cols}")


def test_envelope_shape(empty_schema):
    out = generate_dbt_tests(empty_schema)
    assert out["version"] == 2
    assert len(out["models"]) == 1
    assert out["models"][0]["name"] == "blank_model"
    assert out["models"][0]["columns"] == []


def test_missing_model_name_falls_back():
    out = generate_dbt_tests({"columns": []})
    assert out["models"][0]["name"] == "unnamed_model"


def test_primary_key_emits_unique_and_not_null():
    schema = {
        "model_name": "orders",
        "columns": [_column("order_id", type="INTEGER", primary_key=True)],
    }
    out = generate_dbt_tests(schema)
    col = _get_column(out, "order_id")
    assert col["tests"] == ["unique", "not_null"]


def test_nullable_false_non_pk_emits_not_null():
    schema = {
        "model_name": "users",
        "columns": [_column("email", type="VARCHAR", nullable=False)],
    }
    out = generate_dbt_tests(schema)
    assert _get_column(out, "email")["tests"] == ["not_null"]


def test_nullable_true_or_default_emits_no_not_null():
    schema = {
        "model_name": "users",
        "columns": [
            _column("nickname", type="VARCHAR", nullable=True),
            _column("middle_name", type="VARCHAR"),  # default → nullable
        ],
    }
    out = generate_dbt_tests(schema)
    assert "tests" not in _get_column(out, "nickname")
    assert "tests" not in _get_column(out, "middle_name")


def test_pk_does_not_double_up_not_null_via_nullable_false():
    # PK already emits not_null + unique; nullable=False on a PK
    # must not duplicate not_null.
    schema = {
        "model_name": "orders",
        "columns": [
            _column("order_id", type="INTEGER", primary_key=True, nullable=False),
        ],
    }
    tests = _get_column(generate_dbt_tests(schema), "order_id")["tests"]
    assert tests.count("not_null") == 1
    assert tests.count("unique") == 1


def test_foreign_key_emits_relationships():
    schema = {
        "model_name": "orders",
        "columns": [
            _column(
                "customer_id",
                type="INTEGER",
                foreign_key={"to": "customers", "field": "customer_id"},
            ),
        ],
    }
    tests = _get_column(generate_dbt_tests(schema), "customer_id")["tests"]
    assert {
        "relationships": {
            "to": "ref('customers')",
            "field": "customer_id",
        }
    } in tests


def test_incomplete_foreign_key_is_skipped():
    schema = {
        "model_name": "orders",
        "columns": [
            _column("customer_id", type="INTEGER", foreign_key={"to": "customers"}),
        ],
    }
    col = _get_column(generate_dbt_tests(schema), "customer_id")
    assert "tests" not in col  # no FK test because field is missing


def test_enum_emits_accepted_values():
    schema = {
        "model_name": "orders",
        "columns": [
            _column("status", type="VARCHAR", enum=["pending", "shipped", "cancelled"]),
        ],
    }
    tests = _get_column(generate_dbt_tests(schema), "status")["tests"]
    assert {"accepted_values": {"values": ["pending", "shipped", "cancelled"]}} in tests


def test_range_emits_dbt_expectations_between_on_numeric():
    schema = {
        "model_name": "orders",
        "columns": [
            _column("total_amount", type="NUMERIC", min=0, max=10_000),
        ],
    }
    tests = _get_column(generate_dbt_tests(schema), "total_amount")["tests"]
    assert {
        "dbt_expectations.expect_column_values_to_be_between": {
            "min_value": 0,
            "max_value": 10_000,
        }
    } in tests


def test_range_with_only_min_or_only_max():
    schema = {
        "model_name": "orders",
        "columns": [
            _column("only_min", type="INTEGER", min=5),
            _column("only_max", type="FLOAT", max=100.0),
        ],
    }
    out = generate_dbt_tests(schema)
    min_tests = _get_column(out, "only_min")["tests"]
    max_tests = _get_column(out, "only_max")["tests"]
    assert {"dbt_expectations.expect_column_values_to_be_between": {"min_value": 5}} in min_tests
    assert {
        "dbt_expectations.expect_column_values_to_be_between": {"max_value": 100.0}
    } in max_tests


def test_range_on_non_numeric_is_skipped_with_warning(caplog):
    schema = {
        "model_name": "orders",
        "columns": [
            _column("status", type="VARCHAR", min=0, max=10),
        ],
    }
    with caplog.at_level("WARNING"):
        out = generate_dbt_tests(schema)
    col = _get_column(out, "status")
    assert "tests" not in col
    assert any("non-numeric column" in r.message for r in caplog.records)


def test_no_rules_at_all_emits_no_tests_list():
    schema = {
        "model_name": "orders",
        "columns": [
            _column("notes", type="VARCHAR", description="free-form notes"),
        ],
    }
    col = _get_column(generate_dbt_tests(schema), "notes")
    assert col == {"name": "notes", "description": "free-form notes"}


def test_description_carries_through():
    schema = {
        "model_name": "orders",
        "columns": [
            _column("order_id", type="INTEGER", primary_key=True, description="PK"),
        ],
    }
    col = _get_column(generate_dbt_tests(schema), "order_id")
    assert col["description"] == "PK"


def test_combined_rules_emit_all_tests_in_deterministic_order():
    # PK + FK + enum + range all together — confirm every rule fires.
    schema = {
        "model_name": "events",
        "columns": [
            _column(
                "event_id",
                type="INTEGER",
                primary_key=True,
                nullable=False,
            ),
            _column(
                "user_id",
                type="INTEGER",
                nullable=False,
                foreign_key={"to": "users", "field": "user_id"},
            ),
            _column(
                "event_type",
                type="VARCHAR",
                enum=["click", "view", "submit"],
            ),
            _column(
                "score",
                type="NUMERIC",
                min=0,
                max=1,
            ),
        ],
    }
    out = generate_dbt_tests(schema)
    assert _get_column(out, "event_id")["tests"] == ["unique", "not_null"]
    user = _get_column(out, "user_id")["tests"]
    assert "not_null" in user
    assert any("relationships" in (t if isinstance(t, dict) else {}) for t in user)
    et = _get_column(out, "event_type")["tests"]
    assert any("accepted_values" in (t if isinstance(t, dict) else {}) for t in et)
    sc = _get_column(out, "score")["tests"]
    assert any(
        "dbt_expectations.expect_column_values_to_be_between" in (t if isinstance(t, dict) else {})
        for t in sc
    )


def test_lenient_handling_of_garbage_columns():
    schema = {
        "model_name": "weird",
        "columns": [
            None,
            "string-instead-of-dict",
            {"no_name_key": True},
            {"name": "good", "type": "INTEGER", "primary_key": True},
        ],
    }
    out = generate_dbt_tests(schema)
    names = [c["name"] for c in out["models"][0]["columns"]]
    assert names == ["good"]


def test_columns_field_not_a_list_is_coerced(caplog):
    with caplog.at_level("WARNING"):
        out = generate_dbt_tests({"model_name": "x", "columns": "not-a-list"})
    assert out["models"][0]["columns"] == []
    assert any("not a list" in r.message for r in caplog.records)


def test_dialect_kwarg_accepted_but_not_yet_used():
    # Spec says the dialect kwarg is reserved — assert it doesn't crash
    # and the output is stable across dialects (for now).
    schema = {"model_name": "x", "columns": [_column("a", type="INTEGER", primary_key=True)]}
    sf = generate_dbt_tests(schema, dialect="snowflake")
    bq = generate_dbt_tests(schema, dialect="bigquery")
    assert sf == bq


def test_snowflake_number_type_is_recognised_as_numeric():
    # NUMBER(18,2) is Snowflake's canonical decimal spelling.
    schema = {
        "model_name": "orders",
        "columns": [_column("amount", type="NUMBER(18,2)", min=0, max=1_000_000)],
    }
    tests = _get_column(generate_dbt_tests(schema), "amount")["tests"]
    assert any(
        "dbt_expectations.expect_column_values_to_be_between" in (t if isinstance(t, dict) else {})
        for t in tests
    )
