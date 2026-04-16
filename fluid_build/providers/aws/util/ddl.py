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

"""
DDL generation utilities for AWS provider.

Converts FLUID contract schemas to provider-specific DDL statements
for Athena, Glue, and Redshift.
"""

from typing import Any, Dict, List, Optional


def generate_athena_ddl(
    database: str,
    table: str,
    columns: List[Dict[str, str]],
    location: str,
    file_format: str = "parquet",
    partition_columns: Optional[List[Dict[str, str]]] = None,
    table_properties: Optional[Dict[str, str]] = None,
) -> str:
    """
    Generate CREATE EXTERNAL TABLE DDL for Athena.

    Args:
        database: Database name
        table: Table name
        columns: List of column definitions (Name, Type, Comment)
        location: S3 location
        file_format: File format (parquet, orc, avro, csv, json)
        partition_columns: Partition column definitions
        table_properties: Additional table properties

    Returns:
        Complete DDL statement
    """
    # Build column definitions
    col_defs = []
    for col in columns:
        col_def = f"  `{col['Name']}` {col['Type']}"
        if col.get("Comment"):
            col_def += f" COMMENT '{_escape_sql(col['Comment'])}'"
        col_defs.append(col_def)

    ddl = f"CREATE EXTERNAL TABLE IF NOT EXISTS `{database}`.`{table}` (\n"
    ddl += ",\n".join(col_defs)
    ddl += "\n)"

    # Add partitions
    if partition_columns:
        part_defs = []
        for col in partition_columns:
            part_def = f"  `{col['Name']}` {col['Type']}"
            part_defs.append(part_def)
        ddl += "\nPARTITIONED BY (\n"
        ddl += ",\n".join(part_defs)
        ddl += "\n)"

    # Add storage format
    ddl += f"\nSTORED AS {_get_stored_as_format(file_format)}"

    # Add location
    ddl += f"\nLOCATION '{location}'"

    # Add table properties
    if table_properties:
        props = [f"  '{k}'='{v}'" for k, v in table_properties.items()]
        ddl += "\nTBLPROPERTIES (\n"
        ddl += ",\n".join(props)
        ddl += "\n)"

    ddl += ";"

    return ddl


def generate_redshift_ddl(
    schema: str,
    table: str,
    columns: List[Dict[str, str]],
    distribution_style: str = "AUTO",
    sort_key: Optional[List[str]] = None,
    table_properties: Optional[Dict[str, str]] = None,
) -> str:
    """
    Generate CREATE TABLE DDL for Redshift.

    Args:
        schema: Schema name
        table: Table name
        columns: List of column definitions
        distribution_style: Distribution style (AUTO, EVEN, KEY, ALL)
        sort_key: Columns for sort key
        table_properties: Additional table properties

    Returns:
        Complete DDL statement
    """
    # Build column definitions
    col_defs = []
    for col in columns:
        col_def = f"  {col['Name']} {col['Type']}"

        # Add constraints
        if col.get("NotNull"):
            col_def += " NOT NULL"

        if col.get("Encode"):
            col_def += f" ENCODE {col['Encode']}"

        col_defs.append(col_def)

    ddl = f"CREATE TABLE IF NOT EXISTS {schema}.{table} (\n"
    ddl += ",\n".join(col_defs)
    ddl += "\n)"

    # Add distribution style
    if distribution_style != "AUTO":
        ddl += f"\nDISTSTYLE {distribution_style}"

    # Add sort key
    if sort_key:
        ddl += f"\nSORTKEY ({', '.join(sort_key)})"

    ddl += ";"

    return ddl


