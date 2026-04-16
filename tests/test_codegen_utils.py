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

"""Tests for providers/common/codegen_utils.py — shared codegen helpers."""

import pytest

from fluid_build.providers.common.codegen_utils import (
    calculate_code_metrics,
    convert_schedule_to_airflow,
    convert_schedule_to_cron,
    detect_circular_dependencies,
    escape_sql_for_python,
    generate_file_header,
    generate_task_dependencies_code,
    sanitize_identifier,
    validate_contract_for_export,
)


# ── sanitize_identifier ─────────────────────────────────────────────
class TestSanitizeIdentifier:
    def test_basic(self):
        assert sanitize_identifier("my_table") == "my_table"

    def test_replaces_special_chars(self):
        assert sanitize_identifier("my-table.v2") == "my_table_v2"

    def test_strips_leading_digits(self):
        assert sanitize_identifier("123abc") == "abc"

    def test_special_chars_become_underscores(self):
        assert sanitize_identifier("!!!") == "___"

    def test_purely_numeric(self):
        # Leading digits stripped, leaving empty → 'unnamed'
        assert sanitize_identifier("999") == "unnamed"


# ── convert_schedule_to_cron ─────────────────────────────────────────
class TestConvertScheduleToCron:
    @pytest.mark.parametrize(
        "keyword,expected",
        [
            ("@hourly", "0 * * * *"),
            ("@daily", "0 0 * * *"),
            ("@weekly", "0 0 * * 0"),
            ("@monthly", "0 0 1 * *"),
            ("@yearly", "0 0 1 1 *"),
            ("@annually", "0 0 1 1 *"),
        ],
    )
    def test_keywords(self, keyword, expected):
        assert convert_schedule_to_cron(keyword) == expected

    def test_keyword_case_insensitive(self):
        assert convert_schedule_to_cron("@DAILY") == "0 0 * * *"

    def test_passthrough_cron(self):
        assert convert_schedule_to_cron("5 4 * * *") == "5 4 * * *"

    def test_default_fallback(self):
        assert convert_schedule_to_cron("every 6 hours") == "0 2 * * *"


# ── convert_schedule_to_airflow ──────────────────────────────────────
class TestConvertScheduleToAirflow:
    @pytest.mark.parametrize("kw", ["@hourly", "@daily", "@weekly", "@monthly", "@yearly", "@once"])
    def test_native_keywords(self, kw):
        assert convert_schedule_to_airflow(kw) == kw

    def test_passthrough_cron(self):
        assert convert_schedule_to_airflow("0 12 * * 1-5") == "0 12 * * 1-5"

    def test_default(self):
        assert convert_schedule_to_airflow("garbage") == "@daily"


# ── generate_file_header ─────────────────────────────────────────────
class TestGenerateFileHeader:
    def test_basic_header(self):
        hdr = generate_file_header("c001", "My Pipeline", "gcp")
        assert "Contract ID: c001" in hdr
        assert "Provider: GCP" in hdr
        assert "My Pipeline" in hdr
        assert "DO NOT EDIT MANUALLY" in hdr

    def test_extra_kwargs(self):
        hdr = generate_file_header("c1", "p", "aws", version="1.0", owner="team-x")
        assert "Owner: team-x" in hdr
        assert "Version: 1.0" in hdr

    def test_none_kwargs_excluded(self):
        hdr = generate_file_header("c1", "p", "aws", version=None)
        assert "Version" not in hdr


# ── escape_sql_for_python ────────────────────────────────────────────
class TestEscapeSqlForPython:
    def test_backslashes(self):
        assert escape_sql_for_python("a\\b") == "a\\\\b"

    def test_double_quotes(self):
        assert escape_sql_for_python('say "hi"') == 'say \\"hi\\"'

    def test_triple_quotes(self):
        result = escape_sql_for_python('x"""y')
        assert '"""' not in result

    def test_plain(self):
        assert escape_sql_for_python("SELECT 1") == "SELECT 1"


