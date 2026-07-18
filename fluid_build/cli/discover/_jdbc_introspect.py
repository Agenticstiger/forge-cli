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

Constraint extraction (PK / FK / CHECK)
---------------------------------------

The duckdb postgres + mysql extensions expose ``information_schema``
union views across all attached catalogs, but they STRIP foreign-key
rows out of ``information_schema.table_constraints`` and reduce CHECK
constraints to NOT-NULL placeholders only — application CHECKs like
``o_orderstatus IN ('O','F','P')`` never reach the duckdb side.

Borrowed approach (see borrow-before-build receipts in the PR):

* SQLAlchemy's ``PGInspector.get_pk_constraint`` / ``get_foreign_keys``
  shape — drives our dataclass surface.
* PostgreSQL canonical ``information_schema`` join shapes (the
  standard-SQL views ``table_constraints`` + ``key_column_usage`` +
  ``referential_constraints`` + ``check_constraints`` +
  ``constraint_column_usage``) — see
  ``https://www.postgresql.org/docs/current/infoschema-referential-constraints.html``.
* duckdb's ``postgres_query()`` / ``mysql_query()`` pass-through table
  functions — to ask the source database directly, bypassing the
  duckdb union view's filtering of FK + CHECK rows.

SQLite is a degraded path on the constraint side: duckdb has no
``sqlite_query()`` analog and its information-schema union view drops
FK rows entirely. We surface PKs + NOT-NULL CHECKs via the same union
view; FKs and application CHECKs are simply absent. Documented limitation,
not a regression.
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
    """One column's metadata as introspected from the source.

    Adapted from the OSI (Apache Ossie) / catalog ``CatalogColumn`` shape
    (see ``fluid_build/copilot/catalog/models.py``). Optional
    precision / scale / character_maximum_length fields are
    captured directly from ``information_schema.columns`` so the
    downstream contract emitter can parameterise the logical type
    (e.g. ``decimal(15, 2)``, ``varchar(80)`` instead of bare
    ``decimal`` / ``string``).
    """

    name: str
    type_name: str
    nullable: bool = True
    description: Optional[str] = None
    # Cross-dialect precision/scale (numeric_precision, numeric_scale,
    # character_maximum_length in standard SQL information_schema).
    # None when the column type is not parameterised (e.g. INTEGER,
    # BOOLEAN, TIMESTAMP).
    numeric_precision: Optional[int] = None
    numeric_scale: Optional[int] = None
    character_max_length: Optional[int] = None


@dataclass
class IntrospectedForeignKey:
    """One foreign-key declaration. Mirrors ``CatalogForeignKey``
    so downstream agentic stages can consume both shapes uniformly.

    Composite FKs come back as ONE :class:`IntrospectedForeignKey`
    with ``len(from_columns) == len(to_columns) == N`` (positions
    aligned by ``ordinal_position``).
    """

    constraint_name: Optional[str]
    from_columns: List[str]
    to_schema: Optional[str]
    to_table: str
    to_columns: List[str]
    update_rule: Optional[str] = None  # CASCADE | SET NULL | NO ACTION | ...
    delete_rule: Optional[str] = None
    match_option: Optional[str] = None  # FULL | PARTIAL | NONE


@dataclass
class IntrospectedCheckConstraint:
    """One CHECK constraint with the literal SQL expression.

    We capture the raw ``check_clause`` string verbatim from
    ``information_schema.check_constraints``. The downstream
    contract emitter can choose to round-trip it as a custom
    validation rule, attach it to a single column, or drop it.

    NOT-NULL constraints are emitted by Postgres as auto-generated
    CHECK rows (e.g. ``c_custkey IS NOT NULL``); we filter those out
    on the extraction side and represent them via the column-level
    ``nullable`` flag instead.
    """

    constraint_name: Optional[str]
    check_clause: str
    # Columns the CHECK references (from constraint_column_usage).
    # Empty when the check is a multi-column expression and the source
    # dialect doesn't enumerate per-column entries — keep it best-effort.
    columns: List[str] = field(default_factory=list)


