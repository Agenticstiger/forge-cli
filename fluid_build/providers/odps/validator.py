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

Validates OPDS (Open Data Product Specification) artifacts against the
official OPDS v4.1 JSON Schema.

The schema is VENDORED alongside this module (``odps-schema-v4.1.json``),
copied verbatim from the upstream specification repository
github.com/Open-Data-Product-Initiative/v4.1. Vendoring — rather than
fetching the schema over the network at validation time — keeps validation
deterministic, offline-capable, and pinned to a known schema version. This
mirrors how the ODCS provider bundles ``odcs-schema-v3.1.0.json``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger(__name__)

# In-memory parsed-schema cache, keyed by OPDS version.
_SCHEMA_CACHE: Dict[str, Dict[str, Any]] = {}


def _load_bundled_schema(version: str) -> Optional[Dict[str, Any]]:
    """Load the vendored OPDS JSON Schema for ``version`` from disk.

    Returns ``None`` when no schema is bundled for that version — the caller
    then falls back to basic structural validation.
    """
    if version in _SCHEMA_CACHE:
        return _SCHEMA_CACHE[version]
    schema_path = Path(__file__).parent / f"odps-schema-v{version}.json"
    if not schema_path.is_file():
        LOG.debug("opds_schema_not_bundled", extra={"version": version})
        return None
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:  # pragma: no cover - defensive
        LOG.error("opds_schema_load_failed", extra={"version": version, "error": str(e)})
        return None
    _SCHEMA_CACHE[version] = schema
    return schema


def validate_against_opds_schema(
    opds_data: Dict[str, Any], version: str = "4.1"
) -> Tuple[bool, Optional[List[str]], str]:
    """
    Validate OPDS data against the vendored official JSON Schema.

    Args:
        opds_data: OPDS data dictionary to validate
        version: OPDS version (selects the bundled ``odps-schema-v{version}.json``)

    Returns:
        ``(is_valid, errors, validation_type)`` — ``validation_type`` is
        ``"full_schema"`` when the bundled JSON Schema was applied, or
        ``"basic"`` when it fell back to structural validation (jsonschema
        not installed, or no schema bundled for ``version``).
    """
    try:
        import jsonschema
    except ImportError:
        LOG.warning(
            "jsonschema not installed - falling back to basic OPDS validation. "
            "Install with: pip install jsonschema"
        )
        valid, errors = _basic_validation(opds_data, version)
        return valid, errors, "basic"

    schema = _load_bundled_schema(version)
    if schema is None:
        valid, errors = _basic_validation(opds_data, version)
        return valid, errors, "basic"

    try:
        validator = jsonschema.Draft202012Validator(schema)
        errors = list(validator.iter_errors(opds_data))

        # Filter a known upstream OPDS v4.1 schema false-positive: the
        # official schema declares ``product.dataAccess`` as both an inline
        # ``"type": "object"`` and a ``"$ref"`` to a ``"type": "array"``
        # ``$def`` — mutually exclusive constraints. Our array
        # representation is the correct one.
        errors = [
            e
            for e in errors
            if not (list(e.path) == ["product", "dataAccess"] and "is not of type" in e.message)
        ]

        if errors:
            error_messages: List[str] = []
            for error in errors[:10]:  # Limit to first 10 errors
                path = ".".join(str(p) for p in error.path) if error.path else "root"
                error_messages.append(f"{path}: {error.message}")
            if len(errors) > 10:
                error_messages.append(f"... and {len(errors) - 10} more errors")
            LOG.debug(
                "opds_validation_failed",
                extra={"version": version, "error_count": len(errors)},
            )
            return False, error_messages, "full_schema"

        LOG.debug("opds_validation_success", extra={"version": version})
        return True, None, "full_schema"

    except jsonschema.SchemaError as e:
        LOG.error("opds_schema_invalid", extra={"error": str(e)})
        return False, [f"Schema validation error: {e}"], "full_schema"
    except Exception as e:  # pragma: no cover - defensive
        LOG.error("opds_validation_error", extra={"error": str(e)})
        valid, errors_basic = _basic_validation(opds_data, version)
        return valid, errors_basic, "basic"


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
) -> Dict[str, Any]:
    """
    Validate OPDS data structure and return detailed results.

    Full JSON-Schema validation runs against the vendored official OPDS
    schema (``odps-schema-v{version}.json``) when ``use_full_schema`` is set
    and a schema is bundled for ``version``; otherwise basic structural
    validation runs.

    Args:
        opds_data: OPDS data dictionary to validate
        version: OPDS version (default: "4.1")
        use_full_schema: Whether to attempt full JSON schema validation

    Returns:
        Dictionary with validation results:
        {
            "valid": bool,
            "errors": List[str] or None,
            "validation_type": "full_schema" | "basic",
            "version": str
        }
    """
    if use_full_schema:
        valid, errors, validation_type = validate_against_opds_schema(opds_data, version)
        return {
            "valid": valid,
            "errors": errors,
            "validation_type": validation_type,
            "version": version,
        }

    valid, errors = _basic_validation(opds_data, version)
    return {"valid": valid, "errors": errors, "validation_type": "basic", "version": version}


def clear_schema_cache() -> None:
    """Clear the in-memory parsed-schema cache (useful for tests)."""
    _SCHEMA_CACHE.clear()
    LOG.info("opds_schema_cache_cleared")