def map_fluid_type_to_athena(fluid_type: str) -> str:
    """
    Map FLUID schema types to Athena/Hive types.

    Args:
        fluid_type: FLUID type string

    Returns:
        Athena type string
    """
    # Handle parameterized types
    fluid_type_lower = fluid_type.lower()

    # Decimal types - pass through
    if fluid_type_lower.startswith("decimal"):
        return fluid_type_lower

    # Varchar/char types - pass through
    if fluid_type_lower.startswith(("varchar", "char")):
        return fluid_type_lower

    # Array types
    if fluid_type_lower.startswith("array"):
        return fluid_type_lower

    # Map types
    if fluid_type_lower.startswith("map"):
        return fluid_type_lower

    # Struct types
    if fluid_type_lower.startswith("struct"):
        return fluid_type_lower

    # Simple type mapping
    type_map = {
        "string": "string",
        "str": "string",
        "text": "string",
        "integer": "bigint",
        "int": "bigint",
        "int32": "int",
        "int64": "bigint",
        "long": "bigint",
        "float": "double",
        "float32": "float",
        "float64": "double",
        "double": "double",
        "boolean": "boolean",
        "bool": "boolean",
        "timestamp": "timestamp",
        "datetime": "timestamp",
        "date": "date",
        "binary": "binary",
        "bytes": "binary",
    }

    return type_map.get(fluid_type_lower, "string")


def map_fluid_type_to_redshift(fluid_type: str) -> str:
    """
    Map FLUID schema types to Redshift types.

    Args:
        fluid_type: FLUID type string

    Returns:
        Redshift type string
    """
    fluid_type_lower = fluid_type.lower()

    # Decimal types - pass through
    if fluid_type_lower.startswith("decimal"):
        return fluid_type_lower.upper()

    # Varchar types - pass through
    if fluid_type_lower.startswith("varchar"):
        return fluid_type_lower.upper()

    # Simple type mapping
    type_map = {
        "string": "VARCHAR(65535)",
        "str": "VARCHAR(65535)",
        "text": "VARCHAR(65535)",
        "integer": "BIGINT",
        "int": "BIGINT",
        "int32": "INTEGER",
        "int64": "BIGINT",
        "long": "BIGINT",
        "float": "DOUBLE PRECISION",
        "float32": "REAL",
        "float64": "DOUBLE PRECISION",
        "double": "DOUBLE PRECISION",
        "boolean": "BOOLEAN",
        "bool": "BOOLEAN",
        "timestamp": "TIMESTAMP",
        "datetime": "TIMESTAMP",
        "date": "DATE",
        "binary": "VARBYTE(65535)",
        "bytes": "VARBYTE(65535)",
    }

    return type_map.get(fluid_type_lower, "VARCHAR(65535)")