@dataclass
class IntrospectedTable:
    schema: str
    name: str
    columns: List[IntrospectedColumn] = field(default_factory=list)
    row_count_estimate: Optional[int] = None
    # Constraint surface — parallels CatalogTable in
    # ``fluid_build/copilot/catalog/models.py``.
    primary_key_columns: List[str] = field(default_factory=list)
    foreign_keys: List[IntrospectedForeignKey] = field(default_factory=list)
    checks: List[IntrospectedCheckConstraint] = field(default_factory=list)


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


# ---------------------------------------------------------------------------
# Constraint extractors
# ---------------------------------------------------------------------------
#
# We expose three pure helpers so they're independently unit-testable
# via a mock duckdb connection. Each takes ``con``, ``alias``, ``kind``
# and ``schema_filter`` and returns a dict keyed by table name.
#
# Routing logic:
#
#   * Postgres → ``postgres_query('alias', $$<pg-side info_schema SQL>$$)``.
#     The duckdb union ``information_schema`` strips FOREIGN KEY rows
#     and application-level CHECKs, so we must pass-through to the real
#     Postgres-side information_schema.
#   * MySQL → ``mysql_query('alias', $$...$$)`` — same reason, same shape.
#   * SQLite → fall back to duckdb's union information_schema. FKs are
#     unrecoverable here (duckdb has no ``sqlite_query()`` analog).
#     PKs and NOT-NULL CHECKs flow through; application CHECKs are
#     emitted by SQLite as NOT-NULL placeholders only via the duckdb
#     union view, so we skip them.

# The auto-generated NOT-NULL CHECK rows postgres emits (the duckdb
# union view often only sees these; the real ones are application
# checks we filter for separately). The pattern matches both the
# typical postgres-emitted form ``col IS NOT NULL`` and the
# parenthesised variants.
_NOT_NULL_CHECK_RE = re.compile(r"^\s*\(?\s*\w+\s+IS\s+NOT\s+NULL\s*\)?\s*$", re.IGNORECASE)


def _pg_query(con: Any, alias: str, sql: str) -> List[Any]:
    """Run a SQL string through duckdb's ``postgres_query('<alias>',
    $$...$$)`` pass-through. The ``$$``-quoted dollar-string lets us
    embed arbitrary SQL with single quotes without escape juggling.
    """
    wrapped = f"SELECT * FROM postgres_query('{alias}', $${sql}$$)"
    return con.execute(wrapped).fetchall()


def _mysql_query(con: Any, alias: str, sql: str) -> List[Any]:
    """MySQL pass-through analog. See :func:`_pg_query`."""
    wrapped = f"SELECT * FROM mysql_query('{alias}', $${sql}$$)"
    return con.execute(wrapped).fetchall()


def _extract_primary_keys(
    con: Any, alias: str, kind: str, schema_filter: Optional[str]
) -> Dict[str, List[str]]:
    """Return ``{<table_name>: [pk_col_1, pk_col_2, ...]}``.

    Column order follows ``key_column_usage.ordinal_position`` so
    composite PKs preserve column ordering.
    """
    schema_clause = ""
    if schema_filter:
        # We re-validate the schema_filter through the IDENT regex on
        # the caller side; SQL-literal injection risk here is bounded
        # to the schema name itself. Keep the literal quoted.
        schema_clause = f"AND kcu.table_schema = '{schema_filter}'"

    sql = (
        "SELECT tc.constraint_name, kcu.table_schema, kcu.table_name, "
        "kcu.column_name, kcu.ordinal_position "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON tc.constraint_name = kcu.constraint_name "
        " AND tc.table_schema    = kcu.table_schema "
        "WHERE tc.constraint_type = 'PRIMARY KEY' "
        f"{schema_clause} "
        "ORDER BY kcu.table_name, kcu.ordinal_position"
    )

    rows: List[Any] = []
    if kind == "postgres":
        rows = _pg_query(con, alias, sql)
    elif kind == "mysql":
        rows = _mysql_query(con, alias, sql)
    else:  # sqlite — duckdb union view DOES carry PRIMARY KEY rows
        params: List[Any] = [alias]
        sql_union = (
            "SELECT tc.constraint_name, kcu.table_schema, kcu.table_name, "
            "kcu.column_name, kcu.ordinal_position "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            " AND tc.table_schema    = kcu.table_schema "
            " AND tc.table_catalog   = kcu.table_catalog "
            "WHERE tc.table_catalog = ? AND tc.constraint_type = 'PRIMARY KEY' "
        )
        if schema_filter:
            sql_union += "AND tc.table_schema = ? "
            params.append(schema_filter)
        sql_union += "ORDER BY kcu.table_name, kcu.ordinal_position"
        try:
            rows = con.execute(sql_union, params).fetchall()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("pk_extract_failed_sqlite: %s", type(exc).__name__)
            return {}

    out: Dict[str, List[str]] = {}
    for _cname, _schema, table_name, column_name, _pos in rows:
        out.setdefault(table_name, []).append(column_name)
    return out


