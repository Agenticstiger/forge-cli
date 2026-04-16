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

# fluid_build/providers/snowflake/plan/planner.py
"""
Snowflake Planning Engine - Generates execution plan from FLUID contract.

5-Phase Architecture:
1. Infrastructure: Databases, schemas, warehouses
2. IAM: Roles, grants, row-level security
3. Build: Stored procedures, UDFs, tasks
4. Expose: Tables, views, streams
5. Schedule: Task orchestration, pipes

Enhanced with governance:
- Tag extraction from contract (mirrors GCP labels)
- Policy tag support for column-level classification
- Metadata propagation to Snowflake objects
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from fluid_build.providers._sql_safety import (
    quote_string_literal,
    validate_ident,
    validate_sql_expression_allowlist,
)

from ..util.config import resolve_env_templates as _resolve_env_templates
from ..util.metadata import extract_snowflake_tags


def _first_contract_value(contract: Mapping[str, Any], key: str) -> Optional[str]:
    binding = contract.get("binding", {})
    if isinstance(binding, Mapping) and binding.get("platform") == "snowflake":
        location = binding.get("location", {})
        properties = binding.get("properties", {})
        for source in (location, properties):
            if isinstance(source, Mapping) and source.get(key):
                return _resolve_env_templates(source.get(key))

    for expose in contract.get("exposes", []) or []:
        if not isinstance(expose, Mapping):
            continue
        binding = expose.get("binding", {})
        if not isinstance(binding, Mapping) or binding.get("platform") != "snowflake":
            continue
        location = binding.get("location", expose.get("location", {}))
        properties = binding.get("properties", {})
        location_properties = (
            location.get("properties", {}) if isinstance(location, Mapping) else {}
        )
        for source in (location, properties, location_properties):
            if isinstance(source, Mapping) and source.get(key):
                return _resolve_env_templates(source.get(key))

    for build in contract.get("builds", []) or []:
        if not isinstance(build, Mapping):
            continue
        execution = build.get("execution", {})
        runtime = execution.get("runtime", {}) if isinstance(execution, Mapping) else {}
        resources = runtime.get("resources", {}) if isinstance(runtime, Mapping) else {}
        if runtime.get("platform") == "snowflake" and isinstance(resources, Mapping):
            if resources.get(key):
                return _resolve_env_templates(resources.get(key))

    return None


def plan_actions(
    contract: Mapping[str, Any],
    account: str,
    warehouse: str,
    database: Optional[str],
    schema: str,
    logger=None,
) -> List[Dict[str, Any]]:
    """
    Generate ordered action list from FLUID contract.

    Phases ensure dependency ordering:
    - Infrastructure must exist before schemas
    - Schemas must exist before tables
    - Tables must exist before views/streams
    - IAM can run in parallel with build phase
    - Schedule runs after all objects exist
    """
    actions: List[Dict[str, Any]] = []

    # Phase 1: Infrastructure (databases, schemas, warehouses)
    actions.extend(_plan_infrastructure(contract, account, warehouse, database, schema, logger))

    # Phase 2: IAM (roles, grants, row-level security)
    actions.extend(_plan_iam(contract, account, database, schema, logger))

    # Phase 3: Build (stored procedures, UDFs, tasks)
    actions.extend(_plan_build(contract, account, database, schema, logger))

    # Phase 4: Expose (tables, views, streams)
    actions.extend(_plan_expose(contract, account, database, schema, logger))

    # Phase 5: Schedule (task orchestration, pipes)
    actions.extend(_plan_schedule(contract, account, database, schema, logger))

    return actions


def _plan_infrastructure(
    contract: Mapping[str, Any],
    account: str,
    warehouse: str,
    database: Optional[str],
    schema: str,
    logger=None,
) -> List[Dict[str, Any]]:
    """Phase 1: Create databases and schemas."""
    actions: List[Dict[str, Any]] = []

    # Resolve database from multiple sources
    db_name = (
        _first_contract_value(contract, "database")
        or database
        or contract.get("metadata", {}).get("name", "").upper().replace("-", "_")
    )

    schema_name = _first_contract_value(contract, "schema") or schema

    # Ensure database exists
    if db_name:
        actions.append(
            {
                "id": f"database_{db_name}",
                "op": "sf.database.ensure",
                "phase": "infrastructure",
                "account": account,
                "database": db_name,
                "transient": False,
                "comment": f"Database for {contract.get('metadata', {}).get('name', 'FLUID contract')}",
            }
        )

    # Ensure schema exists
    if db_name and schema_name:
        actions.append(
            {
                "id": f"schema_{db_name}_{schema_name}",
                "op": "sf.schema.ensure",
                "phase": "infrastructure",
                "account": account,
                "database": db_name,
                "schema": schema_name,
                "transient": False,
                "comment": f"Schema for {contract.get('metadata', {}).get('name', 'FLUID contract')}",
            }
        )

    return actions


def _plan_iam(
    contract: Mapping[str, Any],
    account: str,
    database: Optional[str],
    schema: str,
    logger=None,
) -> List[Dict[str, Any]]:
    """Phase 2: Configure roles and grants."""
    actions: List[Dict[str, Any]] = []

    # Extract IAM configuration from contract
    security = contract.get("security", {})
    access_control = security.get("access_control", {})

    # Grant privileges to roles
    grants = access_control.get("grants", [])
    for grant in grants:
        role = grant.get("role")
        privilege = grant.get("privilege")
        object_type = grant.get("object_type")
        object_name = grant.get("object_name")

        if role and privilege:
            actions.append(
                {
                    "id": f"grant_{role}_{privilege}_{object_type}_{object_name}",
                    "op": "sf.grant.privilege",
                    "phase": "iam",
                    "account": account,
                    "role": role,
                    "privilege": privilege,
                    "object_type": object_type or "TABLE",
                    "object_name": object_name,
                    "database": database,
                }
            )

    # Row-level security policies
    row_level_security = security.get("row_level_security", [])
    for policy in row_level_security:
        table = policy.get("table")
        role = policy.get("role")
        condition = policy.get("condition")

        if table and role and condition:
            try:
                safe_table = validate_ident(str(table))
                safe_role_name = validate_ident(str(role))
                safe_role = quote_string_literal(safe_role_name)
                safe_condition = validate_sql_expression_allowlist(str(condition))
            except ValueError as exc:
                if logger is not None:
                    logger.warning(
                        "snowflake_row_level_security_skipped table=%r role=%r error=%s",
                        table,
                        role,
                        exc,
                    )
                continue

            safe_role_id = safe_role_name.lower()
            actions.append(
                {
                    "id": f"rls_{safe_table}_{safe_role_id}",
                    "op": "sf.sql.execute",
                    "phase": "iam",
                    "account": account,
                    "database": database,
                    "sql": (
                        f"CREATE OR REPLACE ROW ACCESS POLICY {safe_table}_rls "
                        "AS (val VARCHAR) RETURNS BOOLEAN -> "
                        f"CASE WHEN CURRENT_ROLE() = {safe_role} "
                        f"THEN {safe_condition} ELSE FALSE END"
                    ),
                    "comment": f"Row-level security for {safe_table}",
                }
            )

    return actions


def _plan_build(
    contract: Mapping[str, Any],
    account: str,
    database: Optional[str],
    schema: str,
    logger=None,
) -> List[Dict[str, Any]]:
    """Phase 3: Create stored procedures, UDFs, tasks."""
    actions: List[Dict[str, Any]] = []
    resolved_database = _first_contract_value(contract, "database") or database
    resolved_schema = _first_contract_value(contract, "schema") or schema

    # Extract build configuration
    build = contract.get("build", {})

    # Stored procedures
    procedures = build.get("procedures", [])
    for proc in procedures:
        name = proc.get("name")
        language = proc.get("language", "SQL")
        body = proc.get("body")
        params = proc.get("parameters", [])

        if name and body:
            actions.append(
                {
                    "id": f"procedure_{name}",
                    "op": "sf.procedure.ensure",
                    "phase": "build",
                    "account": account,
                    "database": resolved_database,
                    "schema": resolved_schema,
                    "name": name,
                    "language": language,
                    "parameters": params,
                    "body": body,
                }
            )

    # User-defined functions (UDFs)
    udfs = build.get("udfs", [])
    for udf in udfs:
        name = udf.get("name")
        language = udf.get("language", "SQL")
        return_type = udf.get("return_type", "VARCHAR")
        body = udf.get("body")
        params = udf.get("parameters", [])

        if name and body:
            actions.append(
                {
                    "id": f"udf_{name}",
                    "op": "sf.udf.ensure",
                    "phase": "build",
                    "account": account,
                    "database": resolved_database,
                    "schema": resolved_schema,
                    "name": name,
                    "language": language,
                    "return_type": return_type,
                    "parameters": params,
                    "body": body,
                }
            )

    # Embedded SQL scripts
    sql_scripts = build.get("sql", [])
    for i, script in enumerate(sql_scripts):
        if isinstance(script, str):
            sql_text = script
            script_id = f"sql_{i}"
        elif isinstance(script, dict):
            sql_text = script.get("sql")
            script_id = script.get("id", f"sql_{i}")
        else:
            continue

        if sql_text:
            actions.append(
                {
                    "id": script_id,
                    "op": "sf.sql.execute",
                    "phase": "build",
                    "account": account,
                    "database": resolved_database,
                    "schema": resolved_schema,
                    "sql": _resolve_env_templates(sql_text),
                }
            )

    # Modern builds[] support for native SQL happy-path contracts.
    for index, build_entry in enumerate(contract.get("builds", []) or []):
        if not isinstance(build_entry, Mapping):
            continue

        properties = build_entry.get("properties", {})
        if not isinstance(properties, Mapping):
            properties = {}

        execution = build_entry.get("execution", {})
        runtime = execution.get("runtime", {}) if isinstance(execution, Mapping) else {}
        resources = runtime.get("resources", {}) if isinstance(runtime, Mapping) else {}

        build_database = (
            resources.get("database") if isinstance(resources, Mapping) else None
        ) or resolved_database
        build_schema = (
            resources.get("schema") if isinstance(resources, Mapping) else None
        ) or resolved_schema

        sql_text = build_entry.get("sql") or properties.get("sql")
        if not sql_text:
            continue

        build_id = build_entry.get("id", f"build_{index}")
        actions.append(
            {
                "id": build_id,
                "op": "sf.sql.execute",
                "phase": "build",
                "account": account,
                "database": _resolve_env_templates(build_database),
                "schema": _resolve_env_templates(build_schema),
                "sql": _resolve_env_templates(sql_text),
                "comment": build_entry.get("description") or build_entry.get("name"),
            }
        )

    return actions


def _plan_expose(
    contract: Mapping[str, Any],
    account: str,
    database: Optional[str],
    schema: str,
    logger=None,
) -> List[Dict[str, Any]]:
    """Phase 4: Create tables, views, streams with governance metadata."""
    actions: List[Dict[str, Any]] = []
    resolved_database = _first_contract_value(contract, "database") or database
    resolved_schema = _first_contract_value(contract, "schema") or schema

    # Process exposes array (0.5.7/0.7.1 pattern)
    for expose in contract.get("exposes", []):
        expose_id = expose.get("exposeId", expose.get("id"))

        # Extract tags from contract + expose (8 sources, mirrors GCP)
        table_tags = extract_snowflake_tags(contract, expose)

        # Get binding information
        binding = expose.get("binding", {})
        location = binding.get("location", expose.get("location", {}))
        properties = binding.get("properties", {})
        location_properties = (
            location.get("properties", {}) if isinstance(location, Mapping) else {}
        )
        format_type = binding.get("format") or location.get("format") or "snowflake_table"

        # Resolve names
        db_name = _resolve_env_templates(location.get("database")) or resolved_database
        schema_name = _resolve_env_templates(location.get("schema")) or resolved_schema
        table_name = _resolve_env_templates(location.get("table")) or expose_id

        # Tables from contract schema
        contract_schema = expose.get("contract", {})
        fields = contract_schema.get("schema") or expose.get("schema", [])

        if table_name and fields and format_type == "snowflake_table":
            # Convert FLUID fields to Snowflake columns with tags
            columns = []
            for field in fields:
                col_name = field.get("name")
                col_type = _map_fluid_type_to_snowflake(field.get("type", "string"))
                nullable = field.get("nullable", not field.get("required", False))
                description = field.get("description")

                col_def = {
                    "name": col_name,
                    "type": col_type,
                    "nullable": nullable,
                    "labels": field.get("labels", {}),  # Pass labels for tag extraction
                }
                if description:
                    col_def["comment"] = description

                columns.append(col_def)

            # Create table action with tags
            actions.append(
                {
                    "id": f"table_{db_name}_{schema_name}_{table_name}",
                    "op": "sf.table.ensure",
                    "phase": "expose",
                    "account": account,
                    "database": db_name,
                    "schema": schema_name,
                    "table": table_name,
                    "columns": columns,
                    "cluster_by": contract_schema.get("cluster_by")
                    or properties.get("cluster_by")
                    or location_properties.get("cluster_by")
                    or expose.get("cluster_by", []),
                    "comment": expose.get("description")
                    or expose.get("title")
                    or properties.get("comment"),
                    "tags": table_tags,  # Table-level tags
                    "contract": contract,  # Full contract for metadata
                }
            )

    # Views
    views = contract.get("views", [])
    db_name = resolved_database
    schema_name = resolved_schema
    for view in views:
        view_name = view.get("name")
        query = view.get("query")
        materialized = view.get("materialized", False)

        if view_name and query:
            op = "sf.view.materialized.ensure" if materialized else "sf.view.ensure"
            actions.append(
                {
                    "id": f"view_{view_name}",
                    "op": op,
                    "phase": "expose",
                    "account": account,
                    "database": db_name,
                    "schema": schema_name,
                    "name": view_name,
                    "query": _resolve_env_templates(query),
                    "secure": view.get("secure", False),
                }
            )

    # Streams (for CDC)
    streams = contract.get("streams", [])
    for stream in streams:
        stream_name = stream.get("name")
        source_table = stream.get("source_table")

        if stream_name and source_table:
            actions.append(
                {
                    "id": f"stream_{stream_name}",
                    "op": "sf.stream.ensure",
                    "phase": "expose",
                    "account": account,
                    "database": db_name,
                    "schema": schema_name,
                    "name": stream_name,
                    "source_table": source_table,
                    "append_only": stream.get("append_only", False),
                }
            )

    return actions


def _plan_schedule(
    contract: Mapping[str, Any],
    account: str,
    database: Optional[str],
    schema: str,
    logger=None,
) -> List[Dict[str, Any]]:
    """Phase 5: Configure task orchestration."""
    actions: List[Dict[str, Any]] = []

    # Extract orchestration configuration
    orchestration = contract.get("orchestration", {})

    # Tasks
    tasks = orchestration.get("tasks", [])
    for task in tasks:
        task_name = task.get("name")
        schedule = task.get("schedule")
        sql = task.get("sql")

        if task_name and sql:
            actions.append(
                {
                    "id": f"task_{task_name}",
                    "op": "sf.task.ensure",
                    "phase": "schedule",
                    "account": account,
                    "database": database,
                    "schema": schema,
                    "name": task_name,
                    "schedule": schedule,
                    "sql": sql,
                    "warehouse": task.get("warehouse"),
                    "after": task.get("after", []),  # Task dependencies
                }
            )

            # Auto-resume task if requested
            if task.get("enabled", True):
                actions.append(
                    {
                        "id": f"task_resume_{task_name}",
                        "op": "sf.task.resume",
                        "phase": "schedule",
                        "account": account,
                        "database": database,
                        "schema": schema,
                        "name": task_name,
                    }
                )

    return actions


def _map_fluid_type_to_snowflake(fluid_type: str) -> str:
    """
    Map FLUID type to Snowflake data type.

    FLUID Types → Snowflake Types:
    - string → VARCHAR
    - integer → NUMBER(38,0)
    - long → NUMBER(38,0)
    - float → FLOAT
    - double → DOUBLE
    - decimal → NUMBER(38,10)
    - boolean → BOOLEAN
    - date → DATE
    - timestamp → TIMESTAMP_NTZ
    - binary → BINARY
    - array → ARRAY
    - object → OBJECT
    """
    raw_type = (fluid_type or "string").strip()
    lower_type = raw_type.lower()
    parameterized_prefixes = {
        "decimal",
        "numeric",
        "number",
        "varchar",
        "char",
        "character",
        "binary",
        "varbinary",
    }
    base_type = lower_type.split("(", 1)[0].strip()
    if "(" in lower_type and base_type in parameterized_prefixes:
        return raw_type.upper()

    type_map = {
        "string": "VARCHAR",
        "integer": "NUMBER(38,0)",
        "int": "NUMBER(38,0)",
        "long": "NUMBER(38,0)",
        "bigint": "NUMBER(38,0)",
        "float": "FLOAT",
        "double": "DOUBLE",
        "decimal": "NUMBER(38,10)",
        "numeric": "NUMBER(38,10)",
        "boolean": "BOOLEAN",
        "bool": "BOOLEAN",
        "date": "DATE",
        "timestamp": "TIMESTAMP_NTZ",
        "datetime": "TIMESTAMP_NTZ",
        "timestamp_ntz": "TIMESTAMP_NTZ",
        "timestamp_tz": "TIMESTAMP_TZ",
        "timestamp_ltz": "TIMESTAMP_LTZ",
        "time": "TIME",
        "binary": "BINARY",
        "array": "ARRAY",
        "object": "OBJECT",
        "variant": "VARIANT",
        "geography": "GEOGRAPHY",
        "geometry": "GEOMETRY",
    }

    return type_map.get(lower_type, "VARCHAR")
