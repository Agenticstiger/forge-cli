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
Table format registry and capabilities for AWS provider.

Supports multiple table formats:
- File formats: Parquet, ORC, Avro, CSV, JSON
- Table formats: Apache Iceberg

Table formats (like Iceberg) provide additional capabilities beyond
file formats, including ACID transactions, time travel, and schema evolution.
"""

from enum import Enum
from typing import Any, Dict, List


class TableFormat(Enum):
    """Supported table formats in AWS provider."""

    PARQUET = "parquet"
    ORC = "orc"
    AVRO = "avro"
    CSV = "csv"
    JSON = "json"
    ICEBERG = "iceberg"

    @classmethod
    def from_string(cls, format_str: str) -> "TableFormat":
        """Convert string to TableFormat enum."""
        format_lower = format_str.lower()
        for fmt in cls:
            if fmt.value == format_lower:
                return fmt
        # Default to Parquet
        return cls.PARQUET


class FormatCapabilities:
    """
    Capabilities of different table formats.

    Used to determine what operations are supported and
    what infrastructure is required.
    """

    CAPABILITIES = {
        TableFormat.PARQUET: {
            "acid": False,
            "time_travel": False,
            "schema_evolution": "limited",
            "hidden_partitioning": False,
            "requires_catalog": False,
            "supports_merge": False,
            "supports_delete": False,
            "requires_athena_v3": False,
            "compaction": "manual",
        },
        TableFormat.ORC: {
            "acid": False,
            "time_travel": False,
            "schema_evolution": "limited",
            "hidden_partitioning": False,
            "requires_catalog": False,
            "supports_merge": False,
            "supports_delete": False,
            "requires_athena_v3": False,
            "compaction": "manual",
        },
        TableFormat.AVRO: {
            "acid": False,
            "time_travel": False,
            "schema_evolution": "good",
            "hidden_partitioning": False,
            "requires_catalog": False,
            "supports_merge": False,
            "supports_delete": False,
            "requires_athena_v3": False,
            "compaction": "manual",
        },
        TableFormat.ICEBERG: {
            "acid": True,
            "time_travel": True,
            "schema_evolution": "full",
            "hidden_partitioning": True,
            "requires_catalog": True,  # Needs Glue Catalog
            "supports_merge": True,
            "supports_delete": True,
            "requires_athena_v3": True,  # Athena Engine v3
            "compaction": "automatic",
            "snapshot_isolation": True,
            "rollback": True,
            "incremental_reads": True,
        },
        TableFormat.CSV: {
            "acid": False,
            "time_travel": False,
            "schema_evolution": "none",
            "hidden_partitioning": False,
            "requires_catalog": False,
            "supports_merge": False,
            "supports_delete": False,
            "requires_athena_v3": False,
            "compaction": "manual",
        },
        TableFormat.JSON: {
            "acid": False,
            "time_travel": False,
            "schema_evolution": "flexible",
            "hidden_partitioning": False,
            "requires_catalog": False,
            "supports_merge": False,
            "supports_delete": False,
            "requires_athena_v3": False,
            "compaction": "manual",
        },
    }

    @classmethod
    def get_capabilities(cls, format_type: TableFormat) -> Dict[str, Any]:
        """Get all capabilities for a format."""
        return cls.CAPABILITIES.get(format_type, {})

    @classmethod
    def supports_acid(cls, format_type: TableFormat) -> bool:
        """Check if format supports ACID transactions."""
        return cls.CAPABILITIES.get(format_type, {}).get("acid", False)

    @classmethod
    def supports_time_travel(cls, format_type: TableFormat) -> bool:
        """Check if format supports time travel queries."""
        return cls.CAPABILITIES.get(format_type, {}).get("time_travel", False)

    @classmethod
    def requires_catalog(cls, format_type: TableFormat) -> bool:
        """Check if format requires Glue Catalog."""
        return cls.CAPABILITIES.get(format_type, {}).get("requires_catalog", False)

    @classmethod
    def requires_athena_v3(cls, format_type: TableFormat) -> bool:
        """Check if format requires Athena Engine v3."""
        return cls.CAPABILITIES.get(format_type, {}).get("requires_athena_v3", False)

    @classmethod
    def supports_merge(cls, format_type: TableFormat) -> bool:
        """Check if format supports MERGE operations."""
        return cls.CAPABILITIES.get(format_type, {}).get("supports_merge", False)


def is_iceberg_format(binding: Dict[str, Any]) -> bool:
    """
    Check if binding specifies Iceberg format.

    Args:
        binding: The binding section from contract

    Returns:
        True if format is Iceberg
    """
    format_type = binding.get("format", "").lower()
    return format_type == "iceberg"


def is_table_format(format_str: str) -> bool:
    """
    Check if format is a table format (vs file format).

    Table formats provide metadata and transactional capabilities
    on top of file formats.

    Args:
        format_str: Format string from contract

    Returns:
        True if it's a table format (currently only Iceberg)
    """
    return format_str.lower() == "iceberg"


def get_iceberg_config(binding: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract Iceberg-specific configuration from binding.

    Args:
        binding: The binding section from contract

    Returns:
        Iceberg configuration dict with defaults applied
    """
    if not is_iceberg_format(binding):
        return {}

    iceberg_config = binding.get("icebergConfig", {})

    # Apply defaults
    return {
        "writeVersion": iceberg_config.get("writeVersion", 2),
        "fileFormat": iceberg_config.get("fileFormat", "parquet"),
        "partitionSpec": iceberg_config.get("partitionSpec", []),
        "sortOrder": iceberg_config.get("sortOrder", []),
        "properties": iceberg_config.get("properties", {}),
    }


