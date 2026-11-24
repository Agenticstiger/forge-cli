# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

# The whole-string allowlists in this module end in ``\Z``, not ``$``. Python's
# ``$`` also matches just before a trailing newline, so ``validate_ident`` used
# to return ``"orders\n"`` unchanged, and that newline split any line-based
# file the name was written into (a Java properties file, a log line).
_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
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
_SAFE_EXPR_CHARS = re.compile(r"^[A-Za-z0-9_\s().,<>=!'+\-*/%|&\"`:\[\]]+\Z")

#: DuckDB functions a caller-supplied statement may not call. The logging functions:
#: ``enable_logging(storage='file')`` with no path makes a DuckDB 1.5.x process abort at
#: exit (SIGABRT), and the logs can hold other queries' text. And the table functions
#: that run a statement given as text (``query``, ``query_table``,
#: ``json_execute_serialized_sql``): a name assembled at run time, such as
#: ``query('SEL' || 'ECT * FROM enable_' || 'logging()')``, never shows in the text a
#: check here reads. Refused as a call, in any case, quoted (``"`` or a backtick, which
#: DuckDB 2.0 reads as an identifier quote) or schema-qualified; a column
#: that only shares a name (``enable_logging``, ``query``) is not a call.
DUCKDB_REFUSED_FUNCTIONS = frozenset(
    {
        "disable_logging",
        "enable_logging",
        # a built-in table macro over query_table (DuckDB 1.5 and 2.0): it reads
        # any relation whose name is assembled at run time
        "histogram_values",
        "json_execute_serialized_sql",
        "query",
        "query_table",
        "truncate_duckdb_logs",
        "write_log",
    }
)
#: DuckDB's log views and functions read as a relation: refused by name, called or not.
DUCKDB_LOG_RELATIONS = frozenset({"duckdb_log_contexts", "duckdb_logs", "duckdb_logs_parsed"})
DUCKDB_LOGGING_NAMES = DUCKDB_REFUSED_FUNCTIONS | DUCKDB_LOG_RELATIONS

_DUCKDB_REFUSED_RE = re.compile(
    r"(?i)\b(?:" + "|".join(sorted(DUCKDB_LOG_RELATIONS)) + r")\b"
    r"|\b(?:" + "|".join(sorted(DUCKDB_REFUSED_FUNCTIONS)) + r')["`]?\s*\('
)


def names_refused_duckdb_function(sql: str) -> bool:
    """Whether ``sql`` calls one of :data:`DUCKDB_REFUSED_FUNCTIONS` or names one of
    :data:`DUCKDB_LOG_RELATIONS`."""
    return _DUCKDB_REFUSED_RE.search(sql) is not None


#: Postgres functions that return a whole table, schema, database or query result as
#: XML. They take the relation or the statement as a string argument, so no table node
#: and no column name shows in the text a check here reads, and the engine reads past
#: any column projection the caller's statement was limited to. Refused as a call, in
#: any case, quoted or schema-qualified, on every dialect; a column that only shares a
#: name is not a call.
POSTGRES_REFUSED_FUNCTIONS = frozenset(
    {
        "cursor_to_xml",
        "cursor_to_xmlschema",
        "query_to_xml",
        "query_to_xml_and_xmlschema",
        "query_to_xmlschema",
        "table_to_xml",
        "table_to_xml_and_xmlschema",
        "table_to_xmlschema",
    }
)
#: ``schema_to_xml*`` and ``database_to_xml*`` are a family: every member is refused.
POSTGRES_REFUSED_FUNCTION_PREFIXES = ("schema_to_xml", "database_to_xml")

_POSTGRES_REFUSED_RE = re.compile(
    r"(?i)\b(?:"
    + "|".join(sorted(POSTGRES_REFUSED_FUNCTIONS))
    + "|"
    + "|".join(rf"{prefix}\w*" for prefix in POSTGRES_REFUSED_FUNCTION_PREFIXES)
    + r')["`]?\s*\('
)


def names_refused_postgres_function(sql: str) -> bool:
    """Whether ``sql`` calls one of :data:`POSTGRES_REFUSED_FUNCTIONS` or a
    ``schema_to_xml*`` / ``database_to_xml*`` function."""
    return _POSTGRES_REFUSED_RE.search(sql) is not None


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


# Dialects that take a quote inside ``'...'`` only as ``\'``. Spark SQL
# (Databricks) reads ``'it''s'`` as the adjacent literals ``'it'`` and ``'s'``
# and joins them into ``its`` (checked on a SQL warehouse); BigQuery rejects
# it. Both honour backslash escapes, so ``\\`` is doubled first.
_BACKSLASH_QUOTE_DIALECTS = frozenset({"bigquery", "databricks", "spark"})


def quote_string_literal_for(dialect: str | None, value: str) -> str:
    """Quote ``value`` as a string literal of ``dialect`` that reads back as ``value``.

    :func:`quote_string_literal` for every dialect that doubles a quote, and
    ``'...\\'...'`` for the ones in :data:`_BACKSLASH_QUOTE_DIALECTS`: with
    every backslash doubled before the quotes are escaped, no value can end
    the literal early on either.
    """
    if not isinstance(value, str):
        raise ValueError(f"Invalid SQL string literal: {value!r}")
    if (dialect or "").strip().lower() in _BACKSLASH_QUOTE_DIALECTS:
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return quote_string_literal(value, backslash_escapes=dialect_uses_backslash_escapes(dialect))


