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
Input Validation Framework for FLUID CLI

Provides comprehensive validation utilities for CLI arguments, file paths,
environment variables, and configuration values with clear error messages.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, List, Optional, Union
from urllib.parse import urlparse

from .errors import ValidationError

# ==========================================
# Path Validation
# ==========================================


def validate_file_exists(path: Union[str, Path], *, description: str = "File") -> Path:
    """
    Validate that a file exists and is readable.

    Args:
        path: Path to validate
        description: Description for error messages

    Returns:
        Absolute Path object

    Raises:
        ValidationError: If file doesn't exist or isn't readable
    """
    path_obj = Path(path)

    if not path_obj.exists():
        raise ValidationError(
            f"{description} not found: {path}",
            context={"path": str(path), "type": "file"},
            suggestions=[
                f"Check that the path is correct: {path}",
                "Use absolute path if relative path fails",
                "Verify the file hasn't been moved or deleted",
            ],
        )

    if not path_obj.is_file():
        raise ValidationError(
            f"{description} is not a file: {path}",
            context={"path": str(path), "type": "directory" if path_obj.is_dir() else "other"},
            suggestions=[
                f"Expected a file, but found a {'directory' if path_obj.is_dir() else 'special file'}",
                "Check the path and try again",
            ],
        )

    # Check readability
    if not os.access(path_obj, os.R_OK):
        raise ValidationError(
            f"{description} is not readable: {path}",
            context={"path": str(path), "readable": False},
            suggestions=[
                "Check file permissions",
                (
                    f"Run: chmod +r {path}"
                    if os.name != "nt"
                    else f"Run: icacls {path} /grant Users:R"
                ),
            ],
        )

    return path_obj.resolve()


def validate_directory_exists(path: Union[str, Path], *, description: str = "Directory") -> Path:
    """
    Validate that a directory exists and is accessible.

    Args:
        path: Path to validate
        description: Description for error messages

    Returns:
        Absolute Path object

    Raises:
        ValidationError: If directory doesn't exist or isn't accessible
    """
    path_obj = Path(path)

    if not path_obj.exists():
        raise ValidationError(
            f"{description} not found: {path}",
            context={"path": str(path), "type": "directory"},
            suggestions=[
                (
                    f"Create the directory: mkdir -p {path}"
                    if os.name != "nt"
                    else f"Create the directory: md {path}"
                ),
                "Check that the path is correct",
            ],
        )

    if not path_obj.is_dir():
        raise ValidationError(
            f"{description} is not a directory: {path}",
            context={"path": str(path), "type": "file" if path_obj.is_file() else "other"},
            suggestions=["Expected a directory, but found a file", "Check the path and try again"],
        )

    return path_obj.resolve()


def validate_writable_path(path: Union[str, Path], *, description: str = "Output path") -> Path:
    """
    Validate that a path is writable (parent directory exists and is writable).

    Args:
        path: Path to validate
        description: Description for error messages

    Returns:
        Absolute Path object

    Raises:
        ValidationError: If path isn't writable
    """
    path_obj = Path(path).resolve()
    parent = path_obj.parent

    # Check parent directory exists
    if not parent.exists():
        raise ValidationError(
            f"Parent directory doesn't exist for {description}: {parent}",
            context={"path": str(path), "parent": str(parent)},
            suggestions=[
                (
                    f"Create parent directory: mkdir -p {parent}"
                    if os.name != "nt"
                    else f"Create parent directory: md {parent}"
                ),
                "Use --workspace-dir to specify a different location",
            ],
        )

    # Check parent is writable
    if not os.access(parent, os.W_OK):
        raise ValidationError(
            f"Cannot write to {description}: {path}",
            context={"path": str(path), "parent": str(parent), "writable": False},
            suggestions=[
                "Check directory permissions",
                (
                    f"Run: chmod +w {parent}"
                    if os.name != "nt"
                    else f"Run: icacls {parent} /grant Users:W"
                ),
            ],
        )

    return path_obj


def validate_contract_path(path: Union[str, Path]) -> Path:
    """
    Validate a FLUID contract file path.

    Args:
        path: Path to contract file

    Returns:
        Absolute Path object

    Raises:
        ValidationError: If contract file is invalid
    """
    path_obj = validate_file_exists(path, description="Contract file")

    # Check extension
    valid_extensions = {".yaml", ".yml", ".json"}
    if path_obj.suffix.lower() not in valid_extensions:
        raise ValidationError(
            f"Invalid contract file extension: {path_obj.suffix}",
            context={
                "path": str(path),
                "extension": path_obj.suffix,
                "valid_extensions": list(valid_extensions),
            },
            suggestions=[
                "Contract files must end with .yaml, .yml, or .json",
                "Rename file to have a valid extension",
            ],
        )

    return path_obj


# ==========================================
# String Validation
# ==========================================


