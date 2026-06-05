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

"""Tests for the dbt-tests exporter (``exporters/dbt_tests.py``).

The exporter reads the FLUID v0.7.x contract shape:
``exposes[].contract.schema[]`` + ``exposes[].contract.dq.rules[]`` +
``exposes[].binding.location.table``. Each ``dqRule.type`` is mapped to
a dbt generic test.
"""

from __future__ import annotations

import pytest
import yaml

from fluid_build.exporters.dbt_tests import MANAGED_BY_SENTINEL, render_dbt_tests


def _parse(out: str) -> dict:
    """Strip the sentinel comment header and parse the YAML body."""
    body = "\n".join(line for line in out.splitlines() if not line.startswith("#"))
    return yaml.safe_load(body)


def _sample_contract():
    """A realistic FLUID v0.7.x contract with a full dq.rules block."""
    return {
        "fluidVersion": "0.7.3",
        "id": "orders-product",
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "description": "Order facts",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "database": "ANALYTICS",
                        "schema": "COMMERCE",
                        "table": "ORDERS",
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "order_id", "type": "NUMBER", "description": "PK"},
                        {"name": "amount", "type": "NUMBER"},
                        {"name": "status", "type": "STRING"},
                    ],
                    "dq": {
                        "rules": [
                            {
                                "id": "order_id_complete",
                                "type": "completeness",
                                "selector": "order_id",
                                "threshold": 1.0,
                                "operator": ">=",
                                "severity": "error",
                            },
                            {
                                "id": "order_id_unique",
                                "type": "uniqueness",
                                "selector": "order_id",
                                "severity": "error",
                            },
                            {
                                "id": "status_values",
                                "type": "valid_values",
                                "selector": "status",
                                "severity": "warn",
                                "description": "status valid values: new, shipped, delivered.",
                            },
                            {
                                "id": "amount_accuracy",
                                "type": "accuracy",
                                "selector": "amount",
                                "threshold": 0.99,
                                "operator": ">=",
                                "severity": "warn",
                            },
                        ]
                    },
                },
            }
        ],
    }


# ---------------------------------------------------------------------------
# Basic shape + sentinel
# ---------------------------------------------------------------------------


def test_render_emits_managed_sentinel():
    out = render_dbt_tests(_sample_contract())
    assert MANAGED_BY_SENTINEL in out


def test_render_produces_valid_yaml():
    doc = _parse(render_dbt_tests(_sample_contract()))
    assert doc["version"] == 2
    assert "models" in doc


def test_render_uses_binding_table_name():
    """The dbt model is named after binding.location.table — v0.7.x shape."""
    doc = _parse(render_dbt_tests(_sample_contract()))
    assert doc["models"][0]["name"] == "ORDERS"


def test_render_emits_columns_from_contract_schema():
    """B1 regression: contract.schema[] columns must appear in the model."""
    doc = _parse(render_dbt_tests(_sample_contract()))
    cols = {c["name"] for c in doc["models"][0]["columns"]}
    assert cols == {"order_id", "amount", "status"}


def test_render_falls_back_to_expose_id_when_no_binding_table():
    contract = {
        "exposes": [
            {
                "exposeId": "fallback_table",
                "contract": {"schema": [{"name": "n", "type": "INT"}]},
            }
        ]
    }
    doc = _parse(render_dbt_tests(contract))
    assert doc["models"][0]["name"] == "fallback_table"


