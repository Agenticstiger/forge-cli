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
