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

# fluid_build/providers/snowflake/actions/grants.py
"""Snowflake RBAC grant operations."""

from __future__ import annotations

import time
from typing import Any, Dict

from ..connection import SnowflakeConnection
from ..util.config import get_connection_params
from ..util.names import build_qualified_name, quote_identifier


def grant_role(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """Grant Snowflake role to user or another role."""
    start_time = time.time()

    role = action["role"]
    to_type = action.get("to_type", "USER")  # USER or ROLE
    to_name = action["to_name"]
    account = action["account"]

    provider.debug_kv(event="grant_role_started", role=role, to_type=to_type, to_name=to_name)

    try:
        params = get_connection_params(
            account=account, warehouse=provider.warehouse, **provider._kwargs
        )

        with SnowflakeConnection(**params) as conn:
            grant_sql = (
                f"GRANT ROLE {quote_identifier(role)} TO {to_type} {quote_identifier(to_name)}"
            )
            conn.execute(grant_sql)

            provider.info_kv(event="role_granted", role=role, to_type=to_type, to_name=to_name)

            return {
                "status": "changed",
                "op": action["op"],
                "role": role,
                "to_type": to_type,
                "to_name": to_name,
                "changed": True,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    except Exception as e:
        # Check if error is "already granted" (idempotent)
        if "already granted" in str(e).lower():
            provider.debug_kv(event="role_already_granted", role=role, to_name=to_name)
            return {
                "status": "ok",
                "op": action["op"],
                "role": role,
                "to_name": to_name,
                "changed": False,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

        provider.err_kv(event="grant_role_failed", role=role, to_name=to_name, error=str(e))
        raise


def grant_privilege(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """Grant privilege on Snowflake object to role."""
    start_time = time.time()

    privilege = action["privilege"]  # SELECT, INSERT, UPDATE, DELETE, etc.
    object_type = action["object_type"]  # TABLE, VIEW, DATABASE, SCHEMA, etc.
    object_name = action.get("object_name")
    database = action.get("database")
    schema = action.get("schema")
    role = action["role"]
    account = action["account"]

    provider.debug_kv(
        event="grant_privilege_started", privilege=privilege, object_type=object_type, role=role
    )

    try:
        params = get_connection_params(
            account=account, warehouse=provider.warehouse, database=database, **provider._kwargs
        )

        with SnowflakeConnection(**params) as conn:
            # Build object reference
            if object_type.upper() in ["TABLE", "VIEW", "MATERIALIZED VIEW", "STREAM"]:
                if not (database and schema and object_name):
                    raise ValueError(
                        f"{object_type} privilege requires database, schema, and object_name"
                    )
                object_ref = build_qualified_name(database, schema, object_name)
            elif object_type.upper() == "SCHEMA":
                if not (database and schema):
                    raise ValueError("SCHEMA privilege requires database and schema")
                object_ref = build_qualified_name(database, schema)
            elif object_type.upper() == "DATABASE":
                if not database:
                    raise ValueError("DATABASE privilege requires database")
                object_ref = quote_identifier(database)
            else:
                object_ref = object_name

            grant_sql = f"GRANT {privilege} ON {object_type.upper()} {object_ref} TO ROLE {quote_identifier(role)}"
            conn.execute(grant_sql)

            provider.info_kv(
                event="privilege_granted", privilege=privilege, object_type=object_type, role=role
            )

            return {
                "status": "changed",
                "op": action["op"],
                "privilege": privilege,
                "object_type": object_type,
                "role": role,
                "changed": True,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    except Exception as e:
        # Check if error is "already granted" (idempotent)
        if "already granted" in str(e).lower():
            provider.debug_kv(event="privilege_already_granted", privilege=privilege, role=role)
            return {
                "status": "ok",
                "op": action["op"],
                "privilege": privilege,
                "role": role,
                "changed": False,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

        provider.err_kv(
            event="grant_privilege_failed",
            privilege=privilege,
            object_type=object_type,
            role=role,
            error=str(e),
        )
        raise
