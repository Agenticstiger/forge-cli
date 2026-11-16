# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Airflow schedule engine.

Generates Airflow DAG Python files from FLUID contracts.  Provider-aware:
dispatches to GCP, AWS, or Snowflake operator emitters
(:mod:`fluid_build.schedulers.airflow._operators`) based on the ``provider``
parameter. This is the one renderer for a contract's ``orchestration`` section:
``fluid generate schedule``, ``fluid export --engine airflow`` and the
providers' ``export()`` all come through :func:`render_dag`.  A scheduled
build's own DAG is :mod:`fluid_build.schedulers.airflow.fluid_apply`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fluid_build.engines.base import Severity, ValidationIssue
from fluid_build.providers.common.codegen_utils import (
    airflow_dag_import,
    airflow_standard_import,
    convert_schedule_to_airflow,
    generate_file_header,
    py_str_literal,
    sanitize_identifier,
    unique_task_identifiers,
)
from fluid_build.schedulers.airflow._operators import (
    EMITTERS,
    HELPER_NAMES,
    HELPERS,
    IMPORTS,
    RESERVED_NAMES,
    TaskContext,
    generic_task,
)
from fluid_build.schedulers.base import ScheduleEngine, ScheduleGenerationResult, ScheduleIntent
from fluid_build.schedulers.registry import register_scheduler

#: Header lines each provider adds to the DAG docstring, as
#: ``{provider_config key: header key}``.
_HEADER_FIELDS: Dict[str, Dict[str, str]] = {
    "aws": {"account_id": "aws_account", "region": "aws_region"},
    "gcp": {"project": "gcp_project", "region": "gcp_region"},
    "snowflake": {"account": "snowflake_account", "database": "database", "warehouse": "warehouse"},
}


@register_scheduler
class AirflowScheduler(ScheduleEngine):
    """Airflow DAG generator — works with any cloud provider."""

    name = "airflow"
    supported_platforms = None  # platform-agnostic

    def generate(
        self,
        contract: Dict[str, Any],
        *,
        provider: Optional[str] = None,
        provider_config: Optional[Dict[str, Any]] = None,
        schedule_intent: Optional[ScheduleIntent] = None,
    ) -> ScheduleGenerationResult:
        orchestration = contract.get("orchestration", {})
        tasks = orchestration.get("tasks", [])
        contract_id = contract.get("id", "unknown")
        contract_name = contract.get("name", contract_id)
        schedule = orchestration.get("schedule", "0 2 * * *")
        timezone = orchestration.get("timezone", "UTC")

        if schedule_intent:
            schedule = schedule_intent.schedule or schedule
            timezone = schedule_intent.timezone or timezone
            if schedule_intent.tasks:
                tasks = schedule_intent.tasks

        provider = provider or contract.get("provider", "")
        provider_config = provider_config or {}
        provider_tasks = [t for t in tasks if t.get("type") == "provider_action"]

        dag_code = _render(
            contract_id,
            contract_name,
            schedule,
            timezone,
            provider,
            provider_config,
            provider_tasks,
        )
        dag_filename = f"{sanitize_identifier(contract_id)}_dag.py"
        return {dag_filename: dag_code}

    def validate(
        self,
        contract: Dict[str, Any],
    ) -> List[ValidationIssue]:
        issues: List[ValidationIssue] = []
        orchestration = contract.get("orchestration")
        if not orchestration:
            issues.append(
                ValidationIssue(
                    message="Contract missing 'orchestration' section",
                    severity=Severity.ERROR,
                    field="orchestration",
                )
            )
            return issues

        tasks = orchestration.get("tasks")
        if not tasks:
            issues.append(
                ValidationIssue(
                    message="Orchestration has no tasks",
                    severity=Severity.WARNING,
                    field="orchestration.tasks",
                )
            )

        # Check for duplicate task IDs
        task_ids = [t.get("taskId") for t in (tasks or [])]
        if len(task_ids) != len(set(task_ids)):
            issues.append(
                ValidationIssue(
                    message="Duplicate task IDs found in orchestration",
                    severity=Severity.ERROR,
                    field="orchestration.tasks",
                )
            )

        # Validate dependencies reference existing tasks
        task_id_set = set(task_ids)
        for task in tasks or []:
            for dep in task.get("dependsOn", []):
                if dep not in task_id_set:
                    issues.append(
                        ValidationIssue(
                            message=f"Task '{task.get('taskId')}' depends on non-existent task '{dep}'",
                            severity=Severity.ERROR,
                            field=f"orchestration.tasks[{task.get('taskId')}].dependsOn",
                        )
                    )

        return issues


