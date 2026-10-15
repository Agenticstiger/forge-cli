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

"""Shared introspection logic for JDBC-style discoverers (postgres, mysql,
mariadb, sqlite, …).

Each per-database discoverer is now a 5-line subclass that supplies a
:class:`JdbcSourceConfig` describing:

* what URI schemes it accepts (``postgres`` / ``mysql`` / etc.),
* the default port,
* the duckdb extension name,
* the ATTACH alias + TYPE keyword,
* which DSN key carries the database name (``dbname`` for postgres,
  ``database`` for mysql),
* a table-filter predicate for the catalog query.

All the actual ``information_schema`` walking, DSN construction, SQL
escaping, and ``DiscoveredStream`` building happens here in one place.
"""

from __future__ import annotations

import contextlib
import inspect
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from fluid_build.providers._sql_safety import quote_ansi_string_literal

from .registry import DiscoveredColumn, DiscoveredStream, Discoverer


@dataclass(frozen=True)
class JdbcSourceConfig:
    """Per-database knobs for the shared JDBC introspector."""

    schemes: Tuple[str, ...]  # e.g. ("postgres", "postgresql")
    default_port: int  # 5432 / 3306 / …
    extension: str  # duckdb extension name (``postgres``)
    attach_alias: str  # alias used in ATTACH ... AS <alias>
    attach_type: str  # the duckdb TYPE keyword (mostly == extension)
    database_dsn_key: str  # ``dbname`` for postgres, ``database`` for mysql
    default_database: str = ""  # fallback when URI path is empty
    # Optional WHERE-clause builder for the table-list query. Receives
    # the connection dict + a ``quote`` helper; returns a SQL fragment
    # appended to the base WHERE. Default (``None``) excludes
    # ``pg_catalog`` / ``information_schema``.
    table_filter: Optional[Callable[[dict, Callable[[str], str]], str]] = None


def _default_table_filter(conn: dict, quote: Callable[[str], str]) -> str:
    """Postgres-style default: skip catalog + information_schema."""
    return "table_schema NOT IN ('pg_catalog', 'information_schema')"


def _mysql_table_filter(conn: dict, quote: Callable[[str], str]) -> str:
    """MySQL-style: filter to rows for the connection's database name."""
    db = conn.get("database") or ""
    return f"table_schema = {quote(db)}"


# Whether the URI being discovered is one the operator typed on the command
# line. It travels out of band, not as an argument, so a JdbcDiscoverer subclass
# written against main's one-argument discover(uri) / _introspect(conn) keeps
# working under ``fluid init --discover``. Only discover_operator_uri and an
# explicit ``discover(..., operator_supplied=True)`` set it, for the length of
# that call; the default is the pin. The value lives in the calling thread's
# (or asyncio task's) context (PEP 567) and is reset when the call returns, so
# no other command sees it, and the MCP server, which calls neither, cannot
# set it.
_OPERATOR_SUPPLIED_URI: ContextVar[bool] = ContextVar(
    "fluid_jdbc_operator_supplied_uri", default=False
)

# A thread the discoverer starts does not run in that context: a
# ThreadPoolExecutor worker or a threading.Thread starts from an empty one
# (PEP 567; contextvars docs). So discover_operator_uri also counts the
# discoverer instance it runs as the operator's for the length of the call,
# keyed by id() (the call holds a reference, so the id cannot be reused while
# it is counted), and _introspect honours either. Only discover_operator_uri
# changes the count, and only ``fluid init --discover`` calls it: the MCP server
# never runs that command, so no MCP route can set it. The count belongs to
# the instance, not to a thread: while the operator's command runs, any thread
# that calls that same instance is unpinned. The command runs nothing else, but
# a program that embeds discoverers must not call discover_operator_uri on an
# instance that another thread is using for a URI the operator did not supply.
_OPERATOR_INSTANCES: Dict[int, int] = {}
_OPERATOR_INSTANCES_LOCK = threading.Lock()


@contextlib.contextmanager
def _operator_supplied_uri() -> Iterator[None]:
    token = _OPERATOR_SUPPLIED_URI.set(True)
    try:
        yield
    finally:
        _OPERATOR_SUPPLIED_URI.reset(token)


