# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

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

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple
from urllib.parse import urlparse

from fluid_build.providers._sql_safety import build_libpq_dsn, quote_string_literal

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

    All SQL composition routes through :func:`quote_string_literal`
    so a tampered upstream catalog row can't break out of the SQL
    string literal.
    """

    config: JdbcSourceConfig = field(default=None)  # type: ignore[assignment]

    @property  # type: ignore[override]
    def scheme(self) -> str:  # type: ignore[override]
        # Surface the first scheme so callers that read ``.scheme``
        # (e.g. the registry's __init__ shape) get a stable value.
        return self.config.schemes[0]

    def discover(self, uri: str) -> List[DiscoveredStream]:
        if self.config is None:
            raise RuntimeError(f"{type(self).__name__} requires a JdbcSourceConfig")
        conn = self._parse_uri(uri)
        return self._introspect(conn)

    def _parse_uri(self, uri: str) -> dict:
        p = urlparse(uri)
        if p.scheme not in self.config.schemes:
            raise ValueError(
                f"{type(self).__name__} expects one of " f"{self.config.schemes}, got {p.scheme!r}"
            )
        return {
            "host": p.hostname or "localhost",
            "port": p.port or self.config.default_port,
            "user": p.username or "",
            "password": p.password or "",
            "database": p.path.lstrip("/") or self.config.default_database,
        }

    def _introspect(self, conn: dict) -> List[DiscoveredStream]:
        import duckdb

        dsn = build_libpq_dsn(conn, database_key=self.config.database_dsn_key)
        alias = self.config.attach_alias

        con = duckdb.connect(":memory:")
        streams: List[DiscoveredStream] = []
        try:
            con.execute(f"INSTALL {self.config.extension}; LOAD {self.config.extension};")
            con.execute(
                f"ATTACH {quote_string_literal(dsn)} AS {alias} "
                f"(TYPE {self.config.attach_type})"
            )

            filter_fn = self.config.table_filter or _default_table_filter
            where = filter_fn(conn, quote_string_literal)
            tables = con.execute(
                f"SELECT table_schema, table_name "
                f"FROM {alias}.information_schema.tables "
                f"WHERE {where} AND table_type = 'BASE TABLE' "
                "ORDER BY table_schema, table_name"
            ).fetchall()

            for schema, table in tables:
                quoted_schema = quote_string_literal(str(schema))
                quoted_table = quote_string_literal(str(table))
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