def _extract_foreign_keys(
    con: Any, alias: str, kind: str, schema_filter: Optional[str]
) -> Dict[str, List[IntrospectedForeignKey]]:
    """Return ``{<table_name>: [IntrospectedForeignKey, ...]}``.

    Composite FKs come back as a single :class:`IntrospectedForeignKey`
    with column-aligned ``from_columns`` / ``to_columns`` (paired via
    ``position_in_unique_constraint``).

    SQLite returns ``{}`` — the duckdb sqlite extension exposes no
    ``information_schema.referential_constraints`` rows and no
    pass-through query function. Documented limitation.
    """
    if kind == "sqlite":
        return {}

    schema_clause = ""
    if schema_filter:
        schema_clause = f"AND kcu_src.table_schema = '{schema_filter}'"

    sql = (
        "SELECT rc.constraint_name, "
        "       kcu_src.table_schema, kcu_src.table_name, kcu_src.column_name, "
        "       kcu_src.ordinal_position, "
        "       kcu_tgt.table_schema AS tgt_schema, kcu_tgt.table_name AS tgt_table, "
        "       kcu_tgt.column_name AS tgt_column, "
        "       rc.update_rule, rc.delete_rule, rc.match_option "
        "FROM information_schema.referential_constraints rc "
        "JOIN information_schema.key_column_usage kcu_src "
        "  ON rc.constraint_catalog = kcu_src.constraint_catalog "
        " AND rc.constraint_schema  = kcu_src.constraint_schema "
        " AND rc.constraint_name    = kcu_src.constraint_name "
        "JOIN information_schema.key_column_usage kcu_tgt "
        "  ON rc.unique_constraint_catalog = kcu_tgt.constraint_catalog "
        " AND rc.unique_constraint_schema  = kcu_tgt.constraint_schema "
        " AND rc.unique_constraint_name    = kcu_tgt.constraint_name "
        " AND kcu_src.position_in_unique_constraint = kcu_tgt.ordinal_position "
        f"WHERE 1=1 {schema_clause} "
        "ORDER BY kcu_src.table_name, rc.constraint_name, kcu_src.ordinal_position"
    )

    rows: List[Any]
    if kind == "postgres":
        rows = _pg_query(con, alias, sql)
    else:  # mysql
        rows = _mysql_query(con, alias, sql)

    # Group by (table_name, constraint_name) so composite FKs roll up.
    grouped: Dict[tuple, IntrospectedForeignKey] = {}
    for row in rows:
        (
            cname,
            _src_schema,
            src_table,
            src_col,
            _src_pos,
            tgt_schema,
            tgt_table,
            tgt_col,
            upd,
            dele,
            match,
        ) = row
        key = (src_table, cname)
        if key not in grouped:
            grouped[key] = IntrospectedForeignKey(
                constraint_name=cname,
                from_columns=[],
                to_schema=tgt_schema,
                to_table=tgt_table,
                to_columns=[],
                update_rule=upd,
                delete_rule=dele,
                match_option=match,
            )
        grouped[key].from_columns.append(src_col)
        grouped[key].to_columns.append(tgt_col)

    out: Dict[str, List[IntrospectedForeignKey]] = {}
    for (table_name, _cname), fk in grouped.items():
        out.setdefault(table_name, []).append(fk)
    return out