def get_underlying_file_format(binding: Dict[str, Any]) -> str:
    """
    Get the underlying file format for table formats.

    For Iceberg: Returns the fileFormat from icebergConfig
    For file formats: Returns the format itself

    Args:
        binding: The binding section from contract

    Returns:
        File format string (parquet, orc, avro, etc.)
    """
    format_type = binding.get("format", "parquet").lower()

    if format_type == "iceberg":
        iceberg_config = get_iceberg_config(binding)
        return iceberg_config.get("fileFormat", "parquet")

    return format_type


def validate_iceberg_config(iceberg_config: Dict[str, Any]) -> List[str]:
    """
    Validate Iceberg configuration.

    Args:
        iceberg_config: Iceberg configuration dict

    Returns:
        List of validation error messages (empty if valid)
    """
    errors = []

    write_version = iceberg_config.get("writeVersion", 2)
    if write_version not in [1, 2]:
        errors.append(f"Invalid writeVersion: {write_version} (must be 1 or 2)")

    file_format = iceberg_config.get("fileFormat", "parquet")
    if file_format not in ["parquet", "orc", "avro"]:
        errors.append(f"Invalid fileFormat: {file_format} (must be parquet, orc, or avro)")

    # Validate partition spec
    partition_spec = iceberg_config.get("partitionSpec", [])
    if not isinstance(partition_spec, list):
        errors.append("partitionSpec must be a list")
    else:
        for i, spec in enumerate(partition_spec):
            if not isinstance(spec, dict):
                errors.append(f"partitionSpec[{i}] must be a dict")
                continue

            if "sourceColumn" not in spec:
                errors.append(f"partitionSpec[{i}] missing 'sourceColumn'")

            if "transform" not in spec:
                errors.append(f"partitionSpec[{i}] missing 'transform'")
            else:
                valid_transforms = [
                    "identity",
                    "year",
                    "month",
                    "day",
                    "hour",
                    "bucket",
                    "truncate",
                ]
                if spec["transform"] not in valid_transforms:
                    errors.append(
                        f"partitionSpec[{i}] invalid transform: {spec['transform']} "
                        f"(must be one of: {', '.join(valid_transforms)})"
                    )

    return errors