def test_render_raises_on_non_mapping():
    with pytest.raises(TypeError):
        render_dbt_tests("not a dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# dqRule.type → dbt test mapping (the core of B1)
# ---------------------------------------------------------------------------


def test_completeness_rule_maps_to_not_null():
    """B1: dqRule type 'completeness' → dbt not_null on the selector column."""
    doc = _parse(render_dbt_tests(_sample_contract()))
    cols = {c["name"]: c for c in doc["models"][0]["columns"]}
    assert "not_null" in cols["order_id"]["tests"]


def test_uniqueness_rule_maps_to_unique():
    """B1: dqRule type 'uniqueness' → dbt unique on the selector column."""
    doc = _parse(render_dbt_tests(_sample_contract()))
    cols = {c["name"]: c for c in doc["models"][0]["columns"]}
    assert "unique" in cols["order_id"]["tests"]


def test_valid_values_rule_maps_to_accepted_values():
    """B1: 'valid_values' → dbt accepted_values; value list parsed from description."""
    doc = _parse(render_dbt_tests(_sample_contract()))
    cols = {c["name"]: c for c in doc["models"][0]["columns"]}
    status_tests = cols["status"]["tests"]
    av = next(t for t in status_tests if isinstance(t, dict) and "accepted_values" in t)
    assert av["accepted_values"]["values"] == ["new", "shipped", "delivered"]


def test_valid_values_rule_reads_explicit_value_list():
    """An explicit validValues / values key wins over description parsing."""
    contract = {
        "exposes": [
            {
                "exposeId": "x",
                "binding": {"location": {"table": "X"}},
                "contract": {
                    "schema": [{"name": "state", "type": "STRING"}],
                    "dq": {
                        "rules": [
                            {
                                "id": "state_valid",
                                "type": "valid_values",
                                "selector": "state",
                                "severity": "warn",
                                "validValues": ["CA", "NY", "TX"],
                            }
                        ]
                    },
                },
            }
        ]
    }
    doc = _parse(render_dbt_tests(contract))
    cols = {c["name"]: c for c in doc["models"][0]["columns"]}
    av = next(t for t in cols["state"]["tests"] if isinstance(t, dict) and "accepted_values" in t)
    assert av["accepted_values"]["values"] == ["CA", "NY", "TX"]


def test_accuracy_rule_with_threshold_maps_to_expression():
    """B1: 'accuracy' with a threshold → dbt_utils.expression_is_true placeholder."""
    doc = _parse(render_dbt_tests(_sample_contract()))
    cols = {c["name"]: c for c in doc["models"][0]["columns"]}
    amt_tests = cols["amount"]["tests"]
    expr = next(t for t in amt_tests if isinstance(t, dict) and "dbt_utils.expression_is_true" in t)
    text = expr["dbt_utils.expression_is_true"]["expression"]
    assert "0.99" in text and ">=" in text


def test_column_scoped_freshness_maps_to_recency():
    contract = {
        "exposes": [
            {
                "exposeId": "x",
                "binding": {"location": {"table": "X"}},
                "contract": {
                    "schema": [{"name": "updated_at", "type": "TIMESTAMP"}],
                    "dq": {
                        "rules": [
                            {
                                "id": "fresh",
                                "type": "freshness",
                                "selector": "updated_at",
                                "window": "P1D",
                                "severity": "warn",
                            }
                        ]
                    },
                },
            }
        ]
    }
    doc = _parse(render_dbt_tests(contract))
    cols = {c["name"]: c for c in doc["models"][0]["columns"]}
    rec = next(
        t for t in cols["updated_at"]["tests"] if isinstance(t, dict) and "dbt_utils.recency" in t
    )
    assert rec["dbt_utils.recency"]["field"] == "updated_at"
    assert rec["dbt_utils.recency"]["_fluid_window"] == "P1D"


# ---------------------------------------------------------------------------
# Table-wide rules (selector "*") → model-level tests
# ---------------------------------------------------------------------------


def test_table_scoped_freshness_becomes_model_level_test():
    """A 'freshness' rule with selector '*' attaches at the model level."""
    contract = {
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {"location": {"table": "ORDERS"}},
                "contract": {
                    "schema": [{"name": "id", "type": "NUMBER"}],
                    "dq": {
                        "rules": [
                            {
                                "id": "rows_fresh",
                                "type": "freshness",
                                "selector": "*",
                                "window": "P1D",
                                "severity": "warn",
                            }
                        ]
                    },
                },
            }
        ]
    }
    doc = _parse(render_dbt_tests(contract))
    model = doc["models"][0]
    assert "tests" in model
    rec = next(t for t in model["tests"] if isinstance(t, dict) and "dbt_utils.recency" in t)
    assert rec["dbt_utils.recency"]["_fluid_window"] == "P1D"


def test_anomaly_detection_table_rule_with_threshold_maps_to_expression():
    contract = {
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {"location": {"table": "ORDERS"}},
                "contract": {
                    "schema": [{"name": "id", "type": "NUMBER"}],
                    "dq": {
                        "rules": [
                            {
                                "id": "row_count",
                                "type": "anomaly_detection",
                                "selector": "*",
                                "threshold": 100,
                                "severity": "warn",
                            }
                        ]
                    },
                },
            }
        ]
    }
    doc = _parse(render_dbt_tests(contract))
    rc = next(
        t
        for t in doc["models"][0]["tests"]
        if isinstance(t, dict) and "dbt_utils.expression_is_true" in t
    )
    assert "count(*) > 100" in rc["dbt_utils.expression_is_true"]["expression"]


