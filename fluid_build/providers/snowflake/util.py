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

# fluid_build/provider/snowflake/util.py
from __future__ import annotations

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
    t = fluid_type.upper().strip()
    return _FLUID_TO_SF.get(t, t)  # fallback to same token if unknown


def backtick(s: str) -> str:
    # Keep consistent quoting (Snowflake prefers double quotes for identifiers)
    return f'"{s}"'


def create_table_ddl(table_spec) -> str:
    cols = []
    for c in table_spec.columns:
        coltype = map_type(c.type)
        nulls = "NULL" if c.nullable else "NOT NULL"
        cols.append(f"{backtick(c.name)} {coltype} {nulls}")
    columns_sql = ",\n  ".join(cols)
    db, sch, name = table_spec.ident.database, table_spec.ident.schema, table_spec.ident.name
    return f"""CREATE TABLE IF NOT EXISTS {backtick(db)}.{backtick(sch)}.{backtick(name)} (
  {columns_sql}
);"""
