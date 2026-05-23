# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Quality mapper.

Three levels of quality survive round-trip:

1. **Property-level** — emitted under ``schemaObject.properties[i].quality[]``.
   Generated from FLUID ``field.required``, primary-key tags, ``field.validations``,
   and any user-provided ``field.quality``. When the FLUID field was itself
   imported from ODCS, ``field.quality`` carries the verbatim list and is
   replayed as-is — no auto-checks added — so the round-trip stays lossless.

2. **Object-level** — ``schemaObject.quality[]``. Pure pass-through via
   ``expose.odcs_passthrough.object_quality`` (handled in :mod:`.schema`).

3. **Contract-level** — top-level ``odcs["quality"]`` (non-spec extension
   emitted by FLUID). Read into ``fluid.quality`` / pass-through and replayed
   verbatim on export.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, List, Optional

from .base import (
    ExportCtx,
    ImportCtx,
    get_metadata_passthrough,
    metadata_passthrough,
)


# ----- ODCS → FLUID --------------------------------------------------------


def to_fluid(ctx: ImportCtx) -> None:
    """Contract-level quality only — property/object level are handled by schema."""
    contract_quality = ctx.odcs.get("quality")
    if contract_quality is None:
        return
    if isinstance(contract_quality, Mapping):
        ctx.fluid["quality"] = dict(contract_quality)
    else:
        metadata_passthrough(ctx.fluid)["contract_quality_raw"] = contract_quality


# ----- FLUID → ODCS (contract level) --------------------------------------


def to_odcs(ctx: ExportCtx) -> None:
    if not ctx.options.get("include_quality_checks", True):
        return

    pt = get_metadata_passthrough(ctx.fluid)
    if "contract_quality_raw" in pt:
        ctx.odcs["quality"] = pt["contract_quality_raw"]
        return

    spec = ctx.fluid.get("quality")
    if not spec:
        return
    if isinstance(spec, Mapping):
        ctx.odcs["quality"] = {
            "type": spec.get("type", "custom"),
            "specification": spec.get("specification", ""),
        }


# ----- FLUID → ODCS (property level, called from schema mapper) -----------


def to_odcs_property(fld: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Return the ``quality[]`` array for an ODCS SchemaProperty.

    If ``field.quality`` is set (a list), it is replayed **verbatim** — this is
    the round-trip pass-through path. Otherwise auto-checks are synthesised
    from ``required``, primary-key tags, and ``field.validations``.
    """
    # Pass-through wins
    custom_quality = fld.get("quality")
    if isinstance(custom_quality, list):
        return list(custom_quality)

    quality_checks: List[Dict[str, Any]] = []
    name = fld.get("name", "unknown")
    tags = fld.get("tags") or []
    is_primary_key = "primary-key" in tags or "primaryKey" in tags

    if fld.get("required"):
        quality_checks.append(
            {
                "type": "library",
                "metric": "nullValues",
                "mustBe": 0,
                "dimension": "completeness",
                "description": f"Field '{name}' must not contain null values",
            }
        )

    if is_primary_key:
        quality_checks.append(
            {
                "type": "library",
                "metric": "duplicateValues",
                "mustBe": 0,
                "dimension": "uniqueness",
                "description": f"Primary key field '{name}' must contain only unique values",
            }
        )
        if not fld.get("required"):
            quality_checks.append(
                {
                    "type": "library",
                    "metric": "nullValues",
                    "mustBe": 0,
                    "dimension": "completeness",
                    "description": (
                        f"Primary key field '{name}' must not contain null values"
                    ),
                }
            )

    validations = fld.get("validations") or []
    if isinstance(validations, Mapping):
        validations = [{"type": k, "value": v} for k, v in validations.items()]
    if not isinstance(validations, list):
        validations = []

    for validation in validations:
        if not isinstance(validation, Mapping):
            continue
        check = _validation_to_quality(validation, name, is_primary_key, fld)
        if check is not None:
            quality_checks.append(check)

    return quality_checks or None


def _validation_to_quality(
    validation: Mapping[str, Any],
    name: str,
    is_primary_key: bool,
    fld: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    val_type = validation.get("type", "")
    val_value = validation.get("value")
    val_values = validation.get("values")

    if val_type in ("pattern", "regex") and val_value:
        return {
            "type": "text",
            "description": f"Field '{name}' must match pattern: {val_value}",
        }
    if val_type == "min_length" and val_value is not None:
        return {
            "type": "text",
            "description": f"Field '{name}' must have minimum length of {val_value}",
        }
    if val_type == "max_length" and val_value is not None:
        return {
            "type": "text",
            "description": f"Field '{name}' must have maximum length of {val_value}",
        }
    if val_type == "min_value" and val_value is not None:
        return {
            "type": "text",
            "description": (
                f"Field '{name}' must be greater than or equal to {val_value}"
            ),
        }
    if val_type == "max_value" and val_value is not None:
        return {
            "type": "text",
            "description": (
                f"Field '{name}' must be less than or equal to {val_value}"
            ),
        }
    if val_type in ("allowed_values", "enum") and val_values:
        values_str = ", ".join(str(v) for v in val_values[:5])
        if len(val_values) > 5:
            values_str += f", ... ({len(val_values)} total)"
        return {
            "type": "text",
            "description": f"Field '{name}' must be one of: {values_str}",
        }
    if val_type == "not_null" and val_value and not fld.get("required"):
        return {
            "type": "library",
            "metric": "nullValues",
            "mustBe": 0,
            "dimension": "completeness",
            "description": f"Field '{name}' must not contain null values",
        }
    if val_type == "unique" and val_value and not is_primary_key:
        return {
            "type": "library",
            "metric": "duplicateValues",
            "mustBe": 0,
            "dimension": "uniqueness",
            "description": f"Field '{name}' must contain only unique values",
        }
    return None
