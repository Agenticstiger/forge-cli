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

# fluid_build/providers/snowflake/orchestration/airflow_generator.py
"""
Airflow DAG generation from FLUID 0.7.1 orchestration specifications.

Converts FLUID provider action tasks into Airflow operators:
- sf.table.ensure → SnowflakeOperator
- sf.sql.execute → SnowflakeOperator
- python → PythonOperator
- bash → BashOperator
"""

from typing import Any, Dict, List, Optional

from ..._sql_safety import quote_string_literal
from ...common.codegen_utils import py_str_literal, sanitize_identifier
from ..registry import SnowflakeActionRegistry
from .common import OrchestrationConfig, OrchestrationEngine, extract_dependencies, sanitize_task_id


def generate_airflow_dag(
    contract: Dict[str, Any],
    orchestration: Dict[str, Any],
    config: Optional[OrchestrationConfig] = None,
) -> str:
    """
    Generate Airflow DAG Python code from FLUID orchestration spec.

    Args:
        contract: Full FLUID contract
        orchestration: Orchestration section from contract
        config: Optional orchestration configuration

    Returns:
        Python code for Airflow DAG
    """
    if config is None:
        config = _build_config_from_contract(contract, orchestration)

    tasks = orchestration.get("tasks", [])
    dependencies = extract_dependencies(tasks)

    # Generate DAG code
    code = _generate_dag_header(config)
    code += _generate_task_definitions(tasks, config)
    code += _generate_task_dependencies(dependencies)

    return code


def _build_config_from_contract(
    contract: Dict[str, Any], orchestration: Dict[str, Any]
) -> OrchestrationConfig:
    """Build orchestration config from contract."""
    metadata = contract.get("metadata", {})
    owner = metadata.get("owner", {})

    # Extract Snowflake connection details
    binding = contract.get("binding", {})
    location = binding.get("location", {})

    dag_id = contract.get("id", "fluid_dag").replace(".", "_")

    return OrchestrationConfig(
        engine=OrchestrationEngine.AIRFLOW,
        dag_id=dag_id,
        schedule=orchestration.get("schedule", "@daily"),
        description=contract.get("description", ""),
        tags=contract.get("tags", []),
        default_args={
            "owner": owner.get("team", "fluid"),
            "depends_on_past": False,
            "email_on_failure": True,
            "email": [owner.get("email", "")],
            "retries": 3,
            "retry_delay": "timedelta(minutes=5)",
        },
        account=location.get("account"),
        warehouse=location.get("warehouse"),
        database=location.get("database"),
        schema=location.get("schema"),
    )


def _generate_dag_header(config: OrchestrationConfig) -> str:
    """Generate DAG header with imports and configuration.

    Every contract/config-derived value (description, owner, email,
    schedule, dag_id, tags) is emitted as a ``repr()``-escaped literal via
    ``py_str_literal`` / ``!r`` rather than wrapped in hand-written quotes,
    so a malicious value containing a single/double/triple quote or a
    newline cannot break out of its literal and inject a top-level statement
    that Airflow would execute when it parses the generated DAG. The module
    docstring is likewise emitted as a single ``repr()``-escaped string
    literal (a bare string expression at module top is a valid docstring).
    """
    owner = config.default_args.get("owner", "fluid")
    email = config.default_args.get("email", [""])[0] if config.default_args.get("email") else ""
    retries = config.default_args.get("retries", 3)
    description = config.description or ""

    docstring_text = (
        f"{description}\n\n"
        "Generated from FLUID 0.7.1 contract.\n"
        "Provider: Snowflake\n"
        "Engine: Airflow"
    )
    docstring_lit = py_str_literal(docstring_text)
    owner_lit = py_str_literal(owner)
    email_lit = py_str_literal(email)
    description_lit = py_str_literal(description)
    dag_id_lit = py_str_literal(sanitize_identifier(config.dag_id))
    schedule_lit = py_str_literal(config.schedule)

    return f"""{docstring_lit}

from datetime import datetime, timedelta
from airflow import DAG
from airflow.providers.snowflake.operators.snowflake import SnowflakeOperator
from airflow.operators.python import PythonOperator
from airflow.operators.bash import BashOperator
from airflow.sensors.external_task import ExternalTaskSensor

# DAG default arguments
default_args = {{
    'owner': {owner_lit},
    'depends_on_past': False,
    'email': [{email_lit}],
    'email_on_failure': True,
    'email_on_retry': False,
    'retries': {retries!r},
    'retry_delay': timedelta(minutes=5),
}}

# DAG definition
dag = DAG(
    dag_id={dag_id_lit},
    default_args=default_args,
    description={description_lit},
    schedule_interval={schedule_lit},
    start_date=datetime(2025, 1, 1),
    catchup=False,
    tags={config.tags!r},
)

"""


