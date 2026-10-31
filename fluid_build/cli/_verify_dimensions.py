# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

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
