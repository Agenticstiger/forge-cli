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

"""Tests for providers/aws/plan/orchestration.py — task planning, DAG validation, topo sort."""

import pytest

from fluid_build.providers.aws.plan.orchestration import (
    OrchestrationError,
    OrchestrationPlanner,
    get_task_execution_order,
    plan_orchestration_tasks,
)


# ── OrchestrationPlanner init ────────────────────────────────────────
class TestOrchestrationPlannerInit:
    def test_stores_config(self):
        p = OrchestrationPlanner("123", "us-east-1")
        assert p.account_id == "123"
        assert p.region == "us-east-1"


# ── plan_orchestration_actions ───────────────────────────────────────
class TestPlanOrchestrationActions:
    def test_no_orchestration(self):
        p = OrchestrationPlanner("123", "us-east-1")
        assert p.plan_orchestration_actions({}) == []

    def test_no_tasks(self):
        p = OrchestrationPlanner("123", "us-east-1")
        assert p.plan_orchestration_actions({"orchestration": {}}) == []

    def test_no_provider_action_tasks(self):
        p = OrchestrationPlanner("123", "us-east-1")
        contract = {"orchestration": {"tasks": [{"taskId": "t1", "type": "manual"}]}}
        assert p.plan_orchestration_actions(contract) == []

    def test_basic_task(self):
        p = OrchestrationPlanner("123", "us-east-1")
        contract = {
            "id": "test",
            "orchestration": {
                "tasks": [
                    {
                        "taskId": "create_bucket",
                        "type": "provider_action",
                        "action": "aws.s3.ensure_bucket",
                        "params": {"bucket": "my-bucket"},
                    }
                ],
            },
        }
        actions = p.plan_orchestration_actions(contract)
        assert len(actions) == 1
        assert actions[0]["id"] == "create_bucket"
        assert actions[0]["op"] == "s3.ensure_bucket"
        assert actions[0]["bucket"] == "my-bucket"

    def test_glue_task_has_contract_metadata(self):
        p = OrchestrationPlanner("123", "us-east-1")
        contract = {
            "id": "cid",
            "name": "cname",
            "orchestration": {
                "tasks": [
                    {
                        "taskId": "t1",
                        "type": "provider_action",
                        "action": "aws.glue.ensure_table",
                        "params": {"database": "db", "table": "tbl"},
                    }
                ],
            },
        }
        actions = p.plan_orchestration_actions(contract)
        assert actions[0]["contract_id"] == "cid"
        assert actions[0]["contract_name"] == "cname"


# ── _validate_dependencies ──────────────────────────────────────────
class TestValidateDependencies:
    def test_valid_deps(self):
        p = OrchestrationPlanner("123", "us-east-1")
        tasks = [
            {"taskId": "t1", "dependsOn": []},
            {"taskId": "t2", "dependsOn": ["t1"]},
        ]
        p._validate_dependencies(tasks)  # Should not raise

    def test_missing_dependency(self):
        p = OrchestrationPlanner("123", "us-east-1")
        tasks = [
            {"taskId": "t1", "dependsOn": ["nonexistent"]},
        ]
        with pytest.raises(OrchestrationError, match="non-existent"):
            p._validate_dependencies(tasks)

    def test_self_dependency(self):
        p = OrchestrationPlanner("123", "us-east-1")
        tasks = [
            {"taskId": "t1", "dependsOn": ["t1"]},
        ]
        with pytest.raises(OrchestrationError, match="circular"):
            p._validate_dependencies(tasks)

    def test_cycle_detected(self):
        p = OrchestrationPlanner("123", "us-east-1")
        tasks = [
            {"taskId": "a", "dependsOn": ["b"]},
            {"taskId": "b", "dependsOn": ["a"]},
        ]
        with pytest.raises(OrchestrationError, match="[Cc]ircular"):
            p._validate_dependencies(tasks)


# ── _task_to_action ─────────────────────────────────────────────────
class TestTaskToAction:
    def test_valid_action(self):
        p = OrchestrationPlanner("123", "us-east-1")
        task = {"taskId": "t1", "action": "aws.s3.ensure_bucket", "params": {"bucket": "b"}}
        action = p._task_to_action(task, {"orchestration": {"engine": "airflow"}})
        assert action["id"] == "t1"
        assert action["op"] == "s3.ensure_bucket"
        assert action["bucket"] == "b"
        assert action["orchestration"]["engine"] == "airflow"

    def test_missing_task_id(self):
        p = OrchestrationPlanner("123", "us-east-1")
        with pytest.raises(OrchestrationError, match="taskId"):
            p._task_to_action({}, {})

    def test_missing_action(self):
        p = OrchestrationPlanner("123", "us-east-1")
        with pytest.raises(OrchestrationError, match="action"):
            p._task_to_action({"taskId": "t1"}, {})

    def test_invalid_action_format(self):
        p = OrchestrationPlanner("123", "us-east-1")
        with pytest.raises(OrchestrationError, match="Invalid action format"):
            p._task_to_action({"taskId": "t1", "action": "short"}, {})

    def test_non_aws_provider(self):
        p = OrchestrationPlanner("123", "us-east-1")
        with pytest.raises(OrchestrationError, match="Unsupported provider"):
            p._task_to_action({"taskId": "t1", "action": "gcp.bq.query"}, {})


# ── get_execution_order ─────────────────────────────────────────────
class TestGetExecutionOrder:
    def test_linear_chain(self):
        p = OrchestrationPlanner("123", "us-east-1")
        tasks = [
            {"taskId": "a", "dependsOn": []},
            {"taskId": "b", "dependsOn": ["a"]},
            {"taskId": "c", "dependsOn": ["b"]},
        ]
        order = p.get_execution_order(tasks)
        assert order == ["a", "b", "c"]

    def test_parallel_independent(self):
        p = OrchestrationPlanner("123", "us-east-1")
        tasks = [
            {"taskId": "b", "dependsOn": []},
            {"taskId": "a", "dependsOn": []},
        ]
        order = p.get_execution_order(tasks)
        # Both independent — sorted deterministically
        assert order == ["a", "b"]

    def test_diamond(self):
        p = OrchestrationPlanner("123", "us-east-1")
        tasks = [
            {"taskId": "a", "dependsOn": []},
            {"taskId": "b", "dependsOn": ["a"]},
            {"taskId": "c", "dependsOn": ["a"]},
            {"taskId": "d", "dependsOn": ["b", "c"]},
        ]
        order = p.get_execution_order(tasks)
        assert order.index("a") == 0
        assert order.index("d") == 3

    def test_empty(self):
        p = OrchestrationPlanner("123", "us-east-1")
        assert p.get_execution_order([]) == []


# ── Convenience functions ────────────────────────────────────────────
class TestConvenienceFunctions:
    def test_plan_orchestration_tasks_empty(self):
        assert plan_orchestration_tasks({}, "123", "us-east-1") == []

    def test_get_task_execution_order(self):
        contract = {
            "orchestration": {
                "tasks": [
                    {"taskId": "a", "type": "provider_action", "dependsOn": []},
                    {"taskId": "b", "type": "provider_action", "dependsOn": ["a"]},
                ],
            },
        }
        order = get_task_execution_order(contract)
        assert order == ["a", "b"]
