# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
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

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "odps-product-v1.0.0.json"


def load_schema() -> Optional[Dict[str, Any]]:
    if not _SCHEMA_PATH.exists():
        LOG.warning("ODPS schema not found: %s", _SCHEMA_PATH)
        return None
    try:
        with open(_SCHEMA_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # pragma: no cover - defensive
        LOG.error("Failed to load ODPS schema: %s", exc)
        return None


def validate(odps: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    """Validate an ODPS product against the vendored v1.0.0 JSON Schema."""
    try:
        import jsonschema
    except ImportError:
        LOG.warning("jsonschema not installed, skipping ODPS validation")
        return
    try:
        jsonschema.validate(instance=odps, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ProviderError(f"ODPS validation failed: {exc.message}") from exc
