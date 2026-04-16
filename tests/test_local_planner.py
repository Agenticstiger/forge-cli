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

"""Tests for providers/local/planner.py — format inference, topo sort, validation, plan helpers."""

import logging

from fluid_build.providers.local.planner import (
    _determine_source_table,
    _extract_input_tables,
    _extract_sql,
    _infer_format,
    _topological_sort,
    validate_plan,
)


# ── _infer_format ────────────────────────────────────────────────────
class TestInferFormat:
    def test_csv(self):
        assert _infer_format("data.csv") == "csv"

    def test_tsv(self):
        assert _infer_format("data.tsv") == "tsv"

    def test_parquet(self):
        assert _infer_format("data.parquet") == "parquet"

    def test_pq_extension(self):
        assert _infer_format("data.pq") == "parquet"

    def test_json(self):
        assert _infer_format("data.json") == "json"

    def test_jsonl(self):
        assert _infer_format("data.jsonl") == "jsonl"

    def test_unknown_defaults_csv(self):
        assert _infer_format("data.xml") == "csv"

    def test_explicit_overrides(self):
        assert _infer_format("data.csv", "parquet") == "parquet"

    def test_explicit_lowered(self):
        assert _infer_format("data.csv", "PARQUET") == "parquet"

    def test_case_insensitive_path(self):
        assert _infer_format("DATA.CSV") == "csv"


# ── _extract_input_tables ───────────────────────────────────────────
class TestExtractInputTables:
    def test_from_properties_parameters(self):
        build = {"properties": {"parameters": {"inputs": [{"name": "t1"}, {"name": "t2"}]}}}
        result = _extract_input_tables(build, {}, {"t1", "t2"})
        assert result == {"t1", "t2"}

    def test_string_inputs(self):
        build = {"properties": {"parameters": {"inputs": ["t1", "t2"]}}}
        result = _extract_input_tables(build, {}, {"t1", "t2"})
        assert result == {"t1", "t2"}

    def test_from_build_inputs(self):
        build = {"inputs": [{"name": "t1"}, "t2"]}
        result = _extract_input_tables(build, {}, set())
        assert result == {"t1", "t2"}

    def test_fallback_all_loaded(self):
        build = {}
        loaded = {"a", "b"}
        result = _extract_input_tables(build, {}, loaded)
        assert result == loaded

    def test_empty_everything(self):
        result = _extract_input_tables({}, {}, set())
        assert result == set()


# ── _determine_source_table ──────────────────────────────────────────
class TestDetermineSourceTable:
    def _logger(self):
        return logging.getLogger("test")

    def test_explicit_source_table(self):
        expose = {"source_table": "my_table"}
        result = _determine_source_table(expose, [], set(), self._logger())
        assert result == "my_table"

    def test_explicit_source(self):
        expose = {"source": "src"}
        result = _determine_source_table(expose, [], set(), self._logger())
        assert result == "src"

    def test_last_build_output(self):
        builds = [{"id": "b1", "output_table": "result_b1"}]
        loaded = {"result_b1"}
        result = _determine_source_table({}, builds, loaded, self._logger())
        assert result == "result_b1"

    def test_match_by_expose_id(self):
        expose = {"id": "my_output"}
        loaded = {"my_output", "other"}
        result = _determine_source_table(expose, [], loaded, self._logger())
        assert result == "my_output"

    def test_fallback_first_sorted(self):
        loaded = {"z_table", "a_table"}
        result = _determine_source_table({}, [], loaded, self._logger())
        assert result == "a_table"

    def test_no_tables_returns_none(self):
        result = _determine_source_table({}, [], set(), self._logger())
        assert result is None


