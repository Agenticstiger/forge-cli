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
from typing import Any, Dict, Mapping, Optional

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