def _extract_check_constraints(
    con: Any, alias: str, kind: str, schema_filter: Optional[str]
) -> Dict[str, List[IntrospectedCheckConstraint]]:
    """Return ``{<table_name>: [IntrospectedCheckConstraint, ...]}``.

    Filters out NOT-NULL auto-generated CHECK rows — those are
    surfaced via the column-level ``nullable`` flag, not as constraint
    rows.

    SQLite returns ``{}`` — the duckdb union view only ever exposes
    NOT-NULL CHECKs for sqlite-attached tables (application CHECKs
    aren't reachable).
    """
    if kind == "sqlite":
        return {}

    schema_clause = ""
    if schema_filter:
        schema_clause = f"AND cc.constraint_schema = '{schema_filter}'"

    sql = (
        "SELECT cc.constraint_name, cc.check_clause, "
        "       ccu.table_schema, ccu.table_name, ccu.column_name "
        "FROM information_schema.check_constraints cc "
        "LEFT JOIN information_schema.constraint_column_usage ccu "
        "  ON cc.constraint_name = ccu.constraint_name "
        " AND cc.constraint_schema = ccu.constraint_schema "
        f"WHERE 1=1 {schema_clause} "
        "ORDER BY ccu.table_name, cc.constraint_name"
    )

    rows: List[Any]
    if kind == "postgres":
        rows = _pg_query(con, alias, sql)
    else:  # mysql
        rows = _mysql_query(con, alias, sql)

    grouped: Dict[tuple, IntrospectedCheckConstraint] = {}
    for cname, clause, _schema, table_name, column_name in rows:
        if clause is None or not str(clause).strip():
            continue
        if _NOT_NULL_CHECK_RE.match(str(clause)):
            continue
        if table_name is None:
            # constraint_column_usage didn't bind to a table — skip.
            continue
        key = (table_name, cname)
        if key not in grouped:
            grouped[key] = IntrospectedCheckConstraint(
                constraint_name=cname,
                check_clause=str(clause),
                columns=[],
            )
        if column_name and column_name not in grouped[key].columns:
            grouped[key].columns.append(column_name)

    out: Dict[str, List[IntrospectedCheckConstraint]] = {}
    for (table_name, _cname), chk in grouped.items():
        out.setdefault(table_name, []).append(chk)
    return out


def _extract_precision_scale(
    con: Any, alias: str, kind: str, schema_filter: Optional[str]
) -> Dict[tuple, Dict[str, Optional[int]]]:
    """Return ``{(table_name, column_name): {"numeric_precision": ..., ...}}``.

    Sourced from ``information_schema.columns`` on the source side
    (postgres / mysql) via pass-through; falls back to the duckdb
    union view for sqlite (which propagates fewer of the parameter
    fields but DOES expose them when present).

    The duckdb postgres extension's union view strips
    ``character_maximum_length`` for VARCHAR columns even though the
    source has it — that's why we go through ``postgres_query()`` for
    the parameter fields too.
    """
    schema_clause = ""
    if schema_filter:
        schema_clause = f"AND table_schema = '{schema_filter}'"

    sql = (
        "SELECT table_name, column_name, "
        "       character_maximum_length, numeric_precision, numeric_scale "
        f"FROM information_schema.columns WHERE 1=1 {schema_clause} "
        "ORDER BY table_name, ordinal_position"
    )

    rows: List[Any]
    if kind == "postgres":
        rows = _pg_query(con, alias, sql)
    elif kind == "mysql":
        rows = _mysql_query(con, alias, sql)
    else:  # sqlite — fall back to the union view (catalog scoped)
        params: List[Any] = [alias]
        sql_union = (
            "SELECT table_name, column_name, "
            "       character_maximum_length, numeric_precision, numeric_scale "
            "FROM information_schema.columns WHERE table_catalog = ? "
        )
        if schema_filter:
            sql_union += "AND table_schema = ? "
            params.append(schema_filter)
        sql_union += "ORDER BY table_name, ordinal_position"
        try:
            rows = con.execute(sql_union, params).fetchall()
        except Exception as exc:  # noqa: BLE001
            LOG.debug("precision_extract_failed_sqlite: %s", type(exc).__name__)
            return {}

    out: Dict[tuple, Dict[str, Optional[int]]] = {}
    for table_name, column_name, char_max, prec, scale in rows:
        out[(table_name, column_name)] = {
            "character_max_length": char_max,
            "numeric_precision": prec,
            "numeric_scale": scale,
        }
    return out


