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

# fluid_build/providers/snowflake/actions/stream.py
"""Snowflake stream operations for CDC."""

from __future__ import annotations

import time
from typing import Any, Dict

from ..connection import SnowflakeConnection
from ..util.config import get_connection_params
from ..util.names import build_qualified_name, normalize_table_name, quote_identifier


def ensure_stream(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """
    Create Snowflake stream for change data capture (CDC).

    Streams track changes to source tables for incremental processing.
    """
    start_time = time.time()

    database = action["database"]
    schema = action["schema"]
    name = normalize_table_name(action["name"])
    source_table = normalize_table_name(action["source_table"])
    account = action["account"]
    append_only = action.get("append_only", False)

    provider.debug_kv(
        event="ensure_stream_started",
        database=database,
        schema=schema,
        name=name,
        source_table=source_table,
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
            qualified_name = build_qualified_name(database, schema, name)
            source_qualified = build_qualified_name(database, schema, source_table)

            # Check if stream exists
            check_sql = f"""
                SELECT COUNT(*) 
                FROM {quote_identifier(database)}.INFORMATION_SCHEMA.TABLES 
                WHERE TABLE_SCHEMA = '{schema.upper()}' 
                AND TABLE_NAME = '{name.upper()}'
                AND TABLE_TYPE = 'STREAM'
            """
            result = conn.execute(check_sql)
            stream_exists = result and result[0][0] > 0

            if stream_exists:
                provider.debug_kv(
                    event="stream_exists", database=database, schema=schema, name=name
                )

                return {
                    "status": "ok",
                    "op": action["op"],
                    "database": database,
                    "schema": schema,
                    "name": name,
                    "changed": False,
                    "duration_ms": int((time.time() - start_time) * 1000),
                }

            # Create stream
            create_sql = f"CREATE STREAM {qualified_name} ON TABLE {source_qualified}"
            if append_only:
                create_sql += " APPEND_ONLY = TRUE"

            conn.execute(create_sql)

            provider.info_kv(
                event="stream_created",
                database=database,
                schema=schema,
                name=name,
                source_table=source_table,
                append_only=append_only,
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
            event="ensure_stream_failed", database=database, schema=schema, name=name, error=str(e)
        )
        raise
