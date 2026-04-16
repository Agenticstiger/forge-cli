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

from ..connection import SnowflakeConnection
from ..util.config import get_connection_params


def execute_sql(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """
    Execute arbitrary SQL statements in Snowflake.

    Useful for:
    - Custom DDL/DML operations
    - Data loading
    - Configuration changes
    - Advanced features not covered by dedicated actions
    """
    start_time = time.time()

    sql = action["sql"]
    account = action["account"]
    database = action.get("database")
    schema = action.get("schema")
    comment = action.get("comment", "Custom SQL execution")

    provider.debug_kv(event="execute_sql_started", comment=comment)

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
