# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The ODCS versions the exporter can write, and how a version is spelled.

Bitol's Open Data Contract Standard names its version in ``apiVersion`` as
``v<major>.<minor>.<patch>``. The exporter writes one of the versions below,
chosen by the caller: :data:`DEFAULT_VERSION` unless asked, so nothing that
already exports (the Data Mesh Manager publish, the artifact fan-out, ``fluid
generate standard``) changes when a newer version is vendored. A version is
supported when its JSON Schema is vendored next to this module
(``odcs-schema-<version>.json``): the document an export writes is validated
against the schema of the version it declares.

``3.2.0`` adds the Iceberg server type among others; the exporter writes it for
an Iceberg binding when asked for that version.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Tuple

from fluid_build.providers.base import ProviderError

#: Every version with a vendored schema, oldest first.
SUPPORTED_VERSIONS: Tuple[str, ...] = ("v3.1.0", "v3.2.0")

#: What an export writes unless it is asked for another version.
DEFAULT_VERSION = "v3.1.0"

_SPELLING = re.compile(r"[vV]?(\d+)\.(\d+)\.(\d+)", re.ASCII)


class OdcsVersionError(ProviderError):
    """A version the exporter does not write. The message names the ones it does."""


def normalize_version(value: Any) -> str:
    """``"3.2.0"``, ``"v3.2.0"`` and ``"V3.2.0"`` as ``"v3.2.0"``; ``None`` is the default.

    Raises :class:`OdcsVersionError`, naming every supported version, for a
    spelling that is not a version or one that has no vendored schema.
    """
    if value is None or value == "":
        return DEFAULT_VERSION
    match = _SPELLING.fullmatch(str(value).strip())
    version = f"v{match.group(1)}.{match.group(2)}.{match.group(3)}" if match else None
    if version not in SUPPORTED_VERSIONS:
        supported = ", ".join(v.lstrip("v") for v in SUPPORTED_VERSIONS)
        raise OdcsVersionError(
            f"ODCS version {str(value)[:40]!r} is not one the exporter writes (supported: {supported}); "
            f"the default is {DEFAULT_VERSION.lstrip('v')}"
        )
    return version


def schema_filename(version: str) -> str:
    """The vendored JSON Schema of ``version`` (already normalized)."""
    return f"odcs-schema-{version}.json"


def version_of(document: Any) -> Optional[str]:
    """The supported version a document declares in ``apiVersion``, else ``None``.

    ``None`` for a document that declares no version, or one with no vendored
    schema (``v3.0.2``, ``v2.2.2``): the caller then validates against the
    default schema, whose ``apiVersion`` enum lists those older versions.
    """
    declared = document.get("apiVersion") if hasattr(document, "get") else None
    match = _SPELLING.fullmatch(declared.strip()) if isinstance(declared, str) else None
    if not match:
        return None
    version = f"v{match.group(1)}.{match.group(2)}.{match.group(3)}"
    return version if version in SUPPORTED_VERSIONS else None


def at_least(version: str, minimum: str) -> bool:
    """Whether ``version`` is ``minimum`` or newer (both normalized)."""
    return SUPPORTED_VERSIONS.index(version) >= SUPPORTED_VERSIONS.index(minimum)
