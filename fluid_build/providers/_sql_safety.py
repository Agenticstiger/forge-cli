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

import re
from typing import Any, Mapping, Optional

_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Identifier-quote characters for every supported dialect belong in the
# safe set: ``"`` (ANSI / Snowflake), ``[`` ``]`` (T-SQL), and ``` ` ```
# (BigQuery / MySQL). The backtick was the missing one — BigQuery table
# references with a hyphenated project id (`` `my-proj.ds.tbl` ``) MUST
# be backtick-quoted, and the MCP output-port query compiler
# allowlist-checks the driver-built ``table_reference`` as a
# defence-in-depth step, so a backtick-quoted reference would otherwise
# be rejected and the ``query`` tool fails on BigQuery. A backtick can't
# open a statement or a comment, so the ``;`` / ``--`` / ``/* */`` and
# blocked-keyword guards below still hold.
_SAFE_EXPR_CHARS = re.compile(r"^[A-Za-z0-9_\s().,<>=!'+\-*/%|&\"`:\[\]]+$")
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


def quote_string_literal(value: str, *, backslash_escapes: bool = True) -> str:
    """Quote a SQL string literal so no input can break out of it.

    Doubling the embedded single quotes is only sufficient on dialects whose
    string literals are *standard-conforming* — i.e. where ``\\`` is an
    ordinary character. Snowflake, MySQL, BigQuery and Redshift instead treat
    ``\\`` as an escape character inside ``'...'``, so a value ending in a
    backslash defeats the doubling: ``\\'`` is consumed as an *escaped* quote
    and the doubled partner **terminates the literal**, handing the caller's
    remaining text to the SQL parser.

    Concretely, on Snowflake ``quote_string_literal("X\\\\') AND 1=0 --")``
    used to emit ``'X\\'') AND 1=0 --'``, which the server parses as the
    literal ``X'`` followed by live SQL. That is a break-out, not a quoted
    value.

    ``backslash_escapes`` therefore defaults to ``True`` — the fail-closed
    setting, correct for every dialect that honours backslash escapes and
    merely redundant nowhere it matters. Callers targeting a
    standard-conforming dialect (DuckDB, PostgreSQL, Trino/Athena) MUST pass
    ``backslash_escapes=False``: doubling backslashes there would corrupt the
    *value* (a Windows path or a secret would gain extra backslashes) without
    buying any safety, because ``\\`` cannot escape a quote in the first place.
    """
    if not isinstance(value, str):
        raise ValueError(f"Invalid SQL string literal: {value!r}")
    escaped = value.replace("\\", "\\\\") if backslash_escapes else value
    return "'" + escaped.replace("'", "''") + "'"


def quote_ansi_string_literal(value: str) -> str:
    """Quote a SQL string literal for a *standard-conforming* dialect.

    DuckDB, PostgreSQL, SQLite and Trino/Athena treat ``\\`` as an ordinary
    character inside ``'...'``, so doubling the single quotes is already
    break-out-proof there — and doubling the backslashes on top (what
    :func:`quote_string_literal` does by default) would corrupt the *value*:
    a Windows path or a secret would silently gain extra backslashes.

    Thin, greppable sibling of :func:`quote_string_literal` so a DuckDB /
    PostgreSQL emitter declares its dialect once at the import line instead of
    repeating ``backslash_escapes=False`` at every call site. Emitters for
    Snowflake / BigQuery / MySQL / Redshift must keep using
    :func:`quote_string_literal`.
    """
    return quote_string_literal(value, backslash_escapes=False)


# Dialect hints used by :func:`dialect_uses_backslash_escapes`. Only the
# *standard-conforming* side is enumerated: an unknown dialect falls through to
# ``True`` (escape the backslash) so a new backend fails closed.
_STANDARD_CONFORMING_DIALECTS = frozenset(
    {
        "ansi",
        "athena",
        "duckdb",
        "postgres",
        "postgresql",
        "presto",
        "sqlite",
        "trino",
    }
)


def dialect_uses_backslash_escapes(dialect: str | None) -> bool:
    """Return whether ``dialect`` treats ``\\`` as an escape inside ``'...'``.

    Feed the result to :func:`quote_string_literal`'s ``backslash_escapes``
    for call sites that already carry a dialect hint (e.g. the quality
    engine). Unknown / missing dialects return ``True`` — the fail-closed
    answer.
    """
    if not dialect:
        return True
    return dialect.strip().lower() not in _STANDARD_CONFORMING_DIALECTS


