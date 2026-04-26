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

"""Dump Snowflake DDL for a database.schema into a single .sql file.

This closes the biggest friction point on the ``from-ddl`` happy path:
before this helper, users had to ``snowsql -q "SELECT GET_DDL('SCHEMA',
...)"`` by hand, wrangle the escaping, and trust that they'd pointed at
the right schema. With this helper the round-trip is:

    fluid forge data-model dump-ddl \\
      --database <DATABASE> --schema <SCHEMA> --output /tmp/snapshot.sql
    fluid forge data-model from-ddl --ddl /tmp/snapshot.sql ...

Connection resolution reuses :func:`get_connection_params` from the
existing Snowflake provider — same env vars, same keyring, same
key-pair support — so anything that works for ``fluid verify snowflake``
works here too.

The ``snowflake.connector`` package is a soft-import: when it isn't
installed, the helper raises a typed :class:`DDLGenerationError`
pointing to ``pip install "data-product-forge[snowflake]"``. The typed
exception keeps ``except DDLGenerationError`` callers — the documented
DDL-failure handler — from also having to catch a bare ``RuntimeError``.
This keeps ``forge_datamodel`` importable on stripped-down installs and
avoids pinning users who never touch Snowflake to the driver.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from fluid_build.copilot.agents.errors import DDLGenerationError

_logger = logging.getLogger(__name__)

_UNQUOTED_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


@dataclass(frozen=True)
class _SnowflakeIdentifier:
    """Validated Snowflake identifier plus its GET_DDL-safe quoted form."""

    label: str
    quoted: str


def _normalize_identifier(value: str, *, kind: str) -> _SnowflakeIdentifier:
    """Validate one Snowflake identifier segment and return a quoted form.

    ``GET_DDL`` accepts object names as string arguments, so interpolating raw
    CLI input into the SQL text is not acceptable. This helper keeps the object
    reference strict before the value is passed through connector parameter
    binding:

    * unquoted identifiers must match Snowflake's normal identifier shape and
      are uppercased to mirror Snowflake's default folding;
    * quoted identifiers are accepted only when embedded double quotes are
      escaped by doubling them.
    """

    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{kind} is required")

    if raw.startswith('"') or raw.endswith('"'):
        if not (raw.startswith('"') and raw.endswith('"') and len(raw) >= 2):
            raise ValueError(f"Invalid Snowflake {kind} identifier: {value!r}")
        inner = raw[1:-1]
        if not inner:
            raise ValueError(f"Invalid Snowflake {kind} identifier: empty quoted name")
        index = 0
        while index < len(inner):
            if inner[index] == '"':
                if index + 1 < len(inner) and inner[index + 1] == '"':
                    index += 2
                    continue
                raise ValueError(
                    f"Invalid Snowflake {kind} identifier: embedded double quotes "
                    'must be escaped as ""'
                )
            index += 1
        return _SnowflakeIdentifier(label=inner.replace('""', '"'), quoted=raw)

    if not _UNQUOTED_IDENTIFIER_RE.fullmatch(raw):
        raise ValueError(
            f"Invalid Snowflake {kind} identifier: {value!r}. "
            "Use an unquoted identifier like CUSTOMER_360 or a fully quoted "
            'identifier like "Customer 360".'
        )
    folded = raw.upper()
    return _SnowflakeIdentifier(label=folded, quoted=f'"{folded}"')


@dataclass
class DumpResult:
    """Structured result from :func:`dump_schema_ddl`."""

    database: str
    schema: str
    ddl: str
    table_count: int


def _import_connector():
    """Soft-import ``snowflake.connector``; raise a helpful error on miss."""
    try:
        import snowflake.connector  # type: ignore
    except ImportError as exc:  # pragma: no cover — exercised only on bare installs
        raise DDLGenerationError(
            "snowflake-connector-python is not installed.\n"
            'Install via: pip install "data-product-forge[snowflake]"\n'
            "Or, if you prefer, dump the DDL manually with:\n"
            "  snowsql -q \"SELECT GET_DDL('SCHEMA', '<DB>.<SCHEMA>', TRUE)\" "
            "-o output_format=plain -o header=false > /tmp/ddl.sql"
        ) from exc
    return snowflake.connector


def dump_schema_ddl(
    database: str,
    schema: str,
    *,
    tables: Optional[Sequence[str]] = None,
    connection_params: Optional[dict] = None,
    role: Optional[str] = None,
    warehouse: Optional[str] = None,
) -> DumpResult:
    """Dump DDL for ``database.schema`` (or a table subset) into one string.

    The SQL boils down to ``SELECT GET_DDL('SCHEMA', '<DB>.<SCHEMA>', TRUE)``
    which returns every CREATE TABLE / CREATE VIEW / CREATE FUNCTION in
    the schema in one payload. When ``tables`` is provided, we fall back
    to per-table ``GET_DDL('TABLE', '<DB>.<SCHEMA>.<TABLE>')`` calls and
    concatenate the results — useful when the schema has 500 tables and
    you only want a handful.

    Identifier quoting: ``GET_DDL`` in Snowflake is case-sensitive when
    the object names are quoted. We double-quote + uppercase all three
    components to mirror Snowflake's default unquoted-identifier folding
    behaviour. Users who created mixed-case objects can pass explicit
    quoted names.
    """
    db_id = _normalize_identifier(database, kind="database")
    schema_id = _normalize_identifier(schema, kind="schema")
    table_ids = [_normalize_identifier(table, kind="table") for table in tables] if tables else []

    connector = _import_connector()

    # Lazy import so missing snowflake package doesn't break module import.
    from fluid_build.providers.snowflake.util.config import get_connection_params

    params = dict(connection_params or {})
    if not params:
        resolved = get_connection_params(database=db_id.label, schema=schema_id.label)
        params = dict(resolved)
    if role and "role" not in params:
        params["role"] = role
    if warehouse and "warehouse" not in params:
        params["warehouse"] = warehouse
    # GET_DDL needs the database context to resolve schema-qualified names
    # reliably. Setting both is cheap and removes a class of "schema not
    # found" surprises.
    params["database"] = db_id.label
    params["schema"] = schema_id.label

    _logger.info("dumping DDL from %s.%s", db_id.label, schema_id.label)

    with connector.connect(**params) as conn:
        with conn.cursor() as cur:
            if table_ids:
                # Per-table dump; preserves user-specified order so the
                # output is deterministic for golden-file tests.
                ddl_parts: list[str] = []
                for table_id in table_ids:
                    cur.execute(
                        "SELECT GET_DDL(%s, %s, TRUE)",
                        ("TABLE", f"{db_id.quoted}.{schema_id.quoted}.{table_id.quoted}"),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        ddl_parts.append(str(row[0]).strip())
                ddl = "\n\n".join(ddl_parts)
                table_count = len(table_ids)
            else:
                # Whole-schema dump. GET_DDL returns one big string with
                # every CREATE statement separated by semicolons.
                cur.execute(
                    "SELECT GET_DDL(%s, %s, TRUE)",
                    ("SCHEMA", f"{db_id.quoted}.{schema_id.quoted}"),
                )
                row = cur.fetchone()
                ddl = str(row[0]).strip() if row and row[0] else ""
                # Approximate table count by counting CREATE TABLE
                # occurrences — exact count isn't critical, just a sanity
                # signal for the user.
                table_count = ddl.upper().count("CREATE OR REPLACE TABLE") + ddl.upper().count(
                    "CREATE TABLE"
                )

    if not ddl:
        raise DDLGenerationError(
            f"GET_DDL returned empty for {db_id.label}.{schema_id.label}. "
            "Verify: (1) the schema exists, (2) your role has USAGE + "
            "REFERENCES on it, (3) the warehouse is running."
        )

    return DumpResult(
        database=db_id.label, schema=schema_id.label, ddl=ddl, table_count=table_count
    )


def dump_schema_ddl_to_file(
    database: str,
    schema: str,
    output: Path,
    *,
    tables: Optional[Sequence[str]] = None,
    connection_params: Optional[dict] = None,
    role: Optional[str] = None,
    warehouse: Optional[str] = None,
) -> DumpResult:
    """Convenience wrapper: :func:`dump_schema_ddl` + write to ``output``.

    The file is written with a small header comment so ``from-ddl``
    users know where the payload came from. The payload itself is the
    raw Snowflake GET_DDL output and is parseable by ``sqlglot``.
    """
    result = dump_schema_ddl(
        database,
        schema,
        tables=tables,
        connection_params=connection_params,
        role=role,
        warehouse=warehouse,
    )

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    table_filter = f" (filter: {', '.join(tables)})" if tables else ""
    header = (
        f"-- Dumped by fluid forge data-model dump-ddl\n"
        f"-- Source: {result.database}.{result.schema}{table_filter}\n"
        f"-- Approximate table count: {result.table_count}\n"
        f"-- NOTE: This file is machine-generated and safe to regenerate.\n\n"
    )
    output.write_text(header + result.ddl + "\n", encoding="utf-8")
    return result


__all__ = ["DumpResult", "dump_schema_ddl", "dump_schema_ddl_to_file"]
