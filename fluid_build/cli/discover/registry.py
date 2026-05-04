# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Discoverer Protocol + registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class DiscoveredColumn:
    name: str
    type: str
    nullable: bool = True
    classifications: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class DiscoveredStream:
    """One source stream (table / file / topic) the discoverer found."""

    name: str  # e.g., "public.orders" for postgres, "orders.csv" for filesystem
    columns: List[DiscoveredColumn]
    metadata: Dict[str, Any] = field(default_factory=dict)


class Discoverer(Protocol):
    scheme: str

    def discover(self, uri: str) -> List[DiscoveredStream]: ...


# ── Registry ────────────────────────────────────────────────────────────


DISCOVERERS: Dict[str, Discoverer] = {}


def register_discoverer(scheme: str, discoverer: Discoverer) -> None:
    DISCOVERERS[scheme] = discoverer


def get_discoverer(uri: str) -> Optional[Discoverer]:
    """Pick the discoverer that matches the URI's scheme."""
    if "://" not in uri:
        return DISCOVERERS.get("file")
    scheme = uri.split("://", 1)[0]
    # mongodb+srv://, postgresql:// etc. — strip drivers from the scheme.
    base = scheme.split("+")[0]
    if base in ("postgresql", "postgres"):
        return DISCOVERERS.get("postgres")
    if base in ("mysql", "mariadb"):
        return DISCOVERERS.get("mysql")
    if base in ("sqlite",):
        return DISCOVERERS.get("sqlite")
    if base in ("s3", "gs", "gcs", "azure", "https", "http", "file"):
        return DISCOVERERS.get("file")
    return DISCOVERERS.get(base)


# Bootstrap is performed in `fluid_build/cli/discover/__init__.py` after all
# discoverer modules are imported, to avoid a circular import here.
