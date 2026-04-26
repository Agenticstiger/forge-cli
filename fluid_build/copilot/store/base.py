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

"""Store abstractions for staged copilot state."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


@dataclass
class StoreRecord:
    """A value persisted in a namespaced store."""

    namespace: str
    key: str
    value: Any
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    expires_at: Optional[datetime] = None
    fluid_version: Optional[str] = None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= utc_now()


class Store(ABC):
    """Common store API for cache, memory, and discovery state."""

    @abstractmethod
    def get(self, ns: str, key: str) -> Optional[StoreRecord]:
        """Return the record for ``(ns, key)`` if present."""

    @abstractmethod
    def put(
        self,
        ns: str,
        key: str,
        value: Any,
        *,
        ttl: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        fluid_version: Optional[str] = None,
    ) -> StoreRecord:
        """Persist ``value`` under ``(ns, key)`` and return the stored record."""

    @abstractmethod
    def query(
        self,
        ns: str,
        *,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 10,
    ) -> List[StoreRecord]:
        """Return up to ``limit`` records from ``ns`` matching ``filter``."""

    @abstractmethod
    def search(
        self,
        ns: str,
        query: str,
        *,
        mode: str = "exact",
        limit: int = 10,
    ) -> List[StoreRecord]:
        """Search records in ``ns`` using the backend's supported mode."""

    @abstractmethod
    def clear(self, ns: Optional[str] = None) -> int:
        """Delete records in ``ns`` or the whole store and return a count."""
