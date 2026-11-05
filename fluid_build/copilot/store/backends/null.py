# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""No-op store backend."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..base import Store, StoreRecord


class NullBackend(Store):
    """A store backend that never persists anything."""

    def get(self, ns: str, key: str) -> Optional[StoreRecord]:
        return None

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
        return StoreRecord(namespace=ns, key=key, value=value, metadata=metadata or {})

    def query(
        self,
        ns: str,
        *,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 10,
    ) -> List[StoreRecord]:
        return []

    def search(
        self,
        ns: str,
        query: str,
        *,
        mode: str = "exact",
        limit: int = 10,
    ) -> List[StoreRecord]:
        return []

    def clear(self, ns: Optional[str] = None) -> int:
        return 0
