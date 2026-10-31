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

"""Shared naming helpers for the IaC emitter plugins."""

from __future__ import annotations

import hashlib
from typing import Any


def safe_ident(value: Any) -> str:
    """Coerce an arbitrary string into a valid OpenTofu identifier.

    OpenTofu resource names allow letters, digits and underscores and may
    not start with a digit. Non-conforming characters become ``_``.
    """
    cleaned = "".join(c if (c.isalnum() or c == "_") else "_" for c in str(value))
    cleaned = cleaned.strip("_") or "x"
    if cleaned[0].isdigit():
        cleaned = f"r_{cleaned}"
    return cleaned


def unique_ident(value: str) -> str:
    """:func:`safe_ident`, plus a digest of ``value`` whenever folding changed it.

    ``safe_ident`` turns every character but letters, digits and ``_`` into
    ``_``, so ``data-eng`` and ``data_eng`` would meet at one address, a
    collision the contract cannot rename away.
    """
    folded = safe_ident(value)
    if folded == value:
        return folded
    return f"{folded}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}"


class TofuExpr(str):
    """A string the emitter built as a deliberate OpenTofu expression — a
    resource cross-reference like ``${google_bigquery_dataset.x.dataset_id}``.

    ``render_tofu_json`` escapes ``${`` / ``%{`` in every *contract-derived*
    string so a contract cannot inject OpenTofu interpolation, but leaves
    ``TofuExpr`` values untouched. Only ever construct one from emitter-
    controlled text (literal resource types/attributes + ``safe_ident``
    output) — never from raw contract content.
    """

    __slots__ = ()


def tofu_ref(expression: str) -> TofuExpr:
    """Wrap an emitter-built interpolation ``expression`` as ``${expression}``.

    For resource cross-references only. The renderer leaves the result
    un-escaped; every non-``TofuExpr`` string is treated as a literal.
    """
    return TofuExpr("${" + expression + "}")
