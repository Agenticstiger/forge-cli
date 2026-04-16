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

"""Extended tests for providers/aws/codegen/airflow.py.

Covers:
- Redshift / Lambda operator generation
- Glue ensure_table & ensure_job task code
- Helper-function string output (_generate_helper_functions)
- TaskFlow API generator (generate_airflow_dag_taskflow)
- Edge-case contracts (single task, many dependencies, special chars)
"""

import pytest

from fluid_build.providers.aws.codegen.airflow import (
    _convert_schedule,
    _generate_glue_task,
    _generate_helper_functions,
    _generate_single_task,
    _generate_task_dependencies,
    generate_airflow_dag,
    generate_airflow_dag_taskflow,
)

# ── Redshift operator ──────────────────────────────────────────────


class TestGenerateRedshiftTask:
    def test_execute_sql(self):
        task = {
            "taskId": "load_redshift",
            "action": "aws.redshift.execute_sql",
            "params": {
                "sql": "CREATE TABLE t(id INT)",
                "cluster": "my-cluster",
                "database": "analytics",
            },
        }
        result = _generate_single_task(task, "111", "us-east-1")
        assert "RedshiftDataOperator" in result
        assert "my-cluster" in result
        assert "analytics" in result

    def test_unknown_redshift_op_falls_back(self):
        task = {"taskId": "rs", "action": "aws.redshift.resize_cluster", "params": {}}
        result = _generate_single_task(task, "111", "us-east-1")
        assert "PythonOperator" in result


# ── Lambda operator ────────────────────────────────────────────────


class TestGenerateLambdaTask:
    def test_invoke(self):
        task = {
            "taskId": "call_fn",
            "action": "aws.lambda.invoke",
            "params": {"function": "my-func", "payload": {"key": "val"}},
        }
        result = _generate_single_task(task, "111", "us-east-1")
        assert "LambdaInvokeFunctionOperator" in result
        assert "my-func" in result

    def test_unknown_lambda_op_falls_back(self):
        task = {"taskId": "lam", "action": "aws.lambda.create_function", "params": {}}
        result = _generate_single_task(task, "111", "us-east-1")
        assert "PythonOperator" in result


# ── Glue task variants ─────────────────────────────────────────────


class TestGlueTaskVariants:
    def test_ensure_table_contains_params(self):
        task = {
            "taskId": "mk_tbl",
            "action": "aws.glue.ensure_table",
            "params": {"database": "db", "table": "orders", "input_format": "parquet"},
        }
        result = _generate_glue_task(task, "ensure_table", task["params"], "111", "us-west-2")
        assert "PythonOperator" in result
        assert "orders" in result
        assert "us-west-2" in result

    def test_unknown_glue_op_falls_back(self):
        task = {"taskId": "x", "action": "aws.glue.run_crawler", "params": {}}
        result = _generate_glue_task(task, "run_crawler", {}, "111", "us-east-1")
        assert "PythonOperator" in result


# ── _generate_helper_functions ─────────────────────────────────────


class TestGenerateHelperFunctions:
    def test_contains_ensure_glue_database(self):
        code = _generate_helper_functions()
        assert "def _ensure_glue_database" in code

    def test_contains_ensure_glue_table(self):
        code = _generate_helper_functions()
        assert "def _ensure_glue_table" in code

    def test_contains_execute_provider_action(self):
        code = _generate_helper_functions()
        assert "def _execute_provider_action" in code

    def test_table_creation_logic(self):
        code = _generate_helper_functions()
        # Should contain actual table creation, not just a TODO
        assert "create_table" in code or "CreateTable" in code

    def test_provider_action_dispatch(self):
        code = _generate_helper_functions()
        # Should dispatch to concrete modules, not just log
        assert "handler" in code or "getattr" in code


# ── TaskFlow API generator ─────────────────────────────────────────


class TestGenerateAirflowDagTaskflow:
    def test_missing_orchestration_raises(self):
        with pytest.raises(ValueError, match="orchestration"):
            generate_airflow_dag_taskflow({}, "123", "us-east-1")

    def test_empty_tasks_raises(self):
        with pytest.raises(ValueError, match="tasks"):
            generate_airflow_dag_taskflow({"orchestration": {"tasks": []}}, "123", "us-east-1")

    def test_basic_dag_has_decorator(self):
        contract = _make_contract(
            [
                {
                    "taskId": "load",
                    "type": "provider_action",
                    "action": "aws.s3.ensure_bucket",
                    "params": {"bucket": "b"},
                },
            ]
        )
        result = generate_airflow_dag_taskflow(contract, "123", "us-east-1")
        assert "@dag(" in result
        assert "S3CreateBucketOperator" in result

    def test_glue_tasks_use_task_decorator(self):
        contract = _make_contract(
            [
                {
                    "taskId": "mk_db",
                    "type": "provider_action",
                    "action": "aws.glue.ensure_database",
                    "params": {"database": "mydb"},
                },
            ]
        )
        result = generate_airflow_dag_taskflow(contract, "123", "us-east-1")
        assert "@task(" in result
        assert "mk_db" in result

    def test_dependencies_present(self):
        contract = _make_contract(
            [
                {
                    "taskId": "a",
                    "type": "provider_action",
                    "action": "aws.s3.ensure_bucket",
                    "params": {"bucket": "b"},
                },
                {
                    "taskId": "b",
                    "type": "provider_action",
                    "action": "aws.glue.ensure_database",
                    "params": {"database": "d"},
                    "dependsOn": ["a"],
                },
            ]
        )
        result = generate_airflow_dag_taskflow(contract, "123", "us-east-1")
        assert "a >> b" in result

    def test_athena_uses_classic_operator(self):
        contract = _make_contract(
            [
                {
                    "taskId": "query",
                    "type": "provider_action",
                    "action": "aws.athena.execute_query",
                    "params": {"query": "SELECT 1", "database": "db"},
                },
            ]
        )
        result = generate_airflow_dag_taskflow(contract, "123", "us-east-1")
        assert "AthenaOperator" in result

    def test_unknown_action_uses_task_decorator(self):
        contract = _make_contract(
            [
                {
                    "taskId": "custom",
                    "type": "provider_action",
                    "action": "aws.custom.do_thing",
                    "params": {"x": 1},
                },
            ]
        )
        result = generate_airflow_dag_taskflow(contract, "123", "us-east-1")
        assert "@task(" in result

    def test_imports_include_decorators(self):
        contract = _make_contract(
            [
                {
                    "taskId": "t",
                    "type": "provider_action",
                    "action": "aws.s3.ensure_bucket",
                    "params": {"bucket": "b"},
                },
            ]
        )
        result = generate_airflow_dag_taskflow(contract, "123", "us-east-1")
        assert "from airflow.decorators import dag, task" in result

    def test_dag_function_called(self):
        """The generated code should call the DAG function to register it."""
        contract = _make_contract(
            [
                {
                    "taskId": "t",
                    "type": "provider_action",
                    "action": "aws.s3.ensure_bucket",
                    "params": {"bucket": "b"},
                },
            ]
        )
        result = generate_airflow_dag_taskflow(contract, "123", "us-east-1")
        # Last non-empty line should invoke the dag function
        lines = [l for l in result.strip().splitlines() if l.strip()]
        assert lines[-1].endswith("()")


