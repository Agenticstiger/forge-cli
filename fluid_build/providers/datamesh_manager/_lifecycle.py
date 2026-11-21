# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Lifecycle status of a Data Mesh Manager data product: draft, active, retired.

A product may be published as ``draft`` and promoted to ``active`` later, for example once
``fluid verify`` passes. Forge moves a product forward only (draft to active): it never
demotes an active product, never revives a deprecated one, and never sets ``retired`` itself.
``retired`` is one way: Entropy keeps a retired product retired, so forge sends it no status
and no new definition.

The status values are Entropy's and the Open Data Product Standard's own vocabulary
(``proposed`` / ``draft`` / ``active`` / ``deprecated`` / ``retired``); the standard defines
no transitions, so the rules here are forge's and stay minimal.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from fluid_build.providers.base import ProviderError

__all__ = [
    "ACTIVE",
    "DEPRECATED",
    "DRAFT",
    "PUBLISH_STATUSES",
    "RETIRED",
    "ProductRetiredError",
    "product_status",
    "promotion_blocker",
    "set_product_status",
    "status_to_send",
]

DRAFT = "draft"
ACTIVE = "active"
DEPRECATED = "deprecated"
RETIRED = "retired"

#: What ``--status`` may ask a publish to send. ``retired`` is never sent.
PUBLISH_STATUSES = (DRAFT, ACTIVE)

#: The statuses a promotion starts from.
_PROMOTABLE = frozenset({DRAFT, "proposed"})


class ProductRetiredError(ProviderError):
    """The product is retired in Entropy; forge sends it nothing."""

    def __init__(self, product_id: str) -> None:
        self.product_id = product_id
        super().__init__(
            f"Data product {product_id!r} is retired in Entropy Data, and a retired product "
            "stays retired: forge sends it no status and no new definition. Publish it under "
            "a new product id, or leave it out of this run."
        )


def product_status(body: Any) -> Optional[str]:
    """The status a product body carries, lower case, or ``None``.

    The Open Data Product Standard keeps it at the root; the older Data Product
    Specification keeps it at ``info.status``.
    """
    if not isinstance(body, Mapping):
        return None
    status = body.get("status")
    if not isinstance(status, str):
        info = body.get("info")
        status = info.get("status") if isinstance(info, Mapping) else None
    if isinstance(status, str) and status.strip():
        return status.strip().lower()
    return None


def set_product_status(body: Dict[str, Any], status: str) -> None:
    """Set ``status`` on a product body in place, where the body's shape keeps it."""
    info = body.get("info")
    if "status" not in body and isinstance(info, dict) and "status" in info:
        info["status"] = status
    else:
        body["status"] = status


def status_to_send(requested: str, current: Optional[str]) -> str:
    """The status a publish sends for ``requested``, given what Entropy holds now.

    Forward only: ``draft`` never demotes a product Entropy holds as active, so re-running a
    migration batch does not pull a live product back to draft; a deprecated product keeps
    its status whatever is asked, as forge does not revive it (``promote`` refuses it too).
    """
    if current == DEPRECATED:
        return DEPRECATED
    if requested == DRAFT and current == ACTIVE:
        return ACTIVE
    return requested


def promotion_blocker(current: Optional[str]) -> Optional[str]:
    """Why a product in status ``current`` cannot be promoted to active, or ``None``.

    Only a draft (or proposed) product is promoted. An active one is already there; a
    deprecated or retired one is not forge's to bring back.
    """
    if current in _PROMOTABLE:
        return None
    if current == ACTIVE:
        return "already active"
    if current == RETIRED:
        return "retired in Entropy Data, which stays retired"
    if current == DEPRECATED:
        return "deprecated: promote moves a draft to active, not a deprecated product"
    return f"status {current!r} is not a draft"
