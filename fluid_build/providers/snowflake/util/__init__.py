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

# fluid_build/providers/snowflake/util/__init__.py
"""Snowflake utility modules.

This package shadows the historical ``util.py`` module (Python resolves
``from .util import X`` to the package, not the sibling file) so the
three helpers that used to live at module scope — ``backtick``,
``map_type``, ``create_table_ddl`` — are re-exported here.

Historical context: these helpers were originally imported by the
legacy ``providers/snowflake/snowflake.py`` ``SnowflakeProvider`` class
(removed in the Phase 7-rest tech-debt cleanup — the class was dead
code after ``providers/snowflake/__init__.py`` aliased
``SnowflakeProviderEnhanced`` as the public ``SnowflakeProvider``).
The re-exports stay because third-party callers may rely on
``from fluid_build.providers.snowflake.util import backtick``, and
the helpers are self-contained + tiny — cheaper to keep than to audit
downstream users.

The sibling ``util.py`` file (same directory) is kept on disk as a
pure reference implementation that duplicates the contents of this
``__init__.py``; Python's import resolution ignores ``util.py`` in
favor of this package.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Identifier quoting + FLUID → Snowflake type mapping (ex-util.py)
# ---------------------------------------------------------------------------

_FLUID_TO_SF = {
    "STRING": "VARCHAR",
    "INT64": "NUMBER",
    "INTEGER": "NUMBER",
    "FLOAT64": "FLOAT",
    "NUMERIC": "NUMBER",
    "BOOL": "BOOLEAN",
    "BOOLEAN": "BOOLEAN",
    "TIMESTAMP": "TIMESTAMP_NTZ",
    "DATE": "DATE",
    "TIME": "TIME",
    "BYTES": "BINARY",
}


def map_type(fluid_type: str) -> str:
    """FLUID column type → Snowflake column type.

    Falls back to the input token when no mapping exists — callers
    handle provider-specific types (``VARIANT``, ``OBJECT``, ``ARRAY``)
    by passing them through literally.
    """
    t = fluid_type.upper().strip()
    return _FLUID_TO_SF.get(t, t)


def backtick(s: str) -> str:
    """Quote a Snowflake identifier.

    Snowflake prefers double quotes for identifiers (unlike MySQL's
    backticks). Name kept as ``backtick`` for historical compatibility
    with the callsites scattered through ``snowflake.py``.
    """
    return f'"{s}"'


def create_table_ddl(table_spec) -> str:
    """Render a ``CREATE TABLE`` DDL from a TableSpec dataclass.

    Used by the legacy-style imperative planner; the 11-stage pipeline's
    stage-7 apply routes through ``_ensure_table`` instead. Kept for
    back-compat with tests and any callers that still build DDL strings
    directly.
    """
    cols = []
    for c in table_spec.columns:
        coltype = map_type(c.type)
        nulls = "NULL" if c.nullable else "NOT NULL"
        cols.append(f"{backtick(c.name)} {coltype} {nulls}")
    columns_sql = ",\n  ".join(cols)
    db, sch, name = (
        table_spec.ident.database,
        table_spec.ident.schema,
        table_spec.ident.name,
    )
    fq = f"{backtick(db)}.{backtick(sch)}.{backtick(name)}" if db and sch else backtick(name)
    return f"CREATE TABLE IF NOT EXISTS {fq} (\n  {columns_sql}\n)"


__all__ = ["map_type", "backtick", "create_table_ddl"]
