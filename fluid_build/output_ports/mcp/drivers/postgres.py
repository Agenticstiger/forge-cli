# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

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

import contextlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from fluid_build.providers._sql_safety import validate_ident

from ..query_compiler import declares_row_filters
from .base import (
    DriverDescriptor,
    EngineDriver,
    QueryResult,
    RowFilterUnavailable,
    UnsupportedBindingError,
    get_binding,
    guard_against_injection_markers,
)

_LOG = logging.getLogger(__name__)

# Rewrite ``:p_<index>`` (compiler form) → ``%(p_<index>)s`` (psycopg form).
_PARAM_REWRITE = re.compile(r":p_(\d+)\b")

#: The predefined Postgres roles that reach past the tables an expose names: they read or
#: write server files, run programs on the server, read every table, or read every
#: session's activity, statistics and settings. A superuser can do all of it. Their
#: functions and views read a table's own files, the write-ahead log and other tables,
#: so the rows a row filter withholds. Membership counts directly or through another role.
PRIVILEGED_ROLES = (
    "pg_execute_server_program",
    "pg_monitor",
    "pg_read_all_data",
    "pg_read_all_settings",
    "pg_read_all_stats",
    "pg_read_server_files",
    "pg_write_server_files",
)
# Every catalog name is qualified with pg_catalog, so no schema on the search path can put
# its own pg_roles or pg_has_role in front of them.
_PRIVILEGE_QUERY = (
    "SELECT r.rolsuper, ARRAY(SELECT p.rolname::text FROM pg_catalog.pg_roles AS p "
    "WHERE p.rolname IN ("
    + ", ".join(f"'{role}'" for role in PRIVILEGED_ROLES)
    + ") AND pg_catalog.pg_has_role(current_user, p.oid, 'MEMBER') ORDER BY p.rolname) "
    "FROM pg_catalog.pg_roles AS r WHERE r.rolname = current_user"
)


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
        readable_paths: Tuple[Path, ...] = (),
    ) -> None:
        super().__init__(
            expose=expose,
            contract=contract,
            logger=logger,
            connection_options=connection_options,
            readable_paths=readable_paths,
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
        bound: Dict[str, Any] = {f"p_{index}": value for index, value in enumerate(params)}
        # With parameters bound psycopg reads the statement as a pyformat string, so a
        # literal ``%`` (a LIKE pattern or a modulo in a contract expression or a row
        # filter) is doubled before the bind markers are written in.
        rendered = _PARAM_REWRITE.sub(r"%(p_\1)s", sql.replace("%", "%%") if bound else sql)
        guard_against_injection_markers(rendered)
        # Each request is a read-only transaction of its own, rolled back at the end. The
        # connection is shared by every caller and runs in autocommit, where neither its
        # read-only flag nor a SET LOCAL lasts past one statement. Inside the transaction
        # both hold, and a session setting the statement changes is undone with it, so no
        # caller's statement runs under state another caller left on the connection.
        with connection.transaction(force_rollback=True), connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            if timeout_seconds is not None and timeout_seconds > 0:
                # A per-request timeout: SET LOCAL lasts until this transaction ends.
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

    def _connect_options(self) -> Dict[str, Any]:
        """What :meth:`_get_connection` connects with."""
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
        return {k: v for k, v in opts.items() if v is not None}

    def _connection_identity(self) -> Optional[Tuple[Any, ...]]:
        # Every parameter the connection is made with: the same host, port, database,
        # role and credentials read the same tables, and a table of the same database
        # is named by its schema. The binding's database must also be the one connected
        # to, since the reference carries it.
        try:
            opts = self._connect_options()
        except (TypeError, ValueError):
            return None
        return (
            "postgres",
            self._database,
            tuple(sorted((str(k), repr(v)) for k, v in opts.items())),
        )

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

        opts = self._connect_options()
        if self.logger is not None:
            redacted = {k: ("***" if k in {"password", "passfile"} else v) for k, v in opts.items()}
            self.logger.debug("postgres_connect: %s", redacted)
        connection = psycopg.connect(**opts)
        try:
            # Transactions psycopg starts are read-only (execute runs each request in
            # one): the gateway never writes.
            connection.read_only = True
            connection.autocommit = True
            self._check_the_role(connection)
        except BaseException:
            # Kept only once the role check has passed: a check that failed for any
            # reason (a timeout, a refused role) runs again on the next request.
            with contextlib.suppress(Exception):
                connection.close()
            raise
        self._connection = connection
        return connection

    def _check_the_role(self, connection: Any) -> None:
        """Refuse to serve an expose with row filters as a role that reaches past the
        tables (a superuser or a member of :data:`PRIVILEGED_ROLES`), and warn on any
        other expose: no check on a statement stops a function such a role may call."""
        with connection.transaction(force_rollback=True), connection.cursor() as cursor:
            cursor.execute(_PRIVILEGE_QUERY)
            rows = cursor.fetchall()
        superuser, memberships = rows[0] if rows else (False, [])
        held = (["superuser"] if superuser else []) + [str(r) for r in memberships or []]
        if not held:
            return
        if declares_row_filters(self.expose):
            raise RowFilterUnavailable(
                f"the gateway connects to Postgres as a role that is {', '.join(held)}. Such a "
                "role can read a table's files and the write-ahead log, so the rows a row "
                "filter withholds; connect as a role that can read only the exposed tables."
            )
        (self.logger or _LOG).warning(
            "postgres_privileged_role: the gateway connects as a role that is %s; connect as "
            "a role that can read only the exposed tables.",
            ", ".join(held),
        )
