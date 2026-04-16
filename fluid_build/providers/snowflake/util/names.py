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

# fluid_build/providers/snowflake/util/names.py
"""Snowflake naming utilities and normalization."""

from __future__ import annotations

import re
from typing import Optional


def normalize_database_name(name: str) -> str:
    """
    Normalize database name to Snowflake conventions.

    Rules:
    - Convert to uppercase
    - Replace hyphens with underscores
    - Remove invalid characters
    - Must start with letter or underscore
    - Max 255 characters
    """
    if not name:
        raise ValueError("Database name cannot be empty")

    # Convert to uppercase
    normalized = name.upper()

    # Replace hyphens with underscores
    normalized = normalized.replace("-", "_")

    # Remove invalid characters (keep alphanumeric and underscores)
    normalized = re.sub(r"[^A-Z0-9_]", "", normalized)

    # Ensure starts with letter or underscore
    if normalized and not (normalized[0].isalpha() or normalized[0] == "_"):
        normalized = f"_{normalized}"

    # Truncate to 255 characters
    normalized = normalized[:255]

    if not normalized:
        raise ValueError(f"Invalid database name: {name}")

    return normalized


def normalize_schema_name(name: str) -> str:
    """
    Normalize schema name to Snowflake conventions.

    Uses same rules as database names.
    """
    return normalize_database_name(name)


def normalize_table_name(name: str) -> str:
    """
    Normalize table name to Snowflake conventions.

    Uses same rules as database names.
    """
    return normalize_database_name(name)


def normalize_column_name(name: str) -> str:
    """
    Normalize column name to Snowflake conventions.

    Uses same rules as database names.
    """
    return normalize_database_name(name)


def quote_identifier(name: str) -> str:
    """
    Quote Snowflake identifier if needed.

    Snowflake identifiers are case-insensitive unless quoted.
    Use double quotes for:
    - Reserved keywords
    - Mixed case names
    - Names with special characters
    - Names starting with underscore
    """
    # Check if already quoted
    if name.startswith('"') and name.endswith('"'):
        return name

    # Check if needs quoting
    needs_quoting = (
        not name.isupper()  # Mixed case
        or name[0] == "_"  # Starts with underscore
        or not re.match(r"^[A-Z_][A-Z0-9_]*$", name)  # Special characters
    )

    if needs_quoting:
        # Escape internal double quotes
        escaped = name.replace('"', '""')
        return f'"{escaped}"'

    return name


def build_qualified_name(
    database: Optional[str] = None, schema: Optional[str] = None, name: Optional[str] = None
) -> str:
    """
    Build fully qualified Snowflake object name.

    Examples:
        database.schema.table
        schema.table
        table
    """
    parts = []

    if database:
        parts.append(quote_identifier(database))
    if schema:
        parts.append(quote_identifier(schema))
    if name:
        parts.append(quote_identifier(name))

    return ".".join(parts)
