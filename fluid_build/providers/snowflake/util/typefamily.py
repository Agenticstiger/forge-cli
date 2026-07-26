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

"""Snowflake type/identifier folding, shared by every surface that compares a
FLUID contract's declared schema against a live Snowflake table.

``fluid verify`` owned this originally. The OpenTofu apply engine needs the
identical comparison to report the column drift its emitter's
``lifecycle.ignore_changes = ["column"]`` deliberately suppresses, and a
hand-mirrored second copy of a type table is exactly the drift-generator the
project's codegen rule exists to prevent — so it lives here, once.
"""

from __future__ import annotations

#: Snowflake type aliases folded to a comparable family. Contracts declare
#: portable FLUID types (``STRING``/``INTEGER``/``TIMESTAMP``); Snowflake
#: reports its own canonical, precision-bearing names
#: (``VARCHAR(16777216)`` / ``NUMBER(38,0)`` / ``TIMESTAMP_NTZ(9)``).
#: Comparison happens at family granularity so a widened precision is not
#: reported as drift.
SNOWFLAKE_TYPE_FAMILIES = {
    "STRING": {"VARCHAR", "CHAR", "CHARACTER", "TEXT", "STRING"},
    "NUMBER": {
        "NUMBER",
        "DECIMAL",
        "NUMERIC",
        "INT",
        "INTEGER",
        "BIGINT",
        "SMALLINT",
        "TINYINT",
        "BYTEINT",
        "FLOAT",
        "FLOAT4",
        "FLOAT8",
        "DOUBLE",
        "DOUBLE PRECISION",
        "REAL",
    },
    "BOOLEAN": {"BOOLEAN", "BOOL"},
    "DATE": {"DATE"},
    "TIME": {"TIME"},
    "TIMESTAMP": {
        "TIMESTAMP",
        "TIMESTAMP_NTZ",
        "TIMESTAMP_LTZ",
        "TIMESTAMP_TZ",
        "DATETIME",
        "TIMESTAMP WITHOUT TIME ZONE",
        "TIMESTAMP WITH LOCAL TIME ZONE",
        "TIMESTAMP WITH TIME ZONE",
    },
    "BINARY": {"BINARY", "VARBINARY", "BYTES"},
    "VARIANT": {"VARIANT", "JSON", "JSONB", "OBJECT", "ARRAY"},
}


def normalize_snowflake_type(value: str) -> str:
    """Fold a declared or reported type to its comparable family name."""
    base = (value or "STRING").upper().split("(", 1)[0].strip()
    for family, aliases in SNOWFLAKE_TYPE_FAMILIES.items():
        if base in aliases:
            return family
    return base


def normalize_snowflake_field_name(value: str) -> str:
    """Snowflake folds unquoted identifiers to uppercase."""
    return (value or "").upper()
