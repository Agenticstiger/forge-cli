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

"""Source / Sink / Connection / Delivery types — the typed view of the
acquisition pattern's contract block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AcquisitionMode(str, Enum):
    FULL_REFRESH = "full_refresh"
    INCREMENTAL_APPEND = "incremental_append"
    INCREMENTAL_DEDUP = "incremental_dedup"
    INCREMENTAL_MERGE = "incremental_merge"
    CDC = "cdc"
    STREAMING = "streaming"


class DeliveryGuarantee(str, Enum):
    AT_MOST_ONCE = "at_most_once"
    AT_LEAST_ONCE = "at_least_once"
    EXACTLY_ONCE = "exactly_once"


@dataclass(frozen=True)
class ConnectionSpec:
    """Connection details for a source. Use ``secretRef`` for credentials —
    inline secrets are validator-rejected.
    """

    raw: Dict[str, Any]

    @property
    def secret_ref(self) -> Optional[str]:
        return self.raw.get("secretRef")

    @property
    def uri(self) -> Optional[str]:
        return self.raw.get("uri")

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)


@dataclass(frozen=True)
class WatermarkSpec:
    strategy: str  # "high_water_mark" | "log_position" | "lsn"
    allowed_lateness: Optional[str] = None  # ISO-8601 duration


@dataclass(frozen=True)
class ReaderSpec:
    format: Optional[str] = None  # "csv" | "parquet" | "json" | "ndjson" | "avro" | "orc"
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceSpec:
    """Typed view of `properties.source` in an acquisition build."""

    kind: str
    mode: AcquisitionMode
    connection: ConnectionSpec
    cursor_field: Optional[str] = None
    watermark: Optional[WatermarkSpec] = None
    streams: List[str] = field(default_factory=list)
    reader: Optional[ReaderSpec] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SourceSpec":
        watermark = None
        if "watermark" in d and d["watermark"]:
            watermark = WatermarkSpec(
                strategy=d["watermark"]["strategy"],
                allowed_lateness=d["watermark"].get("allowedLateness"),
            )
        reader = None
        if "reader" in d and d["reader"]:
            reader = ReaderSpec(
                format=d["reader"].get("format"),
                options=dict(d["reader"].get("options", {})),
            )
        return cls(
            kind=d["kind"],
            mode=AcquisitionMode(d["mode"]),
            connection=ConnectionSpec(raw=dict(d.get("connection", {}))),
            cursor_field=d.get("cursor_field"),
            watermark=watermark,
            streams=list(d.get("streams", [])),
            reader=reader,
        )


@dataclass(frozen=True)
class SinkSpec:
    """Typed view of `properties.sink` in an acquisition build. Note that
    ``binding.platform`` on the expose is the source of truth for *where*;
    SinkSpec is *how* (format/catalog/partition).
    """

    format: Optional[str] = None
    catalog: Optional[str] = None
    partition_by: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> "SinkSpec":
        if not d:
            return cls()
        return cls(
            format=d.get("format"),
            catalog=d.get("catalog"),
            partition_by=list(d.get("partitionBy", [])),
        )
