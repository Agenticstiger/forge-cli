# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The ``--odcs-version`` option the ODCS exporters share.

``fluid odcs export``, ``fluid generate standard --format odcs`` and ``fluid
datamesh-manager publish --with-contract`` write ODCS documents, and each takes
the version to write the same way. Nothing here imports the ODCS provider until a
version is named, so ``--help`` of those commands stays light.
"""

from __future__ import annotations

from typing import Any, Optional

from ._common import CLIError

HELP = (
    "ODCS (Open Data Contract Standard) version to write: 3.1.0 (the default) or 3.2.0, "
    "which adds the Iceberg server type among others. A catalog that validates v3.1.0 "
    "only needs the default"
)


def add_argument(parser: Any, *, help_suffix: str = "") -> None:
    """``--odcs-version VERSION`` on ``parser``."""
    parser.add_argument(
        "--odcs-version",
        dest="odcs_version",
        metavar="VERSION",
        default=None,
        help=HELP + help_suffix,
    )


def requested(args: Any) -> Optional[str]:
    """The version ``args`` ask for as ``v3.x.y``, ``None`` when none is asked.

    A version the exporter does not write is the typed ``odcs_version_unsupported``
    error, which names the ones it does.
    """
    value = getattr(args, "odcs_version", None)
    if value is None:
        return None
    from fluid_build.providers.odcs.versions import OdcsVersionError, normalize_version

    try:
        return normalize_version(value)
    except OdcsVersionError as exc:
        raise CLIError(2, "odcs_version_unsupported", {"message": str(exc)}) from exc
