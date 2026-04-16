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

# fluid_build/providers/snowflake/actions/database.py
"""Snowflake database operations."""

from __future__ import annotations

import time
from typing import Any, Dict

from ..connection import SnowflakeConnection
from ..util.config import get_connection_params
from ..util.names import normalize_database_name, quote_identifier


def ensure_database(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """
    Ensure Snowflake database exists with idempotent semantics.

    Creates database if it doesn't exist, no-op if it already exists.
    """
    start_time = time.time()

    database = normalize_database_name(action["database"])
    account = action["account"]
    comment = action.get("comment")
    transient = action.get("transient", False)

    provider.debug_kv(event="ensure_database_started", database=database, account=account)

    try:
        # Get connection parameters
        params = get_connection_params(
            account=account, warehouse=provider.warehouse, **provider._kwargs
        )

        # Connect and check if database exists
        with SnowflakeConnection(**params) as conn:
            # Check existence
            check_sql = f"SHOW DATABASES LIKE '{database}'"
            result = conn.execute(check_sql)

            if result and len(result) > 0:
                # Database already exists
                provider.debug_kv(event="database_exists", database=database)

                return {
                    "status": "ok",
                    "op": action["op"],
                    "database": database,
                    "changed": False,
                    "duration_ms": int((time.time() - start_time) * 1000),
                }

            # Create database
            create_sql = (
                f"CREATE {'TRANSIENT ' if transient else ''}DATABASE {quote_identifier(database)}"
            )
            if comment:
                # Escape single quotes in comment
                escaped_comment = comment.replace("'", "''")
                create_sql += f" COMMENT = '{escaped_comment}'"

            conn.execute(create_sql)

            provider.info_kv(event="database_created", database=database, transient=transient)

            return {
                "status": "changed",
                "op": action["op"],
                "database": database,
                "changed": True,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    except Exception as e:
        provider.err_kv(event="ensure_database_failed", database=database, error=str(e))
        raise


def drop_database(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """Drop Snowflake database if it exists."""
    start_time = time.time()

    database = normalize_database_name(action["database"])
    account = action["account"]
    cascade = action.get("cascade", False)

    provider.debug_kv(event="drop_database_started", database=database, cascade=cascade)

    try:
        params = get_connection_params(
            account=account, warehouse=provider.warehouse, **provider._kwargs
        )

        with SnowflakeConnection(**params) as conn:
            # Drop database (IF EXISTS makes it idempotent)
            drop_sql = f"DROP DATABASE IF EXISTS {quote_identifier(database)}"
            if cascade:
                drop_sql += " CASCADE"

            conn.execute(drop_sql)

            provider.info_kv(event="database_dropped", database=database)

            return {
                "status": "changed",
                "op": action["op"],
                "database": database,
                "changed": True,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    except Exception as e:
        provider.err_kv(event="drop_database_failed", database=database, error=str(e))
        raise