# ── _topological_sort ────────────────────────────────────────────────
class TestTopologicalSort:
    def _logger(self):
        return logging.getLogger("test")

    def test_simple_chain(self):
        actions = [
            {"resource_id": "a", "op": "load"},
            {"resource_id": "b", "op": "transform"},
            {"resource_id": "c", "op": "materialize"},
        ]
        deps = {"a": set(), "b": {"a"}, "c": {"b"}}
        result = _topological_sort(actions, deps, self._logger())
        ids = [a["resource_id"] for a in result]
        assert ids.index("a") < ids.index("b") < ids.index("c")

    def test_parallel_independent(self):
        actions = [
            {"resource_id": "a", "op": "load"},
            {"resource_id": "b", "op": "load"},
        ]
        deps = {"a": set(), "b": set()}
        result = _topological_sort(actions, deps, self._logger())
        assert len(result) == 2

    def test_cycle_handled_gracefully(self):
        actions = [
            {"resource_id": "a", "op": "x"},
            {"resource_id": "b", "op": "x"},
        ]
        deps = {"a": {"b"}, "b": {"a"}}
        result = _topological_sort(actions, deps, self._logger())
        assert len(result) == 2  # Both included despite cycle

    def test_empty(self):
        result = _topological_sort([], {}, self._logger())
        assert result == []

    def test_actions_without_resource_id_appended(self):
        actions = [
            {"resource_id": "a", "op": "load"},
            {"op": "unknown"},  # No resource_id
        ]
        deps = {"a": set()}
        result = _topological_sort(actions, deps, self._logger())
        assert len(result) == 2


# ── _extract_sql ─────────────────────────────────────────────────────
class TestExtractSql:
    def _logger(self):
        return logging.getLogger("test")

    def test_inline_sql(self):
        build = {"properties": {"sql": "SELECT 1"}}
        assert _extract_sql(build, {}, self._logger()) == "SELECT 1"

    def test_inline_whitespace_stripped(self):
        build = {"properties": {"sql": "  SELECT 1  "}}
        assert _extract_sql(build, {}, self._logger()) == "SELECT 1"

    def test_simple_sql(self):
        build = {"sql": "SELECT 2"}
        assert _extract_sql(build, {}, self._logger()) == "SELECT 2"

    def test_empty_inline_falls_through(self):
        build = {"properties": {"sql": "  "}, "sql": "SELECT 3"}
        assert _extract_sql(build, {}, self._logger()) == "SELECT 3"

    def test_no_sql_returns_none(self):
        assert _extract_sql({}, {}, self._logger()) is None

    def test_model_file_missing(self):
        build = {"transformation": {"properties": {"model": "/nonexistent/model.sql"}}}
        result = _extract_sql(build, {}, self._logger())
        # Falls through to None (file doesn't exist)
        assert result is None


# ── validate_plan ────────────────────────────────────────────────────
class TestValidatePlan:
    def test_valid_plan(self):
        actions = [
            {"op": "load_data", "resource_id": "t1", "payload": {"path": "data.csv"}},
            {"op": "execute_sql", "resource_id": "b1", "payload": {"sql": "SELECT 1"}},
            {
                "op": "materialize",
                "resource_id": "e1",
                "payload": {"path": "out.csv", "source_table": "t1"},
            },
        ]
        valid, errors = validate_plan(actions)
        assert valid is True
        assert errors == []

    def test_load_data_missing_path(self):
        actions = [{"op": "load_data", "resource_id": "t1", "payload": {}}]
        valid, errors = validate_plan(actions)
        assert valid is False
        assert len(errors) == 1
        assert "path" in errors[0].lower()

    def test_execute_sql_missing_sql(self):
        actions = [{"op": "execute_sql", "resource_id": "b1", "payload": {}}]
        valid, errors = validate_plan(actions)
        assert valid is False

    def test_execute_sql_empty_sql(self):
        actions = [{"op": "execute_sql", "resource_id": "b1", "payload": {"sql": "  "}}]
        valid, errors = validate_plan(actions)
        assert valid is False

    def test_materialize_missing_path(self):
        actions = [{"op": "materialize", "resource_id": "e1", "payload": {"source_table": "t1"}}]
        valid, errors = validate_plan(actions)
        assert valid is False

    def test_materialize_missing_source(self):
        actions = [{"op": "materialize", "resource_id": "e1", "payload": {"path": "out.csv"}}]
        valid, errors = validate_plan(actions)
        assert valid is False

    def test_multiple_errors(self):
        actions = [
            {"op": "load_data", "resource_id": "t1", "payload": {}},
            {"op": "materialize", "resource_id": "e1", "payload": {}},
        ]
        valid, errors = validate_plan(actions)
        assert valid is False
        assert len(errors) == 3  # path + path + source_table

    def test_empty_plan(self):
        valid, errors = validate_plan([])
        assert valid is True
        assert errors == []
