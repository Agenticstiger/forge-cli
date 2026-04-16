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

# fluid_build/providers/odps/validator.py
"""
OPDS JSON Schema Validator

Provides validation against the official OPDS v4.1 JSON Schema specification.
Uses jsonschema library for comprehensive validation when available.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.request import urlopen

LOG = logging.getLogger(__name__)

# Cache for downloaded schemas
_SCHEMA_CACHE: Dict[str, Dict[str, Any]] = {}


def validate_against_opds_schema(
    opds_data: Dict[str, Any], schema_url: str, version: str = "4.1"
) -> Tuple[bool, Optional[List[str]]]:
    """
    Validate OPDS data against the official JSON Schema.

    Args:
        opds_data: OPDS data dictionary to validate
        schema_url: URL to the OPDS JSON schema (raw GitHub URL)
        version: OPDS version for context

    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    try:
        import jsonschema
    except ImportError:
        LOG.warning(
            "jsonschema library not available - skipping full schema validation. "
            "Install with: pip install jsonschema"
        )
        return _basic_validation(opds_data, version)

    try:
        # Get or download schema
        schema = _get_schema(schema_url)

        # Validate against schema
        validator = jsonschema.Draft202012Validator(schema)
        errors = list(validator.iter_errors(opds_data))

        # Filter out known OPDS v4.1 schema false-positives.
        # The official schema declares product.dataAccess as both
        # "type": "object" (inline) and "$ref": "#/$defs/DataAccess"
        # which has "type": "array" — these are mutually exclusive
        # constraints that cannot both be satisfied.  This is a bug
        # in the upstream schema; our array representation is correct.
        errors = [
            e
            for e in errors
            if not (list(e.path) == ["product", "dataAccess"] and "is not of type" in e.message)
        ]

        if errors:
            error_messages = []
            for error in errors[:10]:  # Limit to first 10 errors
                path = ".".join(str(p) for p in error.path) if error.path else "root"
                error_messages.append(f"{path}: {error.message}")

            if len(errors) > 10:
                error_messages.append(f"... and {len(errors) - 10} more errors")

            LOG.debug(
                "opds_validation_failed",
                extra={
                    "version": version,
                    "error_count": len(errors),
                    "errors": error_messages[:5],
                },
            )
            return False, error_messages

        LOG.debug("opds_validation_success", extra={"version": version, "schema_url": schema_url})
        return True, None

    except jsonschema.SchemaError as e:
        LOG.error("opds_schema_invalid", extra={"error": str(e)})
        return False, [f"Schema validation error: {e}"]
    except Exception as e:
        LOG.error("opds_validation_error", extra={"error": str(e)})
        # Fall back to basic validation
        return _basic_validation(opds_data, version)


def _get_schema(schema_url: str) -> Dict[str, Any]:
    """
    Get OPDS schema from cache or download it.

    Args:
        schema_url: Raw GitHub URL to the schema JSON file

    Returns:
        Schema dictionary
    """
    if schema_url in _SCHEMA_CACHE:
        LOG.debug("opds_schema_cache_hit", extra={"url": schema_url})
        return _SCHEMA_CACHE[schema_url]

    try:
        LOG.debug("opds_schema_download", extra={"url": schema_url})
        with urlopen(schema_url, timeout=10) as response:
            schema_text = response.read().decode("utf-8")
            schema = json.loads(schema_text)
            _SCHEMA_CACHE[schema_url] = schema
            LOG.debug("opds_schema_downloaded", extra={"url": schema_url})
            return schema
    except URLError as e:
        LOG.error("opds_schema_download_failed", extra={"url": schema_url, "error": str(e)})
        raise RuntimeError(f"Failed to download OPDS schema from {schema_url}: {e}")
    except json.JSONDecodeError as e:
        LOG.error("opds_schema_parse_failed", extra={"url": schema_url, "error": str(e)})
        raise RuntimeError(f"Failed to parse OPDS schema JSON: {e}")


def _basic_validation(opds_data: Dict[str, Any], version: str) -> Tuple[bool, Optional[List[str]]]:
    """
    Perform basic structural validation without full JSON schema.

    Args:
        opds_data: OPDS data dictionary
        version: OPDS version

    Returns:
        Tuple of (is_valid, list_of_errors)
    """
    errors = []

    # Check for required top-level fields based on OPDS v4.1 schema
    if version == "4.1":
        # OPDS v4.1 uses nested structure: schema, version, product
        if "product" in opds_data:
            product = opds_data["product"]

            # Check for details section
            if "details" not in product:
                errors.append("Missing required field: product.details")
            else:
                details = product["details"]
                # Details should have at least one language code
                if not details or not any(isinstance(v, dict) for v in details.values()):
                    errors.append(
                        "product.details must contain at least one language-specific detail block"
                    )
                else:
                    # Check first language block for required fields
                    for lang_code, lang_details in details.items():
                        if isinstance(lang_details, dict):
                            required_in_details = [
                                "name",
                                "productID",
                                "visibility",
                                "status",
                                "type",
                            ]
                            for field in required_in_details:
                                if field not in lang_details:
                                    errors.append(
                                        f"Missing required field in product.details.{lang_code}: {field}"
                                    )
                            break  # Only check first language block
        else:
            # Fallback: Check legacy format fields
            required_fields = ["dataProductId", "dataProductName", "dataProductDescription"]
            for field in required_fields:
                if field not in opds_data:
                    errors.append(f"Missing required field: {field}")

        # Check recommended fields
        if "version" not in opds_data:
            LOG.warning("opds_missing_version", extra={"detail": "OPDS version field recommended"})

        if "schema" not in opds_data and "$schema" not in opds_data:
            LOG.warning("opds_missing_schema", extra={"detail": "Schema reference recommended"})

    if errors:
        return False, errors

    return True, None


def validate_opds_structure(
    opds_data: Dict[str, Any],
    version: str = "4.1",
    use_full_schema: bool = True,
    schema_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Validate OPDS data structure and return detailed results.

    Args:
        opds_data: OPDS data dictionary to validate
        version: OPDS version (default: "4.1")
        use_full_schema: Whether to use full JSON schema validation
        schema_url: Optional schema URL (uses default for version if not provided)

    Returns:
        Dictionary with validation results:
        {
            "valid": bool,
            "errors": List[str] or None,
            "validation_type": "full_schema" | "basic",
            "version": str
        }
    """
    if schema_url is None:
        # Use raw GitHub URL for version 4.1
        if version == "4.1":
            schema_url = "https://raw.githubusercontent.com/Open-Data-Product-Initiative/v4.1/main/source/schema/odps.json"
        else:
            use_full_schema = False  # No schema available for other versions

    if use_full_schema and schema_url:
        try:
            valid, errors = validate_against_opds_schema(opds_data, schema_url, version)
            return {
                "valid": valid,
                "errors": errors,
                "validation_type": "full_schema",
                "version": version,
                "schema_url": schema_url,
            }
        except Exception as e:
            LOG.warning("opds_full_validation_failed_fallback_to_basic", extra={"error": str(e)})
            # Fall through to basic validation

    # Basic validation
    valid, errors = _basic_validation(opds_data, version)
    return {"valid": valid, "errors": errors, "validation_type": "basic", "version": version}


def clear_schema_cache():
    """Clear the cached schemas (useful for testing or forcing re-download)."""
    global _SCHEMA_CACHE
    _SCHEMA_CACHE.clear()
    LOG.info("opds_schema_cache_cleared")
