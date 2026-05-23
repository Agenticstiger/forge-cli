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
