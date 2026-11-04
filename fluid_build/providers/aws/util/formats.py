# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Iceberg table-format helpers for the AWS provider."""

from typing import Any, Dict


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
