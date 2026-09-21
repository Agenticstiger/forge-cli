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
Airflow DAG Generation for AWS Provider.

Generates Python DAG files from FLUID contracts for deployment to MWAA
(Amazon Managed Workflows for Apache Airflow) or self-hosted Airflow.

Supports:
- Provider action tasks
- Task dependencies
- Schedule configuration
- AWS-specific operators (S3, Glue, Athena, Redshift, Lambda)
"""

import json
import logging
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any, Dict, List, Optional

from fluid_build.providers.common.codegen_utils import (
    escape_for_docstring,
    json_literal,
    py_str_literal,
    sanitize_identifier,
)

logger = logging.getLogger(__name__)


# Module-level names the generated DAG binds itself. A task variable must not
# collide with any of them.
_EMITTED_MODULE_NAMES = frozenset(
    {
        "dag",
        "default_args",
        "logger",
        "json",
        "logging",
        "datetime",
        "timedelta",
        "ZoneInfo",
        "DAG",
        "_ensure_glue_database",
        "_ensure_glue_table",
        "_execute_provider_action",
    }
)


def _task_var(task: Dict[str, Any], task_vars: Optional[Dict[str, str]]) -> str:
    """Resolve a task's Python variable name, honouring the collision map.

    ``task_vars`` is built once per DAG by :func:`_unique_task_identifiers`.
    It is optional so the individual emitters stay directly callable (several
    tests exercise them in isolation); without it the behaviour is the plain
    sanitised name.
    """
    raw = task.get("taskId") or ""
    if task_vars and raw in task_vars:
        return task_vars[raw]
    return sanitize_identifier(raw)


def _unique_task_identifiers(tasks: List[Dict[str, Any]]) -> Dict[str, str]:
    """Map each raw ``taskId`` to a variable name unique within this DAG.

    ``sanitize_identifier`` is deliberately not injective — ``a-b`` and ``a.b``
    both become ``a_b`` — so two tasks differing only in punctuation emitted
    two assignments to the SAME variable: the second overwrote the first and
    one declared task vanished from the DAG with no error.

    Sanitize, then suffix on collision. This mirrors
    ``runtimes/airflow_provider_actions._unique_task_identifiers`` (#623),
    which fixed the identical defect on the ``generate-airflow`` path; this
    generator was missed at the time.
    """
    identifiers: Dict[str, str] = {}
    # Seed with the names the emitted module already binds, so a taskId of
    # ``dag`` or ``_execute_provider_action`` cannot rebind one of them and
    # break the DAG in a way that only shows up at task run time.
    used: set = set(_EMITTED_MODULE_NAMES)
    for task in tasks:
        raw = task.get("taskId") or ""
        if raw in identifiers:
            # Exact duplicate raw ids cannot both be represented in this map,
            # so only one survives. ``validate_contract_for_export`` rejects
            # duplicates upstream; warn honestly if one reaches here anyway.
            logger.warning(
                "aws airflow: duplicate taskId %r — only the last occurrence "
                "is emitted; taskIds must be unique",
                raw,
            )
        base = sanitize_identifier(raw)
        candidate, suffix = base, 2
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate)
        identifiers[raw] = candidate
    return identifiers


def generate_airflow_dag(contract: Dict[str, Any], account_id: str, region: str) -> str:
    """
    Generate Airflow DAG Python code from FLUID contract.

    Args:
        contract: FLUID contract with orchestration section
        account_id: AWS account ID
        region: AWS region

    Returns:
        Python code for Airflow DAG
    """
    orchestration = contract.get("orchestration", {})
    if not orchestration:
        raise ValueError("Contract missing orchestration section")

    tasks = orchestration.get("tasks", [])
    if not tasks:
        raise ValueError("Orchestration has no tasks")

    # Extract configuration
    contract_id = contract.get("id", "unknown")
    contract_name = contract.get("name", contract_id)
    schedule = orchestration.get("schedule", "0 2 * * *")
    timezone = orchestration.get("timezone", "UTC")
    orchestration.get("engine", "airflow")

    # Filter provider action tasks
    provider_tasks = [t for t in tasks if t.get("type") == "provider_action"]

    # Generate DAG code
    dag_code = _generate_dag_header(
        contract_id, contract_name, schedule, timezone, account_id, region
    )
    dag_code += "\n\n"
    dag_code += _generate_dag_imports()
    dag_code += "\n\n"
    dag_code += _generate_dag_definition(contract_id, contract_name, schedule, timezone)
    dag_code += "\n\n"
    # The task bodies call _ensure_glue_database / _ensure_glue_table /
    # _execute_provider_action, so their definitions have to be in the emitted
    # file. This block was built but never appended, leaving every generated
    # DAG referencing three undefined names.
    dag_code += _generate_helper_functions()
    dag_code += "\n\n"
    task_vars = _unique_task_identifiers(provider_tasks)
    dag_code += _generate_task_definitions(provider_tasks, account_id, region, task_vars)
    dag_code += "\n\n"
    dag_code += _generate_task_dependencies(provider_tasks, task_vars)

    return dag_code


def _generate_dag_header(
    contract_id: str, contract_name: str, schedule: str, timezone: str, account_id: str, region: str
) -> str:
    """Generate DAG file header with metadata."""
    return f'''"""
FLUID Generated DAG: {escape_for_docstring(contract_name)}

Contract ID: {escape_for_docstring(contract_id)}
Schedule: {escape_for_docstring(schedule)}
Timezone: {escape_for_docstring(timezone)}
AWS Account: {escape_for_docstring(account_id)}
AWS Region: {escape_for_docstring(region)}

Auto-generated by FLUID Forge - DO NOT EDIT MANUALLY
Generated: {datetime.now(dt_timezone.utc).replace(tzinfo=None).isoformat()}
"""'''


def _generate_dag_imports() -> str:
    """Generate import statements."""
    return """from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.amazon.aws.operators.s3 import S3CreateBucketOperator
from airflow.providers.amazon.aws.operators.glue import GlueJobOperator
from airflow.providers.amazon.aws.operators.athena import AthenaOperator
from airflow.providers.amazon.aws.operators.redshift_data import RedshiftDataOperator
from airflow.providers.amazon.aws.operators.lambda_function import LambdaInvokeFunctionOperator
import json
import logging

logger = logging.getLogger(__name__)"""


def _generate_dag_definition(
    contract_id: str, contract_name: str, schedule: str, timezone: str
) -> str:
    """Generate DAG definition with default arguments."""
    # Convert cron to Airflow schedule
    airflow_schedule = _convert_schedule(schedule)

    tags = ["fluid", "auto-generated", str(contract_id)]

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
    dag_id={py_str_literal(_sanitize_dag_id(contract_id))},
    default_args=default_args,
    description={py_str_literal(contract_name)},
    schedule_interval={py_str_literal(airflow_schedule)},
    start_date=datetime(2026, 1, 1, tzinfo=ZoneInfo({py_str_literal(timezone)})),
    catchup=False,
    tags={tags!r},
)"""


def _generate_task_definitions(
    tasks: List[Dict[str, Any]],
    account_id: str,
    region: str,
    task_vars: Optional[Dict[str, str]] = None,
) -> str:
    """Generate task definitions."""
    task_code = "# Task definitions\n"

    for task in tasks:
        task_code += _generate_single_task(task, account_id, region, task_vars)
        task_code += "\n\n"

    return task_code.rstrip()


def _generate_single_task(
    task: Dict[str, Any],
    account_id: str,
    region: str,
    task_vars: Optional[Dict[str, str]] = None,
) -> str:
    """Generate code for a single task."""
    action = task.get("action")
    params = task.get("params", {})
    var = _task_var(task, task_vars)

    # Parse action (e.g., "aws.s3.ensure_bucket")
    action_parts = action.split(".")
    if len(action_parts) < 3:
        # Fallback to PythonOperator
        return _generate_python_task(task, account_id, region, var)

    service = action_parts[1]
    operation = action_parts[2]

    # Map to appropriate Airflow operator
    if service == "s3":
        return _generate_s3_task(task, operation, params, account_id, region, var)
    elif service == "glue":
        return _generate_glue_task(task, operation, params, account_id, region, var)
    elif service == "athena":
        return _generate_athena_task(task, operation, params, account_id, region, var)
    elif service == "redshift":
        return _generate_redshift_task(task, operation, params, account_id, region, var)
    elif service == "lambda":
        return _generate_lambda_task(task, operation, params, account_id, region, var)
    else:
        # Fallback to PythonOperator
        return _generate_python_task(task, account_id, region, var)


def _generate_s3_task(
    task: Dict[str, Any],
    operation: str,
    params: Dict[str, Any],
    account_id: str,
    region: str,
    var: Optional[str] = None,
) -> str:
    """Generate S3 task code."""
    task_id = task.get("taskId")
    var = var or sanitize_identifier(task_id)

    if operation == "ensure_bucket":
        bucket_name = params.get("bucket")
        return f"""{var} = S3CreateBucketOperator(
    task_id={py_str_literal(task_id)},
    bucket_name={py_str_literal(bucket_name)},
    region_name={py_str_literal(params.get("region", region))},
    aws_conn_id='aws_default',
    dag=dag,
)"""
    else:
        # Generic S3 operation via Python
        return _generate_python_task(task, account_id, region, var)


def _generate_glue_task(
    task: Dict[str, Any],
    operation: str,
    params: Dict[str, Any],
    account_id: str,
    region: str,
    var: Optional[str] = None,
) -> str:
    """Generate Glue task code."""
    task_id = task.get("taskId")
    var = var or sanitize_identifier(task_id)

    if operation == "ensure_database":
        database = params.get("database")
        return f"""{var} = PythonOperator(
    task_id={py_str_literal(task_id)},
    python_callable=lambda: _ensure_glue_database(
        database={py_str_literal(database)},
        region={py_str_literal(region)}
    ),
    dag=dag,
)"""

    elif operation == "ensure_table":
        database = params.get("database")
        table = params.get("table")
        return f"""{var} = PythonOperator(
    task_id={py_str_literal(task_id)},
    python_callable=lambda: _ensure_glue_table(
        database={py_str_literal(database)},
        table={py_str_literal(table)},
        params=json.loads({json_literal(params)}),
        region={py_str_literal(region)}
    ),
    dag=dag,
)"""

    else:
        return _generate_python_task(task, account_id, region, var)


def _generate_athena_task(
    task: Dict[str, Any],
    operation: str,
    params: Dict[str, Any],
    account_id: str,
    region: str,
    var: Optional[str] = None,
) -> str:
    """Generate Athena task code."""
    task_id = task.get("taskId")
    var = var or sanitize_identifier(task_id)

    if operation == "execute_query":
        query = params.get("query", "")
        database = params.get("database", "default")
        output_location = params.get(
            "outputLocation", f"s3://aws-athena-query-results-{account_id}-{region}/"
        )

        return f"""{var} = AthenaOperator(
    task_id={py_str_literal(task_id)},
    query={py_str_literal(query)},
    database={py_str_literal(database)},
    output_location={py_str_literal(output_location)},
    aws_conn_id='aws_default',
    dag=dag,
)"""

    else:
        return _generate_python_task(task, account_id, region, var)


def _generate_redshift_task(
    task: Dict[str, Any],
    operation: str,
    params: Dict[str, Any],
    account_id: str,
    region: str,
    var: Optional[str] = None,
) -> str:
    """Generate Redshift task code."""
    task_id = task.get("taskId")
    var = var or sanitize_identifier(task_id)

    if operation == "execute_sql":
        sql = params.get("sql", "")
        cluster_id = params.get("cluster", "")
        database = params.get("database", "dev")

        return f"""{var} = RedshiftDataOperator(
    task_id={py_str_literal(task_id)},
    sql={py_str_literal(sql)},
    cluster_identifier={py_str_literal(cluster_id)},
    database={py_str_literal(database)},
    aws_conn_id='aws_default',
    dag=dag,
)"""

    else:
        return _generate_python_task(task, account_id, region, var)


def _generate_lambda_task(
    task: Dict[str, Any],
    operation: str,
    params: Dict[str, Any],
    account_id: str,
    region: str,
    var: Optional[str] = None,
) -> str:
    """Generate Lambda task code."""
    task_id = task.get("taskId")
    var = var or sanitize_identifier(task_id)

    if operation == "invoke":
        function_name = params.get("function")
        payload = params.get("payload", {})

        return f"""{var} = LambdaInvokeFunctionOperator(
    task_id={py_str_literal(task_id)},
    function_name={py_str_literal(function_name)},
    payload={json_literal(payload)},
    aws_conn_id='aws_default',
    dag=dag,
)"""

    else:
        return _generate_python_task(task, account_id, region, var)


def _generate_python_task(
    task: Dict[str, Any], account_id: str, region: str, var: Optional[str] = None
) -> str:
    """Generate generic Python task (fallback)."""
    task_id = task.get("taskId")
    action = task.get("action")
    params = task.get("params", {})
    var = var or sanitize_identifier(task_id)

    return f"""{var} = PythonOperator(
    task_id={py_str_literal(task_id)},
    python_callable=lambda: _execute_provider_action(
        action={py_str_literal(action)},
        params=json.loads({json_literal(params)}),
        account_id={py_str_literal(account_id)},
        region={py_str_literal(region)}
    ),
    dag=dag,
)"""


def _generate_task_dependencies(
    tasks: List[Dict[str, Any]], task_vars: Optional[Dict[str, str]] = None
) -> str:
    """Generate task dependency declarations."""
    # Callable directly (several tests do). Without a map supplied by the DAG
    # builder, derive one from these tasks so every dep still resolves; the
    # "not in this DAG" branch below then means what it says.
    if task_vars is None:
        task_vars = _unique_task_identifiers(tasks)

    dep_code = "# Task dependencies\n"

    for task in tasks:
        task_id = task.get("taskId")
        depends_on = task.get("dependsOn", [])

        if depends_on:
            sanitized_id = _task_var(task, task_vars)
            for dep in depends_on:
                sanitized_dep = (task_vars or {}).get(dep)
                if sanitized_dep is None:
                    # The dep is not a task in this DAG — non-provider_action
                    # tasks are filtered out upstream. Synthesising a name here
                    # either emits an undefined name (killing the whole DAG at
                    # import) or, because sanitize_identifier is not injective,
                    # silently wires the edge to a DIFFERENT task.
                    logger.warning(
                        "aws airflow: task %r dependsOn %r, which is not a "
                        "provider_action task in this DAG — edge skipped",
                        task.get("taskId"),
                        dep,
                    )
                    dep_code += f"# dependency on non-DAG task {dep!r} skipped\n"
                    continue
                dep_code += f"{sanitized_dep} >> {sanitized_id}\n"

    if dep_code == "# Task dependencies\n":
        dep_code += "# No dependencies defined\n"

    return dep_code


def _generate_helper_functions() -> str:
    """Generate helper functions for tasks."""
    return '''
# Helper functions

def _ensure_glue_database(database: str, region: str):
    """Ensure Glue database exists."""
    import boto3
    client = boto3.client('glue', region_name=region)
    
    try:
        client.create_database(DatabaseInput={'Name': database})
        logger.info(f"Created Glue database: {database}")
    except client.exceptions.AlreadyExistsException:
        logger.info(f"Glue database already exists: {database}")


def _ensure_glue_table(database: str, table: str, params: dict, region: str):
    """Ensure Glue table exists."""
    import boto3
    client = boto3.client('glue', region_name=region)

    columns = params.get('columns', [])
    location = params.get('location', '')
    input_format = params.get('input_format', 'parquet')

    FORMAT_MAP = {
        'parquet': {
            'InputFormat': 'org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat',
            'OutputFormat': 'org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat',
            'SerDe': 'org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe',
        },
        'orc': {
            'InputFormat': 'org.apache.hadoop.hive.ql.io.orc.OrcInputFormat',
            'OutputFormat': 'org.apache.hadoop.hive.ql.io.orc.OrcOutputFormat',
            'SerDe': 'org.apache.hadoop.hive.ql.io.orc.OrcSerde',
        },
        'json': {
            'InputFormat': 'org.apache.hadoop.mapred.TextInputFormat',
            'OutputFormat': 'org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat',
            'SerDe': 'org.openx.data.jsonserde.JsonSerDe',
        },
        'csv': {
            'InputFormat': 'org.apache.hadoop.mapred.TextInputFormat',
            'OutputFormat': 'org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat',
            'SerDe': 'org.apache.hadoop.hive.serde2.OpenCSVSerde',
        },
    }
    fmt = FORMAT_MAP.get(input_format, FORMAT_MAP['parquet'])

    table_input = {
        'Name': table,
        'StorageDescriptor': {
            'Columns': columns,
            'Location': location,
            'InputFormat': fmt['InputFormat'],
            'OutputFormat': fmt['OutputFormat'],
            'SerdeInfo': {'SerializationLibrary': fmt['SerDe']},
        },
        'TableType': 'EXTERNAL_TABLE',
    }
    description = params.get('description')
    if description:
        table_input['Description'] = description

    try:
        client.get_table(DatabaseName=database, Name=table)
        client.update_table(DatabaseName=database, TableInput=table_input)
        logger.info(f"Updated Glue table: {database}.{table}")
    except client.exceptions.EntityNotFoundException:
        client.create_table(DatabaseName=database, TableInput=table_input)
        logger.info(f"Created Glue table: {database}.{table}")


def _execute_provider_action(action: str, params: dict, account_id: str, region: str):
    """Provider-action tasks are retired.

    forge-cli compiles the contract to OpenTofu and runs ``tofu`` — a
    generated DAG no longer performs native cloud CRUD. Provision
    infrastructure with ``fluid apply`` (the OpenTofu engine).
    """
    raise RuntimeError(
        f"provider-action task {action!r} is retired: provision infrastructure "
        "with `fluid apply` (the OpenTofu engine), then re-generate this DAG"
    )
'''


def _sanitize_dag_id(dag_id: str) -> str:
    """Sanitize DAG ID for Airflow."""
    # Airflow DAG IDs can contain letters, numbers, dashes, periods, underscores
    sanitized = ""
    for char in dag_id:
        if char.isalnum() or char in "-._":
            sanitized += char
        else:
            sanitized += "_"
    return sanitized


def _convert_schedule(schedule: str) -> str:
    """Convert schedule to Airflow format."""
    # If already cron, return as-is
    if schedule.count(" ") >= 4:
        return schedule

    # Handle common presets
    presets = {
        "daily": "0 2 * * *",
        "hourly": "0 * * * *",
        "weekly": "0 2 * * 0",
        "monthly": "0 2 1 * *",
    }

    return presets.get(schedule.lower(), schedule)


# ── Airflow 2.x TaskFlow API Generation ──────────────────────────────


def generate_airflow_dag_taskflow(
    contract: Dict[str, Any],
    account_id: str,
    region: str,
) -> str:
    """
    Generate an Airflow 2.x TaskFlow-style DAG from a FLUID contract.

    Uses ``@dag`` / ``@task`` decorators instead of explicit operator
    instantiation.  Falls back to classic operators for AWS-specific
    operators (S3, Athena, Redshift, Lambda) that have no TaskFlow
    equivalent.

    Args:
        contract: FLUID contract with orchestration section
        account_id: AWS account ID
        region: AWS region

    Returns:
        Python source code for an Airflow 2.x TaskFlow DAG
    """
    orchestration = contract.get("orchestration", {})
    if not orchestration:
        raise ValueError("Contract missing orchestration section")

    tasks = orchestration.get("tasks", [])
    if not tasks:
        raise ValueError("Orchestration has no tasks")

    contract_id = contract.get("id", "unknown")
    contract_name = contract.get("name", contract_id)
    schedule = orchestration.get("schedule", "0 2 * * *")
    timezone = orchestration.get("timezone", "UTC")

    provider_tasks = [t for t in tasks if t.get("type") == "provider_action"]

    code = _generate_dag_header(contract_id, contract_name, schedule, timezone, account_id, region)
    code += "\n\n"
    code += _generate_taskflow_imports()
    code += "\n\n"
    # Same defect as the classic path: the @task bodies call these helpers, so
    # their definitions must be in the emitted file.
    code += _generate_helper_functions()
    code += "\n\n"
    code += _generate_taskflow_dag(
        contract_id,
        contract_name,
        schedule,
        timezone,
        provider_tasks,
        account_id,
        region,
    )
    return code


def _generate_taskflow_imports() -> str:
    """Generate import statements for TaskFlow API."""
    return """from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from airflow.decorators import dag, task
from airflow.providers.amazon.aws.operators.s3 import S3CreateBucketOperator
from airflow.providers.amazon.aws.operators.athena import AthenaOperator
from airflow.providers.amazon.aws.operators.redshift_data import RedshiftDataOperator
from airflow.providers.amazon.aws.operators.lambda_function import LambdaInvokeFunctionOperator
import json
import logging

logger = logging.getLogger(__name__)"""


def _generate_taskflow_dag(
    contract_id: str,
    contract_name: str,
    schedule: str,
    timezone: str,
    tasks: List[Dict[str, Any]],
    account_id: str,
    region: str,
) -> str:
    """Generate the full @dag-decorated function body."""
    dag_id = _sanitize_dag_id(contract_id)
    airflow_schedule = _convert_schedule(schedule)
    # The @dag-decorated function name is a Python identifier, so it goes
    # through sanitize_identifier (which also guards keywords and leading
    # digits); the hand-rolled double-replace did neither and emitted
    # ``def ():`` for an empty id. Must match the invocation appended below.
    dag_func = sanitize_identifier(contract_id)
    tags = ["fluid", "auto-generated", str(contract_id)]

    lines: List[str] = []
    lines.append("@dag(")
    lines.append(f"    dag_id={py_str_literal(dag_id)},")
    lines.append(f"    description={py_str_literal(contract_name)},")
    lines.append(f"    schedule={py_str_literal(airflow_schedule)},")
    lines.append(
        f"    start_date=datetime(2026, 1, 1, tzinfo=ZoneInfo({py_str_literal(timezone)})),"
    )
    lines.append("    catchup=False,")
    lines.append("    default_args={")
    lines.append("        'owner': 'fluid-forge',")
    lines.append("        'retries': 3,")
    lines.append("        'retry_delay': timedelta(minutes=5),")
    lines.append("    },")
    lines.append(f"    tags={tags!r},")
    lines.append(")")
    lines.append(f"def {dag_func}():")

    # Build per-task variable names for dependency wiring
    task_vars: Dict[str, str] = _unique_task_identifiers(tasks)

    for t in tasks:
        tid = t.get("taskId", "")
        var = task_vars[tid]
        action = t.get("action", "")
        params = t.get("params", {})
        parts = action.split(".")
        service = parts[1] if len(parts) >= 3 else ""
        operation = parts[2] if len(parts) >= 3 else ""

        if service == "s3" and operation == "ensure_bucket":
            bucket = params.get("bucket", "")
            lines.append("")
            lines.append(f"    {var} = S3CreateBucketOperator(")
            lines.append(f"        task_id={py_str_literal(tid)},")
            lines.append(f"        bucket_name={py_str_literal(bucket)},")
            lines.append(f"        region_name={py_str_literal(params.get('region', region))},")
            lines.append("        aws_conn_id='aws_default',")
            lines.append("    )")
        elif service == "athena" and operation == "execute_query":
            query = params.get("query", "")
            db = params.get("database", "default")
            out = params.get(
                "outputLocation", f"s3://aws-athena-query-results-{account_id}-{region}/"
            )
            lines.append("")
            lines.append(f"    {var} = AthenaOperator(")
            lines.append(f"        task_id={py_str_literal(tid)},")
            lines.append(f"        query={py_str_literal(query)},")
            lines.append(f"        database={py_str_literal(db)},")
            lines.append(f"        output_location={py_str_literal(out)},")
            lines.append("        aws_conn_id='aws_default',")
            lines.append("    )")
        elif service == "glue":
            # Glue operations use @task decorators
            lines.append("")
            lines.append(f"    @task(task_id={py_str_literal(tid)})")
            lines.append(f"    def {var}():")
            if operation == "ensure_database":
                db = params.get("database", "")
                lines.append(
                    f"        _ensure_glue_database({py_str_literal(db)}, {py_str_literal(region)})"
                )
            elif operation == "ensure_table":
                db = params.get("database", "")
                tbl = params.get("table", "")
                lines.append(
                    f"        _ensure_glue_table({py_str_literal(db)}, {py_str_literal(tbl)}, "
                    f"json.loads({json_literal(params)}), {py_str_literal(region)})"
                )
            else:
                lines.append(
                    f"        _execute_provider_action({py_str_literal(action)}, "
                    f"json.loads({json_literal(params)}), {py_str_literal(account_id)}, "
                    f"{py_str_literal(region)})"
                )
            lines.append(f"    {var}_result = {var}()")
        else:
            # Generic @task fallback
            lines.append("")
            lines.append(f"    @task(task_id={py_str_literal(tid)})")
            lines.append(f"    def {var}():")
            lines.append(
                f"        _execute_provider_action({py_str_literal(action)}, "
                f"json.loads({json_literal(params)}), {py_str_literal(account_id)}, "
                f"{py_str_literal(region)})"
            )
            lines.append(f"    {var}_result = {var}()")

    # Dependencies
    for t in tasks:
        tid = t.get("taskId", "")
        depends_on = t.get("dependsOn", [])
        if depends_on:
            var = task_vars[tid]
            for dep in depends_on:
                dep_var = task_vars.get(dep)
                if dep_var is None:
                    logger.warning(
                        "aws airflow: task %r dependsOn %r, which is not a "
                        "provider_action task in this DAG — edge skipped",
                        tid,
                        dep,
                    )
                    lines.append(f"    # dependency on non-DAG task {dep!r} skipped")
                    continue
                lines.append(f"    {dep_var} >> {var}")

    lines.append("")
    lines.append("")
    lines.append(f"{dag_func}()")

    return "\n".join(lines)