@contextlib.contextmanager
def _operator_instance(discoverer: object) -> Iterator[None]:
    """Count ``discoverer`` as running the operator's URI until the block ends.

    A count, so a nested or overlapping operator call on the same instance
    does not end the outer one's.
    """
    key = id(discoverer)
    with _OPERATOR_INSTANCES_LOCK:
        _OPERATOR_INSTANCES[key] = _OPERATOR_INSTANCES.get(key, 0) + 1
    try:
        yield
    finally:
        with _OPERATOR_INSTANCES_LOCK:
            remaining = _OPERATOR_INSTANCES.pop(key) - 1
            if remaining:
                _OPERATOR_INSTANCES[key] = remaining


def _runs_operator_uri(discoverer: object) -> bool:
    """Whether ``discoverer`` is running a URI the operator typed, in any thread."""
    return _OPERATOR_SUPPLIED_URI.get() or id(discoverer) in _OPERATOR_INSTANCES


def _takes_operator_supplied(method: Any) -> bool:
    """Whether ``method`` declares an ``operator_supplied`` keyword."""
    try:
        param = inspect.signature(method).parameters.get("operator_supplied")
    except (TypeError, ValueError):  # a builtin or C callable without a signature
        return False
    return param is not None and param.kind in (
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )


def discover_operator_uri(discoverer: Discoverer, uri: str) -> List[DiscoveredStream]:
    """``discoverer.discover(uri)`` for a URI the operator typed on the command line.

    For ``fluid init --discover`` only: inside it a JdbcDiscoverer may use this
    host's client certificate and libpq settings, as the operator's own
    connection always has, also in a thread it starts to call its own
    ``_introspect``. Every discoverer is still called as ``discover(uri)``, the
    interface the registry documents; a JdbcDiscoverer whose ``discover``
    declares the ``operator_supplied`` keyword is passed it as well. A
    discoverer that hands the URI to a different discoverer instance in a
    thread it starts should make that call with
    ``contextvars.copy_context().run``; otherwise that thread is pinned.
    """
    with _operator_supplied_uri(), _operator_instance(discoverer):
        if isinstance(discoverer, JdbcDiscoverer) and _takes_operator_supplied(discoverer.discover):
            return discoverer.discover(uri, operator_supplied=True)
        return discoverer.discover(uri)