# ---------------------------------------------------------------------------
# SQL type-parameter payload allowlisting (DDL type-string injection hardening)
# ---------------------------------------------------------------------------
#
# A parameterised type-suffix payload — the text inside ``(...)`` such as
# ``"18,4"`` or ``"100"`` — reaches ``CREATE TABLE`` / ``CREATE PROCEDURE``
# DDL. ``validate_sql_type_param_payload`` (below) allowlists it to digits +
# at most one comma so a contract cannot smuggle DDL through a type parameter
# (identifiers/literals are already routed through ``validate_ident`` /
# ``quote_string_literal``).
_SQL_TYPE_PARAM_PAYLOAD = re.compile(r"^[0-9]+(\s*,\s*[0-9]+)?$")

# A *whole* column-type token (not just the ``(...)`` payload) such as
# ``INT`` / ``VARCHAR(255)`` / ``NUMBER(18,4)`` / ``TIMESTAMP_NTZ`` /
# ``TIMESTAMP WITHOUT TIME ZONE``. ``validate_ident`` rejects parens and
# spaces, so a parameterised / multi-word type needs this conservative
# allowlist instead: letters, digits, underscores, spaces, parens, commas
# and apostrophes (a few dialects spell types like ``INTERVAL 'day'``). The
# set deliberately excludes ``;`` ``"`` ``-`` ``*`` ``/`` ``=`` so a type
# token cannot carry a statement terminator, an identifier-quote, or a
# comment sequence into the surrounding ``CREATE TABLE`` DDL.
_SQL_TYPE_NAME = re.compile(r"^[A-Za-z0-9_ ()',]+$")


class SqlTypeError(ValueError):
    """Raised when a SQL type name or language fails its allowlist.

    Subclasses :class:`ValueError` so existing ``except ValueError`` handlers
    around the SQL-safety helpers continue to catch it.
    """


def validate_sql_type_name(type_name: str) -> str:
    """Validate a whole SQL column-type token and return it unchanged.

    For DDL emitters that interpolate a contract-supplied column ``type``
    (e.g. ``VARCHAR(255)``) directly into ``CREATE TABLE`` SQL. ``validate_ident``
    is too strict for parameterised / multi-word types (it forbids parens and
    spaces), so this applies the conservative type allowlist
    ``^[A-Za-z0-9_ ()',]+$`` and additionally fails closed on comment
    sequences (``--`` / ``/*`` / ``*/``) — defence-in-depth mirroring
    :func:`validate_sql_expression_allowlist`. Raises :class:`SqlTypeError`
    (a ``ValueError`` subclass) on anything outside the allowlist so callers
    that already catch ``ValueError`` keep working.
    """
    if not isinstance(type_name, str):
        raise SqlTypeError(f"Invalid SQL type name: {type_name!r}")
    candidate = type_name.strip()
    if not candidate:
        raise SqlTypeError("Invalid SQL type name: empty")
    if any(token in candidate for token in ("--", "/*", "*/")):
        raise SqlTypeError(f"Invalid SQL type name: {type_name!r}")
    if not _SQL_TYPE_NAME.match(candidate):
        raise SqlTypeError(f"Invalid SQL type name: {type_name!r}")
    return candidate


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


# Control characters have no place in a connection value. That is every
# character of Unicode category Cc: C0, DEL and C1 (U+0080..U+009F, whose
# U+0085 NEXT LINE is a line break to str.splitlines()). libpq's conninfo
# parser ends an unquoted value at any C isspace() character (PostgreSQL
# src/interfaces/libpq/fe-connect.c, conninfo_parse) and libpq_escape quotes
# only a value with a space or a quote, so a tab or a newline would start a
# second keyword; and a scanner's connect error quotes the DSN, so an ESC or a
# newline would reach a terminal or split a log line.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_DSN_FIELDS = ("host", "user", "password", "database")


def reject_control_chars(value: Any, field_name: str) -> None:
    """Raise ``ValueError`` if ``value`` carries a control character (Unicode Cc)."""
    if value is not None and _CONTROL_CHARS.search(str(value)):
        raise ValueError(
            f"Invalid {field_name} in database connection: control characters are not permitted."
        )


def libpq_escape(value: Any) -> str:
    """Escape one libpq DSN value (postgres; mysql uses :func:`mysql_dsn_value`).

    Strings with whitespace or single quotes are wrapped in single
    quotes with internal quotes / backslashes doubled (libpq syntax).
    Plain alphanumerics pass through unchanged. The result is then
    wrapped in :func:`quote_string_literal` at the SQL boundary, so
    two layers of quoting compose correctly: libpq sees one level,
    SQL sees the other. Callers MUST still wrap the full DSN with
    :func:`quote_string_literal` at the SQL boundary.

    Used by:

    * The duckdb acquisition runner when building ``postgres_scan(...)``
      DSNs.
    * The JDBC attach builder (``cli/discover/_jdbc_introspect.py``) for
      postgres.
    """
    s = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{s}'" if (" " in s or "'" in s) else s


