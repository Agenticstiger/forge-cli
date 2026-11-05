# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Bitol ODPS v1.0.0 JSON Schema validation + round-trip diff."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, Optional

from fluid_build.providers.base import ProviderError

# Reuse the deep-diff implementation from the odcs validation module — the
# two providers share the same notion of "structural equality".
from fluid_build.providers.odcs.validation import roundtrip_check  # noqa: F401

LOG = logging.getLogger(__name__)

_SCHEMA_DIR = Path(__file__).parent / "schemas"
_SCHEMA_PATH = _SCHEMA_DIR / "odps-product-v1.0.0.json"

#: apiVersion values with a vendored schema. Both are released standards:
#: v1.0.0 (2025-09-24) and v1.1.0 (2026-09-08, top-level ``type`` from
#: RFC 0029). v1.1.0 stays opt-in as an emit target so contracts pinned to a
#: 0.7.5 schema keep emitting the apiVersion their consumers already read.
SUPPORTED_API_VERSIONS = ("v1.0.0", "v1.1.0")
DEFAULT_API_VERSION = "v1.0.0"


def load_schema(api_version: str = DEFAULT_API_VERSION) -> Optional[Dict[str, Any]]:
    if api_version not in SUPPORTED_API_VERSIONS:
        LOG.warning(
            "Unsupported ODPS apiVersion %r (supported: %s), falling back to %s",
            api_version,
            ", ".join(SUPPORTED_API_VERSIONS),
            DEFAULT_API_VERSION,
        )
        api_version = DEFAULT_API_VERSION
    path = _SCHEMA_DIR / f"odps-product-{api_version}.json"
    if not path.exists():
        LOG.warning("ODPS schema not found: %s", path)
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # pragma: no cover - defensive
        LOG.error("Failed to load ODPS schema: %s", exc)
        return None


def schema_for_document(odps: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The vendored schema matching the document's OWN ``apiVersion``.

    Emission targets the provider's configured version, but an incoming
    document declares its version itself; validating a v1.1.0 document
    against the v1.0.0 schema would reject the RFC 0029 ``type`` field
    (v1.0.0 is ``additionalProperties: false``).
    """
    declared = str(odps.get("apiVersion") or DEFAULT_API_VERSION)
    return load_schema(declared)


def validate(odps: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    """Validate an ODPS product against ``schema``, the vendored one for its
    ``apiVersion`` (:func:`schema_for_document`)."""
    try:
        import jsonschema
    except ImportError:
        LOG.warning("jsonschema not installed, skipping ODPS validation")
        return
    from fluid_build.schema_manager import schema_validator

    try:
        # cls: the declared dialect, with a pattern's "$" read as the end of the
        # value, as ECMA-262 does. jsonschema.validate still checks the schema first.
        jsonschema.validate(instance=odps, schema=schema, cls=type(schema_validator(schema)))
    except jsonschema.ValidationError as exc:
        raise ProviderError(f"ODPS validation failed: {exc.message}") from exc