def render_dag(
    contract: Dict[str, Any],
    *,
    provider: Optional[str] = None,
    provider_config: Optional[Dict[str, Any]] = None,
) -> str:
    """The DAG source for ``contract``'s ``orchestration`` section.

    ``provider`` defaults to ``contract["provider"]``. ``provider_config`` takes
    ``project`` / ``region`` (gcp), ``account_id`` / ``region`` (aws) and
    ``connection_id`` / ``account`` / ``database`` / ``warehouse`` (snowflake).
    Raises ``ValueError`` when the contract has no ``orchestration`` section or
    it has no tasks: callers (a provider's ``export()``) turn that into their own
    error.
    """
    orchestration = contract.get("orchestration")
    if not orchestration:
        raise ValueError("Contract missing orchestration section")
    if not orchestration.get("tasks"):
        raise ValueError("Orchestration has no tasks")
    (files,) = (
        AirflowScheduler()
        .generate(contract, provider=provider, provider_config=provider_config)
        .values()
    )
    return files


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _render(
    contract_id: str,
    contract_name: str,
    schedule: str,
    timezone: str,
    provider: Optional[str],
    provider_config: Dict[str, Any],
    tasks: List[Dict[str, Any]],
) -> str:
    """Assemble one DAG file: header, imports, DAG, helpers, tasks, edges."""
    provider_lower = (provider or "").lower()
    emit = EMITTERS.get(provider_lower)
    identifiers = unique_task_identifiers(tasks, reserved=RESERVED_NAMES)

    task_blocks: List[str] = []
    uses: set = set()
    for task in tasks:
        ctx = TaskContext(
            task=task, var=identifiers[task.get("taskId") or ""], config=provider_config
        )
        code, used = (emit or (lambda c: generic_task(c)))(ctx)
        task_blocks.append(code)
        uses |= used

    header_kwargs = {
        header_key: provider_config[config_key]
        for config_key, header_key in _HEADER_FIELDS.get(provider_lower, {}).items()
        if provider_config.get(config_key)
    }
    dag_code = generate_file_header(
        contract_id=contract_id,
        contract_name=contract_name,
        provider=provider or "generic",
        schedule=schedule,
        timezone=timezone,
        **header_kwargs,
    )
    dag_code += "\n\n" + _generate_imports(uses)
    dag_code += "\n\n" + _generate_dag_definition(
        contract_id, contract_name, schedule, timezone, provider
    )
    helpers = [HELPERS[name] for name in HELPER_NAMES if name in uses]
    if helpers:
        dag_code += "\n\n# Helper functions\n\n" + "\n\n".join(h.rstrip() for h in helpers)
    dag_code += "\n\n# Task definitions\n" + "\n\n".join(task_blocks)
    dag_code += "\n\n" + _generate_dependencies(tasks, identifiers)
    return dag_code


def _generate_imports(uses: set) -> str:
    """The shared imports plus one line per operator the tasks use."""
    base = (
        "from datetime import datetime, timedelta\n"
        "from zoneinfo import ZoneInfo\n"
        + airflow_dag_import()
        + airflow_standard_import("PythonOperator")
        + "import json\nimport logging\n"
    )
    operators = [IMPORTS[name] for name in IMPORTS if name in uses]
    if operators:
        base += "\n".join(operators) + "\n"
    return base + "\nlogger = logging.getLogger(__name__)"


def _generate_dependencies(tasks: List[Dict[str, Any]], identifiers: Dict[str, str]) -> str:
    """``upstream >> downstream`` for every edge between two tasks in this DAG.

    A dependency on a task that is not in the DAG (the non-``provider_action``
    tasks are left out) is skipped with a comment. Emitting a name for it would
    either leave an undefined name that fails the whole DAG at import, or, since
    sanitising is not injective, wire the edge to a different task.
    """
    lines = ["# Task dependencies"]
    for task in tasks:
        depends_on = task.get("dependsOn") or []
        downstream = identifiers[task.get("taskId") or ""]
        present = [identifiers[d] for d in depends_on if d in identifiers]
        for dep in depends_on:
            if dep not in identifiers:
                lines.append(f"# dependency on non-DAG task {dep!r} skipped".replace("\n", " "))
        if len(present) == 1:
            lines.append(f"{present[0]} >> {downstream}")
        elif present:
            lines.append(f"[{', '.join(present)}] >> {downstream}")
    if len(lines) == 1:
        lines.append("# No dependencies defined")
    return "\n".join(lines)


def _generate_dag_definition(
    contract_id: str,
    contract_name: str,
    schedule: str,
    timezone: str,
    provider: Optional[str],
) -> str:
    """Generate DAG definition with default arguments."""
    airflow_schedule = convert_schedule_to_airflow(schedule)
    tags = ["fluid", "auto-generated"]
    if provider:
        tags.append(provider.lower())
    tags.append(contract_id)

    # All contract-derived values are emitted as repr()-escaped literals so a
    # malicious name/timezone/schedule cannot break out and inject code that
    # Airflow would run at DAG-parse time. dag_id is an identifier (no quotes).
    return f"""# Default DAG arguments
default_args = {{
    'owner': 'fluid-forge',
    'depends_on_past': False,
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 3,
    'retry_delay': timedelta(minutes=5),
    'execution_timeout': timedelta(hours=2),
}}

# DAG definition
dag = DAG(
    dag_id={py_str_literal(sanitize_identifier(contract_id))},
    default_args=default_args,
    description={py_str_literal(contract_name)},
    schedule={py_str_literal(airflow_schedule)},
    start_date=datetime(2026, 1, 1, tzinfo=ZoneInfo({py_str_literal(timezone)})),
    catchup=False,
    tags={tags!r},
)"""