def _generate_task_definitions(tasks: List[Dict[str, Any]], config: OrchestrationConfig) -> str:
    """Generate Airflow task definitions."""
    code = "# Task definitions\n\n"

    for task in tasks:
        task_type = task.get("type", "provider_action")
        task_name = task.get("name", "unknown")
        task_id = sanitize_task_id(task_name)

        if task_type == "provider_action":
            code += _generate_provider_action_task(task, task_id, config)
        elif task_type == "python":
            code += _generate_python_task(task, task_id)
        elif task_type == "bash":
            code += _generate_bash_task(task, task_id)
        elif task_type == "sensor":
            code += _generate_sensor_task(task, task_id)
        else:
            # repr both values so a newline in type/name can't escape the
            # leading ``#`` and turn the remainder into executable code.
            code += (
                f"# TODO: Unsupported task type {py_str_literal(task_type)} "
                f"for task {py_str_literal(task_name)}\n\n"
            )

    return code


def _generate_provider_action_task(
    task: Dict[str, Any], task_id: str, config: OrchestrationConfig
) -> str:
    """Generate SnowflakeOperator for provider action."""
    action = task.get("action", "")
    params = task.get("parameters", {})
    task.get("description", "")

    # Validate action exists
    action_def = SnowflakeActionRegistry.get(action)
    if not action_def:
        # Route both values through repr so a newline in action/task_id can't
        # escape the leading ``#`` and turn the rest of the line into code.
        return (
            f"# ERROR: Unknown action {py_str_literal(action)} "
            f"for task {py_str_literal(task_id)}\n\n"
        )

    # Generate SQL based on action type
    if action == "sf.table.ensure":
        sql = _generate_create_table_sql(params)
    elif action == "sf.sql.execute":
        sql = params.get("sql", "")
    elif action == "sf.database.ensure":
        sql = _generate_create_database_sql(params)
    elif action == "sf.schema.ensure":
        sql = _generate_create_schema_sql(params)
    elif action == "sf.view.ensure":
        sql = params.get("query", "")
    else:
        sql = f"-- TODO: Generate SQL for {action}"

    # The SQL body is emitted as a single ``repr()``-escaped literal via
    # py_str_literal: repr picks a safe quote style and escapes embedded
    # quotes, triple-quotes, newlines and backslashes, so the SQL cannot
    # terminate the literal and inject a top-level statement that Airflow
    # would run at DAG-parse time. The LHS is a Python variable name, so it
    # routes through sanitize_identifier to match the dependency wiring.
    var = sanitize_identifier(task_id)
    task_id_lit = py_str_literal(task_id)
    conn_lit = py_str_literal(config.snowflake_conn_id)
    sql_lit = py_str_literal(sql)

    return f"""{var} = SnowflakeOperator(
    task_id={task_id_lit},
    snowflake_conn_id={conn_lit},
    sql={sql_lit},
    dag=dag,
)

"""


def _generate_python_task(task: Dict[str, Any], task_id: str) -> str:
    """Generate PythonOperator.

    ``python_callable`` is emitted as a *bare* Python name (a reference to a
    callable), so the contract-supplied ``callable`` is routed through
    ``sanitize_identifier`` -- otherwise a value like ``os.system(...) or x``
    would be interpolated as a top-level expression and executed at parse
    time. The LHS variable name uses the same helper so it matches the
    dependency wiring; ``task_id`` is emitted as a ``repr()``-escaped literal.
    """
    callable_name = task.get("callable", task_id + "_func")

    var = sanitize_identifier(task_id)
    task_id_lit = py_str_literal(task_id)
    callable_ident = sanitize_identifier(callable_name)

    return f"""{var} = PythonOperator(
    task_id={task_id_lit},
    python_callable={callable_ident},
    dag=dag,
)

"""