def introspect_jdbc(
    *,
    source: str,
    uri: str,
    schema_filter: Optional[str] = None,
    table_filter: Optional[List[str]] = None,
) -> IntrospectedDatabase:
    """Connect to the JDBC database via duckdb extensions and enumerate
    its tables, columns, PKs, FKs, and CHECK constraints.

    Args:
        source: One of ``postgres``, ``postgresql``, ``mysql``, ``sqlite``.
        uri: JDBC URI (see module docstring).
        schema_filter: Optional schema name. When None, every
            user-accessible schema is enumerated.
        table_filter: Optional list of table names to enumerate. When
            None, every table is returned.

    Returns:
        :class:`IntrospectedDatabase` with the full table/column tree
        plus PK/FK/CHECK constraint metadata (PG + MySQL).

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

    # The schema_filter goes into pass-through SQL as a literal; bound
    # to a bare ident pattern to keep the surface tight.
    if schema_filter is not None and not _IDENT_RE.match(schema_filter):
        raise ValueError(
            f"Invalid schema filter: {schema_filter!r}. " "Must match ``[A-Za-z_][A-Za-z0-9_]*``."
        )

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

        # Constraint + precision/scale extraction (PG + MySQL via
        # pass-through; SQLite is best-effort). Wrapped in
        # ``try``-each so a partial failure (e.g. permission denied on
        # information_schema for one extractor) doesn't take the whole
        # introspect down.
        try:
            pk_map = _extract_primary_keys(con, alias, kind, schema_filter)
        except Exception as exc:  # noqa: BLE001
            LOG.warning(
                "jdbc_pk_extract_failed kind=%s exc=%s",
                kind,
                type(exc).__name__,
            )
            pk_map = {}
        try:
            fk_map = _extract_foreign_keys(con, alias, kind, schema_filter)
        except Exception as exc:  # noqa: BLE001
            LOG.warning(
                "jdbc_fk_extract_failed kind=%s exc=%s",
                kind,
                type(exc).__name__,
            )
            fk_map = {}
        try:
            check_map = _extract_check_constraints(con, alias, kind, schema_filter)
        except Exception as exc:  # noqa: BLE001
            LOG.warning(
                "jdbc_check_extract_failed kind=%s exc=%s",
                kind,
                type(exc).__name__,
            )
            check_map = {}
        try:
            precision_map = _extract_precision_scale(con, alias, kind, schema_filter)
        except Exception as exc:  # noqa: BLE001
            LOG.warning(
                "jdbc_precision_extract_failed kind=%s exc=%s",
                kind,
                type(exc).__name__,
            )
            precision_map = {}
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
        params = precision_map.get((table_name, column_name)) or {}
        tables[key].columns.append(
            IntrospectedColumn(
                name=column_name,
                type_name=str(data_type).lower(),
                nullable=(str(is_nullable).upper() == "YES"),
                numeric_precision=params.get("numeric_precision"),
                numeric_scale=params.get("numeric_scale"),
                character_max_length=params.get("character_max_length"),
            )
        )

    # Attach PK / FK / CHECK to the matching IntrospectedTable. We
    # tolerate a constraint pointing at a table that isn't in the
    # final ``tables`` map (e.g. table_filter excluded it) — just skip.
    for table_obj in tables.values():
        table_obj.primary_key_columns = pk_map.get(table_obj.name, [])
        table_obj.foreign_keys = fk_map.get(table_obj.name, [])
        table_obj.checks = check_map.get(table_obj.name, [])

    return IntrospectedDatabase(
        source_kind=kind,
        database=args.get("dbname", args.get("path", "")),
        tables=list(tables.values()),
    )


__all__ = [
    "IntrospectedCheckConstraint",
    "IntrospectedColumn",
    "IntrospectedDatabase",
    "IntrospectedForeignKey",
    "IntrospectedTable",
    "SUPPORTED_KINDS",
    "introspect_jdbc",
]
