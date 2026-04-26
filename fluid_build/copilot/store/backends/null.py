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
