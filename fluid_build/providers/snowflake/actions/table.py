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

# fluid_build/providers/snowflake/actions/table.py
"""Snowflake table operations with schema evolution support."""

from __future__ import annotations

import time
from typing import Any, Dict, List

from ..connection import SnowflakeConnection
from ..util.config import get_connection_params
from ..util.names import (
    build_qualified_name,
    normalize_column_name,
    normalize_database_name,
    normalize_schema_name,
    normalize_table_name,
    quote_identifier,
)


def ensure_table(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """
    Ensure Snowflake table exists with proper schema.

    Supports:
    - Table creation with columns
    - Clustering keys
    - Comments
    - Schema evolution (column additions)
    - Snowflake tags from contract labels (NEW)
    """
    start_time = time.time()

    database = normalize_database_name(action["database"])
    schema = normalize_schema_name(action["schema"])
    table = normalize_table_name(action["table"])
    columns = action["columns"]
    account = action["account"]
    comment = action.get("comment")
    cluster_by = action.get("cluster_by", [])
    tags = action.get("tags", {})  # Table-level tags
    action.get("contract")  # Full contract for metadata extraction

    provider.debug_kv(event="ensure_table_started", database=database, schema=schema, table=table)

    try:
        params = get_connection_params(
            account=account,
            warehouse=provider.warehouse,
            database=database,
            schema=schema,
            **provider._kwargs,
        )

        with SnowflakeConnection(**params) as conn:
            qualified_name = build_qualified_name(database, schema, table)

            # Check if table exists
            check_sql = f"""
                SELECT COUNT(*) 
                FROM {quote_identifier(database)}.INFORMATION_SCHEMA.TABLES 
                WHERE TABLE_SCHEMA = '{schema.upper()}' 
                AND TABLE_NAME = '{table.upper()}'
            """
            result = conn.execute(check_sql)
            table_exists = result and result[0][0] > 0

            if not table_exists:
                # Create table
                create_sql = _generate_create_table_sql(
                    qualified_name, columns, cluster_by, comment
                )
                conn.execute(create_sql)

                # Apply table-level tags from contract
                if tags:
                    _apply_table_tags(conn, qualified_name, tags, provider)

                # Apply column-level tags from contract
                tags_applied = _apply_column_tags(conn, qualified_name, columns, provider)

                provider.info_kv(
                    event="table_created",
                    database=database,
                    schema=schema,
                    table=table,
                    columns=len(columns),
                    tags_applied=tags_applied,
                )

                return {
                    "status": "changed",
                    "op": action["op"],
                    "database": database,
                    "schema": schema,
                    "table": table,
                    "changed": True,
                    "action": "created",
                    "tags_applied": tags_applied,
                    "duration_ms": int((time.time() - start_time) * 1000),
                }

            else:
                # Table exists - check for schema evolution
                changes = _apply_schema_evolution(conn, database, schema, table, columns, provider)

                if changes:
                    provider.info_kv(
                        event="table_schema_updated",
                        database=database,
                        schema=schema,
                        table=table,
                        changes=changes,
                    )

                    return {
                        "status": "changed",
                        "op": action["op"],
                        "database": database,
                        "schema": schema,
                        "table": table,
                        "changed": True,
                        "action": "updated",
                        "changes": changes,
                        "duration_ms": int((time.time() - start_time) * 1000),
                    }
                else:
                    provider.debug_kv(
                        event="table_unchanged", database=database, schema=schema, table=table
                    )

                    return {
                        "status": "ok",
                        "op": action["op"],
                        "database": database,
                        "schema": schema,
                        "table": table,
                        "changed": False,
                        "duration_ms": int((time.time() - start_time) * 1000),
                    }

    except Exception as e:
        provider.err_kv(
            event="ensure_table_failed", database=database, schema=schema, table=table, error=str(e)
        )
        raise


def alter_table(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """Alter existing Snowflake table."""
    start_time = time.time()

    database = normalize_database_name(action["database"])
    schema = normalize_schema_name(action["schema"])
    table = normalize_table_name(action["table"])
    account = action["account"]
    alterations = action.get("alterations", [])

    provider.debug_kv(event="alter_table_started", database=database, schema=schema, table=table)

    try:
        params = get_connection_params(
            account=account,
            warehouse=provider.warehouse,
            database=database,
            schema=schema,
            **provider._kwargs,
        )

        with SnowflakeConnection(**params) as conn:
            qualified_name = build_qualified_name(database, schema, table)

            for alteration in alterations:
                alter_type = alteration.get("type")

                if alter_type == "add_column":
                    column_name = normalize_column_name(alteration["name"])
                    column_type = alteration["type"]
                    nullable = alteration.get("nullable", True)

                    alter_sql = f"ALTER TABLE {qualified_name} ADD COLUMN {quote_identifier(column_name)} {column_type}"
                    if not nullable:
                        alter_sql += " NOT NULL"

                    conn.execute(alter_sql)

                elif alter_type == "drop_column":
                    column_name = normalize_column_name(alteration["name"])
                    alter_sql = (
                        f"ALTER TABLE {qualified_name} DROP COLUMN {quote_identifier(column_name)}"
                    )
                    conn.execute(alter_sql)

                elif alter_type == "rename_column":
                    old_name = normalize_column_name(alteration["old_name"])
                    new_name = normalize_column_name(alteration["new_name"])
                    alter_sql = f"ALTER TABLE {qualified_name} RENAME COLUMN {quote_identifier(old_name)} TO {quote_identifier(new_name)}"
                    conn.execute(alter_sql)

            provider.info_kv(
                event="table_altered",
                database=database,
                schema=schema,
                table=table,
                alterations=len(alterations),
            )

            return {
                "status": "changed",
                "op": action["op"],
                "database": database,
                "schema": schema,
                "table": table,
                "changed": True,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    except Exception as e:
        provider.err_kv(
            event="alter_table_failed", database=database, schema=schema, table=table, error=str(e)
        )
        raise


def drop_table(action: Dict[str, Any], provider) -> Dict[str, Any]:
    """Drop Snowflake table if it exists."""
    start_time = time.time()

    database = normalize_database_name(action["database"])
    schema = normalize_schema_name(action["schema"])
    table = normalize_table_name(action["table"])
    account = action["account"]

    provider.debug_kv(event="drop_table_started", database=database, schema=schema, table=table)

    try:
        params = get_connection_params(
            account=account,
            warehouse=provider.warehouse,
            database=database,
            schema=schema,
            **provider._kwargs,
        )

        with SnowflakeConnection(**params) as conn:
            qualified_name = build_qualified_name(database, schema, table)
            drop_sql = f"DROP TABLE IF EXISTS {qualified_name}"
            conn.execute(drop_sql)

            provider.info_kv(event="table_dropped", database=database, schema=schema, table=table)

            return {
                "status": "changed",
                "op": action["op"],
                "database": database,
                "schema": schema,
                "table": table,
                "changed": True,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    except Exception as e:
        provider.err_kv(
            event="drop_table_failed", database=database, schema=schema, table=table, error=str(e)
        )
        raise


def _generate_create_table_sql(
    qualified_name: str, columns: List[Dict[str, Any]], cluster_by: List[str], comment: str = None
) -> str:
    """Generate CREATE TABLE SQL statement."""
    column_defs = []

    for col in columns:
        col_name = quote_identifier(normalize_column_name(col["name"]))
        col_type = col["type"]
        nullable = col.get("nullable", True)
        col_comment = col.get("comment")

        col_def = f"{col_name} {col_type}"
        if not nullable:
            col_def += " NOT NULL"
        if col_comment:
            escaped_comment = col_comment.replace("'", "''")
            col_def += f" COMMENT '{escaped_comment}'"

        column_defs.append(col_def)

    # Create column definitions string with newlines (can't use \n inside f-string expressions)
    columns_str = ",\n  ".join(column_defs)
    sql = f"CREATE TABLE {qualified_name} (\n  {columns_str}\n)"

    if cluster_by:
        quoted_keys = [quote_identifier(key) for key in cluster_by]
        sql += f"\nCLUSTER BY ({', '.join(quoted_keys)})"

    if comment:
        escaped_comment = comment.replace("'", "''")
        sql += f"\nCOMMENT = '{escaped_comment}'"

    return sql


def _apply_schema_evolution(
    conn: SnowflakeConnection,
    database: str,
    schema: str,
    table: str,
    desired_columns: List[Dict[str, Any]],
    provider,
) -> List[str]:
    """
    Apply schema evolution by adding missing columns.

    Returns list of changes made.
    """
    changes = []

    # Get existing columns
    columns_sql = f"""
        SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE
        FROM {quote_identifier(database)}.INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = '{schema.upper()}'
        AND TABLE_NAME = '{table.upper()}'
    """
    result = conn.execute(columns_sql)

    existing_columns = {
        row[0].upper(): {"type": row[1], "nullable": row[2] == "YES"} for row in result
    }

    # Check for missing columns
    qualified_name = build_qualified_name(database, schema, table)

    for col in desired_columns:
        col_name_upper = normalize_column_name(col["name"]).upper()

        if col_name_upper not in existing_columns:
            # Column doesn't exist - add it
            col_name = quote_identifier(col["name"])
            col_type = col["type"]
            nullable = col.get("nullable", True)

            alter_sql = f"ALTER TABLE {qualified_name} ADD COLUMN {col_name} {col_type}"
            if not nullable:
                alter_sql += " NOT NULL"

            conn.execute(alter_sql)
            changes.append(f"Added column {col_name}")

            provider.debug_kv(event="column_added", table=table, column=col_name, type=col_type)

    return changes


def _apply_table_tags(
    conn: SnowflakeConnection, qualified_name: str, tags: Dict[str, str], provider
) -> None:
    """
    Apply Snowflake tags to table from contract labels.

    Tags enable governance features:
    - Data classification
    - Masking policies
    - Row access policies
    - Compliance tracking
    """
    if not tags:
        return

    for tag_name, tag_value in tags.items():
        try:
            # Sanitize tag name (Snowflake allows alphanumeric + underscore)
            safe_tag_name = quote_identifier(tag_name.upper().replace("-", "_"))
            safe_tag_value = str(tag_value).replace("'", "''")

            # Ensure tag exists (idempotent)
            create_tag_sql = f"CREATE TAG IF NOT EXISTS {safe_tag_name}"
            conn.execute(create_tag_sql)

            # Apply tag to table
            set_tag_sql = (
                f"ALTER TABLE {qualified_name} SET TAG {safe_tag_name} = '{safe_tag_value}'"
            )
            conn.execute(set_tag_sql)

            provider.debug_kv(
                event="table_tag_applied",
                table=qualified_name,
                tag=safe_tag_name,
                value=safe_tag_value,
            )

        except Exception as e:
            provider.warn_kv(
                event="table_tag_failed", table=qualified_name, tag=tag_name, error=str(e)
            )


def _apply_column_tags(
    conn: SnowflakeConnection, qualified_name: str, columns: List[Dict[str, Any]], provider
) -> int:
    """
    Apply Snowflake tags to columns from contract labels.

    Extracts tags from column labels:
    - snowflakeTag: Direct tag name
    - tagValue: Tag value (defaults to 'true')
    - policyTag: GCP-style tag (mapped to Snowflake)
    - classification: Data classification level

    Returns count of tags applied.
    """
    tags_applied = 0

    for col in columns:
        labels = col.get("labels", {})
        col_name = quote_identifier(normalize_column_name(col["name"]))

        # Extract tag mappings
        tag_mappings = {}

        # Direct Snowflake tag
        if "snowflakeTag" in labels:
            tag_name = labels["snowflakeTag"]
            tag_value = labels.get("tagValue", "true")
            tag_mappings[tag_name] = tag_value

        # GCP-style policy tag (map to Snowflake tag)
        if "policyTag" in labels:
            policy_tag = labels["policyTag"]
            taxonomy = labels.get("taxonomy", "default")
            tag_mappings[f"{taxonomy}_{policy_tag}".upper()] = "true"

        # Data classification
        if "classification" in labels:
            tag_mappings["DATA_CLASSIFICATION"] = labels["classification"]

        # Sensitivity level
        if "sensitivity" in labels:
            tag_mappings["SENSITIVITY"] = labels["sensitivity"]

        # PII indicator
        if labels.get("pii") == "true" or labels.get("contains_pii") == "true":
            tag_mappings["PII"] = "true"

        # Apply all extracted tags
        for tag_name, tag_value in tag_mappings.items():
            try:
                safe_tag_name = quote_identifier(tag_name.upper().replace("-", "_"))
                safe_tag_value = str(tag_value).replace("'", "''")

                # Ensure tag exists
                create_tag_sql = f"CREATE TAG IF NOT EXISTS {safe_tag_name}"
                conn.execute(create_tag_sql)

                # Apply tag to column
                alter_sql = f"ALTER TABLE {qualified_name} MODIFY COLUMN {col_name} SET TAG {safe_tag_name} = '{safe_tag_value}'"
                conn.execute(alter_sql)

                tags_applied += 1

                provider.debug_kv(
                    event="column_tag_applied",
                    table=qualified_name,
                    column=col_name,
                    tag=safe_tag_name,
                    value=safe_tag_value,
                )

            except Exception as e:
                provider.warn_kv(
                    event="column_tag_failed",
                    table=qualified_name,
                    column=col_name,
                    tag=tag_name,
                    error=str(e),
                )

    return tags_applied