# ── Task dependency edge cases ─────────────────────────────────────


class TestTaskDependencyEdgeCases:
    def test_diamond_dependency(self):
        """A → B, A → C, B → D, C → D."""
        tasks = [
            {"taskId": "A", "dependsOn": []},
            {"taskId": "B", "dependsOn": ["A"]},
            {"taskId": "C", "dependsOn": ["A"]},
            {"taskId": "D", "dependsOn": ["B", "C"]},
        ]
        result = _generate_task_dependencies(tasks)
        assert "A >> B" in result
        assert "A >> C" in result
        assert "B >> D" in result
        assert "C >> D" in result

    def test_special_chars_in_task_ids(self):
        tasks = [
            {"taskId": "step 1!", "dependsOn": []},
            {"taskId": "step@2", "dependsOn": ["step 1!"]},
        ]
        result = _generate_task_dependencies(tasks)
        assert "step_1_" in result
        assert "step_2" in result
        assert ">>" in result


# ── Full DAG edge cases ───────────────────────────────────────────


class TestFullDagEdgeCases:
    def test_single_task_no_deps(self):
        contract = _make_contract(
            [
                {
                    "taskId": "only",
                    "type": "provider_action",
                    "action": "aws.s3.ensure_bucket",
                    "params": {"bucket": "b"},
                },
            ]
        )
        result = generate_airflow_dag(contract, "123", "us-east-1")
        assert "S3CreateBucketOperator" in result
        assert "No dependencies" in result

    def test_schedule_presets_in_dag(self):
        for preset, expected in [
            ("hourly", "0 * * * *"),
            ("weekly", "0 2 * * 0"),
            ("monthly", "0 2 1 * *"),
        ]:
            contract = _make_contract(
                [
                    {
                        "taskId": "t",
                        "type": "provider_action",
                        "action": "aws.s3.ensure_bucket",
                        "params": {"bucket": "b"},
                    }
                ],
                schedule=preset,
            )
            result = generate_airflow_dag(contract, "123", "us-east-1")
            assert expected in result

    def test_custom_cron_passthrough(self):
        contract = _make_contract(
            [
                {
                    "taskId": "t",
                    "type": "provider_action",
                    "action": "aws.s3.ensure_bucket",
                    "params": {"bucket": "b"},
                }
            ],
            schedule="15 3 * * 1-5",
        )
        result = generate_airflow_dag(contract, "123", "us-east-1")
        assert "15 3 * * 1-5" in result

    def test_mixed_task_types_filters_non_provider(self):
        contract = {
            "id": "mix",
            "orchestration": {
                "tasks": [
                    {
                        "taskId": "a",
                        "type": "provider_action",
                        "action": "aws.s3.ensure_bucket",
                        "params": {"bucket": "b"},
                    },
                    {"taskId": "b", "type": "manual", "action": "notify"},
                ],
            },
        }
        result = generate_airflow_dag(contract, "123", "us-east-1")
        assert "S3CreateBucketOperator" in result
        # manual task should NOT appear as operator
        assert (
            "notify" not in result.split("# Task definitions")[1]
            if "# Task definitions" in result
            else True
        )


# ── _convert_schedule edge cases ──────────────────────────────────


class TestConvertScheduleEdgeCases:
    def test_extra_spaces_cron(self):
        # 5+ spaces means "already cron"
        assert _convert_schedule("0 0 * * * *") == "0 0 * * * *"

    def test_empty_string(self):
        assert _convert_schedule("") == ""

    def test_mixed_case(self):
        assert _convert_schedule("Weekly") == "0 2 * * 0"
        assert _convert_schedule("MONTHLY") == "0 2 1 * *"


# ── Helpers ───────────────────────────────────────────────────────


def _make_contract(tasks, schedule="daily", timezone="UTC"):
    return {
        "id": "test-dag",
        "name": "Test DAG",
        "orchestration": {
            "schedule": schedule,
            "timezone": timezone,
            "tasks": tasks,
        },
    }
