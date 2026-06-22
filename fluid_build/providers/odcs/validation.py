# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""ODCS JSON Schema validation + round-trip diff utility."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional

from fluid_build.providers.base import ProviderError

LOG = logging.getLogger(__name__)

_SCHEMA_FILENAME = "odcs-schema-v3.1.0.json"


def load_schema() -> Optional[Dict[str, Any]]:
    """Load the vendored ODCS v3.1.0 JSON Schema. Returns None if missing."""
    schema_path = Path(__file__).parent / _SCHEMA_FILENAME
    if not schema_path.exists():
        LOG.warning("ODCS schema not found: %s", schema_path)
        return None
    try:
        with open(schema_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # pragma: no cover - defensive
        LOG.error("Failed to load ODCS schema: %s", exc)
        return None


def validate(odcs: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    """Validate an ODCS contract against the v3.1.0 JSON Schema.

    Raises :class:`ProviderError` on validation failure carrying ALL
    violations (not just the first). Silently no-ops when the optional
    :mod:`jsonschema` dependency is not installed.
    """
    errors = collect_errors(odcs, schema)
    if errors:
        # Cap at 20 lines of detail so the exception message stays
        # readable; full structured list is available via collect_errors().
        head = errors[:20]
        more = f" (+{len(errors) - 20} more)" if len(errors) > 20 else ""
        body = "; ".join(f"{e['path'] or '<root>'}: {e['message']}" for e in head)
        raise ProviderError(f"ODCS validation failed ({len(errors)} error(s)): {body}{more}")


def collect_errors(odcs: Mapping[str, Any], schema: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Return EVERY ODCS schema violation as a structured list.

    Each entry has::

        {
            "path": "schema.0.relationships.0",  # dotted instance path
            "message": "'type' was unexpected",
            "validator": "unevaluatedProperties",
        }

    Empty list = valid. No-op (empty list) when :mod:`jsonschema` is not
    installed. Used by ``validate()`` (which raises on non-empty) and by
    ``fluid odcs validate --report`` (which serializes to JSON for CI).
    """
    try:
        import jsonschema
    except ImportError:
        LOG.warning("jsonschema not installed, skipping ODCS validation")
        return []

    validator_cls = jsonschema.validators.validator_for(schema)
    validator = validator_cls(schema)
    out: List[Dict[str, Any]] = []
    for err in validator.iter_errors(odcs):
        out.append(
            {
                "path": ".".join(str(p) for p in err.absolute_path),
                "message": err.message,
                "validator": err.validator,
            }
        )
    return out


def validate_via_vowl(odcs: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Optional second-pass ODCS validation via the ``vowl`` library.

    Why: ``jsonschema`` alone misses format-level (uuid/email/...) and
    unevaluated-properties enforcement; vowl is the Bitol-vendor-listed
    validator that catches them. Pip-extra: ``fluid-build[odcs-strict]``.

    Returns ``None`` when vowl isn't installed (so callers can skip
    cleanly). Returns ``{api_version, schemas, total_checks}`` on
    success. Raises :class:`ProviderError` on validation failure.
    """
    try:
        from vowl import Contract  # type: ignore[import-untyped]
    except ImportError:
        LOG.debug("vowl not installed, skipping second-pass ODCS validation")
        return None

    # Narrow the exception scope: vowl raises jsonschema-derived
    # ValidationError on bad input; other exceptions (system errors,
    # KeyboardInterrupt, MemoryError) should propagate unwrapped so we
    # don't hide real bugs behind a generic "vowl failed" message.
    import jsonschema  # type: ignore[import-untyped]

    try:
        contract = Contract(dict(odcs))
        refs = contract.get_check_references_by_schema()
    except (jsonschema.ValidationError, jsonschema.SchemaError, ValueError, KeyError) as exc:
        raise ProviderError(f"vowl: {type(exc).__name__}: {exc}") from exc

    return {
        "api_version": contract.get_api_version(),
        "schemas": list(contract.get_schema_names() or []),
        "total_checks": sum(len(v) for v in (refs or {}).values()),
    }


def roundtrip_check(odcs: Mapping[str, Any], reconstructed: Mapping[str, Any]) -> Dict[str, Any]:
    """Compare an original ODCS dict to one rebuilt via ``import → export``.

    Returns a structured diff::

        {
            "equal": bool,
            "missing": [".path.to.lost.field", ...],
            "extra":   [".path.added.by.export", ...],
            "changed": [{"path": ".x", "old": ..., "new": ...}, ...]
        }

    Used by tests and the forge ground-truth guard. Pure; no I/O. Backed by
    :mod:`deepdiff` so list-of-objects ordering, nested-dict comparison, and
    type coercion are handled consistently.
    """
    from deepdiff import DeepDiff

    diff = DeepDiff(
        dict(odcs),
        dict(reconstructed),
        ignore_order=False,
        verbose_level=0,
        view="tree",
    )

    def _path(node: Any) -> str:
        # deepdiff path strings come in like ``root['schema'][0]['name']``.
        return str(node.path(output_format="list"))

    missing: List[str] = []
    extra: List[str] = []
    changed: List[Dict[str, Any]] = []

    for node in diff.get("dictionary_item_removed", []):
        missing.append(_path(node))
    for node in diff.get("iterable_item_removed", []):
        missing.append(_path(node))
    for node in diff.get("dictionary_item_added", []):
        extra.append(_path(node))
    for node in diff.get("iterable_item_added", []):
        extra.append(_path(node))
    for node in diff.get("values_changed", []):
        changed.append({"path": _path(node), "old": node.t1, "new": node.t2})
    for node in diff.get("type_changes", []):
        changed.append({"path": _path(node), "old": node.t1, "new": node.t2})

    return {
        "equal": not (missing or extra or changed),
        "missing": missing,
        "extra": extra,
        "changed": changed,
    }
