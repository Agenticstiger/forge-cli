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

"""PostgreSQL driver for the consumer MCP output-port server.

Postgres is the most common OLTP / lightweight-analytical engine
forge-cli users encounter. The driver mirrors the Snowflake / BigQuery
shape so the gateway treats it as a peer engine — same descriptor
surface, same SQL-safety contract, same per-row redaction hook.

Phase-1 identity model:

* Connection params resolved from env vars
  (``POSTGRES_HOST``, ``POSTGRES_PORT``, ``POSTGRES_DATABASE``,
  ``POSTGRES_USER``, ``POSTGRES_PASSWORD``, ``POSTGRES_SSLMODE``)
  OR an explicit ``connection_options`` mapping for in-process tests.
* Server-side row-level security policies (``CREATE POLICY``) remain
  the operator's responsibility — the gateway only enforces what the
  contract declares (``allowedModels`` / ``allowedUseCases`` /
  column restrictions / row-level PII redaction).

Parameter binding:

* psycopg uses ``%s`` positional or ``%(name)s`` named placeholders.
  Our compiler emits ``:p_<index>`` named placeholders; this driver
  rewrites to the psycopg ``%(p_<index>)s`` form before executing.

Borrowed-not-built per /borrow-before-build:

* `psycopg <https://www.psycopg.org/psycopg3/>`_ v3 is the canonical
  Python driver (maintained, async-capable, parameter-binding native,
  SQL-injection-safe identifier quoting). Pinned via the optional
  ``postgres`` extra.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.providers._sql_safety import validate_ident

from .base import (
    DriverDescriptor,
    EngineDriver,
    QueryResult,
    UnsupportedBindingError,
    get_binding,
    guard_against_injection_markers,
)

# Rewrite ``:p_<index>`` (compiler form) → ``%(p_<index>)s`` (psycopg form).
_PARAM_REWRITE = re.compile(r":p_(\d+)\b")


class PostgresDriver(EngineDriver):
    """PostgreSQL driver.

    Requires the ``postgres`` extra:
    ``pip install 'data-product-forge[postgres]'``.
    """

    name = "postgres"

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
        if platform != "postgres":
            raise UnsupportedBindingError(
                f"PostgresDriver requires binding.platform='postgres'; got {platform!r}"
            )
        if fmt not in {"postgres_table", "table"}:
            raise UnsupportedBindingError(
                "PostgresDriver requires binding.format in {'postgres_table','table'}; "
                f"got {fmt!r}"
            )
        database = location.get("database") or location.get("dataset")
        schema = location.get("schema") or "public"
        table = location.get("table") or location.get("name")
        for component, label in [
            (database, "binding.location.database"),
            (table, "binding.location.table"),
        ]:
            if not isinstance(component, str) or not component:
                raise UnsupportedBindingError(f"PostgresDriver requires {label}")
        self._database = database
        self._schema = validate_ident(schema)
        self._table = validate_ident(str(table))
        self._fully_qualified = f'"{self._schema}"."{self._table}"'
        self._connection = None  # lazy

    # ------------------------------------------------------------------
    # EngineDriver surface
    # ------------------------------------------------------------------

    def descriptor(self) -> DriverDescriptor:
        return DriverDescriptor(
            platform="postgres",
            format="postgres_table",
            table_reference=f"{self._database}.{self._schema}.{self._table}",
            dialect="postgres",
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
        # The compiler uses fully-qualified table reference baked into
        # the SQL string; psycopg does the parameter-binding for the
        # caller's filter/limit values.
        rendered = _PARAM_REWRITE.sub(r"%(p_\1)s", sql)
        guard_against_injection_markers(rendered)
        bound: Dict[str, Any] = {f"p_{index}": value for index, value in enumerate(params)}
        with connection.cursor() as cursor:
            if timeout_seconds is not None and timeout_seconds > 0:
                # Postgres supports per-statement timeouts via SET
                # LOCAL. We use SET LOCAL inside an implicit
                # transaction so it auto-resets after the statement.
                ms = max(1, int(timeout_seconds * 1000))
                cursor.execute(f"SET LOCAL statement_timeout = {ms}")
            if bound:
                cursor.execute(rendered, bound)
            else:
                cursor.execute(rendered)
            description = cursor.description or []
            columns = tuple(
                str(col.name if hasattr(col, "name") else col[0]) for col in description
            )
            raw_rows = cursor.fetchall()
        rows = tuple(dict(zip(columns, row, strict=False)) for row in raw_rows)
        return QueryResult(columns=columns, rows=rows)

    def health_check(self) -> Dict[str, Any]:
        started = time.monotonic()
        try:
            connection = self._get_connection()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchall()
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "detail": str(exc),
                "engine": "postgres",
            }
        return {
            "status": "ok",
            "detail": "postgres-ok",
            "engine": "postgres",
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
        }

    def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception as exc:  # noqa: BLE001
                if self.logger is not None:
                    self.logger.debug("postgres_close_failed: %s", exc)
            self._connection = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_connection(self):
        if self._connection is not None:
            return self._connection
        try:
            import psycopg  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise UnsupportedBindingError(
                "psycopg is required for PostgresDriver. Install with: "
                "pip install 'data-product-forge[postgres]'"
            ) from exc

        opts = dict(self.connection_options or {})
        # Layered resolution: explicit connection_options > env vars >
        # binding location hints. Hostname / port / credentials always
        # come from outside the contract; database is preferred from
        # the binding (so you can serve different databases from the
        # same env).
        opts.setdefault("host", os.environ.get("POSTGRES_HOST", "localhost"))
        opts.setdefault("port", int(os.environ.get("POSTGRES_PORT", "5432")))
        opts.setdefault("dbname", opts.pop("database", None) or self._database)
        opts.setdefault("user", os.environ.get("POSTGRES_USER"))
        password = opts.pop("password", None) or os.environ.get("POSTGRES_PASSWORD")
        if password is not None:
            opts["password"] = password
        sslmode = os.environ.get("POSTGRES_SSLMODE")
        if sslmode and "sslmode" not in opts:
            opts["sslmode"] = sslmode
        # Drop any None values — psycopg complains about them.
        opts = {k: v for k, v in opts.items() if v is not None}
        if self.logger is not None:
            redacted = {k: ("***" if k in {"password", "passfile"} else v) for k, v in opts.items()}
            self.logger.debug("postgres_connect: %s", redacted)
        self._connection = psycopg.connect(**opts)
        # Default to read-only sessions — the gateway never mutates.
        self._connection.read_only = True
        self._connection.autocommit = True
        return self._connection
