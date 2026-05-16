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

from __future__ import annotations

import contextlib
import logging
import re
from typing import Any, Dict, Iterator, List, Mapping, Optional

import sqlglot
from sqlglot import exp

_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_EXPR_CHARS = re.compile(r"^[A-Za-z0-9_\s().,<>=!'+\-*/%|&\":\[\]]+$")
_BLOCKED_EXPR_TOKENS = re.compile(
    r"(?i)\b("
    r"alter|call|copy|create|delete|drop|execute|grant|insert|merge|put|remove|"
    r"revoke|select|show|truncate|update|use"
    r")\b"
)


def validate_ident(name: str) -> str:
    """Validate a SQL identifier to prevent injection and return it unchanged."""
    if not isinstance(name, str) or not _SAFE_IDENT.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def quote_string_literal(value: str) -> str:
    """Quote a SQL string literal by doubling embedded single quotes."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid SQL string literal: {value!r}")
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# SQL data-type + language allowlisting (BUG-SQL-TYPE — DDL type-string
# injection hardening)
# ---------------------------------------------------------------------------
#
# Column ``type`` strings and procedure/UDF ``param.type`` / ``return_type`` /
# ``language`` reach ``CREATE TABLE`` / ``CREATE PROCEDURE`` / ``CREATE
# FUNCTION`` DDL f-strings. Identifiers route through ``validate_ident`` and
# literals through ``quote_string_literal``; a raw *type* string is the same
# regression class — a contract column type of
# ``decimal(18); DROP TABLE victims; CREATE TABLE r (a INT`` would otherwise
# reach ``conn.execute()`` verbatim. These helpers add an allowlist boundary.
#
# Borrowed-not-built (/borrow-before-build receipts, search 2026-05):
#   prior art: sqlglot ``DataType.Type`` enum + ``DataType.from_str()`` —
#              https://github.com/tobymao/sqlglot/blob/main/sqlglot/expressions/datatypes.html
#   prior art: OWASP SQL Injection Prevention Cheat Sheet — allowlist-validate
#              identifiers/types where parameter binding cannot apply (DDL).
#   decision:  adapt-the-pattern, not depend-on-it. ``util/types.py`` already
#              carries a receipt rejecting a sqlglot dependency just for a type
#              table, and sqlglot's parser is documented as deliberately
#              *forgiving* — the wrong tool for a fail-closed allowlist. A tight
#              frozenset + regex mirrors sqlglot's structural model (base-type
#              enum + parameterised suffix) and the ``validate_ident`` /
#              ``SqlAllowlistError`` conventions already in this module.

# Standard Snowflake base type names (uppercased, no parameters). Object data
# kinds only — this is intentionally not a free-form set so a contract cannot
# smuggle DDL through a column ``type``. Mirrors the Snowflake "Summary of data
# types" reference and ``util/types._TYPE_MAP``'s value set.
_ALLOWED_SQL_BASE_TYPES = frozenset(
    {
        # Numeric
        "NUMBER",
        "DECIMAL",
        "NUMERIC",
        "INT",
        "INTEGER",
        "BIGINT",
        "SMALLINT",
        "TINYINT",
        "BYTEINT",
        "FLOAT",
        "FLOAT4",
        "FLOAT8",
        "DOUBLE",
        "DOUBLE PRECISION",
        "REAL",
        # String / binary
        "VARCHAR",
        "CHAR",
        "CHARACTER",
        "STRING",
        "TEXT",
        "NCHAR",
        "NVARCHAR",
        "NVARCHAR2",
        "CHAR VARYING",
        "NCHAR VARYING",
        "BINARY",
        "VARBINARY",
        # Boolean
        "BOOLEAN",
        # Date & time
        "DATE",
        "TIME",
        "DATETIME",
        "TIMESTAMP",
        "TIMESTAMP_LTZ",
        "TIMESTAMP_NTZ",
        "TIMESTAMP_TZ",
        # Semi-structured
        "VARIANT",
        "OBJECT",
        "ARRAY",
        # Geospatial
        "GEOGRAPHY",
        "GEOMETRY",
    }
)

# Languages a Snowflake procedure / UDF may be written in.
_ALLOWED_SQL_LANGUAGES = frozenset({"SQL", "JAVASCRIPT", "PYTHON", "JAVA", "SCALA"})

# An optional parameterised suffix: ``(N)`` or ``(N,N)`` — e.g. ``(38,0)``,
# ``(100)``, ``(18, 4)``. Digits and an optional single comma only; nothing
# that could carry SQL syntax.
_SQL_TYPE_PARAM_SUFFIX = re.compile(r"^\([0-9]+(\s*,\s*[0-9]+)?\)$")
# The same payload without the surrounding parens — used by callers that have
# already split the ``(...)`` off (e.g. ``util/types`` passthrough branch).
_SQL_TYPE_PARAM_PAYLOAD = re.compile(r"^[0-9]+(\s*,\s*[0-9]+)?$")


class SqlTypeError(ValueError):
    """Raised when a SQL type name or language fails its allowlist.

    Subclasses :class:`ValueError` so existing ``except ValueError`` handlers
    around the SQL-safety helpers continue to catch it (consistent with
    :class:`SqlAllowlistError`).
    """


def validate_sql_type(type_str: str) -> str:
    """Validate a SQL data-type string against the base-type allowlist.

    Accepts a base type name from :data:`_ALLOWED_SQL_BASE_TYPES` with an
    optional parameterised ``(...)`` suffix matching :data:`_SQL_TYPE_PARAM_SUFFIX`
    (``(N)`` or ``(N,N)``). The check is case-insensitive and tolerant of
    surrounding whitespace. The validated type is returned **unchanged** (the
    caller controls casing) so it can be interpolated into DDL safely.

    Examples:

    - ``"NUMBER"`` → ``"NUMBER"``
    - ``"DECIMAL(18,4)"`` → ``"DECIMAL(18,4)"``
    - ``"varchar(100)"`` → ``"varchar(100)"``
    - ``"TIMESTAMP_TZ"`` → ``"TIMESTAMP_TZ"``

    Raises :class:`SqlTypeError` for anything else — an unknown base type, a
    malformed suffix, or an injection payload such as
    ``"decimal(18); DROP TABLE t; --"``. The message never echoes the rejected
    value beyond its ``repr`` (which a contract author needs to fix it).
    """
    if not isinstance(type_str, str):
        raise SqlTypeError(f"Invalid SQL type: {type_str!r}")

    candidate = type_str.strip()
    if not candidate:
        raise SqlTypeError("Invalid SQL type: empty")

    # Split an optional parameterised suffix off the base name. Only the first
    # ``(`` matters; any second ``(`` lands inside ``suffix`` and the suffix
    # regex below rejects it.
    paren = candidate.find("(")
    if paren == -1:
        base, suffix = candidate, ""
    else:
        base, suffix = candidate[:paren], candidate[paren:]

    base_norm = " ".join(base.upper().split())
    if base_norm not in _ALLOWED_SQL_BASE_TYPES:
        raise SqlTypeError(f"Invalid SQL type: {type_str!r}")

    if suffix and not _SQL_TYPE_PARAM_SUFFIX.match(suffix):
        raise SqlTypeError(f"Invalid SQL type: {type_str!r}")

    return type_str


def validate_sql_type_param_payload(payload: str) -> str:
    """Validate a bare parameterised-suffix payload (the text inside ``(...)``).

    For callers that have already split the ``(...)`` from a type string and
    hold only the inner text — e.g. ``"18,4"`` or ``"100"``. Returns the
    payload unchanged; raises :class:`SqlTypeError` on anything that is not
    ``N`` or ``N,N`` (digits + at most one comma).
    """
    if not isinstance(payload, str) or not _SQL_TYPE_PARAM_PAYLOAD.match(payload.strip()):
        raise SqlTypeError(f"Invalid SQL type parameter: {payload!r}")
    return payload


def validate_sql_language(language: str) -> str:
    """Validate a procedure/UDF language against the language allowlist.

    Accepts ``SQL`` / ``JAVASCRIPT`` / ``PYTHON`` / ``JAVA`` / ``SCALA``
    (case-insensitive, whitespace-tolerant) and returns the value unchanged.
    Raises :class:`SqlTypeError` for anything else — the ``LANGUAGE`` clause of
    ``CREATE PROCEDURE`` / ``CREATE FUNCTION`` is otherwise an injection point.
    """
    if not isinstance(language, str) or language.strip().upper() not in _ALLOWED_SQL_LANGUAGES:
        raise SqlTypeError(f"Invalid SQL language: {language!r}")
    return language


def libpq_escape(value: Any) -> str:
    """Escape one libpq-style DSN value (postgres + mysql + sqlite extension).

    Strings with whitespace or single quotes are wrapped in single
    quotes with internal quotes / backslashes doubled (libpq syntax).
    Plain alphanumerics pass through unchanged. The result is then
    wrapped in :func:`quote_string_literal` at the SQL boundary, so
    two layers of quoting compose correctly: libpq sees one level,
    SQL sees the other. Callers MUST still wrap the full DSN with
    :func:`quote_string_literal` at the SQL boundary.

    Used by:

    * The duckdb acquisition runner when building ``postgres_scan(...)``
      DSNs and ``ATTACH '<dsn>' ... TYPE mysql`` strings.
    * The discoverer modules (``cli/discover/postgres.py``,
      ``cli/discover/mysql.py``).
    """
    s = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{s}'" if (" " in s or "'" in s) else s


def build_libpq_dsn(
    connection: Mapping[str, Any],
    *,
    database_key: str = "dbname",
) -> str:
    """Construct a libpq-style DSN ``key=value`` whitespace-separated string.

    The postgres path uses ``database_key="dbname"`` (libpq convention);
    the mysql path uses ``database_key="database"`` (duckdb mysql
    extension expects this key). Both engines share the same escape +
    field-emission logic; only the database key name differs.

    Empty / missing fields are omitted (no ``user=`` for an empty
    user). The returned DSN is SQL-literal-safe once the caller wraps
    it with :func:`quote_string_literal`. ``port`` is validated as
    numeric and raises ``ValueError`` at build time when non-digit;
    other values route through :func:`libpq_escape`.
    """
    parts = []
    if connection.get("host"):
        parts.append(f"host={libpq_escape(connection['host'])}")
    if connection.get("port") not in (None, ""):
        port_str = str(connection["port"])
        if not port_str.isdigit():
            raise ValueError(f"connection.port must be numeric, got {port_str!r}")
        parts.append(f"port={port_str}")
    if connection.get("user"):
        parts.append(f"user={libpq_escape(connection['user'])}")
    if connection.get("password"):
        parts.append(f"password={libpq_escape(connection['password'])}")
    if connection.get("database"):
        parts.append(f"{database_key}={libpq_escape(connection['database'])}")
    return " ".join(parts)


def validate_sql_expression_allowlist(expr: str) -> str:
    """Allow only a narrow SQL-expression subset suitable for RLS conditions."""
    if not isinstance(expr, str):
        raise ValueError(f"Invalid SQL expression: {expr!r}")

    candidate = expr.strip()
    if not candidate:
        raise ValueError("Invalid SQL expression: empty")

    if any(token in candidate for token in (";", "--", "/*", "*/")):
        raise ValueError(f"Invalid SQL expression: {expr!r}")

    if not _SAFE_EXPR_CHARS.match(candidate):
        raise ValueError(f"Invalid SQL expression: {expr!r}")

    if _BLOCKED_EXPR_TOKENS.search(candidate):
        raise ValueError(f"Invalid SQL expression: {expr!r}")

    return candidate


# ---------------------------------------------------------------------------
# Statement-body allowlisting (D1/D2/D3 — arbitrary-SQL-execution hardening)
# ---------------------------------------------------------------------------
#
# The three Snowflake action surfaces below accept a raw *statement body* from
# a contract author and execute it. Identifiers/literals are already routed
# through ``validate_ident`` / ``quote_string_literal`` by an earlier pass;
# this layer adds AST-based allowlisting of the *statement kind* so a contract
# cannot smuggle ``DROP ROLE``, ``CREATE USER``, account-level DDL, or a
# multi-statement ``SELECT 1; DROP TABLE t`` payload through.
#
# Borrowed-not-built (/borrow-before-build receipts):
#   tool:  sqlglot (MIT) — the standard Python SQL parser/transpiler.
#   repo:  https://github.com/tobymao/sqlglot
#   why:   OWASP's SQL-injection cheat sheet prescribes allow-listing when
#          parameterised queries cannot apply (DDL identifiers can't bind);
#          sqlglot gives a real AST so classification is structural, not regex.


class SqlAllowlistError(ValueError):
    """Raised when a statement body fails the surface-specific allowlist.

    Subclasses :class:`ValueError` so existing ``except ValueError`` handlers
    around the SQL-safety helpers continue to catch it.
    """


# CREATE/ALTER/DROP ``.kind`` values the pipeline legitimately emits. Object
# kinds only — account-level kinds (USER, ROLE, WAREHOUSE, DATABASE, ACCOUNT,
# INTEGRATION, NETWORK POLICY, ...) are intentionally absent so role/account
# escalation DDL is rejected.
_ALLOWED_DDL_KINDS = frozenset(
    {
        "TABLE",
        "VIEW",
        "MATERIALIZED VIEW",
        "SCHEMA",
        "STREAM",
        "TASK",
        "STAGE",
        "FILE FORMAT",
        "SEQUENCE",
        "FUNCTION",
        "PROCEDURE",
        "DYNAMIC TABLE",
    }
)

# DML / data-movement statement node types that are always permitted for the
# ``custom`` surface (they cannot escalate privileges).
_DML_NODE_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Copy,
    exp.TruncateTable,
)


def _statement_label(node: exp.Expression) -> str:
    """Human-readable label for an error message, never echoing the body."""
    name = type(node).__name__
    kind = getattr(node, "kind", None)
    if isinstance(node, exp.Command):
        # ``Command`` is sqlglot's opaque fallback; surface the leading keyword.
        return f"Command:{str(node.this).upper()}"
    if kind:
        return f"{name}:{kind}"
    return name


def _is_select_like(node: exp.Expression) -> bool:
    """True for SELECT / SELECT-with-CTEs / set operations (UNION etc.)."""
    return isinstance(node, (exp.Select, exp.Union, exp.Subquery))


@contextlib.contextmanager
def _quiet_sqlglot_fallback_warning() -> Iterator[None]:
    """Suppress sqlglot's ``contains unsupported syntax`` WARNING during parse.

    sqlglot logs a WARNING and falls back to an opaque ``Command`` node when it
    cannot model a statement. That fallback is exactly what the allowlist below
    rejects fail-closed, so the warning is redundant noise — the raised
    :class:`SqlAllowlistError` carries the actionable signal instead. Scoped to
    the parse call so the ``sqlglot`` logger is otherwise left untouched.
    """
    logger = logging.getLogger("sqlglot")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous)


def parse_and_allowlist_sql(
    sql: str,
    *,
    surface: str,
    dialect: str = "snowflake",
) -> List[exp.Expression]:
    """Parse ``sql`` and reject anything outside the per-surface allowlist.

    ``surface`` selects the policy:

    * ``"custom"``  — :func:`~fluid_build.providers.snowflake.actions.sql.execute_sql`.
      Permits the DDL/DML the pipeline legitimately emits (CREATE/ALTER/DROP of
      object kinds, INSERT/UPDATE/DELETE/MERGE/COPY/TRUNCATE, GRANT, SELECT).
      Rejects account/role-level DDL (CREATE USER, DROP ROLE, CREATE WAREHOUSE,
      ALTER ACCOUNT, ...) and any statement sqlglot cannot structurally model.
    * ``"task_body"`` — :func:`~fluid_build.providers.snowflake.actions.task.ensure_task`.
      A Snowflake task body is a *single* statement: CALL / INSERT / UPDATE /
      DELETE / MERGE / SELECT. Multi-statement bodies and DDL are rejected.
    * ``"view_body"`` — :func:`~fluid_build.providers.snowflake.actions.view.ensure_view`.
      A view body must be a *single* SELECT (CTEs / UNION allowed). Any DDL,
      DML, or multiple statements are rejected.

    Returns the parsed statement list (callers may ignore it). Raises
    :class:`SqlAllowlistError` on any violation; the message never echoes the
    rejected SQL body, only a structural label.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise SqlAllowlistError(f"{surface}: empty or non-string SQL body")

    try:
        with _quiet_sqlglot_fallback_warning():
            parsed = sqlglot.parse(sql, dialect=dialect)
    except sqlglot.errors.ParseError as e:
        # Unparseable input is rejected fail-closed — a body the standard
        # parser cannot read must not reach the warehouse verbatim.
        raise SqlAllowlistError(f"{surface}: SQL body failed to parse: {e}") from e

    # ``sqlglot.parse`` yields a ``None`` element for an empty / comment-only
    # statement (e.g. a stray trailing ``;`` or ``-- comment``). Drop those so
    # a comment-only payload or trailing separator isn't miscounted, but a body
    # that is *entirely* comments still fails the emptiness check below.
    statements = [s for s in parsed if s is not None]
    if not statements:
        raise SqlAllowlistError(f"{surface}: SQL body has no executable statement")

    if surface in ("task_body", "view_body") and len(statements) > 1:
        raise SqlAllowlistError(
            f"{surface}: expected a single statement, got {len(statements)} "
            f"({', '.join(_statement_label(s) for s in statements)})"
        )

    for node in statements:
        if surface == "view_body":
            if not _is_select_like(node):
                raise SqlAllowlistError(
                    f"view_body: a view body must be a single SELECT statement, "
                    f"got {_statement_label(node)}"
                )
            continue

        if surface == "task_body":
            if _is_select_like(node) or isinstance(
                node, (exp.Insert, exp.Update, exp.Delete, exp.Merge)
            ):
                continue
            if isinstance(node, exp.Command) and str(node.this).upper() == "CALL":
                # ``CALL proc()`` — sqlglot models CALL as an opaque Command;
                # it is the canonical task body so it is allowed by exception.
                continue
            raise SqlAllowlistError(
                f"task_body: a task body must be a single CALL / INSERT / "
                f"UPDATE / DELETE / MERGE / SELECT statement, got "
                f"{_statement_label(node)}"
            )

        # surface == "custom"
        if _is_select_like(node) or isinstance(node, _DML_NODE_TYPES):
            continue
        if isinstance(node, exp.Grant):
            continue
        if isinstance(node, (exp.Create, exp.Alter, exp.Drop)):
            kind = (getattr(node, "kind", None) or "").upper()
            if kind in _ALLOWED_DDL_KINDS:
                continue
            raise SqlAllowlistError(
                f"custom: {type(node).__name__} of kind {kind or '<unknown>'!r} "
                f"is not allowed (account/role-level DDL is rejected)"
            )
        # ``Command`` is sqlglot's opaque fallback — CALL, EXECUTE IMMEDIATE,
        # CREATE USER, GRANT ROLE ... TO ROLE PUBLIC, ALTER ACCOUNT, USE ROLE,
        # etc. all land here. Reject fail-closed: a statement the parser cannot
        # classify must not be executed as arbitrary custom SQL.
        raise SqlAllowlistError(
            f"custom: statement {_statement_label(node)} is not on the "
            f"allowlist (unrecognised or account-level statement rejected)"
        )

    return statements
