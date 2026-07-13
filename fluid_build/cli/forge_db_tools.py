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

"""Opt-in ``fetch_sample_rows`` tool — a LIVE, read-only database row sample.

The forge copilot agent loop can call tools (``forge_copilot_tools``). This
module adds ONE **live-database** tool — ``fetch_sample_rows`` — that returns a
small, row-capped, redacted sample from a real Postgres / MySQL / SQLite source
so the agent can see actual column shapes + value distributions while authoring
a contract (the schema-only ``read_sample_schema`` tool covers local files; this
covers a warehouse the user already has connected).

It is **off by default** and only surfaces when the operator opts in with
``FLUID_FORGE_DB_TOOLS=1``. The gate is modelled 1:1 on the dbt-MCP delegate
(:mod:`cli.dbt_mcp`) and the web-tools delegate (:mod:`cli.forge_web_tools`):
``is_enabled`` reads the env flag, ``db_tool_definitions`` returns ``[]`` when
disabled, and ``dispatch_db_tool`` mirrors the typed-error contract so a failure
never bubbles a raw exception (which could echo a DSN / credentials / a path)
into the LLM context.

Security posture (the whole reason this is gated + env-sourced):

* **Read-only by construction.** The only statement ever issued is
  ``SELECT * FROM <ref> LIMIT <n>``. The LLM never supplies SQL — it names a
  table / schema / connection and nothing else — so there is no write, DDL, or
  arbitrary-query surface. Table + schema names are validated as bare SQL
  identifiers (``providers._sql_safety.validate_ident``) before they touch the
  reference, so a crafted ``customers; DROP TABLE ...`` is refused, not run.

* **Credentials are env-sourced, never LLM-supplied.** The connection URI (which
  carries host + credentials) is read from an environment variable — the LLM
  passes only a connection *alias*. ``default`` → ``FLUID_FORGE_DB_URI``;
  ``<name>`` → ``FLUID_FORGE_DB_URI_<NAME>``. This blocks the LLM from steering a
  connection to an arbitrary host (an SSRF-shaped move) or injecting a crafted
  DSN — the same model the dbt-MCP delegate uses (secrets stay in the shell env,
  never in a config file or a tool argument).

* **Bounded output — no full table dump, no leaked secret.** Rows are hard-capped
  (``MAX_ROWS``); a higher LLM-supplied ``limit`` is clamped and the effective
  cap surfaced. Every returned cell is routed through the central secret
  redactor (``observability.secret_redactor``): credential-SHAPED values are
  masked (API keys, JWTs, connection strings, …) AND any cell in a
  credential-NAMED column (``password`` / ``api_key`` / ``secret`` / …, per
  ``is_sensitive_key_name``) is wholesale-masked, even when its value doesn't
  match a known token shape. Long cells are truncated. The row cap makes the
  result a *sample*, not an export, so a "full PII dump" is impossible by
  construction.

Borrow-before-build receipts (see the PR body for the full search log):
  * The tool shape (LLM sees a table + a small sample) mirrors LangChain's
    ``InfoSQLDatabaseTool`` ("output is the schema and sample rows for those
    tables") and its documented read-only posture ("create a SQL user without
    write permissions"). We enforce read-only in-process (SELECT-only) rather
    than relying on DB grants.
  * The env-gated "return [] when off" delegate + typed-error contract is this
    repo's own dbt-MCP / web-tools pattern.
  * The live-connection mechanics (duckdb ``INSTALL/LOAD/ATTACH`` for
    postgres/mysql/sqlite, URI parsing incl. credentials) are REUSED verbatim
    from the in-repo ``cli/discover/_jdbc_introspect`` introspector rather than
    re-derived — same connection surface, no drift.

Env vars:

* ``FLUID_FORGE_DB_TOOLS`` — ``1``/``true`` to expose the tool (default off →
  ABSENT from ``get_tool_definitions``).
* ``FLUID_FORGE_DB_URI`` — the ``default`` connection's URI, e.g.
  ``postgresql://user:pass@host:5432/db`` / ``mysql://user:pass@host/db`` /
  ``sqlite:////abs/path.db``.
* ``FLUID_FORGE_DB_URI_<NAME>`` — a named connection reachable via
  ``connection=<name>`` (case-insensitive; the alias is upper-cased).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

LOG = logging.getLogger("fluid.cli.forge_db_tools")

_TRUTHY = {"1", "true", "yes", "on"}

# The single tool this module owns.
TOOL_NAME = "fetch_sample_rows"

# Output bounds. The row cap is the primary "no full dump" control; the cell +
# column caps bound the width so one wide/blob column can't blow the context.
DEFAULT_ROWS = 10
MAX_ROWS = 50
MAX_CELL_CHARS = 500
MAX_COLUMNS = 100

# Connection-alias env prefix. ``default`` maps to the bare var; a named alias
# maps to ``<PREFIX>_<UPPER-NAME>``.
_DEFAULT_URI_ENV = "FLUID_FORGE_DB_URI"
_NAMED_URI_ENV_PREFIX = "FLUID_FORGE_DB_URI_"

# A connection alias is a bare identifier so it can only ever name an env var,
# never traverse or interpolate anything.
_ALIAS_RE = re.compile(r"^[A-Za-z0-9_]+$")


# ---------------------------------------------------------------------------
# Gate (mirrors cli.dbt_mcp / cli.forge_web_tools)
# ---------------------------------------------------------------------------
def is_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when the live-DB tool is opted in via ``FLUID_FORGE_DB_TOOLS``."""
    env = env if env is not None else os.environ
    return str(env.get("FLUID_FORGE_DB_TOOLS", "")).strip().lower() in _TRUTHY


