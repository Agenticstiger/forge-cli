# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The profile document: PROFILE-SPEC format 1-0-0, its canonical bytes and digest.

flux-engine ``docs/spec/PROFILE-SPEC.md`` §1 fixes the shape: the OpenLineage
facet envelope (``_producer``, ``_schemaURL``), the grid (``encoding``), the
disclosure rules by name, and one table per FLUID ``exposeId``. A count is a JSON
integer from 0 to 2^53 - 1, every other number a decimal string, every string in
Unicode Normalization Form C, and no float appears anywhere.

The digest is flux-engine's ``jcs-document``: ``sha256:`` and the hex sha256 of
the RFC 8785 canonical bytes. :func:`canonical_bytes` uses ``rfc8785``, the
Trail of Bits implementation flux-engine digests with, so both sides compute one
digest from one implementation. The file holds exactly those bytes, so its sha256
is the digest a Simulation pins.

A document that breaks a rule is refused here, before anything is written. The
refusal names where in the document the problem is, never a value.
"""

from __future__ import annotations

import hashlib
import unicodedata
from typing import Any, Dict, List, Mapping

from fluid_build.profiler.disclosure import MAX_COUNT, DisclosureRule

__all__ = [
    "DEFAULT_FRACTIONAL_BITS",
    "DocumentError",
    "PRODUCER_REPOSITORY",
    "SCHEMA_URL",
    "build_document",
    "canonical_bytes",
    "digest_of",
    "producer_uri",
]

#: The format this module writes. A later format is a new URL (PROFILE-SPEC §8).
SCHEMA_URL = "https://agenticstiger.github.io/flux/engine/spec/profile/1-0-0/FluxProfile.json"

#: ``_producer`` names this repository, at the release that wrote the profile.
PRODUCER_REPOSITORY = "https://github.com/Agenticstiger/forge-cli"

#: The grid a profile declares when nothing in it depends on one. Column metrics
#: are read in their own domain, never on the grid; 16 is the grid FLUX's own
#: examples declare, so a metrics-only profile reads like the rest.
DEFAULT_FRACTIONAL_BITS = 16


class DocumentError(ValueError):
    """A document that PROFILE-SPEC refuses; the message names where, never a value."""


def producer_uri(version: str) -> str:
    """The ``_producer`` URI: the profiler's source tree at this release's tag."""
    return f"{PRODUCER_REPOSITORY}/tree/v{version}/fluid_build/profiler"


def build_document(
    *,
    producer: str,
    rule: DisclosureRule,
    tables: Mapping[str, Mapping[str, Any]],
    fractional_bits: int = DEFAULT_FRACTIONAL_BITS,
) -> Dict[str, Any]:
    """Assemble a format 1-0-0 document from tables already checked by *rule*."""
    if isinstance(fractional_bits, bool) or not isinstance(fractional_bits, int):
        raise DocumentError("encoding.fractionalBits is a whole number")
    if not 0 <= fractional_bits <= 128:
        raise DocumentError("encoding.fractionalBits lies from 0 to 128")
    if not tables:
        raise DocumentError("tables holds at least one table")
    return {
        "_producer": producer,
        "_schemaURL": SCHEMA_URL,
        "encoding": {"fractionalBits": fractional_bits, "rounding": "floor"},
        "disclosureRules": {rule.name: rule.to_document()},
        "tables": {expose_id: dict(table) for expose_id, table in tables.items()},
    }


def _check(value: Any, where: List[str]) -> None:
    """Refuse what the format cannot carry: a float, an unsafe integer, a non-NFC string."""
    here = ".".join(where) or "the document"
    if isinstance(value, Mapping):
        for key, item in value.items():
            _check_key(key, here)
            _check(item, where + [key])
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check(item, where + [f"[{index}]"])
    else:
        _check_scalar(value, here)


def _check_key(key: Any, here: str) -> None:
    if not isinstance(key, str) or not key:
        raise DocumentError(f"{here}: every key is a non-empty string")
    if unicodedata.normalize("NFC", key) != key:
        raise DocumentError(f"{here}: a key is not in Unicode Normalization Form C")


def _check_scalar(value: Any, here: str) -> None:
    if isinstance(value, str):
        if unicodedata.normalize("NFC", value) != value:
            raise DocumentError(f"{here}: a string is not in Unicode Normalization Form C")
    elif isinstance(value, int) and not isinstance(value, bool):
        if not -MAX_COUNT <= value <= MAX_COUNT:
            raise DocumentError(f"{here}: an integer lies outside -(2^53 - 1) to 2^53 - 1")
    elif not (isinstance(value, bool) or value is None):
        # A float has no one exact meaning; a Decimal or a Fraction must already
        # be spelled as a decimal string by the registry.
        raise DocumentError(f"{here}: a {type(value).__name__} is not a profile value")


def canonical_bytes(document: Mapping[str, Any]) -> bytes:
    """The RFC 8785 canonical UTF-8 bytes of *document*, after the format's checks."""
    _check(document, [])
    import rfc8785  # a base dependency, imported here to keep it off the --help path

    # Annotated, not returned bare: where rfc8785 is not installed (the strict
    # mypy job) its dumps is Any.
    canonical: bytes = rfc8785.dumps(document)
    return canonical


def digest_of(canonical: bytes) -> str:
    """flux-engine's ``jcs-document`` digest of canonical bytes: ``sha256:<64 hex>``."""
    return "sha256:" + hashlib.sha256(canonical).hexdigest()
