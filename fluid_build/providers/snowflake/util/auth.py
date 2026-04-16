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

# fluid_build/providers/snowflake/util/auth.py
"""Snowflake authentication and environment reporting."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from .config import SECRET_KEYS, get_connection_params, resolve_snowflake_settings


def _normalize_auth_config(
    account_or_config: Any,
    warehouse: Optional[str] = None,
    database: Optional[str] = None,
    schema: Optional[str] = None,
    role: Optional[str] = None,
    user: Optional[str] = None,
    authenticator: Optional[str] = None,
) -> Dict[str, Any]:
    if isinstance(account_or_config, dict):
        resolved = dict(account_or_config)
    else:
        resolved = resolve_snowflake_settings(
            account=account_or_config,
            warehouse=warehouse,
            database=database,
            schema=schema,
            role=role,
            user=user,
            authenticator=authenticator,
        )

    resolved.setdefault("warehouse", None)
    resolved.setdefault("database", None)
    resolved.setdefault("schema", None)
    resolved.setdefault("role", None)
    resolved.setdefault("user", None)
    resolved.setdefault("authenticator", None)
    return resolved


def _required_actions(config: Dict[str, Any]) -> Dict[str, list[str]]:
    auth_ready_missing = [key for key in ["account", "user"] if not config.get(key)]
    if not any(
        config.get(key) for key in ["password", "private_key_path", "oauth_token", "authenticator"]
    ):
        auth_ready_missing.append("password/private-key/oauth-token/authenticator")

    apply_ready_missing = [
        key for key in ["account", "user", "warehouse", "database", "schema"] if not config.get(key)
    ]
    return {
        "auth_ready_missing": auth_ready_missing,
        "provider_ready_missing": apply_ready_missing,
    }


def get_auth_report(
    account_or_config: Any,
    warehouse: Optional[str] = None,
    database: Optional[str] = None,
    schema: Optional[str] = None,
    role: Optional[str] = None,
    user: Optional[str] = None,
    authenticator: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate authentication and environment diagnostics.

    Useful for troubleshooting connection issues and verifying
    configuration before deployment.
    """
    config = _normalize_auth_config(
        account_or_config,
        warehouse=warehouse,
        database=database,
        schema=schema,
        role=role,
        user=user,
        authenticator=authenticator,
    )

    readiness = _required_actions(config)

    # Defensively strip any secret entries from `sources`. The resolver
    # already filters these, but callers may pass a hand-built config dict
    # directly as `account_or_config`.
    sources = {k: v for k, v in (config.get("_sources") or {}).items() if k not in SECRET_KEYS}

    report = {
        "provider": "snowflake",
        "status": "success",
        "account": config.get("account"),
        "warehouse": config.get("warehouse"),
        "database": config.get("database"),
        "schema": config.get("schema"),
        "role": config.get("role"),
        "user": config.get("user"),
        "authenticator": config.get("authenticator"),
        "sources": sources,
        "environment_variables": {},
        "readiness": {
            "auth_ready": not readiness["auth_ready_missing"],
            "provider_ready": not readiness["provider_ready_missing"],
            "auth_ready_missing": readiness["auth_ready_missing"],
            "provider_ready_missing": readiness["provider_ready_missing"],
        },
    }

    # Check environment variables
    env_vars = [
        "SNOWFLAKE_ACCOUNT",
        "SNOWFLAKE_USER",
        "SNOWFLAKE_WAREHOUSE",
        "SNOWFLAKE_DATABASE",
        "SNOWFLAKE_SCHEMA",
        "SNOWFLAKE_ROLE",
        "SNOWFLAKE_AUTHENTICATOR",
        "SF_ACCOUNT",
        "SF_USER",
        "SF_WAREHOUSE",
    ]

    for var in env_vars:
        value = os.environ.get(var)
        if value:
            # Redact sensitive values
            if "PASSWORD" in var or "TOKEN" in var or "KEY" in var:
                report["environment_variables"][var] = "***REDACTED***"
            else:
                report["environment_variables"][var] = value

    # Check for password in environment (redacted)
    if os.environ.get("SNOWFLAKE_PASSWORD"):
        report["environment_variables"]["SNOWFLAKE_PASSWORD"] = "***REDACTED***"

    # Attempt to query current user (requires connection)
    try:
        current_identity = _get_current_identity(config)
        report.update(current_identity)
        report["connection_test"] = "success"
    except Exception as e:
        report["connection_test"] = "failed"
        report["connection_error"] = str(e)

    return report


def _get_current_identity(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Query Snowflake for current user and role.

    Requires active connection.
    """
    try:
        # Import connection utilities
        from ..connection import SnowflakeConnection

        # Build connection params
        params = get_connection_params(
            account=config.get("account"),
            warehouse=config.get("warehouse"),
            database=config.get("database"),
            schema=config.get("schema"),
            user=config.get("user"),
            role=config.get("role"),
            authenticator=config.get("authenticator"),
            password=config.get("password"),
            private_key_path=config.get("private_key_path"),
            private_key_passphrase=config.get("private_key_passphrase"),
            oauth_token=config.get("oauth_token"),
        )

        # Query current identity
        with SnowflakeConnection(**params) as conn:
            result = conn.execute(
                "SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), CURRENT_DATABASE(), CURRENT_SCHEMA()"
            )
            if not result:
                return {
                    "current_user": "unknown",
                    "current_role": "unknown",
                    "current_warehouse": "unknown",
                    "current_database": "unknown",
                    "current_schema": "unknown",
                }
            current_user, current_role, current_wh, current_db, current_schema = result[0]
            return {
                "current_user": current_user or "unknown",
                "current_role": current_role or "unknown",
                "current_warehouse": current_wh or "unknown",
                "current_database": current_db or "unknown",
                "current_schema": current_schema or "unknown",
            }

    except Exception as exc:
        raise RuntimeError(str(exc)) from exc