def is_db_tool(name: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """True when *name* is the live-DB tool and the delegate is enabled."""
    return bool(name) and name == TOOL_NAME and is_enabled(env)


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------
def _input_schema(model: Any) -> Dict[str, Any]:
    """Derive the LLM-facing JSON Schema from a Pydantic args model.

    Matches ``forge_tool.ForgeTool.input_schema`` — the args model is the single
    source of truth and unknown fields are rejected.
    """
    schema = model.model_json_schema()
    schema.setdefault("additionalProperties", False)
    return schema


def db_tool_definitions(env: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    """Forge-shaped tool defs for the LLM tool list.

    Returns ``[]`` when the delegate is disabled (default) so the tool is simply
    ABSENT from ``get_tool_definitions`` — the exact gate semantics of the
    dbt-MCP / web-tools delegates.
    """
    if not is_enabled(env):
        return []
    from fluid_build.cli._forge_copilot_tool_args import FetchSampleRowsArgs

    return [
        {
            "name": TOOL_NAME,
            "description": (
                "Return a small, row-capped, redacted sample of rows from a "
                "LIVE database table (Postgres / MySQL / SQLite) so you can see "
                "real column shapes and value distributions. READ-ONLY: only a "
                "'SELECT ... LIMIT' is issued; you cannot run arbitrary SQL. You "
                "name a table (and optional schema) plus a connection alias — "
                "the connection URI and its credentials are configured by the "
                "operator in the environment, never passed by you. Secret-shaped "
                "values and credential-named columns are masked in the result; "
                f"rows are capped at {MAX_ROWS}. Use read_sample_schema instead "
                "for local files (CSV / Parquet / …)."
            ),
            "input_schema": _input_schema(FetchSampleRowsArgs),
        }
    ]


# ---------------------------------------------------------------------------
# Connection resolution — env-sourced, never LLM-supplied
# ---------------------------------------------------------------------------
def _resolve_connection_uri(
    connection: str, env: Mapping[str, str]
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a connection alias to its env-sourced URI.

    Returns ``(uri, error_code)``: exactly one is non-None. ``error_code`` is
    ``"InvalidConnection"`` for a malformed alias and ``"DatabaseNotConfigured"``
    when the alias is well-formed but no matching env var is set.
    """
    name = (connection or "default").strip() or "default"
    if name.lower() == "default":
        env_key = _DEFAULT_URI_ENV
    else:
        if not _ALIAS_RE.match(name):
            return None, "InvalidConnection"
        env_key = f"{_NAMED_URI_ENV_PREFIX}{name.upper()}"
    uri = (env.get(env_key) or "").strip()
    if not uri:
        return None, "DatabaseNotConfigured"
    return uri, None


# ---------------------------------------------------------------------------
# Redaction — mask credential-shaped values AND credential-named columns
# ---------------------------------------------------------------------------
def _redact_cell(column_name: str, value: Any) -> Any:
    """Return a JSON-safe, redacted + injection-neutralised rendering of one cell.

    Live DB cells are UNTRUSTED on two axes and both are closed here:

    * **Secret leak** — a cell in a credential-NAMED column
      (``password`` / ``api_key`` / …) is wholesale-masked via the central
      redactor's marker; a credential-SHAPED string value (API key / JWT / DSN)
      is masked by ``redact_secret_text``.
    * **Prompt injection** — a cell value like ``"SYSTEM: ignore prior…"`` or
      ``"<system>…"`` is neutralised via ``demote_markers`` so it can't fake a
      turn boundary once it enters the model's context (mirrors Command
      Center's ``mcp/sanitize.py``).

    Non-string scalars (int / float / bool / None) pass through — they can't be
    secret-shaped or carry markers, and preserving them keeps the sample useful.
    Everything else (datetime, Decimal, bytes, …) is stringified, redacted,
    neutralised, and truncated so it stays JSON-serialisable and bounded.
    """
    # Single source of truth for the mask marker + the sensitive-key predicate:
    # import the redactor MODULE so we track its ``_REDACTED`` constant rather
    # than duplicating the literal (the codebase's "don't let the two redaction
    # layers drift" invariant).
    from fluid_build.cli._untrusted_content import demote_markers
    from fluid_build.observability import secret_redactor

    if secret_redactor.is_sensitive_key_name(str(column_name)):
        return secret_redactor._REDACTED

    if value is None or isinstance(value, (bool, int, float)):
        return value

    text = value if isinstance(value, str) else str(value)
    text = secret_redactor.redact_secret_text(text)
    # Neutralise prompt-injection shapes AFTER redaction (order-independent, but
    # keeps the redactor's marker intact). demote_markers never returns None for
    # a non-empty str.
    text = demote_markers(text) or text
    if len(text) > MAX_CELL_CHARS:
        text = text[:MAX_CELL_CHARS] + "…(truncated)"
    return text


# ---------------------------------------------------------------------------
# The tool impl
# ---------------------------------------------------------------------------
def _fetch_sample_rows(arguments: Dict[str, Any], env: Mapping[str, str]) -> Dict[str, Any]:
    """Validate args, resolve the connection, and return a capped sample.

    Every failure path returns the repo's typed-tool-error shape
    ``{"error": <Code>, "message": …}`` — no raw exception text (which could
    carry the DSN / credentials / a filesystem path) ever reaches the model.
    """
    from fluid_build.cli._forge_copilot_tool_args import FetchSampleRowsArgs

    # Reuse the in-repo JDBC connection scaffolding verbatim (borrow, no drift).
    from fluid_build.cli.discover._jdbc_introspect import (
        _attach_string_mysql,
        _attach_string_postgres,
        _attach_string_sqlite,
        _normalize_kind,
        _parse_uri,
        _validate_alias,
    )
    from fluid_build.providers._sql_safety import validate_ident

    try:
        args = FetchSampleRowsArgs.model_validate(arguments)
    except Exception as exc:  # noqa: BLE001 — Pydantic ValidationError etc.
        return {"error": "ToolValidationError", "message": f"fetch_sample_rows: {exc}"}

    table = (args.table or "").strip()
    if not table:
        return {"error": "InvalidArgs", "message": "table is required"}

    # Resolve the connection URI from the environment (never from the LLM).
    uri, err = _resolve_connection_uri(args.connection, env)
    if err == "InvalidConnection":
        return {
            "error": "InvalidConnection",
            "message": (
                f"connection alias {args.connection!r} is not a bare identifier " "([A-Za-z0-9_]+)."
            ),
        }
    if err == "DatabaseNotConfigured" or not uri:
        return {
            "error": "DatabaseNotConfigured",
            "message": (
                "No connection configured. Set FLUID_FORGE_DB_URI (for the "
                "'default' connection) or FLUID_FORGE_DB_URI_<NAME> (for a named "
                "connection) to a postgres/mysql/sqlite URI."
            ),
        }

    # Validate identifiers BEFORE any connection so a crafted table/schema is
    # refused without touching the network.
    try:
        table = validate_ident(table)
        schema = validate_ident(args.schema_name.strip()) if args.schema_name else None
    except ValueError:
        return {
            "error": "InvalidIdentifier",
            "message": "table/schema must be bare SQL identifiers ([A-Za-z_][A-Za-z0-9_]*).",
        }

    applied_limit = max(1, min(int(args.limit or DEFAULT_ROWS), MAX_ROWS))

    # Resolve the source kind + parse the URI (credentials included) via the
    # shared JDBC helpers.
    try:
        kind = _normalize_kind(uri.split("://", 1)[0])
        conn_args = _parse_uri(uri, kind)
    except ValueError as exc:
        return {"error": "InvalidConnectionUri", "message": str(exc)}

    alias = _validate_alias("source_db")
    if kind == "postgres":
        attach = _attach_string_postgres(conn_args, alias)
    elif kind == "mysql":
        attach = _attach_string_mysql(conn_args, alias)
    else:  # sqlite
        attach = _attach_string_sqlite(conn_args, alias)
    # Defence-in-depth: attach READ_ONLY where the duckdb extension supports it
    # (postgres + sqlite) so even a bug that emitted a write would be refused at
    # the connection level. mysql's extension has no READ_ONLY flag, but the
    # SELECT-only construction below is the real guarantee regardless of engine.
    # (Mirrors Command Center's chat-with-data READ-ONLY connection.)
    if kind in ("postgres", "sqlite") and attach.endswith(")"):
        attach = attach[:-1] + ", READ_ONLY)"

    # Build the read-only reference. Identifiers are validated + double-quoted
    # (duckdb identifier quote) so a reserved word is legal and there is no
    # interpolation surface beyond the validated idents.
    ref = f'"{alias}"."{schema}"."{table}"' if schema else f'"{alias}"."{table}"'
    sql = f"SELECT * FROM {ref} LIMIT {applied_limit}"

    try:
        import duckdb
    except ImportError:
        return {
            "error": "DuckDbNotInstalled",
            "message": "fetch_sample_rows requires duckdb (pip install 'fluid-build[local]').",
        }

    con = duckdb.connect(":memory:")
    try:
        if kind == "postgres":
            con.execute("INSTALL postgres; LOAD postgres;")
        elif kind == "mysql":
            con.execute("INSTALL mysql; LOAD mysql;")
        else:
            con.execute("INSTALL sqlite; LOAD sqlite;")
        con.execute(attach)
        cur = con.execute(sql)
        raw_columns = [str(d[0]) for d in (cur.description or [])]
        raw_rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — connect / attach / SQL failures
        # The duckdb error text can echo the DSN (host/user/password) or a
        # filesystem path — log the detail server-side, surface only the class.
        LOG.warning("fetch_sample_rows failed: %s", type(exc).__name__, exc_info=True)
        return {
            "error": type(exc).__name__,
            "message": f"fetch_sample_rows could not read {table!r} — see server logs",
        }
    finally:
        con.close()

    # Second row ceiling (belt-and-suspenders, mirrors Command Center's
    # nl_query: LIMIT rewrite AND a fetch ceiling): even if the SQL LIMIT were
    # somehow ignored, the sample can never exceed the cap.
    truncated_rows = len(raw_rows) > applied_limit
    raw_rows = raw_rows[:applied_limit]

    # Bound the width, then redact + neutralise every cell.
    columns = raw_columns[:MAX_COLUMNS]
    truncated_columns = len(raw_columns) > MAX_COLUMNS
    rows: List[List[Any]] = []
    for raw in raw_rows:
        row = list(raw)[:MAX_COLUMNS]
        rows.append([_redact_cell(columns[i], row[i]) for i in range(len(columns))])

    from fluid_build.cli._untrusted_content import UNTRUSTED_DATA_NOTICE

    return {
        "connection": (args.connection or "default"),
        "source_kind": kind,
        "schema": schema,
        "table": table,
        "columns": columns,
        "truncated_columns": truncated_columns,
        "applied_limit": applied_limit,
        "row_count": len(rows),
        "truncated_rows": truncated_rows,
        # Label the sample as untrusted DATA so the model never treats a cell
        # value as an instruction (the fence half of the injection mitigation).
        "content_notice": UNTRUSTED_DATA_NOTICE,
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Dispatch (mirrors cli.dbt_mcp.dispatch_dbt_mcp_tool / cli.forge_web_tools)
# ---------------------------------------------------------------------------
def dispatch_db_tool(
    name: str,
    arguments: Optional[Dict[str, Any]],
    env: Optional[Mapping[str, str]] = None,
) -> Any:
    """Route a ``fetch_sample_rows`` agent call.

    Mirrors ``dispatch_tool_call``'s error contract: a failure returns a typed
    ``{"error": …, "message": …}`` dict (no raw exception text) so the agent
    loop continues.
    """
    env = env if env is not None else os.environ
    if name != TOOL_NAME:
        return {"error": "UnknownTool", "message": f"Unknown db tool: {name}"}
    try:
        return _fetch_sample_rows(arguments or {}, env)
    except Exception as exc:  # noqa: BLE001 — final safety net
        LOG.warning("db tool %s failed: %s", name, type(exc).__name__, exc_info=True)
        return {
            "error": type(exc).__name__,
            "message": f"db tool {name} failed — see server logs",
        }
