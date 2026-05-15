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

# fluid_build/providers/snowflake/actions/sql.py
"""Snowflake arbitrary SQL execution."""

from __future__ import annotations

import time
from typing import Any, Dict

from ..._sql_safety import SqlAllowlistError, parse_and_allowlist_sql
from ..connection import SnowflakeConnection
from ..util.config import get_connection_params, resolve_env_templates


def execute_sql(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """
    Execute arbitrary SQL statements in Snowflake.

    Useful for:
    - Custom DDL/DML operations
    - Data loading
    - Configuration changes
    - Advanced features not covered by dedicated actions

    The SQL body is parsed with sqlglot and every statement is checked against
    the ``custom`` allowlist (:func:`fluid_build.providers._sql_safety.
    parse_and_allowlist_sql`) before execution. Account/role-level statements
    (CREATE USER, DROP ROLE, ALTER ACCOUNT, ...) and anything the parser cannot
    structurally classify are rejected fail-closed.

    ``{{ env.VAR }}`` placeholders in the SQL body, database, and schema are
    resolved from the environment before validation and execution — matching the
    behaviour of every other Snowflake action (provisionDataset, scheduleTask,
    etc.).  Unresolved placeholders (env var absent) are left intact so the
    warehouse error message identifies the missing variable clearly.
    """
    start_time = time.time()

    sql = resolve_env_templates(action["sql"])
    account = action["account"]
    database = resolve_env_templates(action.get("database"))
    schema = resolve_env_templates(action.get("schema"))
    comment = action.get("comment", "Custom SQL execution")

    provider.debug_kv(event="execute_sql_started", comment=comment)

    # Statement-kind allowlist gate. Raised as SqlAllowlistError (a ValueError
    # subclass) before any connection is opened so a rejected payload never
    # reaches the warehouse.
    try:
        statements = parse_and_allowlist_sql(sql, surface="custom")
    except SqlAllowlistError as e:
        provider.err_kv(event="execute_sql_rejected", comment=comment, reason=str(e))
        raise

    # Audit trail: custom SQL is a sensitive capability — record that it ran,
    # what it structurally was, and how many statements, without echoing the
    # raw body into logs.
    provider.info_kv(
        event="custom_sql_allowed",
        comment=comment,
        database=database,
        schema=schema,
        statement_count=len(statements),
        statement_kinds=[type(s).__name__ for s in statements],
    )

    try:
        params = get_connection_params(
            account=account,
            warehouse=provider.warehouse,
            database=database,
            schema=schema,
            **provider._kwargs,
        )

        with SnowflakeConnection(**params) as conn:
            # Execute SQL (may be multiple statements)
            if ";" in sql and sql.strip().count(";") > 1:
                # Multiple statements - use executescript
                conn.executescript(sql)
            else:
                # Single statement
                conn.execute(sql)

            provider.info_kv(
                event="sql_executed", comment=comment, database=database, schema=schema
            )

            return {
                "status": "changed",
                "op": action["op"],
                "database": database,
                "schema": schema,
                "comment": comment,
                "changed": True,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    except Exception as e:
        provider.err_kv(event="execute_sql_failed", comment=comment, error=str(e))
        raise
