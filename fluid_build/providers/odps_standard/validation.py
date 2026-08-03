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

#: apiVersion values with a vendored schema. v1.1.0 is sourced from the dev
#: branch (approved RFCs staged for release, top-level ``type`` from RFC 0029)
#: and stays opt-in as an emit target until Bitol cuts it on main.
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
