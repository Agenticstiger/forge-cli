# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""ODCS JSON Schema validation + round-trip diff utility."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fluid_build.providers.base import ProviderError

from .versions import DEFAULT_VERSION, schema_filename, version_of

LOG = logging.getLogger(__name__)


def load_schema(version: str = DEFAULT_VERSION) -> Optional[Dict[str, Any]]:
    """Load the vendored ODCS JSON Schema of ``version`` (``v3.1.0`` unless named).

    ``version`` is a normalized supported version (``versions.normalize_version``).
    Returns None if the file is missing.
    """
    schema_path = Path(__file__).parent / schema_filename(version)
    if not schema_path.exists():
        LOG.warning("ODCS schema not found: %s", schema_path)
        return None
    try:
        with open(schema_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # pragma: no cover - defensive
        LOG.error("Failed to load ODCS schema: %s", exc)
        return None


def load_schema_for(odcs: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The vendored schema of the version ``odcs`` declares in ``apiVersion``.

    A document that declares no version, or one with no vendored schema (``v3.0.2``,
    ``v2.2.2``), gets the default schema, whose ``apiVersion`` enum lists the older
    versions. Returns None if the file is missing.
    """
    return load_schema(version_of(odcs) or DEFAULT_VERSION)


def validate(odcs: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    """Validate an ODCS contract against a vendored JSON Schema (``load_schema``).

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

    A ``pattern``'s ``$`` is the end of the value, as ECMA-262 reads it, so
    ``schema[].id: "t1\\n"`` fails ``^[A-Za-z0-9_-]+$`` like ``"t 1"`` does.
    """
    from fluid_build.schema_manager import JSONSCHEMA_AVAILABLE, schema_validator

    if not JSONSCHEMA_AVAILABLE:
        LOG.warning("jsonschema not installed, skipping ODCS validation")
        return []

    validator = schema_validator(schema)
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


def _vowl_versions() -> Optional[Tuple[str, ...]]:
    """The ODCS versions the installed vowl reads, newest first, or ``None`` when it does not say."""
    try:
        from vowl.contracts.models import SUPPORTED_VERSIONS  # type: ignore[import-untyped]
    except ImportError:
        return None
    return tuple(str(v) for v in SUPPORTED_VERSIONS)


def validate_via_vowl(odcs: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Optional second-pass ODCS validation via the ``vowl`` library.

    Why: ``jsonschema`` alone misses format-level (uuid/email/...) and
    unevaluated-properties enforcement; vowl is the Bitol-vendor-listed
    validator that catches them. Pip-extra: ``data-product-forge[odcs-strict]``.

    Returns ``None`` when vowl isn't installed (so callers can skip
    cleanly). Returns ``{"skipped": reason}`` when it is installed but does not
    read the version the document declares (a vowl release lags a new ODCS one;
    the jsonschema pass is the check for that version meanwhile). Returns
    ``{api_version, schemas, total_checks}`` on success. Raises
    :class:`ProviderError` on validation failure.
    """
    try:
        from vowl import Contract  # type: ignore[import-untyped]
    except ImportError:
        LOG.debug("vowl not installed, skipping second-pass ODCS validation")
        return None

    # A version this exporter writes but the installed vowl cannot read is not a defect of
    # the document: vowl would refuse it as unsupported. Any other version, a mistyped one
    # included, goes to vowl, which says so.
    declared = version_of(odcs)
    reads = _vowl_versions()
    if declared is not None and reads is not None and declared not in reads:
        return {"skipped": f"vowl reads ODCS {', '.join(reads)}, not {declared}"}

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
    from fluid_build._optional import MissingOptionalDependency, is_available

    if not is_available("deepdiff"):
        raise MissingOptionalDependency("deepdiff", "odcs-strict", "the ODCS round-trip check")
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
