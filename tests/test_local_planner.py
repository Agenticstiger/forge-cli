# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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

    def test_the_table_of_the_build_that_names_the_expose(self):
        written_by = {"A": "result_build_a", "B": "result_build_b"}
        for expose_id, table in written_by.items():
            result = _determine_source_table(
                {"exposeId": expose_id},
                [],
                set(written_by.values()),
                self._logger(),
                written_by=written_by,
            )
            assert result == table  # never "the last build's table" for both

    def test_match_by_expose_id(self):
        expose = {"exposeId": "my_output"}
        loaded = {"my_output", "other"}
        result = _determine_source_table(expose, [], loaded, self._logger())
        assert result == "my_output"

    def test_an_expose_no_build_names_gets_no_table(self):
        # The first loaded table, sorted, used to be landed under its name.
        loaded = {"z_table", "a_table"}
        result = _determine_source_table(
            {"exposeId": "orphan"}, [], loaded, self._logger(), written_by={"other": "a_table"}
        )
        assert result is None

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

    def test_materialize_a_build_lands_needs_no_source(self):
        payload = {"path": "out.parquet", "landed_by": ["ingest"]}
        actions = [{"op": "materialize", "resource_id": "e1", "payload": payload}]
        assert validate_plan(actions) == (True, [])

    def test_landed_by_still_needs_an_output_path(self):
        actions = [{"op": "materialize", "resource_id": "e1", "payload": {"landed_by": ["ingest"]}}]
        assert validate_plan(actions) == (False, ["e1: materialize missing output path"])

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


# ── plan_actions: each build lands in the expose its outputs name ─────
def _two_build_contract(*, a_id: str = "build_a", b_id: str = "build_b") -> dict:
    def build(build_id: str, sql: str, out: str) -> dict:
        return {
            "id": build_id,
            "engine": "sql",
            "pattern": "embedded-logic",
            "properties": {"sql": sql},
            "outputs": [out],
        }

    def expose(expose_id: str) -> dict:
        return {
            "exposeId": expose_id,
            "binding": {
                "platform": "local",
                "format": "csv",
                "location": {"path": f"out/{expose_id}.csv"},
            },
        }

    return {
        "id": "repro.two_builds",
        "builds": [build(a_id, "SELECT 1 AS a", "A"), build(b_id, "SELECT 2 AS b", "B")],
        "exposes": [expose("A"), expose("B")],
    }


class TestPlanActionsMultipleBuilds:
    def _plan(self, contract: dict) -> list:
        from fluid_build.providers.local.planner import plan_actions

        return plan_actions(contract, logger=logging.getLogger("test"))

    def test_each_expose_is_materialized_from_its_own_builds_table(self):
        actions = self._plan(_two_build_contract())
        landed = {
            a["resource_id"]: a["payload"]["source_table"]
            for a in actions
            if a["op"] == "materialize"
        }
        assert landed == {"A": "result_build_a", "B": "result_build_b"}

    def test_a_later_build_registers_no_file_for_an_earlier_builds_table(self):
        # "Input file not found: result_build_a": the implicit dependency was
        # listed as an input to register.
        actions = self._plan(_two_build_contract())
        sql = {a["resource_id"]: a for a in actions if a["op"] == "execute_sql"}
        assert sql["build_b"]["payload"]["inputs"] == []
        assert (
            "build_a" in sql["build_b"]["depends_on"]
            or "result_build_a" in sql["build_b"]["depends_on"]
        )

    def test_a_hyphenated_build_id_keeps_its_result_in_a_safe_table_name(self):
        from fluid_build.providers._sql_safety import validate_ident

        actions = self._plan(_two_build_contract(a_id="hello-transformation"))
        tables = [a["table_name"] for a in actions if a["op"] == "execute_sql"]
        assert tables[0] == "result_hello_transformation"
        assert all(validate_ident(t) for t in tables)

    def test_two_build_ids_that_map_to_one_table_are_refused(self):
        import pytest

        from fluid_build.util.contract import BuildOutputsError

        with pytest.raises(BuildOutputsError) as refused:
            self._plan(_two_build_contract(a_id="a-b", b_id="a_b"))
        assert refused.value.code == "build_ids_collide"

    def test_an_unknown_output_refuses_the_plan(self):
        import pytest

        from fluid_build.util.contract import BuildOutputsError

        contract = _two_build_contract()
        contract["builds"][1]["outputs"] = ["Z"]
        with pytest.raises(BuildOutputsError) as refused:
            self._plan(contract)
        assert refused.value.code == "build_output_unknown_expose"

    def test_an_expose_no_build_names_is_not_given_another_builds_table(self):
        contract = _two_build_contract()
        contract["builds"][1]["outputs"] = ["A"]  # nothing writes B
        actions = self._plan(contract)
        landed = {
            a["resource_id"]: a["payload"]["source_table"]
            for a in actions
            if a["op"] == "materialize"
        }
        assert landed == {"A": "result_build_b", "B": None}  # the last build to name A wins
