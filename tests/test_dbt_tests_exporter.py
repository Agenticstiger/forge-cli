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

"""Tests for the dbt-tests exporter (``exporters/dbt_tests.py``)."""

from __future__ import annotations

import pytest
import yaml

from fluid_build.exporters.dbt_tests import MANAGED_BY_SENTINEL, render_dbt_tests


def _sample_contract():
    return {
        "id": "orders-product",
        "exposes": [
            {
                "id": "orders",
                "binding": {
                    "platform": "snowflake",
                    "location": {
                        "format": "TABLE",
                        "properties": {
                            "database": "ANALYTICS",
                            "schema": "COMMERCE",
                            "table": "ORDERS",
                        },
                    },
                },
                "schema": [
                    {"name": "order_id", "type": "BIGINT"},
                    {"name": "amount", "type": "INT"},
                    {"name": "status", "type": "STRING"},
                ],
                "quality": {
                    "tests": [
                        {"name": "t_pk", "type": "not_null", "column": "order_id"},
                        {"name": "t_unique", "type": "unique", "column": "order_id"},
                        {
                            "name": "t_status_values",
                            "type": "accepted_values",
                            "column": "status",
                            "values": ["new", "shipped", "delivered"],
                        },
                        {
                            "name": "t_amount_range",
                            "type": "range",
                            "column": "amount",
                            "min": 0,
                            "max": 1000000,
                        },
                    ],
                },
            }
        ],
    }


def test_render_emits_managed_sentinel():
    out = render_dbt_tests(_sample_contract())
    assert MANAGED_BY_SENTINEL in out


def test_render_produces_valid_yaml():
    out = render_dbt_tests(_sample_contract())
    # Strip the sentinel comment header before parsing.
    body = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
    doc = yaml.safe_load(body)
    assert doc["version"] == 2
    assert "models" in doc


def test_render_uses_binding_table_name():
    out = render_dbt_tests(_sample_contract())
    body = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
    doc = yaml.safe_load(body)
    assert doc["models"][0]["name"] == "ORDERS"


def test_render_attaches_per_column_tests():
    out = render_dbt_tests(_sample_contract())
    body = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
    doc = yaml.safe_load(body)
    cols = {c["name"]: c for c in doc["models"][0]["columns"]}

    # not_null + unique are emitted as bare strings (dbt convention).
    assert "not_null" in cols["order_id"]["tests"]
    assert "unique" in cols["order_id"]["tests"]

    # accepted_values is a dict with the values list.
    status_tests = cols["status"]["tests"]
    av = next(t for t in status_tests if isinstance(t, dict) and "accepted_values" in t)
    assert av["accepted_values"]["values"] == ["new", "shipped", "delivered"]

    # range becomes a dbt_utils expression check.
    amt_tests = cols["amount"]["tests"]
    rng = next(t for t in amt_tests if isinstance(t, dict) and "dbt_utils.expression_is_true" in t)
    assert "amount >= 0" in rng["dbt_utils.expression_is_true"]["expression"]
    assert "amount <= 1000000" in rng["dbt_utils.expression_is_true"]["expression"]


def test_render_routes_table_level_to_model_tests_and_column_level_to_columns():
    """Column-less tests go to model.tests; column-bearing tests go to columns.<col>.tests."""
    contract = {
        "exposes": [
            {
                "id": "x",
                "binding": {"location": {"properties": {"table": "X"}}},
                "schema": [{"name": "id", "type": "BIGINT"}],
                "quality": {
                    "tests": [
                        {"name": "row_count", "type": "row_count_anomaly"},
                        {"name": "id_pk", "type": "not_null", "column": "id"},
                    ]
                },
            }
        ]
    }
    out = render_dbt_tests(contract)
    body = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
    doc = yaml.safe_load(body)
    model = doc["models"][0]
    cols = {c["name"]: c for c in model["columns"]}
    # Column-level: not_null on id.
    assert "not_null" in cols["id"]["tests"]
    # Model-level: row_count_anomaly mapped to its sentinel since no threshold given.
    assert "fluid_row_count_anomaly" in model["tests"]


def test_render_falls_back_to_expose_id_when_no_binding():
    contract = {
        "exposes": [
            {
                "id": "fallback_table",
                "schema": [{"name": "n", "type": "INT"}],
                "quality": {"tests": []},
            }
        ]
    }
    out = render_dbt_tests(contract)
    body = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
    doc = yaml.safe_load(body)
    assert doc["models"][0]["name"] == "fallback_table"


def test_render_raises_on_non_mapping():
    with pytest.raises(TypeError):
        render_dbt_tests("not a dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Model-level tests (table-wide, no ``column`` field)
# ---------------------------------------------------------------------------


def test_row_count_anomaly_with_threshold_becomes_dbt_utils_expression():
    """row_count_anomaly with a numeric threshold maps to dbt_utils.expression_is_true."""
    contract = {
        "exposes": [
            {
                "id": "orders",
                "binding": {"location": {"properties": {"table": "ORDERS"}}},
                "schema": [{"name": "id", "type": "BIGINT"}],
                "quality": {
                    "tests": [
                        {"type": "row_count_anomaly", "threshold": 100},
                    ]
                },
            }
        ]
    }
    out = render_dbt_tests(contract)
    body = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
    doc = yaml.safe_load(body)
    model = doc["models"][0]
    assert "tests" in model
    rc = next(
        t for t in model["tests"] if isinstance(t, dict) and "dbt_utils.expression_is_true" in t
    )
    assert "count(*) > 100" in rc["dbt_utils.expression_is_true"]["expression"]


def test_row_count_anomaly_without_threshold_emits_sentinel():
    """row_count_anomaly without a threshold emits a sentinel test name."""
    contract = {
        "exposes": [
            {
                "id": "orders",
                "binding": {"location": {"properties": {"table": "ORDERS"}}},
                "schema": [{"name": "id", "type": "BIGINT"}],
                "quality": {"tests": [{"type": "row_count_anomaly"}]},
            }
        ]
    }
    out = render_dbt_tests(contract)
    body = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
    doc = yaml.safe_load(body)
    assert "fluid_row_count_anomaly" in doc["models"][0]["tests"]


def test_freshness_with_max_age_becomes_dbt_utils_recency():
    contract = {
        "exposes": [
            {
                "id": "orders",
                "binding": {"location": {"properties": {"table": "ORDERS"}}},
                "schema": [{"name": "id", "type": "BIGINT"}],
                "quality": {
                    "tests": [
                        {
                            "type": "freshness",
                            "column_name": "updated_at",
                            "max_age": "1d",
                        }
                    ]
                },
            }
        ]
    }
    out = render_dbt_tests(contract)
    body = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
    doc = yaml.safe_load(body)
    rec = next(
        t for t in doc["models"][0]["tests"] if isinstance(t, dict) and "dbt_utils.recency" in t
    )
    assert rec["dbt_utils.recency"]["field"] == "updated_at"
    assert rec["dbt_utils.recency"]["_fluid_max_age"] == "1d"


def test_table_level_unknown_test_emits_sentinel_marker():
    """An unmapped table-level test surfaces as ``fluid_unmapped_<kind>``."""
    contract = {
        "exposes": [
            {
                "id": "orders",
                "binding": {"location": {"properties": {"table": "ORDERS"}}},
                "schema": [{"name": "id", "type": "BIGINT"}],
                "quality": {"tests": [{"type": "weird_custom_check"}]},
            }
        ]
    }
    out = render_dbt_tests(contract)
    body = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
    doc = yaml.safe_load(body)
    assert "fluid_unmapped_weird_custom_check" in doc["models"][0]["tests"]