def _generate_bash_task(task: Dict[str, Any], task_id: str) -> str:
    """Generate BashOperator.

    ``bash_command`` is the highest-risk sink in this file: a single ``'`` in
    the contract-supplied command would close the hand-written ``'...'`` and
    inject Python that Airflow runs at DAG-parse time (and the value is then
    handed to a shell at run time). It is emitted as a ``repr()``-escaped
    literal via ``py_str_literal`` so neither break-out is possible.
    """
    command = task.get("command", "echo 'No command specified'")

    var = sanitize_identifier(task_id)
    task_id_lit = py_str_literal(task_id)
    command_lit = py_str_literal(command)

    return f"""{var} = BashOperator(
    task_id={task_id_lit},
    bash_command={command_lit},
    dag=dag,
)

"""


def _generate_sensor_task(task: Dict[str, Any], task_id: str) -> str:
    """Generate Sensor.

    The external DAG/task ids are contract-derived string kwargs, emitted as
    ``repr()``-escaped literals via ``py_str_literal`` so an embedded quote or
    newline cannot break out of the literal. The LHS routes through
    ``sanitize_identifier`` to match the dependency wiring.
    """
    external_dag_id = task.get("external_dag_id", "")
    external_task_id = task.get("external_task_id", "")

    var = sanitize_identifier(task_id)
    task_id_lit = py_str_literal(task_id)
    external_dag_id_lit = py_str_literal(external_dag_id)
    external_task_id_lit = py_str_literal(external_task_id)

    return f"""{var} = ExternalTaskSensor(
    task_id={task_id_lit},
    external_dag_id={external_dag_id_lit},
    external_task_id={external_task_id_lit},
    dag=dag,
)

"""


def _generate_task_dependencies(dependencies: List) -> str:
    """Generate task dependency declarations."""
    if not dependencies:
        return ""

    code = "# Task dependencies\n\n"

    for dep in dependencies:
        # These are bare Python variable names (``upstream >> downstream``), so
        # both sides route through sanitize_identifier to match the identifiers
        # the task generators emit -- otherwise an untrusted task name could
        # inject code as a bare identifier, or a non-identifier name would
        # produce a SyntaxError / dangle against the wrong variable.
        upstream = sanitize_identifier(dep.upstream_task)
        downstream = sanitize_identifier(dep.downstream_task)
        code += f"{upstream} >> {downstream}\n"

    return code + "\n"


def _generate_create_table_sql(params: Dict[str, Any]) -> str:
    """Generate CREATE TABLE SQL."""
    database = params.get("database", "")
    schema = params.get("schema", "PUBLIC")
    table = params.get("table", "")
    columns = params.get("columns", [])
    cluster_by = params.get("cluster_by", [])
    comment = params.get("comment", "")

    sql = f'CREATE TABLE IF NOT EXISTS "{database}"."{schema}"."{table}" (\n'

    col_defs = []
    for col in columns:
        col_name = col.get("name", "")
        col_type = col.get("type", "VARCHAR")
        nullable = "" if col.get("nullable", True) else " NOT NULL"
        col_defs.append(f'  "{col_name}" {col_type}{nullable}')

    sql += ",\n".join(col_defs)
    sql += "\n)"

    if cluster_by:
        cols = ", ".join(f'"{c}"' for c in cluster_by)
        sql += f"\nCLUSTER BY ({cols})"

    if comment:
        sql += f"\nCOMMENT = {quote_string_literal(str(comment))}"

    sql += ";"

    return sql


def _generate_create_database_sql(params: Dict[str, Any]) -> str:
    """Generate CREATE DATABASE SQL."""
    database = params.get("database", "")
    comment = params.get("comment", "")
    transient = params.get("transient", False)

    sql = f'CREATE {"TRANSIENT " if transient else ""}DATABASE IF NOT EXISTS "{database}"'

    if comment:
        sql += f" COMMENT = {quote_string_literal(str(comment))}"

    sql += ";"

    return sql


def _generate_create_schema_sql(params: Dict[str, Any]) -> str:
    """Generate CREATE SCHEMA SQL."""
    database = params.get("database", "")
    schema = params.get("schema", "")
    comment = params.get("comment", "")
    transient = params.get("transient", False)

    sql = f'CREATE {"TRANSIENT " if transient else ""}SCHEMA IF NOT EXISTS "{database}"."{schema}"'

    if comment:
        sql += f" COMMENT = {quote_string_literal(str(comment))}"

    sql += ";"

    return sql