def validate_non_empty(value: str, *, field_name: str) -> str:
    """
    Validate that a string is not empty.

    Args:
        value: String to validate
        field_name: Name of the field for error messages

    Returns:
        The validated string

    Raises:
        ValidationError: If string is empty or only whitespace
    """
    if not value or not value.strip():
        raise ValidationError(
            f"{field_name} cannot be empty",
            context={"field": field_name, "value": value},
            suggestions=[
                f"Provide a non-empty value for {field_name}",
                "Check your command arguments",
            ],
        )

    return value.strip()


def validate_identifier(value: str, *, field_name: str, allow_hyphens: bool = True) -> str:
    """
    Validate that a string is a valid identifier (alphanumeric + underscores/hyphens).

    Args:
        value: String to validate
        field_name: Name of the field for error messages
        allow_hyphens: Whether to allow hyphens in addition to underscores

    Returns:
        The validated string

    Raises:
        ValidationError: If string is not a valid identifier
    """
    value = validate_non_empty(value, field_name=field_name)

    # Check pattern
    pattern = r"^[a-zA-Z][a-zA-Z0-9_-]*$" if allow_hyphens else r"^[a-zA-Z][a-zA-Z0-9_]*$"
    if not re.match(pattern, value):
        raise ValidationError(
            f"Invalid {field_name}: {value}",
            context={
                "field": field_name,
                "value": value,
                "pattern": "alphanumeric + underscores" + (" + hyphens" if allow_hyphens else ""),
            },
            suggestions=[
                f"{field_name} must start with a letter",
                f"{field_name} can only contain letters, numbers, underscores"
                + (", and hyphens" if allow_hyphens else ""),
                "Example: my_data_product" + (" or my-data-product" if allow_hyphens else ""),
            ],
        )

    return value


def validate_enum(
    value: str, *, field_name: str, allowed_values: List[str], case_sensitive: bool = False
) -> str:
    """
    Validate that a string is one of the allowed values.

    Args:
        value: String to validate
        field_name: Name of the field for error messages
        allowed_values: List of allowed values
        case_sensitive: Whether comparison is case-sensitive

    Returns:
        The validated string (normalized to allowed value case)

    Raises:
        ValidationError: If string is not in allowed values
    """
    value = validate_non_empty(value, field_name=field_name)

    if case_sensitive:
        if value not in allowed_values:
            raise ValidationError(
                f"Invalid {field_name}: {value}",
                context={"field": field_name, "value": value, "allowed_values": allowed_values},
                suggestions=[
                    f"Allowed values: {', '.join(allowed_values)}",
                    "Check spelling and capitalization",
                ],
            )
        return value
    else:
        # Case-insensitive comparison
        value_lower = value.lower()
        for allowed in allowed_values:
            if allowed.lower() == value_lower:
                return allowed  # Return the canonical case

        raise ValidationError(
            f"Invalid {field_name}: {value}",
            context={"field": field_name, "value": value, "allowed_values": allowed_values},
            suggestions=[
                f"Allowed values: {', '.join(allowed_values)}",
                "Value is case-insensitive",
            ],
        )


def validate_url(value: str, *, field_name: str, require_https: bool = False) -> str:
    """
    Validate that a string is a valid URL.

    Args:
        value: String to validate
        field_name: Name of the field for error messages
        require_https: Whether to require HTTPS protocol

    Returns:
        The validated URL

    Raises:
        ValidationError: If string is not a valid URL
    """
    value = validate_non_empty(value, field_name=field_name)

    try:
        parsed = urlparse(value)

        if not parsed.scheme:
            raise ValidationError(
                f"Invalid {field_name}: missing protocol",
                context={"field": field_name, "value": value},
                suggestions=[
                    (
                        "Add protocol: https://example.com"
                        if require_https
                        else "Add protocol: http://example.com or https://example.com"
                    )
                ],
            )

        if require_https and parsed.scheme != "https":
            raise ValidationError(
                f"Invalid {field_name}: HTTPS required",
                context={"field": field_name, "value": value, "scheme": parsed.scheme},
                suggestions=[
                    f"Change to HTTPS: {value.replace(parsed.scheme + '://', 'https://', 1)}"
                ],
            )

        if parsed.scheme not in ("http", "https"):
            raise ValidationError(
                f"Invalid {field_name}: unsupported protocol",
                context={"field": field_name, "value": value, "scheme": parsed.scheme},
                suggestions=["Use HTTP or HTTPS protocol"],
            )

        if not parsed.netloc:
            raise ValidationError(
                f"Invalid {field_name}: missing host",
                context={"field": field_name, "value": value},
                suggestions=["Provide a valid hostname: https://example.com"],
            )

        return value

    except ValueError as e:
        raise ValidationError(
            f"Invalid {field_name}: {e}",
            context={"field": field_name, "value": value},
            suggestions=["Provide a valid URL: https://example.com"],
        )


