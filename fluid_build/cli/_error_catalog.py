# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Backwards-compat shim for the central error catalog.

The catalog moved to the tier-0 shared leaf :mod:`fluid_build._error_catalog`
(so ``CLIError``'s auto-enrichment stays reachable from both ``cli`` and
``build_runners`` without a cross-package edge). This module re-exports the
public surface so ``from ._error_catalog import enrich`` /
``from fluid_build.cli import _error_catalog`` sites keep working.
"""

from __future__ import annotations

from fluid_build._error_catalog import (
    _GUIDANCE,
    DOC_BASE,
    _docs_url,
    catalogued_events,
    docs_url_for,
    enrich,
    slug_for,
    suggestions_for,
)

__all__ = [
    "DOC_BASE",
    "_GUIDANCE",
    "_docs_url",
    "catalogued_events",
    "docs_url_for",
    "enrich",
    "slug_for",
    "suggestions_for",
]
