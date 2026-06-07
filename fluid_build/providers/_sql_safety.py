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
from typing import Any, Mapping

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


def quote_string_literal(value: str) -> str:
    """Quote a SQL string literal by doubling embedded single quotes."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid SQL string literal: {value!r}")
    return "'" + value.replace("'", "''") + "'"


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
