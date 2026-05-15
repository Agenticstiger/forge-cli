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

# Allowlist of Snowflake privileges that may be interpolated into a GRANT
# statement. ``privilege`` arrives from contract input and flows directly
# into a DDL f-string — it cannot be parameterized, so an allowlist is the
# only defense against keyword injection. Covers the standard global,
# account-object, schema, and schema-object privileges plus the catch-all
# ``ALL PRIVILEGES``. Extend here if a new privilege is genuinely needed.
_ALLOWED_PRIVILEGES = frozenset(
    {
        "ALL",
        "ALL PRIVILEGES",
        "APPLYBUDGET",
        "AUDIT",
        "CREATE DATABASE",
        "CREATE ROLE",
        "CREATE SCHEMA",
        "CREATE TABLE",
        "CREATE VIEW",
        "CREATE FUNCTION",
        "CREATE PROCEDURE",
        "CREATE STAGE",
        "CREATE STREAM",
        "CREATE TASK",
        "CREATE PIPE",
        "CREATE SEQUENCE",
        "CREATE MATERIALIZED VIEW",
        "CREATE WAREHOUSE",
        "CREATE USER",
        "DELETE",
        "INSERT",
        "MODIFY",
        "MONITOR",
        "OPERATE",
        "OWNERSHIP",
        "REFERENCES",
        "REFERENCE_USAGE",
        "SELECT",
        "TRUNCATE",
        "UPDATE",
        "USAGE",
        "APPLY",
        "EXECUTE",
        "IMPORTED PRIVILEGES",
        "READ",
        "WRITE",
    }
)

# Allowlist of Snowflake object types that may be interpolated into the
# ``ON <OBJECT_TYPE>`` clause of a GRANT statement. Same rationale as
# ``_ALLOWED_PRIVILEGES`` — DDL identifiers/keywords cannot be bound.
_ALLOWED_OBJECT_TYPES = frozenset(
    {
        "ACCOUNT",
        "DATABASE",
        "SCHEMA",
        "TABLE",
        "VIEW",
        "MATERIALIZED VIEW",
        "STREAM",
        "TASK",
        "STAGE",
        "PIPE",
        "SEQUENCE",
        "FUNCTION",
        "PROCEDURE",
        "FILE FORMAT",
        "WAREHOUSE",
        "ROLE",
        "USER",
        "INTEGRATION",
        "EXTERNAL TABLE",
        "MASKING POLICY",
        "ROW ACCESS POLICY",
    }
)


def _validate_privilege(privilege: str) -> str:
    """Validate a GRANT privilege against the Snowflake allowlist.

    Returns the upper-cased privilege when valid; raises ``ValueError``
    with a clear message otherwise. The privilege flows into a DDL
    f-string and cannot be parameterized, so allowlisting is mandatory.
    """
    if not isinstance(privilege, str):
        raise ValueError(f"Invalid Snowflake privilege: {privilege!r}")
    normalized = privilege.strip().upper()
    if normalized not in _ALLOWED_PRIVILEGES:
        raise ValueError(
            f"Unsupported Snowflake privilege: {privilege!r}. "
            f"Allowed values: {sorted(_ALLOWED_PRIVILEGES)}"
        )
    return normalized


def _validate_object_type(object_type: str) -> str:
    """Validate a GRANT object type against the Snowflake allowlist.

    Returns the upper-cased object type when valid; raises ``ValueError``
    otherwise. Same injection rationale as :func:`_validate_privilege`.
    """
    if not isinstance(object_type, str):
        raise ValueError(f"Invalid Snowflake object type: {object_type!r}")
    normalized = object_type.strip().upper()
    if normalized not in _ALLOWED_OBJECT_TYPES:
        raise ValueError(
            f"Unsupported Snowflake object type: {object_type!r}. "
            f"Allowed values: {sorted(_ALLOWED_OBJECT_TYPES)}"
        )
    return normalized


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

    # SECURITY: ``privilege`` and ``object_type`` flow into a GRANT DDL
    # f-string below. DDL keywords cannot be bound as parameters, so both
    # are checked against module-level allowlists. Invalid input raises a
    # clear ``ValueError`` instead of reaching Snowflake.
    privilege = _validate_privilege(action["privilege"])
    object_type = _validate_object_type(action["object_type"])
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
            # Build object reference (``object_type`` is already validated
            # + upper-cased by ``_validate_object_type`` above).
            if object_type in ["TABLE", "VIEW", "MATERIALIZED VIEW", "STREAM"]:
                if not (database and schema and object_name):
                    raise ValueError(
                        f"{object_type} privilege requires database, schema, and object_name"
                    )
                object_ref = build_qualified_name(database, schema, object_name)
            elif object_type == "SCHEMA":
                if not (database and schema):
                    raise ValueError("SCHEMA privilege requires database and schema")
                object_ref = build_qualified_name(database, schema)
            elif object_type == "DATABASE":
                if not database:
                    raise ValueError("DATABASE privilege requires database")
                object_ref = quote_identifier(database)
            else:
                object_ref = quote_identifier(object_name)

            grant_sql = (
                f"GRANT {privilege} ON {object_type} {object_ref} "
                f"TO ROLE {quote_identifier(role)}"
            )
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
