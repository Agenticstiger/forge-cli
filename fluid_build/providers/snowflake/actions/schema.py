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

# fluid_build/providers/snowflake/actions/schema.py
"""Snowflake schema operations."""

from __future__ import annotations

import time
from typing import Any, Dict

from ..connection import SnowflakeConnection
from ..util.config import get_connection_params
from ..util.names import (
    build_qualified_name,
    normalize_database_name,
    normalize_schema_name,
    quote_identifier,
)


def ensure_schema(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """
    Ensure Snowflake schema exists with idempotent semantics.

    Creates schema if it doesn't exist, no-op if it already exists.
    """
    start_time = time.time()

    database = normalize_database_name(action["database"])
    schema = normalize_schema_name(action["schema"])
    account = action["account"]
    comment = action.get("comment")
    transient = action.get("transient", False)

    provider.debug_kv(event="ensure_schema_started", database=database, schema=schema)

    try:
        params = get_connection_params(
            account=account, warehouse=provider.warehouse, database=database, **provider._kwargs
        )

        with SnowflakeConnection(**params) as conn:
            # Check existence
            check_sql = f"SHOW SCHEMAS LIKE '{schema}' IN DATABASE {quote_identifier(database)}"
            result = conn.execute(check_sql)

            if result and len(result) > 0:
                # Schema already exists
                provider.debug_kv(event="schema_exists", database=database, schema=schema)

                return {
                    "status": "ok",
                    "op": action["op"],
                    "database": database,
                    "schema": schema,
                    "changed": False,
                    "duration_ms": int((time.time() - start_time) * 1000),
                }

            # Create schema
            qualified_name = build_qualified_name(database, schema)
            create_sql = f"CREATE {'TRANSIENT ' if transient else ''}SCHEMA {qualified_name}"
            if comment:
                escaped_comment = comment.replace("'", "''")
                create_sql += f" COMMENT = '{escaped_comment}'"

            conn.execute(create_sql)

            provider.info_kv(
                event="schema_created", database=database, schema=schema, transient=transient
            )

            return {
                "status": "changed",
                "op": action["op"],
                "database": database,
                "schema": schema,
                "changed": True,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    except Exception as e:
        provider.err_kv(
            event="ensure_schema_failed", database=database, schema=schema, error=str(e)
        )
        raise


def drop_schema(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """Drop Snowflake schema if it exists."""
    start_time = time.time()

    database = normalize_database_name(action["database"])
    schema = normalize_schema_name(action["schema"])
    account = action["account"]
    cascade = action.get("cascade", False)

    provider.debug_kv(
        event="drop_schema_started", database=database, schema=schema, cascade=cascade
    )

    try:
        params = get_connection_params(
            account=account, warehouse=provider.warehouse, database=database, **provider._kwargs
        )

        with SnowflakeConnection(**params) as conn:
            qualified_name = build_qualified_name(database, schema)
            drop_sql = f"DROP SCHEMA IF EXISTS {qualified_name}"
            if cascade:
                drop_sql += " CASCADE"

            conn.execute(drop_sql)

            provider.info_kv(event="schema_dropped", database=database, schema=schema)

            return {
                "status": "changed",
                "op": action["op"],
                "database": database,
                "schema": schema,
                "changed": True,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    except Exception as e:
        provider.err_kv(event="drop_schema_failed", database=database, schema=schema, error=str(e))
        raise
