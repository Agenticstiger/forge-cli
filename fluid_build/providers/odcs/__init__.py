# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

# fluid_build/providers/odcs/__init__.py
"""
ODCS (Open Data Contract Standard) Provider - Bitol.io

This provider handles bidirectional conversion between FLUID and ODCS formats.
ODCS contracts define the data structure, quality, and SLA requirements.

ODCS Specification: https://github.com/bitol-io/open-data-contract-standard
"""

from .odcs import OdcsProvider

# ODCS (Open Data Contract Standard, Bitol) is a data-contract SPEC / export
# format — NOT a cloud/infrastructure provider. ``OdcsProvider.apply()`` raises
# ("does not support apply()"); it only renders/imports the ODCS spec. Like ODPS,
# it is therefore intentionally NOT registered in the provider registry and never
# appears in ``fluid providers`` / ``fluid plugins`` / ``--provider``.
#
# The class stays importable for the spec commands, which construct it directly
# (``from fluid_build.providers.odcs import OdcsProvider`` — see cli/odcs.py,
# cli/generate_standard.py, api/catalog_publication.py, datamesh_manager). The
# opt-out below stops the provider auto-discovery single-subclass fallback from
# re-registering it on the strength of that import.
__fluid_no_autoregister__ = True

__all__ = ["OdcsProvider"]
