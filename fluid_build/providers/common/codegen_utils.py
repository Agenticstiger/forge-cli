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
Shared utilities for code generation across all providers.

Common functions for DAG/pipeline generation to reduce code duplication
and ensure consistent behavior across AWS, GCP, and Snowflake providers.
"""

import keyword
import re
from datetime import datetime
from typing import Any, Dict, List


def sanitize_identifier(name: str) -> str:
    """
    Sanitize a name to be a valid Python identifier.

    Replaces non-alphanumeric characters with underscores and removes
    leading digits. Used for every place a (potentially untrusted)
    contract value becomes a *Python variable name* in generated source —
    a hyphen/quote/newline in a ``taskId`` would otherwise produce a
    ``SyntaxError`` at best and arbitrary code at worst. The mapping is
    deterministic so a task definition (``<id> = Operator(...)``) and the
    dependency wiring (``<id> >> <other>``) resolve to the same variable.

    Args:
        name: Original name

    Returns:
        Valid Python identifier
    """
    # Replace non-alphanumeric with underscores
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", str(name))

    # Remove leading digits
    sanitized = re.sub(r"^[0-9]+", "", sanitized)

    # Ensure not empty
    if not sanitized:
        sanitized = "unnamed"

    # A sanitised value can land on a Python keyword (a taskId of ``class``
    # / ``import`` would emit ``class = PythonOperator(...)`` → SyntaxError in
    # the generated DAG). Suffix an underscore so the result is always a
    # *non-keyword* legal identifier (``class`` → ``class_``).
    if keyword.iskeyword(sanitized):
        sanitized += "_"

    # NOTE: this mapping is intentionally not injective — two distinct raw
    # ids (``a-b`` and ``a.b``) collapse to the same identifier. The
    # upstream duplicate-taskId validator (codegen_utils.validate_contract_for_export
    # / the schedulers' dup-id checks) already rejects duplicate *raw* ids,
    # so a collision here cannot silently merge two declared tasks.
    return sanitized


def py_str_literal(value: Any) -> str:
    """Return a safe Python **string-literal** for ``value`` for embedding
    into generated source code.

    Every value that originates from a (potentially untrusted)
    ``contract.fluid.yaml`` and gets interpolated into generated Python —
    DAG files, pipeline scripts, operator kwargs — MUST route through here
    instead of being wrapped in hand-written quotes (``'{value}'`` /
    ``\"\"\"{value}\"\"\"``).

    ``repr()`` of a ``str`` is a fully-escaped Python literal: it picks a
    safe quote style and escapes embedded quotes, triple-quotes, newlines
    and backslashes, so a contract value such as
    ``SELECT 1\"\"\"\\nimport os; os.system('…')\\nx=\"\"\"`` cannot break
    out of the literal and inject a top-level statement that Airflow would
    execute when it parses the generated DAG. ``None`` becomes an empty
    string; non-strings are coerced with ``str()`` so a malicious non-string
    value can't slip a raw object repr into the source.

    Borrowed-not-built (/borrow-before-build): ``repr()`` is the canonical
    stdlib primitive for emitting a Python literal (``ast.unparse`` is for
    whole-tree codegen, overkill here). This centralises the pattern already
    used by ``schedulers/airflow``'s generic-task path — the same way SQL
    string literals are centralised through ``providers/_sql_safety.py``.
    """
    return repr("" if value is None else str(value))


def convert_schedule_to_cron(schedule: str) -> str:
    """
    Convert FLUID schedule notation to cron expression.

    Handles special keywords and passes through valid cron expressions.

    Args:
        schedule: FLUID schedule string (@hourly, @daily, cron expression)

    Returns:
        Cron expression (5 fields: minute hour day month weekday)
    """
    schedule_lower = schedule.lower().strip()

    # Handle special keywords
    keyword_map = {
        "@hourly": "0 * * * *",
        "@daily": "0 0 * * *",
        "@weekly": "0 0 * * 0",
        "@monthly": "0 0 1 * *",
        "@yearly": "0 0 1 1 *",
        "@annually": "0 0 1 1 *",
    }

    if schedule_lower in keyword_map:
        return keyword_map[schedule_lower]

    # Pass through cron expressions (5+ fields)
    if schedule.count(" ") >= 4:
        return schedule

    # Default to daily at 2 AM
    return "0 2 * * *"


def convert_schedule_to_airflow(schedule: str) -> str:
    """
    Convert FLUID schedule to Airflow schedule_interval format.

    Airflow accepts both cron and special keywords like @daily.

    Args:
        schedule: FLUID schedule string

    Returns:
        Airflow schedule_interval value
    """
    schedule_lower = schedule.lower().strip()

    # Airflow native keywords
    if schedule_lower in ("@hourly", "@daily", "@weekly", "@monthly", "@yearly", "@once"):
        return schedule_lower

    # Pass through cron expressions
    if schedule.count(" ") >= 4:
        return schedule

    # Default to daily
    return "@daily"


def escape_for_docstring(value: Any) -> str:
    """Escape ``value`` for safe embedding inside a generated triple-quoted
    Python docstring (``\"\"\"...\"\"\"``).

    Escapes backslashes then double-quotes so the value can neither form a
    closing ``\"\"\"`` delimiter nor leave a trailing line-continuation
    backslash. Without this, a contract ``name`` containing ``\"\"\"`` followed
    by a newline and ``import os; …`` would close the generated file's header
    docstring and inject a top-level statement that Airflow runs at DAG-parse
    time. Newlines are left intact (legal inside a triple-quoted string).

    ``None`` becomes an empty string (a null contract field renders as
    blank rather than the literal text ``None``) — matching ``py_str_literal``'s
    ``None`` handling so the two codegen escapers behave consistently. This is
    the single source of truth shared by every provider's DAG-header builder
    (the snowflake provider's former local copy delegated here).
    """
    return ("" if value is None else str(value)).replace("\\", "\\\\").replace('"', '\\"')


def generate_file_header(contract_id: str, contract_name: str, provider: str, **kwargs: Any) -> str:
    """
    Generate standardized file header with metadata.

    Args:
        contract_id: Contract identifier
        contract_name: Human-readable contract name
        provider: Provider name (aws, gcp, snowflake)
        **kwargs: Additional metadata to include

    Returns:
        Multi-line docstring header
    """
    # Contract-derived values are escaped so they cannot terminate the
    # surrounding ``\"\"\"`` docstring and inject code (see escape_for_docstring).
    lines = [
        f"FLUID Generated Pipeline: {escape_for_docstring(contract_name)}",
        "",
        f"Contract ID: {escape_for_docstring(contract_id)}",
        f"Provider: {escape_for_docstring(provider.upper())}",
    ]

    # Add any additional metadata
    for key, value in sorted(kwargs.items()):
        if value:
            # Format key for display
            display_key = key.replace("_", " ").title()
            lines.append(f"{escape_for_docstring(display_key)}: {escape_for_docstring(value)}")

    lines.extend(
        [
            "",
            "Auto-generated by FLUID Forge - DO NOT EDIT MANUALLY",
            f"Generated: {datetime.utcnow().isoformat()}Z",
        ]
    )

    # Wrap in docstring
    return '"""\n' + "\n".join(lines) + '\n"""'


def escape_sql_for_python(sql: str) -> str:
    """
    Escape SQL string for safe inclusion in Python code.

    Handles quotes, backslashes, and other special characters.

    Args:
        sql: Raw SQL string

    Returns:
        Escaped SQL safe for Python string literals
    """
    # Escape backslashes first
    escaped = sql.replace("\\", "\\\\")
    # Escape double quotes
    escaped = escaped.replace('"', '\\"')
    # Escape triple quotes if present
    escaped = escaped.replace('"""', '\\"\\"\\""')

    return escaped


def generate_task_dependencies_code(tasks: List[Dict[str, Any]], syntax: str = "airflow") -> str:
    """
    Generate task dependency declarations.

    Args:
        tasks: List of task specifications with 'taskId' and 'dependsOn'
        syntax: Dependency syntax style ('airflow', 'dagster', 'prefect')

    Returns:
        Code declaring task dependencies
    """
    if not tasks:
        return "# No task dependencies"

    dep_code = "# Task dependencies\n"

    for task in tasks:
        task_id = task.get("taskId")
        depends_on = task.get("dependsOn", [])

        if not depends_on:
            continue

        if syntax == "airflow":
            # Airflow: upstream >> downstream. These are Python *variable
            # names*, so they must be sanitised to match the identifiers the
            # task generators emit (``sanitize_identifier(taskId) = Operator``)
            # — otherwise an untrusted taskId / dependsOn entry could inject
            # code as a bare identifier, and a non-identifier id would dangle.
            safe_task = sanitize_identifier(task_id)
            safe_deps = [sanitize_identifier(dep) for dep in depends_on]
            if len(safe_deps) == 1:
                dep_code += f"{safe_deps[0]} >> {safe_task}\n"
            else:
                dep_code += f"[{', '.join(safe_deps)}] >> {safe_task}\n"

        elif syntax == "dagster":
            # Dagster: handled via op() ins parameter
            pass

        elif syntax == "prefect":
            # Prefect: implicit via execution order
            pass

    return dep_code.rstrip()


def validate_contract_for_export(contract: Dict[str, Any]) -> None:
    """
    Validate contract has required fields for code generation.

    Args:
        contract: FLUID contract

    Raises:
        ValueError: If contract is invalid
    """
    if not contract.get("orchestration"):
        raise ValueError("Contract missing 'orchestration' section")

    orchestration = contract["orchestration"]

    if not orchestration.get("tasks"):
        raise ValueError("Orchestration has no tasks")

    tasks = orchestration["tasks"]
    if not isinstance(tasks, list) or len(tasks) == 0:
        raise ValueError("Orchestration tasks must be a non-empty list")

    # Validate task IDs are unique
    task_ids = [t.get("taskId") for t in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("Duplicate task IDs found in orchestration")

    # Validate dependencies reference existing tasks
    task_id_set = set(task_ids)
    for task in tasks:
        depends_on = task.get("dependsOn", [])
        for dep in depends_on:
            if dep not in task_id_set:
                raise ValueError(
                    f"Task '{task.get('taskId')}' depends on non-existent task '{dep}'"
                )


def detect_circular_dependencies(tasks: List[Dict[str, Any]]) -> List[str]:
    """
    Detect circular dependencies in task graph.

    Args:
        tasks: List of task specifications

    Returns:
        List of task IDs involved in circular dependencies (empty if none)
    """
    # Build dependency graph
    graph = {}
    for task in tasks:
        task_id = task.get("taskId")
        graph[task_id] = task.get("dependsOn", [])

    # DFS to detect cycles
    visited = set()
    rec_stack = set()
    cycles = []

    def visit(node):
        if node in rec_stack:
            cycles.append(node)
            return True
        if node in visited:
            return False

        visited.add(node)
        rec_stack.add(node)

        for neighbor in graph.get(node, []):
            if visit(neighbor):
                cycles.append(node)
                return True

        rec_stack.remove(node)
        return False

    for task_id in graph:
        if task_id not in visited:
            visit(task_id)

    return list(set(cycles))


def calculate_code_metrics(code: str) -> Dict[str, Any]:
    """
    Calculate basic metrics for generated code.

    Args:
        code: Generated Python code

    Returns:
        Dictionary with metrics (lines, size, complexity estimate)
    """
    lines = code.split("\n")

    return {
        "line_count": len(lines),
        "non_empty_lines": sum(1 for line in lines if line.strip()),
        "comment_lines": sum(1 for line in lines if line.strip().startswith("#")),
        "docstring_lines": sum(1 for line in lines if '"""' in line or "'''" in line),
        "byte_size": len(code.encode("utf-8")),
        "function_count": code.count("def "),
        "class_count": code.count("class "),
    }
