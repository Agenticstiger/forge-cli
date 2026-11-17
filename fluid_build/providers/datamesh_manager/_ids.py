# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""One spelling of a data product id for both ends of a Data Mesh Manager edge.

Entropy Data ids are case-sensitive: ``CaseProbe.Mixed`` and ``caseprobe.mixed`` are
two products. The native provider has always published a product under a slugged id
(lower case, spaces and slashes to ``-``, a valid URL path segment), while the ids
that name it from elsewhere (a consumer's ``consumes[].productId``, the registrar's
product id) were used as written, so a product with an upper-case letter was
published under one id and consumed under another, and its lineage edge went
nowhere. Both publish paths now run the contract through
:func:`with_dmm_product_ids` before rendering, so the product's own id, the
``{productId}.{exposeId}`` ids derived from it, and every upstream it consumes are
spelled one way. A pinned contract id (``exposes[].contract.id``,
``consumes[].contractId``) is the author's own and stays as written.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Mapping

__all__ = ["dmm_product_id", "with_dmm_product_ids"]


def dmm_product_id(value: str) -> str:
    """The id Entropy knows a product by: stripped, lower case, spaces and ``/`` as ``-``."""
    return str(value).strip().lower().replace(" ", "-").replace("/", "-")


def with_dmm_product_ids(fluid: Mapping[str, Any]) -> Dict[str, Any]:
    """A copy of ``fluid`` with its ``id`` and every ``consumes[].productId`` slugged."""
    out = copy.deepcopy(dict(fluid))
    if isinstance(out.get("id"), str) and out["id"].strip():
        out["id"] = dmm_product_id(out["id"])
    for consume in out.get("consumes") or []:
        if isinstance(consume, dict) and isinstance(consume.get("productId"), str):
            consume["productId"] = dmm_product_id(consume["productId"])
    return out