# ── generate_task_dependencies_code ──────────────────────────────────
class TestGenerateTaskDependenciesCode:
    def test_empty_tasks(self):
        assert generate_task_dependencies_code([]) == "# No task dependencies"

    def test_single_dep_airflow(self):
        tasks = [{"taskId": "t2", "dependsOn": ["t1"]}]
        code = generate_task_dependencies_code(tasks, "airflow")
        assert "t1 >> t2" in code

    def test_multi_dep_airflow(self):
        tasks = [{"taskId": "t3", "dependsOn": ["t1", "t2"]}]
        code = generate_task_dependencies_code(tasks, "airflow")
        assert "[t1, t2] >> t3" in code

    def test_no_deps_skipped(self):
        tasks = [{"taskId": "t1", "dependsOn": []}]
        code = generate_task_dependencies_code(tasks, "airflow")
        assert "t1" not in code or "# Task" in code

    def test_dagster_passthrough(self):
        tasks = [{"taskId": "t2", "dependsOn": ["t1"]}]
        code = generate_task_dependencies_code(tasks, "dagster")
        assert ">>" not in code

    def test_prefect_passthrough(self):
        tasks = [{"taskId": "t2", "dependsOn": ["t1"]}]
        code = generate_task_dependencies_code(tasks, "prefect")
        assert ">>" not in code


# ── validate_contract_for_export ─────────────────────────────────────
class TestValidateContractForExport:
    def test_missing_orchestration(self):
        with pytest.raises(ValueError, match="orchestration"):
            validate_contract_for_export({})

    def test_no_tasks(self):
        with pytest.raises(ValueError, match="tasks"):
            validate_contract_for_export({"orchestration": {"other": True}})

    def test_empty_tasks_list(self):
        with pytest.raises(ValueError, match="[Nn]o.*tasks|non-empty"):
            validate_contract_for_export({"orchestration": {"tasks": []}})

    def test_duplicate_task_ids(self):
        tasks = [{"taskId": "t1"}, {"taskId": "t1"}]
        with pytest.raises(ValueError, match="Duplicate"):
            validate_contract_for_export({"orchestration": {"tasks": tasks}})

    def test_dependency_on_nonexistent(self):
        tasks = [{"taskId": "t1", "dependsOn": ["t99"]}]
        with pytest.raises(ValueError, match="non-existent"):
            validate_contract_for_export({"orchestration": {"tasks": tasks}})

    def test_valid_contract(self):
        tasks = [{"taskId": "t1"}, {"taskId": "t2", "dependsOn": ["t1"]}]
        validate_contract_for_export({"orchestration": {"tasks": tasks}})


# ── detect_circular_dependencies ─────────────────────────────────────
class TestDetectCircularDependencies:
    def test_no_cycles(self):
        tasks = [{"taskId": "a"}, {"taskId": "b", "dependsOn": ["a"]}]
        assert detect_circular_dependencies(tasks) == []

    def test_simple_cycle(self):
        tasks = [
            {"taskId": "a", "dependsOn": ["b"]},
            {"taskId": "b", "dependsOn": ["a"]},
        ]
        result = detect_circular_dependencies(tasks)
        assert len(result) > 0

    def test_self_cycle(self):
        tasks = [{"taskId": "x", "dependsOn": ["x"]}]
        result = detect_circular_dependencies(tasks)
        assert "x" in result


# ── calculate_code_metrics ───────────────────────────────────────────
class TestCalculateCodeMetrics:
    def test_basic(self):
        code = '# header\ndef foo():\n    """doc"""\n    pass\n\n'
        m = calculate_code_metrics(code)
        assert m["line_count"] == 6
        assert m["non_empty_lines"] == 4
        assert m["comment_lines"] == 1
        assert m["function_count"] == 1
        assert m["class_count"] == 0
        assert m["byte_size"] > 0

    def test_empty(self):
        m = calculate_code_metrics("")
        assert m["line_count"] == 1  # split gives ['']
        assert m["non_empty_lines"] == 0
