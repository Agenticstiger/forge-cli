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

"""Regression tests for the consumer-MCP UX helpers.

Pins the behaviour of :func:`find_expose` (auto-pick single expose),
:func:`list_exposes` (CLI ``list`` summary), :func:`resolve_expose_paths`
(relative-path resolution), and :func:`_annotate_engine_error` (error
hints) so a future refactor can't silently degrade the operator
experience."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.output_ports.mcp import (
    find_expose,
    list_exposes,
    resolve_expose_paths,
)
from fluid_build.output_ports.mcp._expose_utils import _annotate_engine_error

from ._fixtures import make_contract, make_expose

# ---------------------------------------------------------------------
# find_expose — auto-pick + helpful errors
# ---------------------------------------------------------------------


def test_find_expose_auto_picks_single_expose():
    contract = make_contract()
    assert len(contract["exposes"]) == 1
    expose = find_expose(contract, None)
    assert expose["exposeId"] == contract["exposes"][0]["exposeId"]


def test_find_expose_with_explicit_id_returns_match():
    contract = make_contract()
    expose = find_expose(contract, "customer_profiles")
    assert expose["exposeId"] == "customer_profiles"


def test_find_expose_unknown_id_lists_available():
    contract = make_contract()
    with pytest.raises(ValueError, match="customer_profiles"):
        find_expose(contract, "wrong_name")


def test_find_expose_multi_expose_no_id_asks_for_one():
    contract = make_contract(
        exposes=[
            make_expose(expose_id="a"),
            make_expose(expose_id="b"),
        ]
    )
    with pytest.raises(ValueError, match="2 exposes"):
        find_expose(contract, None)


def test_find_expose_empty_contract_rejected():
    with pytest.raises(ValueError, match="no exposes"):
        find_expose({"exposes": []}, None)


# ---------------------------------------------------------------------
# list_exposes — surfaces what the CLI ``list`` shows
# ---------------------------------------------------------------------


def test_list_exposes_reports_minimal_summary():
    contract = make_contract(
        exposes=[
            make_expose(
                expose_id="customer_profiles",
                semantics={
                    "measures": [{"name": "customer_count", "agg": "count", "expr": "customer_id"}]
                },
            ),
        ]
    )
    summaries = list_exposes(contract)
    assert len(summaries) == 1
    entry = summaries[0]
    assert entry["exposeId"] == "customer_profiles"
    assert entry["kind"] == "table"
    assert entry["platform"] == "local"
    assert entry["format"] == "csv"
    assert entry["hasSemantics"] is True
    assert entry["hasMcpOverrides"] is False


def test_list_exposes_renders_bigquery_table_reference():
    contract = make_contract(
        exposes=[
            make_expose(
                binding={
                    "platform": "gcp",
                    "format": "bigquery_table",
                    "location": {
                        "project": "my-proj",
                        "dataset": "analytics",
                        "table": "customers",
                    },
                }
            )
        ]
    )
    summary = list_exposes(contract)[0]
    assert summary["tableReference"] == "my-proj.analytics.customers"


def test_list_exposes_renders_snowflake_table_reference():
    contract = make_contract(
        exposes=[
            make_expose(
                binding={
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "database": "PROD",
                        "schema": "ANALYTICS",
                        "table": "CUSTOMERS",
                    },
                }
            )
        ]
    )
    summary = list_exposes(contract)[0]
    assert summary["tableReference"] == "PROD.ANALYTICS.CUSTOMERS"


def test_list_exposes_flags_mcp_overrides():
    expose = make_expose()
    expose["mcp"] = {"sampling": {"maxRows": 25}}
    contract = make_contract(exposes=[expose])
    summary = list_exposes(contract)[0]
    assert summary["hasMcpOverrides"] is True


# ---------------------------------------------------------------------
# resolve_expose_paths — example-friendly relative paths
# ---------------------------------------------------------------------


def test_resolve_expose_paths_resolves_relative_to_contract_dir(tmp_path: Path):
    csv_path = tmp_path / "data" / "customers.csv"
    csv_path.parent.mkdir(parents=True)
    csv_path.write_text("customer_id\nC1\n")
    expose = make_expose(
        binding={
            "platform": "local",
            "format": "csv",
            "location": {"path": "./data/customers.csv", "table": "customer_segments"},
        }
    )
    resolved = resolve_expose_paths(expose, contract_dir=tmp_path)
    assert resolved["binding"]["location"]["path"] == str(csv_path.resolve())


def test_resolve_expose_paths_leaves_absolute_paths_alone(tmp_path: Path):
    expose = make_expose(
        binding={
            "platform": "local",
            "format": "csv",
            "location": {"path": "/absolute/path/customers.csv", "table": "x"},
        }
    )
    resolved = resolve_expose_paths(expose, contract_dir=tmp_path)
    assert resolved["binding"]["location"]["path"] == "/absolute/path/customers.csv"


def test_resolve_expose_paths_no_contract_dir_is_passthrough():
    expose = make_expose()
    assert resolve_expose_paths(expose, contract_dir=None) is expose


def test_resolve_expose_paths_no_path_field_is_passthrough(tmp_path: Path):
    expose = make_expose(
        binding={
            "platform": "snowflake",
            "format": "snowflake_table",
            "location": {"database": "DB", "schema": "S", "table": "T"},
        }
    )
    resolved = resolve_expose_paths(expose, contract_dir=tmp_path)
    assert resolved["binding"]["location"]["database"] == "DB"


# ---------------------------------------------------------------------
# _annotate_engine_error — actionable engine hints
# ---------------------------------------------------------------------


def test_annotate_engine_error_adds_hint_for_snowflake_missing_object():
    expose = make_expose()
    raw = (
        "002003 (42S02): SQL compilation error: Object "
        "'PROD.X.Y' does not exist or not authorized."
    )
    annotated = _annotate_engine_error(RuntimeError(raw), expose=expose)
    assert "Hint:" in annotated
    assert "binding.location" in annotated
    assert raw in annotated


def test_annotate_engine_error_adds_hint_for_duckdb_catalog_error():
    expose = make_expose()
    raw = "Catalog Error: Table with name 'customers' does not exist!"
    annotated = _annotate_engine_error(RuntimeError(raw), expose=expose)
    assert "Hint:" in annotated
    assert "binding.location.path" in annotated


def test_annotate_engine_error_passthrough_when_no_pattern_matches():
    expose = make_expose()
    raw = "Some other engine failure not recognised by the hint table"
    annotated = _annotate_engine_error(RuntimeError(raw), expose=expose)
    assert annotated == raw  # no Hint: added
