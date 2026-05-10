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

"""JDBC introspection via duckdb extensions.

Shared helper between two callers:

1. ``fluid forge`` SDP path — already pulls a Postgres source via
   ``INSTALL postgres; LOAD postgres; ATTACH ...`` to enumerate tables
   for the LLM-driven contract synthesis. The relevant scanner code
   lives inline in the prompt-building path; this module lifts it out
   so it's also reusable by:

2. ``fluid forge data-model from-source --source postgres|mysql|sqlite``
   — the new (V1.5) CLI surface added in Phase 2.6 of the world-class
   plan. Same connection mechanics, different output shape (logical
   model + sidecars instead of LLM-driven SDP contract).

The duckdb extensions (``postgres``, ``mysql``, ``sqlite``) install
on first use; subsequent runs hit the cached extension binary. No
extra Python deps beyond duckdb itself.

URI format mirrors SQLAlchemy / standard JDBC:

* ``postgresql://user:pass@host:5432/db``
* ``postgres://user:pass@host:5432/db`` (alias)
* ``mysql://user:pass@host:3306/db``
* ``sqlite:///absolute/path/to/db.sqlite``
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

LOG = logging.getLogger("fluid.cli.discover.jdbc")


@dataclass
class IntrospectedColumn:
    name: str
    type_name: str
    nullable: bool = True
    description: Optional[str] = None


@dataclass
class IntrospectedTable:
    schema: str
    name: str
    columns: List[IntrospectedColumn] = field(default_factory=list)
    row_count_estimate: Optional[int] = None


@dataclass
class IntrospectedDatabase:
    """Minimal shape the from-source pipeline consumes."""

    source_kind: str  # "postgres" | "mysql" | "sqlite"
    database: str
    tables: List[IntrospectedTable] = field(default_factory=list)


SUPPORTED_KINDS = {"postgres", "postgresql", "mysql", "sqlite"}


def _normalize_kind(source: str) -> str:
    s = source.lower()
    if s == "postgresql":
        return "postgres"
    if s not in SUPPORTED_KINDS:
        raise ValueError(
            f"Unsupported JDBC source: {source!r}. Supported: postgres, postgresql, mysql, sqlite."
        )
    return s if s != "postgresql" else "postgres"


def _parse_uri(uri: str, kind: str) -> Dict[str, str]:
    """Normalise a JDBC URI into a duckdb ATTACH-string map.

    Returns the kwargs duckdb's postgres / mysql ATTACH expects. For
    SQLite, returns ``{"path": "/abs/path"}`` since SQLite ATTACH is
    just a file path.
    """
    if kind == "sqlite":
        # SQLAlchemy / standard SQLite URI forms:
        #   ``sqlite:///rel/path``      → ``rel/path``  (relative)
        #   ``sqlite:////abs/path``     → ``/abs/path`` (absolute)
        # We strip the ``sqlite://`` prefix (two slashes for the
        # scheme separator), then the optional third slash that
        # SQLAlchemy uses to mark an empty host. The remaining path
        # is what duckdb's ATTACH wants.
        body = uri.split("sqlite://", 1)[-1]
        if body.startswith("/"):
            # ``sqlite:///path`` → strip exactly one slash
            #                      (host = "", path = "/path" → "path")
            # ``sqlite:////abs`` → strip exactly one slash
            #                      (host = "", path = "//abs" → "/abs")
            body = body[1:]
        return {"path": body}

    parsed = urlparse(uri)
    if not parsed.hostname:
        raise ValueError(f"Invalid {kind} URI: missing hostname. Got: {uri!r}")
    out = {
        "host": parsed.hostname,
        "port": str(parsed.port or (5432 if kind == "postgres" else 3306)),
        "user": parsed.username or "",
        "password": parsed.password or "",
        "dbname": (parsed.path or "/").lstrip("/") or "",
    }
    return out


def _attach_string_postgres(args: Dict[str, str], alias: str) -> str:
    parts = [f"dbname={args['dbname']}"]
    if args.get("host"):
        parts.append(f"host={args['host']}")
    if args.get("port"):
        parts.append(f"port={args['port']}")
    if args.get("user"):
        parts.append(f"user={args['user']}")
    if args.get("password"):
        parts.append(f"password={args['password']}")
    return f"ATTACH '{' '.join(parts)}' AS {alias} (TYPE postgres)"


def _attach_string_mysql(args: Dict[str, str], alias: str) -> str:
    parts = [f"database={args['dbname']}"]
    if args.get("host"):
        parts.append(f"host={args['host']}")
    if args.get("port"):
        parts.append(f"port={args['port']}")
    if args.get("user"):
        parts.append(f"user={args['user']}")
    if args.get("password"):
        parts.append(f"password={args['password']}")
    return f"ATTACH '{' '.join(parts)}' AS {alias} (TYPE mysql)"


def _attach_string_sqlite(args: Dict[str, str], alias: str) -> str:
    return f"ATTACH '{args['path']}' AS {alias} (TYPE sqlite)"


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_alias(alias: str) -> str:
    """Defensive: ATTACH alias goes into a SQL string; reject any shape
    that wouldn't parse as a bare identifier."""
    if not _IDENT_RE.match(alias):
        raise ValueError(
            f"Invalid duckdb attach alias: {alias!r}. Must match ``[A-Za-z_][A-Za-z0-9_]*``."
        )
    return alias


