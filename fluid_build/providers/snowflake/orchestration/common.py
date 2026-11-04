# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

# fluid_build/providers/snowflake/orchestration/common.py
"""Common types and utilities for orchestration generation."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class OrchestrationEngine(Enum):
    """Supported orchestration engines."""

    AIRFLOW = "airflow"
    DAGSTER = "dagster"
    PREFECT = "prefect"
    CUSTOM = "custom"


@dataclass
class OrchestrationConfig:
    """Configuration for orchestration generation."""

    engine: OrchestrationEngine
    dag_id: str
    schedule: Optional[str] = None
    description: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    default_args: Dict[str, Any] = field(default_factory=dict)

    # Provider-specific settings
    snowflake_conn_id: str = "snowflake_default"
    account: Optional[str] = None
    warehouse: Optional[str] = None
    database: Optional[str] = None
    schema: Optional[str] = None


@dataclass
class TaskDependency:
    """Task dependency relationship."""

    upstream_task: str
    downstream_task: str
    dependency_type: str = "sequential"  # sequential, parallel, conditional


def sanitize_task_id(name: str) -> str:
    """
    Sanitize task name to valid Airflow task_id.

    Rules:
    - Alphanumeric and underscores only
    - No spaces or special characters
    - Lowercase
    """
    # Replace spaces and hyphens with underscores
    sanitized = name.replace(" ", "_").replace("-", "_")

    # Remove non-alphanumeric characters
    sanitized = "".join(c for c in sanitized if c.isalnum() or c == "_")

    # Lowercase
    sanitized = sanitized.lower()

    # Ensure doesn't start with number
    if sanitized and sanitized[0].isdigit():
        sanitized = f"task_{sanitized}"

    return sanitized or "task"


def extract_dependencies(tasks: List[Dict[str, Any]]) -> List[TaskDependency]:
    """
    Extract task dependencies from task definitions.

    Supports:
    - depends_on: List of upstream task names
    - after: List of upstream task names (Snowflake tasks)
    - produces/consumes: Data dependencies
    """
    dependencies = []

    for task in tasks:
        task_name = task.get("name", "")

        # Explicit dependencies
        depends_on = task.get("depends_on", []) or task.get("after", [])
        for upstream in depends_on:
            dependencies.append(
                TaskDependency(
                    upstream_task=sanitize_task_id(upstream),
                    downstream_task=sanitize_task_id(task_name),
                    dependency_type="sequential",
                )
            )

    return dependencies
