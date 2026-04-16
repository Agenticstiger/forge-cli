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

# fluid_build/providers/snowflake/actions/view.py
"""Snowflake view operations."""

from __future__ import annotations

import time
from typing import Any, Dict

from ..connection import SnowflakeConnection
from ..util.config import get_connection_params
from ..util.names import build_qualified_name, normalize_table_name, quote_identifier


def ensure_view(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """Create or replace Snowflake view."""
    start_time = time.time()

    database = action["database"]
    schema = action["schema"]
    name = normalize_table_name(action["name"])
    query = action["query"]
    account = action["account"]
    secure = action.get("secure", False)

    provider.debug_kv(
        event="ensure_view_started", database=database, schema=schema, name=name, secure=secure
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

            # Create or replace view (idempotent)
            create_sql = (
                f"CREATE OR REPLACE {'SECURE ' if secure else ''}VIEW {qualified_name} AS {query}"
            )
            conn.execute(create_sql)

            provider.info_kv(
                event="view_created", database=database, schema=schema, name=name, secure=secure
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
            event="ensure_view_failed", database=database, schema=schema, name=name, error=str(e)
        )
        raise


def ensure_materialized_view(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """Create or replace Snowflake materialized view."""
    start_time = time.time()

    database = action["database"]
    schema = action["schema"]
    name = normalize_table_name(action["name"])
    query = action["query"]
    account = action["account"]
    secure = action.get("secure", False)
    cluster_by = action.get("cluster_by", [])

    provider.debug_kv(
        event="ensure_materialized_view_started", database=database, schema=schema, name=name
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

            # Create or replace materialized view
            create_sql = (
                f"CREATE OR REPLACE {'SECURE ' if secure else ''}MATERIALIZED VIEW {qualified_name}"
            )

            if cluster_by:
                quoted_keys = [quote_identifier(key) for key in cluster_by]
                create_sql += f" CLUSTER BY ({', '.join(quoted_keys)})"

            create_sql += f" AS {query}"

            conn.execute(create_sql)

            provider.info_kv(
                event="materialized_view_created",
                database=database,
                schema=schema,
                name=name,
                cluster_by=cluster_by,
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
            event="ensure_materialized_view_failed",
            database=database,
            schema=schema,
            name=name,
            error=str(e),
        )
        raise
