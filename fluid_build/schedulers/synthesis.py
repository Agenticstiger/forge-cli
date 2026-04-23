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

"""Synthesize an ``orchestration`` section from a contract's ``builds`` entries.

When the LLM generates a contract, it produces ``builds`` (transformations)
but not ``orchestration`` (scheduling).  The scheduler generators require
``orchestration.tasks`` to produce DAG files.  This module bridges the gap
by deriving a reasonable default orchestration from the build steps.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Provider + engine → scheduler action mapping.
# Keys are (provider, engine) tuples; values are the action string used
# by the scheduler generators to pick the right operator.
_ACTION_MAP: Dict[tuple, str] = {
    ("gcp", "sql"): "gcp.bigquery.query",
    ("gcp", "python"): "gcp.dataflow.run",
    ("gcp", "dbt"): "gcp.bigquery.query",
    ("gcp", "dataform"): "gcp.dataform.run",
    ("aws", "sql"): "aws.athena.run_query",
    ("aws", "python"): "aws.glue.run_job",
    ("aws", "dbt"): "aws.athena.run_query",
    ("aws", "glue"): "aws.glue.run_job",
    ("snowflake", "sql"): "snowflake.sql.execute_sql",
    ("snowflake", "python"): "snowflake.task.execute",
    ("snowflake", "dbt"): "snowflake.sql.execute_sql",
    ("local", "sql"): "local.duckdb.query",
    ("local", "python"): "local.python.run",
    ("local", "dbt"): "local.duckdb.query",
}


def _sanitize_task_id(name: str) -> str:
    """Convert a build name/id into a valid task identifier."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_").lower()
    if not sanitized:
        return "task_unnamed"
    if sanitized[0].isdigit():
        sanitized = f"task_{sanitized}"
    return sanitized


def _resolve_action(provider: str, engine: str) -> str:
    """Resolve the scheduler action for a provider+engine combination."""
    key = (provider.lower(), engine.lower())
    if key in _ACTION_MAP:
        return _ACTION_MAP[key]
    # Fallback: generic action
    return f"generic.{engine.lower()}.run"


def _build_to_task(
    build: Dict[str, Any],
    index: int,
    provider: str,
    prev_task_id: Optional[str],
) -> Dict[str, Any]:
    """Convert a single ``builds[]`` entry into an orchestration task."""
    build_id = build.get("id") or build.get("name") or f"step_{index}"
    task_id = _sanitize_task_id(build_id)
    engine = build.get("engine", "sql")

    # Extract params from build properties
    params: Dict[str, Any] = {}
    properties = build.get("properties", {})
    if properties.get("sql"):
        params["query"] = properties["sql"]
    elif properties.get("query"):
        params["query"] = properties["query"]
    if properties.get("model"):
        params["model"] = properties["model"]
    if properties.get("destination"):
        params["destination"] = properties["destination"]

    task: Dict[str, Any] = {
        "taskId": task_id,
        "type": "provider_action",
        "action": _resolve_action(provider, engine),
        "params": params,
        "dependsOn": [prev_task_id] if prev_task_id else [],
    }
    return task


def synthesize_orchestration_from_builds(
    contract: Dict[str, Any],
    scheduler_name: str,
    provider: str = "",
) -> Dict[str, Any]:
    """Derive an ``orchestration`` dict from the contract's ``builds`` list.

    Returns an empty dict if the contract has no builds, so callers can
    use ``if synthesized:`` as a guard.

    The returned dict is suitable for injection into the contract before
    calling a scheduler's ``validate()`` and ``generate()`` methods.
    """
    builds: List[Dict[str, Any]] = contract.get("builds", [])
    if not builds:
        return {}

    provider = provider or contract.get("provider", "")

    tasks: List[Dict[str, Any]] = []
    prev_task_id: Optional[str] = None
    for i, build in enumerate(builds):
        task = _build_to_task(build, i, provider, prev_task_id)
        tasks.append(task)
        prev_task_id = task["taskId"]

    return {
        "engine": scheduler_name,
        "schedule": "0 2 * * *",
        "timezone": "UTC",
        "tasks": tasks,
    }
