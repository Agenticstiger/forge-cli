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

"""Unit tests for the physical-layout suggester."""

from __future__ import annotations

import pytest

from fluid_build.copilot.tools.physical_layout import suggest_physical_layout


def _col(name: str, **extra) -> dict:
    base = {"name": name, "type": "VARCHAR"}
    base.update(extra)
    return base


# -- partition column detection ------------------------------------------------


def test_partition_detection_prefers_event_time():
    schema = {
        "model_name": "events",
        "columns": [
            _col("id", type="INTEGER", primary_key=True),
            _col("updated_at", type="TIMESTAMP"),
            _col("event_time", type="TIMESTAMP"),
        ],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["partition_by"] == "event_time"


def test_partition_detection_falls_back_to_first_time_column():
    schema = {
        "model_name": "events",
        "columns": [
            _col("id", type="INTEGER", primary_key=True),
            _col("first_seen", type="TIMESTAMP"),
            _col("last_seen", type="TIMESTAMP"),
        ],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["partition_by"] == "first_seen"


def test_partition_detection_returns_none_when_no_time_columns():
    schema = {
        "model_name": "lookup",
        "columns": [
            _col("id", type="INTEGER", primary_key=True),
            _col("name", type="VARCHAR"),
        ],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["partition_by"] is None
    assert out["partition_grain"] is None


def test_partition_detects_date_type():
    schema = {
        "model_name": "sales",
        "columns": [_col("partition_date", type="DATE")],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["partition_by"] == "partition_date"


# -- partition grain inference -------------------------------------------------


@pytest.mark.parametrize("kind", ["streaming", "realtime", "real-time", "real_time", "cdc"])
def test_partition_grain_streaming_is_hour(kind):
    schema = {
        "model_name": "events",
        "source_kind": kind,
        "columns": [_col("event_time", type="TIMESTAMP")],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["partition_grain"] == "hour"


@pytest.mark.parametrize("kind", ["aggregate", "rollup", "summary", "mart"])
def test_partition_grain_aggregates_is_month(kind):
    schema = {
        "model_name": "events",
        "source_kind": kind,
        "columns": [_col("event_time", type="TIMESTAMP")],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["partition_grain"] == "month"


def test_partition_grain_default_is_day():
    schema = {
        "model_name": "events",
        "source_kind": "oltp",
        "columns": [_col("event_time", type="TIMESTAMP")],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["partition_grain"] == "day"


# -- query-pattern ranking -----------------------------------------------------


def test_query_pattern_ranking_takes_top_4():
    schema = {
        "model_name": "events",
        "columns": [_col(c, type="VARCHAR") for c in ["a", "b", "c", "d", "e", "f"]],
    }
    patterns = [
        {"filter_columns": ["a"], "frequency": 100},
        {"filter_columns": ["b"], "frequency": 90},
        {"filter_columns": ["c"], "frequency": 80},
        {"filter_columns": ["d"], "frequency": 70},
        {"filter_columns": ["e"], "frequency": 60},
        {"filter_columns": ["f"], "frequency": 50},
    ]
    out = suggest_physical_layout(schema, provider="snowflake", query_patterns=patterns)
    assert out["clustering_keys"] == ["a", "b", "c", "d"]


def test_query_pattern_ranking_sums_frequencies_across_patterns():
    schema = {"model_name": "events", "columns": []}
    patterns = [
        {"filter_columns": ["x", "y"], "frequency": 10},
        {"filter_columns": ["y"], "frequency": 50},
        {"filter_columns": ["x"], "frequency": 5},
    ]
    # x: 10 + 5 = 15; y: 10 + 50 = 60 → y first.
    out = suggest_physical_layout(schema, provider="snowflake", query_patterns=patterns)
    assert out["clustering_keys"][0] == "y"
    assert out["clustering_keys"][1] == "x"


def test_query_pattern_ranking_skips_partition_column():
    schema = {
        "model_name": "events",
        "columns": [_col("event_time", type="TIMESTAMP")],
    }
    patterns = [
        {"filter_columns": ["event_time", "user_id"], "frequency": 100},
    ]
    out = suggest_physical_layout(schema, provider="snowflake", query_patterns=patterns)
    assert "event_time" not in out["clustering_keys"]
    assert "user_id" in out["clustering_keys"]


def test_no_query_patterns_falls_back_to_pk_fk_partition():
    schema = {
        "model_name": "orders",
        "columns": [
            _col("order_id", type="INTEGER", primary_key=True),
            _col(
                "customer_id",
                type="INTEGER",
                foreign_key={"to": "customers", "field": "customer_id"},
            ),
            _col("event_time", type="TIMESTAMP"),
        ],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    # PK first, then FK, then partition column.
    assert out["clustering_keys"][:3] == ["order_id", "customer_id", "event_time"]


def test_no_query_patterns_no_pk_no_fk_uses_partition_only():
    schema = {
        "model_name": "raw",
        "columns": [
            _col("payload", type="VARCHAR"),
            _col("event_time", type="TIMESTAMP"),
        ],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["clustering_keys"] == ["event_time"]


# -- materialization hint matrix ----------------------------------------------


def test_materialization_streaming_is_incremental():
    schema = {
        "model_name": "events",
        "source_kind": "streaming",
        "columns": [_col("event_time", type="TIMESTAMP")],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["materialization_hint"] == "incremental"


def test_materialization_cdc_is_incremental():
    schema = {
        "model_name": "events",
        "source_kind": "cdc",
        "columns": [_col("event_time", type="TIMESTAMP")],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["materialization_hint"] == "incremental"


def test_materialization_pk_only_is_view():
    schema = {
        "model_name": "lookup",
        "columns": [
            _col("id", type="INTEGER", primary_key=True),
            _col("code", type="INTEGER", primary_key=True),
        ],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["materialization_hint"] == "view"


def test_materialization_small_table_is_table():
    schema = {
        "model_name": "small",
        "columns": [_col(f"c{i}", type="VARCHAR") for i in range(5)],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["materialization_hint"] == "table"


def test_materialization_large_table_is_table():
    schema = {
        "model_name": "wide",
        "columns": [_col(f"c{i}", type="VARCHAR") for i in range(20)],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["materialization_hint"] == "table"


# -- provider rendering --------------------------------------------------------


def test_snowflake_renders_cluster_by():
    schema = {
        "model_name": "events",
        "columns": [
            _col("user_id", type="INTEGER", primary_key=True),
            _col("event_time", type="TIMESTAMP"),
        ],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    snippet = out["provider_specific"]["snowflake"]
    assert snippet.startswith("CLUSTER BY (")
    assert "user_id" in snippet


def test_snowflake_empty_when_no_clustering():
    schema = {"model_name": "x", "columns": []}
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["provider_specific"] == {"snowflake": ""}


def test_bigquery_renders_partition_and_cluster():
    schema = {
        "model_name": "events",
        "columns": [
            _col("user_id", type="INTEGER", primary_key=True),
            _col("event_time", type="TIMESTAMP"),
        ],
    }
    out = suggest_physical_layout(schema, provider="bigquery")
    snippet = out["provider_specific"]["bigquery"]
    assert "PARTITION BY DATE(event_time)" in snippet
    assert "CLUSTER BY (" in snippet
    assert "user_id" in snippet


def test_bigquery_uses_timestamp_trunc_for_non_day_grain():
    schema = {
        "model_name": "events",
        "source_kind": "streaming",
        "columns": [_col("event_time", type="TIMESTAMP")],
    }
    out = suggest_physical_layout(schema, provider="bigquery")
    snippet = out["provider_specific"]["bigquery"]
    assert "TIMESTAMP_TRUNC(event_time, HOUR)" in snippet


def test_athena_renders_partitioned_by_bucket():
    schema = {
        "model_name": "events",
        "columns": [
            _col("user_id", type="INTEGER", primary_key=True),
            _col("event_time", type="TIMESTAMP"),
        ],
    }
    out = suggest_physical_layout(schema, provider="athena")
    snippet = out["provider_specific"]["athena"]
    assert "partitioned_by=[" in snippet
    assert "day(event_time)" in snippet
    assert "bucket(user_id, 16)" in snippet


def test_athena_partition_only_when_no_clustering():
    schema = {
        "model_name": "events",
        "columns": [_col("event_time", type="TIMESTAMP")],
    }
    out = suggest_physical_layout(schema, provider="athena")
    snippet = out["provider_specific"]["athena"]
    assert snippet == "partitioned_by=['day(event_time)']"


def test_redshift_renders_distkey_and_sortkey():
    schema = {
        "model_name": "events",
        "columns": [
            _col("user_id", type="INTEGER", primary_key=True),
            _col("event_time", type="TIMESTAMP"),
        ],
    }
    out = suggest_physical_layout(schema, provider="redshift")
    snippet = out["provider_specific"]["redshift"]
    assert "DISTKEY(user_id)" in snippet
    assert "SORTKEY(event_time" in snippet


def test_unknown_provider_returns_empty_stub():
    schema = {
        "model_name": "x",
        "columns": [_col("id", type="INTEGER", primary_key=True)],
    }
    out = suggest_physical_layout(schema, provider="duckdb")
    assert out["provider_specific"] == {"duckdb": ""}


# -- envelope shape ------------------------------------------------------------


def test_output_envelope_shape():
    out = suggest_physical_layout({"model_name": "x", "columns": []}, provider="snowflake")
    assert set(out.keys()) == {
        "clustering_keys",
        "partition_by",
        "partition_grain",
        "materialization_hint",
        "provider_specific",
    }
    assert isinstance(out["clustering_keys"], list)
    assert out["materialization_hint"] in {"table", "view", "incremental"}


def test_clustering_keys_capped_at_4():
    schema = {"model_name": "events", "columns": []}
    patterns = [{"filter_columns": [f"c{i}"], "frequency": 100 - i} for i in range(10)]
    out = suggest_physical_layout(schema, provider="snowflake", query_patterns=patterns)
    assert len(out["clustering_keys"]) == 4


def test_lenient_on_non_dict_columns():
    schema = {
        "model_name": "x",
        "columns": [None, "junk", {"name": "ok", "type": "INTEGER", "primary_key": True}],
    }
    out = suggest_physical_layout(schema, provider="snowflake")
    assert out["clustering_keys"] == ["ok"]