#: Dialects whose quoted identifiers are ``"..."`` with ``""`` for a quote inside. Athena's
#: queries run on Trino, which quotes this way (its DDL, run on Hive, takes backticks).
_DOUBLE_QUOTE_IDENT_DIALECTS = frozenset(
    {"ansi", "athena", "duckdb", "postgres", "postgresql", "snowflake", "trino"}
)
#: Dialects whose quoted identifiers are ```...``` with a doubled backtick inside (Spark SQL).
_DOUBLED_BACKTICK_IDENT_DIALECTS = frozenset({"databricks", "spark"})


def quote_identifier_for(dialect: str | None, name: str) -> str:
    """Quote ``name`` as an identifier of ``dialect`` that reads back as exactly ``name``.

    For the FLUID SQL/1 renderer, whose names are arbitrary contract column names
    (``Net Amount``, ``order``): :func:`validate_ident` refuses those, and quoting is
    what makes them safe. Every quote inside is doubled, so no name can end the
    identifier early. A name with a control character is refused, and so is a
    dialect whose identifier quoting this module does not know yet (fail closed).
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    if _CONTROL_CHARS.search(name):
        raise ValueError(f"Invalid SQL identifier {name!r}: control characters are not permitted")
    key = (dialect or "").strip().lower()
    if key in _DOUBLE_QUOTE_IDENT_DIALECTS:
        return '"' + name.replace('"', '""') + '"'
    if key in _DOUBLED_BACKTICK_IDENT_DIALECTS:
        return "`" + name.replace("`", "``") + "`"
    if key == "bigquery":
        # BigQuery reads escape sequences inside a quoted identifier, as inside a string
        # literal: the backslash is doubled first, then the backtick escaped.
        return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"
    raise ValueError(f"no identifier quoting for SQL dialect {dialect!r}")


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
_SQL_TYPE_PARAM_PAYLOAD = re.compile(r"^[0-9]+(\s*,\s*[0-9]+)?\Z")

# A *whole* column-type token (not just the ``(...)`` payload) such as
# ``INT`` / ``VARCHAR(255)`` / ``NUMBER(18,4)`` / ``TIMESTAMP_NTZ`` /
# ``TIMESTAMP WITHOUT TIME ZONE``. ``validate_ident`` rejects parens and
# spaces, so a parameterised / multi-word type needs this conservative
# allowlist instead: letters, digits, underscores, spaces, parens, commas
# and apostrophes (a few dialects spell types like ``INTERVAL 'day'``). The
# set deliberately excludes ``;`` ``"`` ``-`` ``*`` ``/`` ``=`` so a type
# token cannot carry a statement terminator, an identifier-quote, or a
# comment sequence into the surrounding ``CREATE TABLE`` DDL.
_SQL_TYPE_NAME = re.compile(r"^[A-Za-z0-9_ ()',]+\Z")


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
    ``^[A-Za-z0-9_ ()',]+\\Z`` and additionally fails closed on comment
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

    * The duckdb acquisition runner when building its postgres ``ATTACH``
      DSN.
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


#: What a connection URL reads in a host or a database name as more than the
#: name: ``?`` starts the connection properties (pgjdbc reads ``host=`` there,
#: after the URL's own host), ``;`` starts SQL Server's, ``&`` and ``#`` delimit
#: them, and ``@`` and ``\`` end a part in other parsers. A host may not hold a
#: ``/`` either, which ends it, nor whitespace; it may hold a ``\``, which names a
#: SQL Server instance (``host\SQLEXPRESS``).
_URL_NAME_SYNTAX = "?#;&@\\"


def reject_url_syntax(connection: Mapping[str, Any], *, where: str) -> None:
    """Raise ``ValueError`` when ``host``, ``port`` or ``database`` would not stay in its URL part.

    For the runners that build a connection URL from these fields, or hand them
    to a connector that does: the Kafka Connect JDBC source builds
    ``jdbc:<protocol>://host:port/database`` itself, and Debezium builds the
    same URL from ``database.hostname``, ``database.port`` and
    ``database.dbname``. A field that holds URL syntax would move the
    connection, so it is refused before the URL is built. ``port`` must be
    ASCII digits. The message names the field and never its value.
    """
    for key, name in (("host", "host name"), ("database", "database name")):
        value = connection.get(key)
        shown = None if value is None else _url_syntax_in(str(value), host=key == "host")
        if shown is not None:
            raise ValueError(
                f"Invalid {key} for {where}: {shown} is not permitted, because the "
                f"connection URL would read it as more than a {name}."
            )
    port = connection.get("port")
    if port not in (None, ""):
        text = str(port)
        if not (text.isascii() and text.isdigit()):
            raise ValueError(f"Invalid port for {where}: connection.port must be a port number.")


def _url_syntax_in(text: str, *, host: bool) -> Optional[str]:
    """The first character of ``text`` a URL would read as more than a name, described."""
    refused = _URL_NAME_SYNTAX.replace("\\", "/") if host else _URL_NAME_SYNTAX
    for char in text:
        if _CONTROL_CHARS.match(char):
            return "a control character"
        if host and char.isspace():
            return "whitespace"
        if char in refused:
            return repr(char)
    return None


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
    if names_refused_duckdb_function(candidate) or names_refused_postgres_function(candidate):
        raise ValueError(f"Invalid SQL expression: {expr!r}")

    return candidate
