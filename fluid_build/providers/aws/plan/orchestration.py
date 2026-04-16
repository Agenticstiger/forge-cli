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

"""
Orchestration Planning for AWS Provider (FLUID 0.7.1).

NEW in FLUID 0.7.1: Provider-first orchestration with type: provider_action tasks.

This module parses orchestration.tasks from FLUID contracts and maps them to
AWS provider actions. Supports Airflow, Dagster, Prefect, and custom orchestration
engines with direct provider action invocation.

Example contract:
    {
        "orchestration": {
            "engine": "airflow",
            "schedule": "0 2 * * *",
            "tasks": [
                {
                    "taskId": "create_bucket",
                    "type": "provider_action",
                    "action": "aws.s3.ensure_bucket",
                    "params": {
                        "bucket": "my-data-lake",
                        "versioning": true
                    }
                },
                {
                    "taskId": "create_table",
                    "type": "provider_action",
                    "action": "aws.glue.ensure_table",
                    "dependsOn": ["create_bucket"],
                    "params": {
                        "database": "analytics",
                        "table": "transactions"
                    }
                }
            ]
        }
    }
"""

import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class OrchestrationError(Exception):
    """Raised when orchestration planning fails."""

    pass


class OrchestrationPlanner:
    """
    Plans orchestration tasks from FLUID contracts.

    Converts orchestration.tasks with type: provider_action into
    concrete AWS provider actions.
    """

    def __init__(self, account_id: str, region: str, logger: Optional[logging.Logger] = None):
        """
        Initialize orchestration planner.

        Args:
            account_id: AWS account ID
            region: AWS region
            logger: Optional logger
        """
        self.account_id = account_id
        self.region = region
        self.logger = logger or logging.getLogger(__name__)

    def plan_orchestration_actions(self, contract: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Generate AWS actions from orchestration.tasks.

        Args:
            contract: FLUID contract with orchestration section

        Returns:
            List of AWS provider actions

        Raises:
            OrchestrationError: If orchestration planning fails
        """
        orchestration = contract.get("orchestration")
        if not orchestration:
            return []

        tasks = orchestration.get("tasks", [])
        if not tasks:
            self.logger.debug("No orchestration tasks found")
            return []

        # Extract provider action tasks
        provider_action_tasks = [task for task in tasks if task.get("type") == "provider_action"]

        if not provider_action_tasks:
            self.logger.debug("No provider_action tasks found in orchestration")
            return []

        # Validate dependencies
        self._validate_dependencies(provider_action_tasks)

        # Convert tasks to actions
        actions = []
        for task in provider_action_tasks:
            try:
                action = self._task_to_action(task, contract)
                actions.append(action)
            except Exception as e:
                raise OrchestrationError(
                    f"Failed to convert task '{task.get('taskId')}' to action: {e}"
                ) from e

        self.logger.info(f"Planned {len(actions)} orchestration actions")
        return actions

    def _validate_dependencies(self, tasks: List[Dict[str, Any]]) -> None:
        """
        Validate task dependencies form a valid DAG.

        Args:
            tasks: List of orchestration tasks

        Raises:
            OrchestrationError: If dependencies are invalid
        """
        task_ids = {task.get("taskId") for task in tasks}

        for task in tasks:
            task_id = task.get("taskId")
            depends_on = task.get("dependsOn", [])

            # Check all dependencies exist
            for dep in depends_on:
                if dep not in task_ids:
                    raise OrchestrationError(
                        f"Task '{task_id}' depends on non-existent task '{dep}'"
                    )

            # Check for self-dependency
            if task_id in depends_on:
                raise OrchestrationError(
                    f"Task '{task_id}' has circular dependency (depends on itself)"
                )

        # Check for cycles using DFS
        self._check_cycles(tasks)

    def _check_cycles(self, tasks: List[Dict[str, Any]]) -> None:
        """
        Check for circular dependencies using DFS.

        Args:
            tasks: List of orchestration tasks

        Raises:
            OrchestrationError: If circular dependency detected
        """
        # Build adjacency list
        graph: Dict[str, List[str]] = {}
        for task in tasks:
            task_id = task.get("taskId")
            depends_on = task.get("dependsOn", [])
            graph[task_id] = depends_on

        # Track visited and recursion stack
        visited: Set[str] = set()
        rec_stack: Set[str] = set()

        def dfs(node: str, path: List[str]) -> None:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    dfs(neighbor, path.copy())
                elif neighbor in rec_stack:
                    # Cycle detected
                    cycle_path = path[path.index(neighbor) :] + [neighbor]
                    raise OrchestrationError(
                        f"Circular dependency detected: {' → '.join(cycle_path)}"
                    )

            rec_stack.remove(node)

        # Run DFS from each node
        for task in tasks:
            task_id = task.get("taskId")
            if task_id not in visited:
                dfs(task_id, [])

    def _task_to_action(self, task: Dict[str, Any], contract: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert orchestration task to AWS provider action.

        Args:
            task: Orchestration task specification
            contract: Parent FLUID contract

        Returns:
            AWS provider action

        Raises:
            OrchestrationError: If task cannot be converted
        """
        task_id = task.get("taskId")
        action_str = task.get("action")
        params = task.get("params", {})

        if not task_id:
            raise OrchestrationError("Task missing 'taskId' field")

        if not action_str:
            raise OrchestrationError(f"Task '{task_id}' missing 'action' field")

        # Parse action string (e.g., "aws.s3.ensure_bucket")
        action_parts = action_str.split(".")

        if len(action_parts) < 3:
            raise OrchestrationError(
                f"Invalid action format: '{action_str}'. " f"Expected 'aws.service.operation'"
            )

        provider = action_parts[0]
        service = action_parts[1]
        operation = action_parts[2]

        # Verify provider is AWS
        if provider != "aws":
            raise OrchestrationError(
                f"Unsupported provider in action: '{provider}'. "
                f"AWS provider only supports 'aws.*' actions"
            )

        # Build provider action
        action = {"id": task_id, "op": f"{service}.{operation}", **params}

        # Add metadata
        action["orchestration"] = {
            "task_id": task_id,
            "depends_on": task.get("dependsOn", []),
            "engine": contract.get("orchestration", {}).get("engine"),
        }

        # Add contract context if needed by the operation
        if service in ["glue", "athena", "redshift"]:
            # These services might need contract metadata for tagging
            action["contract_id"] = contract.get("id")
            action["contract_name"] = contract.get("name")

        return action

    def get_execution_order(self, tasks: List[Dict[str, Any]]) -> List[str]:
        """
        Determine optimal execution order for tasks based on dependencies.

        Uses topological sort to order tasks.

        Args:
            tasks: List of orchestration tasks

        Returns:
            List of task IDs in execution order
        """
        # Build adjacency list (reverse: task -> tasks that depend on it)
        graph: Dict[str, List[str]] = {task["taskId"]: [] for task in tasks}
        in_degree: Dict[str, int] = {task["taskId"]: 0 for task in tasks}

        for task in tasks:
            task_id = task["taskId"]
            for dep in task.get("dependsOn", []):
                graph[dep].append(task_id)
                in_degree[task_id] += 1

        # Kahn's algorithm for topological sort
        queue = [tid for tid, degree in in_degree.items() if degree == 0]
        execution_order = []

        while queue:
            # Sort to ensure deterministic ordering
            queue.sort()
            current = queue.pop(0)
            execution_order.append(current)

            for neighbor in graph[current]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # Check if all tasks were processed
        if len(execution_order) != len(tasks):
            raise OrchestrationError(
                "Cannot determine execution order - possible circular dependency"
            )

        return execution_order


def plan_orchestration_tasks(
    contract: Dict[str, Any], account_id: str, region: str, logger: Optional[logging.Logger] = None
) -> List[Dict[str, Any]]:
    """
    Convenience function to plan orchestration tasks.

    Args:
        contract: FLUID contract with orchestration section
        account_id: AWS account ID
        region: AWS region
        logger: Optional logger

    Returns:
        List of AWS provider actions

    Raises:
        OrchestrationError: If orchestration planning fails
    """
    planner = OrchestrationPlanner(account_id, region, logger)
    return planner.plan_orchestration_actions(contract)


def get_task_execution_order(contract: Dict[str, Any]) -> List[str]:
    """
    Get optimal execution order for orchestration tasks.

    Args:
        contract: FLUID contract with orchestration section

    Returns:
        List of task IDs in execution order
    """
    orchestration = contract.get("orchestration", {})
    tasks = orchestration.get("tasks", [])

    provider_action_tasks = [task for task in tasks if task.get("type") == "provider_action"]

    if not provider_action_tasks:
        return []

    planner = OrchestrationPlanner("", "")
    return planner.get_execution_order(provider_action_tasks)