# ==========================================
# Numeric Validation
# ==========================================


def validate_positive_int(value: int, *, field_name: str) -> int:
    """
    Validate that an integer is positive (> 0).

    Args:
        value: Integer to validate
        field_name: Name of the field for error messages

    Returns:
        The validated integer

    Raises:
        ValidationError: If integer is not positive
    """
    if value <= 0:
        raise ValidationError(
            f"{field_name} must be positive",
            context={"field": field_name, "value": value},
            suggestions=[
                "Provide a value greater than 0",
                f"Example: --{field_name.lower().replace(' ', '-')} 10",
            ],
        )

    return value


def validate_int_range(
    value: int, *, field_name: str, min_value: Optional[int] = None, max_value: Optional[int] = None
) -> int:
    """
    Validate that an integer is within a range.

    Args:
        value: Integer to validate
        field_name: Name of the field for error messages
        min_value: Minimum allowed value (inclusive)
        max_value: Maximum allowed value (inclusive)

    Returns:
        The validated integer

    Raises:
        ValidationError: If integer is out of range
    """
    if min_value is not None and value < min_value:
        raise ValidationError(
            f"{field_name} is too small: {value}",
            context={"field": field_name, "value": value, "min": min_value},
            suggestions=[
                f"Minimum value: {min_value}",
                f"Example: --{field_name.lower().replace(' ', '-')} {min_value}",
            ],
        )

    if max_value is not None and value > max_value:
        raise ValidationError(
            f"{field_name} is too large: {value}",
            context={"field": field_name, "value": value, "max": max_value},
            suggestions=[
                f"Maximum value: {max_value}",
                f"Example: --{field_name.lower().replace(' ', '-')} {max_value}",
            ],
        )

    return value


# ==========================================
# Environment Validation
# ==========================================


def validate_environment_name(env: str) -> str:
    """
    Validate an environment name (dev, test, staging, prod, etc.).

    Args:
        env: Environment name to validate

    Returns:
        The validated environment name

    Raises:
        ValidationError: If environment name is invalid
    """
    return validate_identifier(env, field_name="Environment name", allow_hyphens=True)


def validate_gcp_project_id(project_id: str) -> str:
    """
    Validate a GCP project ID.

    Args:
        project_id: GCP project ID to validate

    Returns:
        The validated project ID

    Raises:
        ValidationError: If project ID is invalid
    """
    project_id = validate_non_empty(project_id, field_name="GCP project ID")

    # GCP project IDs must:
    # - Be 6-30 characters
    # - Contain only lowercase letters, numbers, and hyphens
    # - Start with a letter
    if not re.match(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$", project_id):
        raise ValidationError(
            f"Invalid GCP project ID: {project_id}",
            context={"project_id": project_id},
            suggestions=[
                "GCP project IDs must be 6-30 characters",
                "Must start with a lowercase letter",
                "Can only contain lowercase letters, numbers, and hyphens",
                "Must end with a letter or number",
                "Example: my-data-project-123",
            ],
        )

    return project_id


def validate_gcp_region(region: str) -> str:
    """
    Validate a GCP region name.

    Args:
        region: GCP region to validate

    Returns:
        The validated region

    Raises:
        ValidationError: If region is invalid
    """
    region = validate_non_empty(region, field_name="GCP region")

    # Common GCP regions
    common_regions = [
        "us-central1",
        "us-east1",
        "us-west1",
        "us-west2",
        "europe-west1",
        "europe-west2",
        "europe-west4",
        "asia-east1",
        "asia-northeast1",
        "asia-southeast1",
    ]

    # Basic pattern: region-zone
    if not re.match(r"^[a-z]+-[a-z]+\d+$", region):
        raise ValidationError(
            f"Invalid GCP region format: {region}",
            context={"region": region, "common_regions": common_regions[:5]},
            suggestions=[
                "GCP regions follow pattern: continent-direction#",
                f"Common regions: {', '.join(common_regions[:5])}",
                "See: https://cloud.google.com/compute/docs/regions-zones",
            ],
        )

    return region


# ==========================================
# Custom Validation
# ==========================================


def validate_with_custom(
    value: Any, *, field_name: str, validator: Callable[[Any], bool], error_message: str
) -> Any:
    """
    Validate a value with a custom validator function.

    Args:
        value: Value to validate
        field_name: Name of the field for error messages
        validator: Function that returns True if valid, False otherwise
        error_message: Error message to show if validation fails

    Returns:
        The validated value

    Raises:
        ValidationError: If validation fails
    """
    try:
        if not validator(value):
            raise ValidationError(
                f"Invalid {field_name}: {error_message}",
                context={"field": field_name, "value": value},
            )
        return value
    except Exception as e:
        if isinstance(e, ValidationError):
            raise
        raise ValidationError(
            f"Validation error for {field_name}: {e}",
            context={"field": field_name, "value": value},
            original_error=e,
        )
