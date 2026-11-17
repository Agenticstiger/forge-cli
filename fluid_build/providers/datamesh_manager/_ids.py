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

An upstream Entropy holds under the id as written and not under its slug (one forge
did not publish, ``Acme.Orders``) is consumed as written: the slug is for products forge publishes, and
slugging that upstream would point at a product that does not exist. Callers that can
read the product list pass it as ``existing_ids``; :func:`needs_existing_ids` says
whether there is anything to look up.
"""

from __future__ import annotations

import copy
from typing import Any, Collection, Dict, Mapping, Optional

__all__ = ["dmm_product_id", "needs_existing_ids", "with_dmm_product_ids"]


def dmm_product_id(value: str) -> str:
    """The id Entropy knows a product by: stripped, lower case, spaces and ``/`` as ``-``."""
    return str(value).strip().lower().replace(" ", "-").replace("/", "-")


def _consumes(fluid: Mapping[str, Any]) -> list:
    consumes = fluid.get("consumes")
    return [c for c in consumes if isinstance(c, dict)] if isinstance(consumes, list) else []


def needs_existing_ids(fluid: Mapping[str, Any]) -> bool:
    """Whether some ``consumes[].productId`` is not already spelled as its slug."""
    return any(
        isinstance(c.get("productId"), str) and c["productId"] != dmm_product_id(c["productId"])
        for c in _consumes(fluid)
    )


def with_dmm_product_ids(
    fluid: Mapping[str, Any], existing_ids: Optional[Collection[str]] = None
) -> Dict[str, Any]:
    """A copy of ``fluid`` with its ``id`` slugged, and each ``consumes[].productId``
    slugged unless ``existing_ids`` holds it as written."""
    out = copy.deepcopy(dict(fluid))
    if isinstance(out.get("id"), str) and out["id"].strip():
        out["id"] = dmm_product_id(out["id"])
    held = set(existing_ids or ())
    for consume in _consumes(out):
        raw = consume.get("productId")
        if not isinstance(raw, str):
            continue
        slug = dmm_product_id(raw)
        # The slug wins whenever Entropy holds it: a product forge published (and so
        # live), even beside a stale as-written copy an older publish left behind.
        if slug in held or raw not in held:
            consume["productId"] = slug
    return out
