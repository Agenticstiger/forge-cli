# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The schema dimensions every live verifier reports, built from one schema diff.

``fluid verify`` renders a ``structure`` and a ``types`` dimension for each
verified table (``cli/verify.py``). The Glue/Athena and the Databricks
verifiers each compare a live table with the contract, so both build those two
dimensions here, from a diff with ``matching``, ``missing``, ``extra``,
``type_mismatches``, ``total_expected`` and ``total_actual``.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping


def schema_dimensions(diff: Mapping[str, Any]) -> Dict[str, Any]:
    """``{"structure": ..., "types": ...}`` for one table's schema diff."""
    return {
        "structure": {
            "status": "pass" if not (diff["missing"] or diff["extra"]) else "fail",
            "matching_fields": diff["matching"],
            "missing_fields": diff["missing"],
            "extra_fields": diff["extra"],
            "total_expected": diff["total_expected"],
            "total_actual": diff["total_actual"],
        },
        "types": {
            "status": "pass" if not diff["type_mismatches"] else "fail",
            "mismatches": diff["type_mismatches"],
        },
    }


__all__ = ["schema_dimensions"]
