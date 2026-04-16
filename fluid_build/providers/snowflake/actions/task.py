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

# fluid_build/providers/snowflake/actions/task.py
"""Snowflake task operations for scheduling."""

from __future__ import annotations

import time
from typing import Any, Dict

from ..connection import SnowflakeConnection
from ..util.config import get_connection_params
from ..util.names import build_qualified_name, normalize_table_name, quote_identifier


def ensure_task(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """
    Create or update Snowflake task for scheduled execution.

    Tasks enable scheduled or triggered SQL execution.
    """
    start_time = time.time()

    database = action["database"]
    schema = action["schema"]
    name = normalize_table_name(action["name"])
    sql = action["sql"]
    account = action["account"]
    schedule = action.get("schedule")
    warehouse_name = action.get("warehouse") or provider.warehouse
    after = action.get("after", [])  # Predecessor tasks

    provider.debug_kv(event="ensure_task_started", database=database, schema=schema, name=name)

    try:
        params = get_connection_params(
            account=account,
            warehouse=provider.warehouse,
            database=database,
            schema=schema,
            **provider._kwargs,
        )

        with SnowflakeConnection(**params) as conn:
            qualified_name = build_qualified_name(database, schema, name)

            # Create or replace task (idempotent)
            create_sql = f"CREATE OR REPLACE TASK {qualified_name}\n"
            create_sql += f"  WAREHOUSE = {quote_identifier(warehouse_name)}\n"

            if schedule:
                create_sql += f"  SCHEDULE = '{schedule}'\n"

            if after:
                # Task dependencies (runs after other tasks)
                after_tasks = [build_qualified_name(database, schema, t) for t in after]
                create_sql += f"  AFTER {', '.join(after_tasks)}\n"

            create_sql += f"AS\n{sql}"

            conn.execute(create_sql)

            provider.info_kv(
                event="task_created",
                database=database,
                schema=schema,
                name=name,
                schedule=schedule,
                warehouse=warehouse_name,
            )

            return {
                "status": "changed",
                "op": action["op"],
                "database": database,
                "schema": schema,
                "name": name,
                "changed": True,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    except Exception as e:
        provider.err_kv(
            event="ensure_task_failed", database=database, schema=schema, name=name, error=str(e)
        )
        raise


def resume_task(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """Resume (enable) a Snowflake task."""
    start_time = time.time()

    database = action["database"]
    schema = action["schema"]
    name = normalize_table_name(action["name"])
    account = action["account"]

    provider.debug_kv(event="resume_task_started", database=database, schema=schema, name=name)

    try:
        params = get_connection_params(
            account=account,
            warehouse=provider.warehouse,
            database=database,
            schema=schema,
            **provider._kwargs,
        )

        with SnowflakeConnection(**params) as conn:
            qualified_name = build_qualified_name(database, schema, name)
            resume_sql = f"ALTER TASK {qualified_name} RESUME"
            conn.execute(resume_sql)

            provider.info_kv(event="task_resumed", database=database, schema=schema, name=name)

            return {
                "status": "changed",
                "op": action["op"],
                "database": database,
                "schema": schema,
                "name": name,
                "changed": True,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    except Exception as e:
        provider.err_kv(
            event="resume_task_failed", database=database, schema=schema, name=name, error=str(e)
        )
        raise


def suspend_task(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """Suspend (disable) a Snowflake task."""
    start_time = time.time()

    database = action["database"]
    schema = action["schema"]
    name = normalize_table_name(action["name"])
    account = action["account"]

    provider.debug_kv(event="suspend_task_started", database=database, schema=schema, name=name)

    try:
        params = get_connection_params(
            account=account,
            warehouse=provider.warehouse,
            database=database,
            schema=schema,
            **provider._kwargs,
        )

        with SnowflakeConnection(**params) as conn:
            qualified_name = build_qualified_name(database, schema, name)
            suspend_sql = f"ALTER TASK {qualified_name} SUSPEND"
            conn.execute(suspend_sql)

            provider.info_kv(event="task_suspended", database=database, schema=schema, name=name)

            return {
                "status": "changed",
                "op": action["op"],
                "database": database,
                "schema": schema,
                "name": name,
                "changed": True,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    except Exception as e:
        provider.err_kv(
            event="suspend_task_failed", database=database, schema=schema, name=name, error=str(e)
        )
        raise
