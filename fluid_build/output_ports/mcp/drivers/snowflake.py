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

"""Snowflake driver for the consumer MCP output-port server.

Phase-1 identity model:

* Username + password OR private-key authentication, resolved
  through ``snowflake.connector.connect`` reading from environment
  variables (``SNOWFLAKE_ACCOUNT``, ``SNOWFLAKE_USER``,
  ``SNOWFLAKE_PASSWORD``, ``SNOWFLAKE_PRIVATE_KEY_PATH``,
  ``SNOWFLAKE_WAREHOUSE``, ``SNOWFLAKE_ROLE``).
* Phase-3 will add OAuth-token passthrough so the caller's identity
  is honoured by Snowflake's row-access policies and column masks.

Quoting:

* Snowflake double-quotes identifiers when case-sensitivity matters.
  Phase-1 keeps identifiers UPPERCASE-folded by default (Snowflake's
  natural case) and double-quotes only when the identifier contains
  characters the safe-ident regex can't accept.

Parameter binding:

* Snowflake DB-API uses ``%(name)s`` named placeholders. The query
  compiler emits ``:p_<index>``; this driver rewrites to the
  Snowflake form before executing.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.providers._sql_safety import validate_ident, validate_sql_expression_allowlist

from .base import (
    DriverDescriptor,
    EngineDriver,
    QueryResult,
    UnsupportedBindingError,
    get_binding,
    guard_against_injection_markers,
)

# Rewrite ``:p_<index>`` (compiler form) → ``%(p_<index>)s`` (Snowflake DB-API
# pyformat). Word-boundary anchored so ``:p_1`` is not rewritten inside
# ``:p_10`` — a per-index ``str.replace`` loop corrupts placeholders ≥11
# (``:p_10`` → ``%(p_1)s0``). Mirrors the postgres / athena driver pattern.
_PARAM_REWRITE = re.compile(r":p_(\d+)\b")


class SnowflakeDriver(EngineDriver):
    """Snowflake driver.

    Requires the ``snowflake`` extra:
    ``pip install 'data-product-forge[snowflake]'``.
    """

    name = "snowflake"

    def __init__(
        self,
        *,
        expose: Mapping[str, Any],
        contract: Mapping[str, Any],
        logger: Optional[logging.Logger] = None,
        connection_options: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(
            expose=expose,
            contract=contract,
            logger=logger,
            connection_options=connection_options,
        )
        platform, fmt, location = get_binding(expose)
        if platform != "snowflake":
            raise UnsupportedBindingError(
                f"SnowflakeDriver requires binding.platform='snowflake'; got {platform!r}"
            )
        if fmt != "snowflake_table":
            raise UnsupportedBindingError(
                f"SnowflakeDriver only handles binding.format='snowflake_table'; got {fmt!r}"
            )
        database_raw = location.get("database") or location.get("dataset")
        schema_raw = location.get("schema") or location.get("namespace")
        table_raw = location.get("table") or location.get("name")
        if not isinstance(database_raw, str) or not database_raw:
            raise UnsupportedBindingError("SnowflakeDriver requires binding.location.database")
        if not isinstance(schema_raw, str) or not schema_raw:
            raise UnsupportedBindingError("SnowflakeDriver requires binding.location.schema")
        if not isinstance(table_raw, str) or not table_raw:
            raise UnsupportedBindingError("SnowflakeDriver requires binding.location.table")
        validate_ident(database_raw)
        validate_ident(schema_raw)
        validate_ident(table_raw)
        self._database = database_raw
        self._schema = schema_raw
        self._table = table_raw
        self._table_reference = f"{database_raw}.{schema_raw}.{table_raw}"
        self._connection = None  # lazy

    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            platform="snowflake",
            format="snowflake_table",
            table_reference=self._table_reference,
            dialect="snowflake",
            capabilities={
                "describe": True,
                "sample": True,
                "query": True,
                "query_sql": True,
                "lineage": False,
                "quality": False,
            },
        )

    def execute(
        self,
        *,
        sql: str,
        params: Sequence[Any] = (),
        timeout_seconds: Optional[float] = None,
    ) -> QueryResult:
        connection = self._get_connection()
        rendered = _PARAM_REWRITE.sub(r"%(p_\1)s", sql)
        guard_against_injection_markers(rendered)
        bound = {f"p_{index}": value for index, value in enumerate(params)}
        cursor = connection.cursor()
        try:
            if timeout_seconds is not None:
                cursor.execute(
                    f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS={int(max(1, timeout_seconds))}"
                )
            cursor.execute(rendered, bound or None)
            description = cursor.description or []
            columns = tuple(
                str(col.name if hasattr(col, "name") else col[0]) for col in description
            )
            raw_rows = cursor.fetchall()
        finally:
            cursor.close()
        rows = tuple(dict(zip(columns, row, strict=False)) for row in raw_rows)
        return QueryResult(columns=columns, rows=rows)

    def health_check(self) -> Dict[str, Any]:
        try:
            import snowflake.connector  # type: ignore[import-not-found] # noqa: F401
        except ImportError as exc:  # pragma: no cover
            return {
                "status": "unavailable",
                "detail": "snowflake-connector-python not installed",
                "engine": "snowflake",
                "exception": str(exc),
            }
        started = time.monotonic()
        try:
            connection = self._get_connection()
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT 1")
                cursor.fetchall()
            finally:
                cursor.close()
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "detail": str(exc),
                "engine": "snowflake",
            }
        return {
            "status": "ok",
            "detail": "snowflake-ok",
            "engine": "snowflake",
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_connection(self):
        if self._connection is not None:
            return self._connection
        try:
            import snowflake.connector  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise UnsupportedBindingError(
                "snowflake-connector-python is not installed; install via "
                "the 'snowflake' extra: pip install 'data-product-forge[snowflake]'"
            ) from exc
        kwargs: Dict[str, Any] = {
            "account": self._env("SNOWFLAKE_ACCOUNT"),
            "user": self._env("SNOWFLAKE_USER"),
            "warehouse": self._env("SNOWFLAKE_WAREHOUSE", required=False),
            "database": self._database,
            "schema": self._schema,
            "role": self._env("SNOWFLAKE_ROLE", required=False),
            "client_session_keep_alive": False,
        }
        password = os.environ.get("SNOWFLAKE_PASSWORD")
        private_key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")
        if password:
            kwargs["password"] = password
        elif private_key_path:
            kwargs["private_key_file"] = private_key_path
        else:
            raise UnsupportedBindingError(
                "SnowflakeDriver requires SNOWFLAKE_PASSWORD or "
                "SNOWFLAKE_PRIVATE_KEY_PATH in the environment."
            )
        # Drop None-valued kwargs so the connector applies its own defaults.
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        connection = snowflake.connector.connect(**kwargs)
        self._connection = connection
        return connection

    @staticmethod
    def _env(name: str, *, required: bool = True) -> Optional[str]:
        value = os.environ.get(name)
        if required and not value:
            raise UnsupportedBindingError(f"Snowflake driver requires environment variable {name}")
        return value
