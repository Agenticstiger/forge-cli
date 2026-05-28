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

This package historically re-exported three module-scope helpers —
``map_type`` and ``create_table_ddl`` (plus a now-removed ``backtick``).
The legacy ``backtick`` quoted identifiers WITHOUT escaping embedded
double quotes, so it was an injection hazard; identifier quoting now
routes exclusively through :func:`fluid_build.providers.snowflake.util.names.quote_identifier`,
which doubles embedded quotes. The redundant sibling ``util.py``
reference file has been deleted.
"""

from __future__ import annotations

from .names import quote_identifier

# ---------------------------------------------------------------------------
# FLUID → Snowflake type mapping + DDL rendering
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


def create_table_ddl(table_spec) -> str:
    """Render a ``CREATE TABLE`` DDL from a TableSpec dataclass.

    Identifiers are quoted via :func:`names.quote_identifier`, which
    doubles embedded double quotes.
    """
    cols = []
    for c in table_spec.columns:
        coltype = map_type(c.type)
        nulls = "NULL" if c.nullable else "NOT NULL"
        cols.append(f"{quote_identifier(c.name)} {coltype} {nulls}")
    columns_sql = ",\n  ".join(cols)
    db, sch, name = (
        table_spec.ident.database,
        table_spec.ident.schema,
        table_spec.ident.name,
    )
    fq = (
        f"{quote_identifier(db)}.{quote_identifier(sch)}.{quote_identifier(name)}"
        if db and sch
        else quote_identifier(name)
    )
    return f"CREATE TABLE IF NOT EXISTS {fq} (\n  {columns_sql}\n)"


__all__ = ["map_type", "create_table_ddl"]
