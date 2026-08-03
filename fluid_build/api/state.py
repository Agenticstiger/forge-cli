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

"""State store + cursor + watermark + lock primitives.

State is **separate from data** — replay does not require destination access.
Implementations may persist to local filesystem (default), S3, GCS, or any
KV store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ContextManager, Dict, Optional, Protocol


@dataclass(frozen=True)
class Cursor:
    """Per-stream cursor state. Opaque to forge; runners interpret."""

    stream: str
    value: Any  # JSON-serializable
    updated_at: str  # ISO-8601


@dataclass(frozen=True)
class Watermark:
    """High-water-mark / log-position style progress marker."""

    stream: str
    kind: str  # "high_water_mark" | "log_position" | "lsn"
    value: Any
    updated_at: str


@dataclass(frozen=True)
class RunLock:
    """Single-flight lock handle. Released via ``StateStore.release_lock``."""

    holder: str  # PID or run-id
    acquired_at: str  # ISO-8601
    lease_seconds: int
    scope: str  # "product" | "build"
    resource_id: str


class StateStore(Protocol):
    """State store contract. Implementations atomic-write + checksum + rotate."""

    def get_cursor(self, product_id: str, build_id: str, stream: str) -> Optional[Cursor]: ...

    def set_cursor(self, product_id: str, build_id: str, cursor: Cursor) -> None: ...

    def get_watermark(self, product_id: str, build_id: str, stream: str) -> Optional[Watermark]: ...

    def set_watermark(self, product_id: str, build_id: str, watermark: Watermark) -> None: ...

    def acquire_lock(
        self,
        scope: str,
        resource_id: str,
        timeout_seconds: int,
        on_contended: str = "abort",  # "abort" | "queue" | "replace"
    ) -> ContextManager[RunLock]:
        """Acquire a single-flight lock. Released on context-manager exit."""
        ...

    def write_run_record(
        self, product_id: str, build_id: str, run_record: Dict[str, Any]
    ) -> None: ...

    def read_run_record(
        self, product_id: str, build_id: str, run_id: str
    ) -> Optional[Dict[str, Any]]: ...

    def list_runs(
        self, product_id: str, build_id: str, limit: int = 50
    ) -> list[Dict[str, Any]]: ...