def test_schema_rule_emits_sentinel_test_name():
    """An unmapped dq type surfaces as a fluid_* sentinel, never silently dropped."""
    contract = {
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {"location": {"table": "ORDERS"}},
                "contract": {
                    "schema": [{"name": "id", "type": "NUMBER"}],
                    "dq": {
                        "rules": [
                            {
                                "id": "schema_check",
                                "type": "schema",
                                "selector": "*",
                                "severity": "warn",
                            }
                        ]
                    },
                },
            }
        ]
    }
    doc = _parse(render_dbt_tests(contract))
    tests = doc["models"][0].get("tests", [])
    assert any(isinstance(t, str) and t.startswith("fluid_") for t in tests)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_no_dq_block_still_emits_columns():
    """A contract with schema but no dq.rules emits columns with no tests."""
    contract = {
        "exposes": [
            {
                "exposeId": "x",
                "binding": {"location": {"table": "X"}},
                "contract": {"schema": [{"name": "id", "type": "NUMBER"}]},
            }
        ]
    }
    doc = _parse(render_dbt_tests(contract))
    cols = doc["models"][0]["columns"]
    assert cols[0]["name"] == "id"
    assert "tests" not in cols[0]


def test_dq_rule_targeting_undeclared_column_still_emitted():
    """A dq rule for a column missing from contract.schema[] is not dropped."""
    contract = {
        "exposes": [
            {
                "exposeId": "x",
                "binding": {"location": {"table": "X"}},
                "contract": {
                    "schema": [{"name": "id", "type": "NUMBER"}],
                    "dq": {
                        "rules": [
                            {
                                "id": "ghost",
                                "type": "completeness",
                                "selector": "not_in_schema",
                                "severity": "error",
                            }
                        ]
                    },
                },
            }
        ]
    }
    doc = _parse(render_dbt_tests(contract))
    cols = {c["name"]: c for c in doc["models"][0]["columns"]}
    assert "not_in_schema" in cols
    assert "not_null" in cols["not_in_schema"]["tests"]


def test_multiple_exposes_each_become_a_model():
    contract = {
        "exposes": [
            {
                "exposeId": "a",
                "binding": {"location": {"table": "A"}},
                "contract": {"schema": [{"name": "x", "type": "INT"}]},
            },
            {
                "exposeId": "b",
                "binding": {"location": {"table": "B"}},
                "contract": {"schema": [{"name": "y", "type": "INT"}]},
            },
        ]
    }
    doc = _parse(render_dbt_tests(contract))
    names = {m["name"] for m in doc["models"]}
    assert names == {"A", "B"}


# ---------------------------------------------------------------------------
# Field-level constraint -> dbt test mapping
#
# A contract may express its quality intent inline on the schema columns
# (required / unique / enum / minimum / maximum) rather than via dq.rules[].
# Pre-2026-06 the exporter ignored these entirely, emitting a model with column
# names but ZERO executable tests.
# ---------------------------------------------------------------------------


def _constraint_contract():
    """A contract whose constraints live inline on the schema columns."""
    return {
        "fluidVersion": "0.7.3",
        "id": "orders-product",
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {"platform": "snowflake", "location": {"table": "ORDERS"}},
                "contract": {
                    "schema": [
                        {"name": "order_id", "type": "STRING", "required": True, "unique": True},
                        {
                            "name": "amount",
                            "type": "NUMBER",
                            "required": True,
                            "minimum": 0,
                            "maximum": 1000000,
                        },
                        {
                            "name": "status",
                            "type": "STRING",
                            "required": True,
                            "enum": ["pending", "completed", "cancelled"],
                        },
                        {"name": "customer_id", "type": "STRING", "required": False},
                    ]
                },
            }
        ],
    }


def _cols(contract) -> dict:
    doc = _parse(render_dbt_tests(contract))
    return {c["name"]: c for c in doc["models"][0]["columns"]}


def test_required_constraint_emits_not_null():
    cols = _cols(_constraint_contract())
    assert "not_null" in cols["order_id"]["tests"]
    assert "not_null" in cols["amount"]["tests"]
    assert "not_null" in cols["status"]["tests"]


def test_optional_column_has_no_not_null():
    cols = _cols(_constraint_contract())
    # required: false -> no tests at all on this column.
    assert "tests" not in cols["customer_id"]