@dataclass
class JdbcDiscoverer(Discoverer):
    """Generic JDBC-style discoverer.

    Subclasses pass a :class:`JdbcSourceConfig` and inherit the
    shared ``discover`` implementation that:

    1. Parses the URI into a connection dict.
    2. Builds a libpq-style DSN via :func:`build_libpq_dsn`.
    3. Spins a duckdb in-memory connection, INSTALLs / LOADs the
       extension, ATTACHes the upstream as a named alias.
    4. Walks ``information_schema.tables`` (filtered per source) and
       emits one :class:`DiscoveredStream` per table with its
       columns.

    All SQL composition routes through :func:`quote_ansi_string_literal`
    so a tampered upstream catalog row can't break out of the SQL
    string literal.
    """

    config: JdbcSourceConfig = field(default=None)  # type: ignore[assignment]

    @property  # type: ignore[override]
    def scheme(self) -> str:  # type: ignore[override]
        # Surface the first scheme so callers that read ``.scheme``
        # (e.g. the registry's __init__ shape) get a stable value.
        return self.config.schemes[0]

    def discover(self, uri: str, *, operator_supplied: bool = False) -> List[DiscoveredStream]:
        """Introspect ``uri``.

        ``operator_supplied=True`` only for a URI the operator typed on the
        command line; see :func:`~._jdbc_introspect.build_attach_dsn` for what
        the default pins. ``fluid init --discover`` says so through
        :func:`discover_operator_uri` instead, so a subclass that overrides
        ``discover(uri)`` or ``_introspect(conn)`` with main's one-argument
        signatures keeps working and still runs as the operator there.
        """
        if self.config is None:
            raise RuntimeError(f"{type(self).__name__} requires a JdbcSourceConfig")
        conn = self._parse_uri(uri)
        if operator_supplied:
            with _operator_supplied_uri():
                return self._introspect(conn)
        return self._introspect(conn)

    def _parse_uri(self, uri: str) -> dict:
        p = urlparse(uri)
        if p.scheme not in self.config.schemes:
            raise ValueError(
                f"{type(self).__name__} expects one of {self.config.schemes}, got {p.scheme!r}"
            )
        return {
            "host": p.hostname or "localhost",
            "port": p.port or self.config.default_port,
            "user": p.username or "",
            "password": p.password or "",
            "database": p.path.lstrip("/") or self.config.default_database,
        }

    def _introspect(self, conn: dict) -> List[DiscoveredStream]:
        from fluid_build.providers._duckdb_sandbox import DuckDBAllowlist, secure_duckdb_connect

        from ._jdbc_introspect import _host_credential_env_suppressed, build_attach_dsn

        operator_supplied = _runs_operator_uri(self)
        dsn = build_attach_dsn(
            conn,
            database_key=self.config.database_dsn_key,
            postgres=self.config.attach_type == "postgres",
            operator_supplied=operator_supplied,
        )
        alias = self.config.attach_alias
        attach_sql = (
            f"ATTACH {quote_ansi_string_literal(dsn)} AS {alias} (TYPE {self.config.attach_type})"
        )
        # The whole pin, as introspect_jdbc applies it: a password the caller
        # supplied is its own material, so this host's ambient DB credential
        # environment is hidden while the scanner connects. For mysql that
        # window is the whole pin; the DSN carries the libpq half.
        pinned = (
            self.config.attach_type in ("postgres", "mysql")
            and bool(conn.get("password"))
            and not operator_supplied
        )

        def _attach(c: Any) -> None:
            if pinned:
                with _host_credential_env_suppressed():
                    c.execute(attach_sql)
            else:
                c.execute(attach_sql)

        # The source is attached before the sandbox locks; the catalog queries
        # after it reach that database and no file or URL.
        con = secure_duckdb_connect(
            ":memory:",
            allow=DuckDBAllowlist.none(),
            extensions=[self.config.extension],
            before_lock=_attach,
        )
        streams: List[DiscoveredStream] = []
        try:

            filter_fn = self.config.table_filter or _default_table_filter
            where = filter_fn(conn, quote_ansi_string_literal)
            tables = con.execute(
                f"SELECT table_schema, table_name "
                f"FROM {alias}.information_schema.tables "
                f"WHERE {where} AND table_type = 'BASE TABLE' "
                "ORDER BY table_schema, table_name"
            ).fetchall()

            for schema, table in tables:
                quoted_schema = quote_ansi_string_literal(str(schema))
                quoted_table = quote_ansi_string_literal(str(table))
                cols = con.execute(
                    "SELECT column_name, data_type, is_nullable "
                    f"FROM {alias}.information_schema.columns "
                    f"WHERE table_schema = {quoted_schema} "
                    f"AND table_name = {quoted_table} "
                    "ORDER BY ordinal_position"
                ).fetchall()
                streams.append(
                    DiscoveredStream(
                        name=f"{schema}.{table}",
                        columns=[
                            DiscoveredColumn(
                                name=c[0],
                                type=str(c[1]),
                                nullable=str(c[2]).upper() == "YES",
                            )
                            for c in cols
                        ],
                        metadata={"schema": schema, "table": table},
                    )
                )
        finally:
            con.close()
        return streams


# Per-source configs — extending to a new JDBC database is a single
# new ``JdbcSourceConfig`` row + a 1-line registry registration.
POSTGRES_CONFIG = JdbcSourceConfig(
    schemes=("postgres", "postgresql"),
    default_port=5432,
    extension="postgres",
    attach_alias="pg",
    attach_type="postgres",
    database_dsn_key="dbname",
    default_database="postgres",
)

MYSQL_CONFIG = JdbcSourceConfig(
    schemes=("mysql", "mariadb"),
    default_port=3306,
    extension="mysql",
    attach_alias="mysql_db",
    attach_type="mysql",
    database_dsn_key="database",
    table_filter=_mysql_table_filter,
)
