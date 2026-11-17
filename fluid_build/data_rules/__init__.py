# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Rules about valid data in a FLUID contract (``RFC-data-rules.md``, schema 0.7.7).

``exposes[].contract.invariants[]`` (checks, derived columns, summaries,
functional dependencies, each with a band of violating rows), and the column and
relationship keys ``identifierFormat``, ``placeholders``, ``sharedAttribute`` and
``sharedColumns``. :func:`lint_contract` is the v1 validator lint that
``fluid validate`` runs; :mod:`fluid_build.data_rules._masks` compiles the
identifier formats into the one regex every consumer matches.

Standard library only (with :mod:`fluid_build.fluid_sql`), imported by
``fluid validate`` when it runs, never by ``fluid --help``.
"""

from __future__ import annotations

from typing import Any, List, Mapping

from ._lint import Finding, lint

#: The first schema that carries these fields (a preview: ``PREVIEW_VERSIONS``).
FIRST_VERSION = (0, 7, 7)


def _version(contract: Mapping[str, Any]) -> tuple:
    text = str(contract.get("fluidVersion") or "").strip()
    parts = text.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return ()
    return tuple(int(part) for part in parts)


def lint_contract(contract: Mapping[str, Any]) -> List[Finding]:
    """The data-rules findings for ``contract``; none below 0.7.7, where the schema
    refuses the fields and says to declare 0.7.7 as the ``fluidVersion``."""
    if not isinstance(contract, Mapping):
        return []
    version = _version(contract)
    if not version or version < FIRST_VERSION:
        return []
    return lint(contract)


__all__ = ["FIRST_VERSION", "Finding", "lint_contract"]
