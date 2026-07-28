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

"""Helpers for capturing source schema fingerprints. Built on top of
``api.schema.SchemaFingerprint``; this module wires it into common shapes
(DuckDB ``DESCRIBE`` output, Singer ``RECORD`` schema, Airbyte catalog).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from fluid_build.api.schema import SchemaColumn, SchemaFingerprint

from ._acquisition_common import utc_now_iso


def fingerprint_from_columns(columns: Iterable[Dict[str, Any]]) -> SchemaFingerprint:
    """Build a fingerprint from a sequence of dicts with name/type/nullable."""
    cols = [
        SchemaColumn(
            name=str(c["name"]),
            type=str(c.get("type", "unknown")),
            nullable=bool(c.get("nullable", True)),
        )
        for c in columns
    ]
    return SchemaFingerprint.of(cols, captured_at=utc_now_iso())


def fingerprint_from_duckdb_describe(rows: Iterable[Any]) -> SchemaFingerprint:
    """DuckDB's ``DESCRIBE`` returns rows like (column_name, column_type, null, key, default, extra)."""
    cols: List[SchemaColumn] = []
    for row in rows:
        # row may be a sequence or a duckdb.Row; index 0/1/2 are name/type/null.
        try:
            name = row[0]
            type_ = row[1]
            nullable = (str(row[2]).upper() != "NO") if len(row) > 2 else True
        except Exception:
            continue
        cols.append(SchemaColumn(name=str(name), type=str(type_), nullable=bool(nullable)))
    return SchemaFingerprint.of(cols, captured_at=utc_now_iso())
