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

"""Tests for providers/aws/codegen/airflow.py — pure codegen helpers."""

import pytest

from fluid_build.providers.aws.codegen.airflow import (
    _convert_schedule,
    _generate_dag_definition,
    _generate_dag_header,
    _generate_dag_imports,
    _generate_python_task,
    _generate_single_task,
    _generate_task_dependencies,
    _sanitize_dag_id,
    _sanitize_task_id,
    generate_airflow_dag,
)


# ── _sanitize_dag_id ────────────────────────────────────────────────
class TestSanitizeDagId:
    def test_clean_id(self):
        assert _sanitize_dag_id("my-dag_1.0") == "my-dag_1.0"

    def test_spaces_replaced(self):
        assert _sanitize_dag_id("my dag") == "my_dag"

    def test_special_chars(self):
        assert _sanitize_dag_id("a@b#c") == "a_b_c"

    def test_slashes(self):
        assert _sanitize_dag_id("org/proj") == "org_proj"

    def test_empty(self):
        assert _sanitize_dag_id("") == ""


# ── _sanitize_task_id ───────────────────────────────────────────────
class TestSanitizeTaskId:
    def test_delegates_to_dag_id(self):
        assert _sanitize_task_id("my task!") == _sanitize_dag_id("my task!")


# ── _convert_schedule ───────────────────────────────────────────────
class TestConvertSchedule:
    def test_daily(self):
        assert _convert_schedule("daily") == "0 2 * * *"

    def test_hourly(self):
        assert _convert_schedule("hourly") == "0 * * * *"

    def test_weekly(self):
        assert _convert_schedule("weekly") == "0 2 * * 0"

    def test_monthly(self):
        assert _convert_schedule("monthly") == "0 2 1 * *"

    def test_cron_passthrough(self):
        assert _convert_schedule("0 3 * * *") == "0 3 * * *"

    def test_unknown_passthrough(self):
        assert _convert_schedule("every_5_min") == "every_5_min"

    def test_case_insensitive(self):
        assert _convert_schedule("DAILY") == "0 2 * * *"


# ── _generate_dag_header ───────────────────────────────────────────
class TestGenerateDagHeader:
    def test_contains_metadata(self):
        header = _generate_dag_header("cid", "cname", "daily", "UTC", "123456", "us-east-1")
        assert "cid" in header
        assert "cname" in header
        assert "daily" in header
        assert "123456" in header
        assert "us-east-1" in header
        assert "DO NOT EDIT" in header


# ── _generate_dag_imports ──────────────────────────────────────────
class TestGenerateDagImports:
    def test_contains_airflow(self):
        result = _generate_dag_imports()
        assert "from airflow import DAG" in result
        assert "PythonOperator" in result
        assert "S3CreateBucketOperator" in result


# ── _generate_dag_definition ─────────────────────────────────────────
class TestGenerateDagDefinition:
    def test_contains_dag_id(self):
        result = _generate_dag_definition("my-dag", "My DAG", "0 2 * * *", "UTC")
        assert "my-dag" in result
        assert "My DAG" in result
        assert "owner" in result


# ── _generate_task_dependencies ──────────────────────────────────────
class TestGenerateTaskDependencies:
    def test_with_dependencies(self):
        tasks = [
            {"taskId": "t1", "dependsOn": []},
            {"taskId": "t2", "dependsOn": ["t1"]},
        ]
        result = _generate_task_dependencies(tasks)
        assert "t1 >> t2" in result

    def test_no_dependencies(self):
        tasks = [{"taskId": "t1"}]
        result = _generate_task_dependencies(tasks)
        assert "No dependencies" in result

    def test_empty_tasks(self):
        result = _generate_task_dependencies([])
        assert "No dependencies" in result


# ── _generate_single_task ───────────────────────────────────────────
class TestGenerateSingleTask:
    def test_s3_task(self):
        task = {
            "taskId": "create_bucket",
            "action": "aws.s3.ensure_bucket",
            "params": {"bucket": "my-bucket"},
        }
        result = _generate_single_task(task, "123", "us-east-1")
        assert "S3CreateBucketOperator" in result
        assert "my-bucket" in result

    def test_glue_ensure_database(self):
        task = {
            "taskId": "create_db",
            "action": "aws.glue.ensure_database",
            "params": {"database": "mydb"},
        }
        result = _generate_single_task(task, "123", "us-east-1")
        assert "mydb" in result

    def test_glue_ensure_table(self):
        task = {
            "taskId": "create_tbl",
            "action": "aws.glue.ensure_table",
            "params": {"database": "mydb", "table": "tbl"},
        }
        result = _generate_single_task(task, "123", "us-east-1")
        assert "mydb" in result
        assert "tbl" in result

    def test_athena_task(self):
        task = {
            "taskId": "run_query",
            "action": "aws.athena.execute_query",
            "params": {"query": "SELECT 1", "database": "mydb"},
        }
        result = _generate_single_task(task, "123", "us-east-1")
        assert "AthenaOperator" in result

    def test_unknown_service_fallback(self):
        task = {"taskId": "custom", "action": "aws.custom.do_thing", "params": {}}
        result = _generate_single_task(task, "123", "us-east-1")
        assert "PythonOperator" in result

    def test_short_action_fallback(self):
        task = {"taskId": "short", "action": "custom", "params": {}}
        result = _generate_single_task(task, "123", "us-east-1")
        assert "PythonOperator" in result


# ── _generate_python_task ───────────────────────────────────────────
class TestGeneratePythonTask:
    def test_basic(self):
        task = {"taskId": "my_task", "action": "custom.action", "params": {"key": "val"}}
        result = _generate_python_task(task, "123", "us-east-1")
        assert "PythonOperator" in result
        assert "my_task" in result


# ── generate_airflow_dag (integration) ──────────────────────────────
class TestGenerateAirflowDag:
    def test_missing_orchestration_raises(self):
        with pytest.raises(ValueError, match="orchestration"):
            generate_airflow_dag({}, "123", "us-east-1")

    def test_empty_tasks_raises(self):
        with pytest.raises(ValueError, match="tasks"):
            generate_airflow_dag({"orchestration": {"tasks": []}}, "123", "us-east-1")

    def test_full_dag_generation(self):
        contract = {
            "id": "btc-tracker",
            "name": "Bitcoin Price Tracker",
            "orchestration": {
                "schedule": "daily",
                "timezone": "UTC",
                "tasks": [
                    {
                        "taskId": "load",
                        "type": "provider_action",
                        "action": "aws.s3.ensure_bucket",
                        "params": {"bucket": "data-bucket"},
                    },
                    {
                        "taskId": "transform",
                        "type": "provider_action",
                        "action": "aws.glue.ensure_database",
                        "params": {"database": "analytics"},
                        "dependsOn": ["load"],
                    },
                ],
            },
        }
        result = generate_airflow_dag(contract, "123456789", "us-west-2")
        assert "btc-tracker" in result
        assert "Bitcoin Price Tracker" in result
        assert "S3CreateBucketOperator" in result
        assert "load >> transform" in result

    def test_no_provider_action_tasks(self):
        contract = {
            "orchestration": {
                "tasks": [{"taskId": "t1", "type": "manual", "action": "do.something"}],
            },
        }
        result = generate_airflow_dag(contract, "123", "us-east-1")
        # Should still produce a DAG with header/imports, just no task defs
        assert "from airflow import DAG" in result