def introspect_jdbc(
    *,
    source: str,
    uri: str,
    schema_filter: Optional[str] = None,
    table_filter: Optional[List[str]] = None,
) -> IntrospectedDatabase:
    """Connect to the JDBC database via duckdb extensions and enumerate
    its tables and columns.

    Args:
        source: One of ``postgres``, ``postgresql``, ``mysql``, ``sqlite``.
        uri: JDBC URI (see module docstring).
        schema_filter: Optional schema name. When None, every
            user-accessible schema is enumerated.
        table_filter: Optional list of table names to enumerate. When
            None, every table is returned.

    Returns:
        :class:`IntrospectedDatabase` with the full table/column tree.

    Raises:
        ImportError: duckdb not installed.
        ValueError: malformed URI / unsupported source.
        Exception: connect / extension load / SQL failures bubble up
            with the duckdb error preserved (operators want to see the
            "could not connect to server" message verbatim).
    """
    try:
        import duckdb
    except ImportError as exc:
        raise ImportError(
            "duckdb is required for JDBC introspection. Install via ``pip install duckdb``."
        ) from exc

    kind = _normalize_kind(source)
    args = _parse_uri(uri, kind)
    alias = _validate_alias("source_db")

    con = duckdb.connect(":memory:")
    try:
        # Install + load the extension. duckdb caches the binary so
        # this is fast on second run.
        if kind == "postgres":
            con.execute("INSTALL postgres; LOAD postgres;")
            con.execute(_attach_string_postgres(args, alias))
        elif kind == "mysql":
            con.execute("INSTALL mysql; LOAD mysql;")
            con.execute(_attach_string_mysql(args, alias))
        elif kind == "sqlite":
            con.execute("INSTALL sqlite; LOAD sqlite;")
            con.execute(_attach_string_sqlite(args, alias))

        # Enumerate via duckdb's union ``information_schema`` view,
        # filtered to our attached database. Postgres + MySQL each
        # expose a per-attached information_schema; SQLite doesn't,
        # but duckdb's union view still lists SQLite tables under
        # ``table_catalog = <alias>``. One query, all three engines.
        params: List[Any] = [alias]
        sql_parts = [
            "SELECT table_schema, table_name, column_name, data_type, "
            "is_nullable FROM information_schema.columns "
            "WHERE table_catalog = ?"
        ]
        if schema_filter:
            sql_parts.append("AND table_schema = ?")
            params.append(schema_filter)
        sql_parts.append("ORDER BY table_schema, table_name, ordinal_position")
        rows = con.execute(" ".join(sql_parts), params).fetchall()
    finally:
        con.close()

    # Group rows into IntrospectedTable.
    tables: Dict[str, IntrospectedTable] = {}
    keep = set(table_filter or [])
    for table_schema, table_name, column_name, data_type, is_nullable in rows:
        if keep and table_name not in keep:
            continue
        key = f"{table_schema}.{table_name}"
        if key not in tables:
            tables[key] = IntrospectedTable(schema=table_schema, name=table_name)
        tables[key].columns.append(
            IntrospectedColumn(
                name=column_name,
                type_name=str(data_type).lower(),
                nullable=(str(is_nullable).upper() == "YES"),
            )
        )

    return IntrospectedDatabase(
        source_kind=kind,
        database=args.get("dbname", args.get("path", "")),
        tables=list(tables.values()),
    )


__all__ = [
    "IntrospectedColumn",
    "IntrospectedDatabase",
    "IntrospectedTable",
    "SUPPORTED_KINDS",
    "introspect_jdbc",
]
