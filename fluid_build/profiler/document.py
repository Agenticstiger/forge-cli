# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The profile document: PROFILE-SPEC formats 1-0-0 and 1-1-0, its canonical bytes and digest.

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

A profile names format 1-0-0, unless it holds a ``quantile/2`` statistic, which only
format 1-1-0 carries (:func:`schema_url_for`): a profile without one keeps the bytes it
had.

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
    "MAX_FRACTIONAL_BITS",
    "PRODUCER_REPOSITORY",
    "SCHEMA_URL",
    "SCHEMA_URL_1_1_0",
    "build_document",
    "canonical_bytes",
    "check_fractional_bits",
    "digest_of",
    "producer_uri",
    "schema_url_for",
]

#: The format a profile names unless it needs the next one. A later format is a new URL
#: (PROFILE-SPEC §8).
SCHEMA_URL = "https://agenticstiger.github.io/flux/engine/spec/profile/1-0-0/FluxProfile.json"

#: Format 1-1-0 adds the ``quantile/2`` statistic and changes nothing 1-0-0 says
#: (``flux_stats.profile.SCHEMA_URL_1_1_0``; a test keeps the two equal).
SCHEMA_URL_1_1_0 = "https://agenticstiger.github.io/flux/engine/spec/profile/1-1-0/FluxProfile.json"

#: The statistics only format 1-1-0 holds.
_ADDED_IN_1_1_0 = frozenset({"quantile/2"})

#: ``_producer`` names this repository, at the release that wrote the profile.
PRODUCER_REPOSITORY = "https://github.com/Agenticstiger/forge-cli"

#: The grid a profile declares unless told otherwise: KERNEL-SPEC §12's default,
#: which a fidelity block that declares no ``encoding`` uses too. ``mean/1`` is
#: formed on it, and the check compares the two only on one grid (PROFILE-SPEC
#: §7). Column metrics are read in their own domain, never on the grid.
DEFAULT_FRACTIONAL_BITS = 16

#: The widest grid PROFILE-SPEC §2 allows.
MAX_FRACTIONAL_BITS = 128


class DocumentError(ValueError):
    """A document that PROFILE-SPEC refuses; the message names where, never a value."""


def check_fractional_bits(fractional_bits: Any) -> None:
    """Refuse a grid PROFILE-SPEC §2 does not allow: a whole number from 0 to 128."""
    if isinstance(fractional_bits, bool) or not isinstance(fractional_bits, int):
        raise DocumentError("encoding.fractionalBits is a whole number")
    if not 0 <= fractional_bits <= MAX_FRACTIONAL_BITS:
        raise DocumentError(f"encoding.fractionalBits lies from 0 to {MAX_FRACTIONAL_BITS}")


def producer_uri(version: str) -> str:
    """The ``_producer`` URI: the profiler's source tree at this release's tag."""
    return f"{PRODUCER_REPOSITORY}/tree/v{version}/fluid_build/profiler"


def schema_url_for(tables: Mapping[str, Mapping[str, Any]]) -> str:
    """The format *tables* need: 1-1-0 when any statistic is ``quantile/2``, else 1-0-0."""
    for table in tables.values():
        for column in table.get("columns", {}).values():
            for statistic in column.get("statistics", ()):
                if statistic.get("definition") in _ADDED_IN_1_1_0:
                    return SCHEMA_URL_1_1_0
    return SCHEMA_URL


def build_document(
    *,
    producer: str,
    rule: DisclosureRule,
    tables: Mapping[str, Mapping[str, Any]],
    fractional_bits: int = DEFAULT_FRACTIONAL_BITS,
) -> Dict[str, Any]:
    """Assemble a document from tables already checked by *rule*, in the format they need."""
    check_fractional_bits(fractional_bits)
    if not tables:
        raise DocumentError("tables holds at least one table")
    return {
        "_producer": producer,
        "_schemaURL": schema_url_for(tables),
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
