# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Importer Protocol + registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class ImportReport:
    """Translation report for a foreign-config import."""

    mapped_one_to_one: List[str] = field(default_factory=list)
    required_defaults: List[str] = field(default_factory=list)
    unsupported: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


class Importer(Protocol):
    name: str

    def can_import(self, source: str) -> bool: ...

    def import_to_contract(
        self,
        source: str,
        *,
        options: Optional[Dict[str, Any]] = None,
    ) -> tuple[Dict[str, Any], ImportReport]: ...


IMPORTERS: Dict[str, Importer] = {}


def register_importer(name: str, importer: Importer) -> None:
    IMPORTERS[name] = importer


def get_importer(name: str) -> Optional[Importer]:
    return IMPORTERS.get(name)