def schema_to_glue_columns(schema: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """
    Convert FLUID contract schema to Glue column definitions.

    Args:
        schema: FLUID schema list

    Returns:
        List of Glue column dicts (Name, Type, Comment)
    """
    columns = []

    for field in schema:
        col = {
            "Name": field["name"],
            "Type": map_fluid_type_to_athena(field["type"]),
        }

        if field.get("description"):
            col["Comment"] = field["description"]

        columns.append(col)

    return columns


def schema_to_redshift_columns(schema: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """
    Convert FLUID contract schema to Redshift column definitions.

    Args:
        schema: FLUID schema list

    Returns:
        List of Redshift column dicts
    """
    columns = []

    for field in schema:
        col = {
            "Name": field["name"],
            "Type": map_fluid_type_to_redshift(field["type"]),
        }

        if field.get("required"):
            col["NotNull"] = True

        # Default encoding based on type
        redshift_type = col["Type"].upper()
        if "VARCHAR" in redshift_type or "CHAR" in redshift_type:
            col["Encode"] = "LZO"
        elif redshift_type in ("BIGINT", "INTEGER"):
            col["Encode"] = "AZ64"
        elif redshift_type == "TIMESTAMP":
            col["Encode"] = "AZ64"

        columns.append(col)

    return columns


def _get_stored_as_format(file_format: str) -> str:
    """Get STORED AS clause for Athena DDL."""
    format_map = {
        "parquet": "PARQUET",
        "orc": "ORC",
        "avro": "AVRO",
        "csv": "TEXTFILE",
        "json": "TEXTFILE",
    }
    return format_map.get(file_format.lower(), "PARQUET")


def _escape_sql(text: str) -> str:
    """Escape SQL string literals."""
    return text.replace("'", "''")


def extract_partition_columns(
    schema: List[Dict[str, Any]], partition_keys: Optional[List[str]] = None
) -> tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """
    Split schema into data columns and partition columns.

    Args:
        schema: Full FLUID schema
        partition_keys: List of column names to use as partitions

    Returns:
        Tuple of (data_columns, partition_columns)
    """
    if not partition_keys:
        return schema_to_glue_columns(schema), []

    partition_set = set(partition_keys)
    data_columns = []
    partition_columns = []

    for field in schema:
        col = {
            "Name": field["name"],
            "Type": map_fluid_type_to_athena(field["type"]),
        }

        if field.get("description"):
            col["Comment"] = field["description"]

        if field["name"] in partition_set:
            partition_columns.append(col)
        else:
            data_columns.append(col)

    return data_columns, partition_columns


def generate_iceberg_athena_ddl(
    database: str,
    table: str,
    columns: List[Dict[str, str]],
    location: str,
    iceberg_config: Optional[Dict[str, Any]] = None,
    table_properties: Optional[Dict[str, str]] = None,
) -> str:
    """
    Generate CREATE TABLE DDL for an Iceberg table on Athena v3.

    Athena Engine v3 uses the ``CREATE TABLE ... TBLPROPERTIES ('table_type'='ICEBERG')``
    syntax, *not* ``CREATE EXTERNAL TABLE``.  The underlying file format is
    controlled via ``'write.format.default'``.

    Args:
        database: Glue database name
        table: Table name
        columns: List of column definitions (Name, Type)
        location: S3 location for table data
        iceberg_config: Iceberg configuration (writeVersion, fileFormat, partitionSpec, etc.)
        table_properties: Additional TBLPROPERTIES

    Returns:
        Complete DDL statement for Athena v3
    """
    iceberg_config = iceberg_config or {}
    table_properties = table_properties or {}

    # Build column definitions
    col_defs = []
    for col in columns:
        col_def = f"  `{col['Name']}` {col['Type']}"
        if col.get("Comment"):
            col_def += f" COMMENT '{_escape_sql(col['Comment'])}'"
        col_defs.append(col_def)

    ddl = f"CREATE TABLE IF NOT EXISTS `{database}`.`{table}` (\n"
    ddl += ",\n".join(col_defs)
    ddl += "\n)"

    # Iceberg hidden partitioning via PARTITIONED BY transforms
    partition_spec = iceberg_config.get("partitionSpec", [])
    if partition_spec:
        parts = []
        for spec in partition_spec:
            source = spec.get("sourceColumn", "")
            transform = spec.get("transform", "identity")
            if transform == "identity":
                parts.append(f"`{source}`")
            else:
                parts.append(f"{transform}(`{source}`)")
        ddl += "\nPARTITIONED BY (\n  " + ",\n  ".join(parts) + "\n)"

    # Build TBLPROPERTIES
    props: Dict[str, str] = {
        "table_type": "ICEBERG",
        "format": iceberg_config.get("fileFormat", "parquet"),
        "write.format.default": iceberg_config.get("fileFormat", "parquet"),
        "format-version": str(iceberg_config.get("writeVersion", 2)),
    }

    # Location
    if location:
        props["location"] = location

    # Merge any custom iceberg properties
    for key, value in iceberg_config.get("properties", {}).items():
        props[key] = str(value)

    # Merge explicit table_properties (lower priority so they don't clobber iceberg keys)
    for key, value in table_properties.items():
        props.setdefault(key, str(value))

    prop_lines = [f"  '{_escape_sql(k)}'='{_escape_sql(v)}'" for k, v in props.items()]
    ddl += "\nTBLPROPERTIES (\n"
    ddl += ",\n".join(prop_lines)
    ddl += "\n);"

    return ddl