def test_unique_constraint_emits_unique():
    cols = _cols(_constraint_contract())
    assert "unique" in cols["order_id"]["tests"]
    # A non-key column must not get a spurious unique.
    assert "unique" not in cols["amount"]["tests"]


def test_enum_constraint_emits_accepted_values():
    cols = _cols(_constraint_contract())
    accepted = [
        t for t in cols["status"]["tests"] if isinstance(t, dict) and "accepted_values" in t
    ]
    assert accepted, "status enum did not produce an accepted_values test"
    assert accepted[0]["accepted_values"]["values"] == ["pending", "completed", "cancelled"]


def test_minmax_constraint_emits_accepted_range():
    cols = _cols(_constraint_contract())
    ranged = [
        t
        for t in cols["amount"]["tests"]
        if isinstance(t, dict) and "dbt_utils.accepted_range" in t
    ]
    assert ranged, "amount min/max did not produce a dbt_utils.accepted_range test"
    args = ranged[0]["dbt_utils.accepted_range"]
    assert args["min_value"] == 0
    assert args["max_value"] == 1000000
    assert args["inclusive"] is True


def test_model_carries_real_tests_not_just_names():
    """The end-to-end regression: the model is no longer a bare skeleton."""
    doc = _parse(render_dbt_tests(_constraint_contract()))
    model = doc["models"][0]
    # Every column block exists...
    assert {c["name"] for c in model["columns"]} == {
        "order_id",
        "amount",
        "status",
        "customer_id",
    }
    # ...and the constrained columns carry executable tests.
    total_tests = sum(len(c.get("tests", [])) for c in model["columns"])
    assert total_tests >= 6, f"expected real tests, got only {total_tests}"


@pytest.mark.parametrize(
    "key_marker",
    [
        {"unique": True},
        {"primaryKey": True},
        {"pk": True},
        {"primary_key": True},
        {"semanticType": "identifier"},
        {"labels": {"constraint": "primary_key", "unique": "true"}},
    ],
)
def test_various_key_markers_emit_unique(key_marker):
    col = {"name": "id", "type": "STRING", **key_marker}
    contract = {
        "exposes": [
            {
                "exposeId": "t",
                "binding": {"location": {"table": "T"}},
                "contract": {"schema": [col]},
            }
        ]
    }
    cols = _cols(contract)
    assert "unique" in cols["id"]["tests"], f"{key_marker} did not yield a unique test"


def test_min_only_and_max_only_ranges():
    contract = {
        "exposes": [
            {
                "exposeId": "t",
                "binding": {"location": {"table": "T"}},
                "contract": {
                    "schema": [
                        {"name": "age", "type": "INT", "minimum": 0},
                        {"name": "pct", "type": "NUMBER", "maximum": 100},
                    ]
                },
            }
        ]
    }
    cols = _cols(contract)
    age_range = cols["age"]["tests"][0]["dbt_utils.accepted_range"]
    assert age_range == {"min_value": 0, "inclusive": True}
    pct_range = cols["pct"]["tests"][0]["dbt_utils.accepted_range"]
    assert pct_range == {"max_value": 100, "inclusive": True}


def test_constraint_and_dq_rule_dedupe_to_single_test():
    """required + a completeness dq rule on one column -> a single not_null."""
    contract = {
        "exposes": [
            {
                "exposeId": "orders",
                "binding": {"location": {"table": "ORDERS"}},
                "contract": {
                    "schema": [{"name": "order_id", "type": "STRING", "required": True}],
                    "dq": {"rules": [{"type": "completeness", "selector": "order_id"}]},
                },
            }
        ]
    }
    cols = _cols(contract)
    tests = cols["order_id"]["tests"]
    assert tests.count("not_null") == 1, f"expected a single not_null, got {tests}"


def test_enum_string_aliases_acceptedvalues_camel_and_snake():
    for key in ("acceptedValues", "accepted_values"):
        contract = {
            "exposes": [
                {
                    "exposeId": "t",
                    "binding": {"location": {"table": "T"}},
                    "contract": {"schema": [{"name": "s", "type": "STRING", key: ["a", "b"]}]},
                }
            ]
        }
        cols = _cols(contract)
        accepted = [t for t in cols["s"]["tests"] if isinstance(t, dict) and "accepted_values" in t]
        assert accepted, f"{key} did not produce an accepted_values test"
        assert accepted[0]["accepted_values"]["values"] == ["a", "b"]