def build_libpq_dsn(
    connection: Mapping[str, Any],
    *,
    database_key: str = "dbname",
) -> str:
    """Construct a libpq-style DSN ``key=value`` whitespace-separated string.

    The postgres path uses ``database_key="dbname"`` (libpq convention).
    The mysql path (``database_key="database"``) must not use this
    function: DuckDB's mysql extension does not read libpq syntax, so
    :func:`build_mysql_dsn` builds that DSN.

    Empty / missing fields are omitted (no ``user=`` for an empty
    user). The returned DSN is SQL-literal-safe once the caller wraps
    it with :func:`quote_string_literal`. ``port`` must be ASCII digits
    and a value with a control character is refused, both with
    ``ValueError`` at build time; other values route through
    :func:`libpq_escape`.
    """
    for key in _DSN_FIELDS:
        reject_control_chars(connection.get(key), key)
    parts = []
    if connection.get("host"):
        parts.append(f"host={libpq_escape(connection['host'])}")
    if connection.get("port") not in (None, ""):
        port_str = str(connection["port"])
        # str.isdigit() alone also accepts fullwidth, Arabic-Indic, superscript
        # and other Unicode digits; a port is ASCII 0-9 only.
        if not (port_str.isascii() and port_str.isdigit()):
            raise ValueError(f"connection.port must be numeric, got {port_str!r}")
        parts.append(f"port={port_str}")
    if connection.get("user"):
        parts.append(f"user={libpq_escape(connection['user'])}")
    if connection.get("password"):
        parts.append(f"password={libpq_escape(connection['password'])}")
    if connection.get("database"):
        parts.append(f"{database_key}={libpq_escape(connection['database'])}")
    return " ".join(parts)


def mysql_dsn_value(value: Any) -> str:
    """Quote one value for DuckDB's mysql_scanner DSN.

    That DSN is not libpq syntax: an unquoted value is taken literally up to
    the next space, single quotes mean nothing, and inside double quotes only
    ``\\\\`` and ``\\"`` are escapes. Every value is therefore double-quoted
    with backslash escaped first, then the double quote, so it reaches the
    server exactly and cannot add a second keyword (``socket=``, ``ssl_key=``).
    A ``?`` cannot be carried at all: :func:`build_mysql_dsn` refuses it.
    """
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_mysql_dsn(connection: Mapping[str, Any], *, database_key: str = "database") -> str:
    """The mysql counterpart of :func:`build_libpq_dsn`, in mysql_scanner syntax.

    Every value goes through :func:`mysql_dsn_value`. mysql_scanner first
    runs every attach string through a URI parser that splits on ``?``
    whatever the quoting and reads what follows as URI options (``socket``,
    ``ssl-key``, ...), so a value carrying ``?`` is refused. So is a host
    with a ``/``: ``name://...`` makes MariaDB Connector/C load a client
    plugin of that name, and the scanner reads a path as a host name, not a
    socket. ``port`` must be ASCII digits, and a value with a control
    character is refused. Callers MUST still wrap the full DSN with
    :func:`quote_string_literal` at the SQL boundary.
    """
    for key in _DSN_FIELDS:
        reject_control_chars(connection.get(key), key)
    for key in _DSN_FIELDS:
        if "?" in str(connection.get(key) or ""):
            raise ValueError(
                f"Invalid {key} in mysql source: '?' cannot be passed through "
                "DuckDB's mysql extension."
            )
    host = connection.get("host")
    if host and "/" in str(host):
        raise ValueError("Invalid host in mysql source: '/' is not permitted in a mysql host.")
    parts = []
    if host:
        parts.append(f"host={mysql_dsn_value(host)}")
    if connection.get("port") not in (None, ""):
        port = str(connection["port"])
        # str.isdigit() alone also accepts fullwidth, Arabic-Indic, superscript
        # and other Unicode digits; a port is ASCII 0-9 only.
        if not (port.isascii() and port.isdigit()):
            raise ValueError(f"connection.port must be numeric, got {port!r}")
        parts.append(f"port={port}")
    for key, name in (("user", "user"), ("password", "password"), ("database", database_key)):
        if connection.get(key):
            parts.append(f"{name}={mysql_dsn_value(connection[key])}")
    return " ".join(parts)


def mask_dsn_password(text: str, password: Optional[str]) -> str:
    """Replace every quoted form of ``password`` in a driver message with ``***``.

    The postgres and mysql scanners quote the DSN they were given in a connect
    error, so the raw value, its libpq and mysql DSN forms and their SQL-literal
    forms are all masked. The JDBC introspection and the duckdb build runner's
    mysql ``ATTACH`` share it.
    """
    if not password:
        return text
    forms = {
        password,
        libpq_escape(password),
        mysql_dsn_value(password),
        mysql_dsn_value(password)[1:-1],
    }
    forms |= {form.replace("'", "''") for form in forms}
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, "***")
    return text


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
