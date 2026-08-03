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

"""Pre-land quality gates + anomaly signals."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol


class QualityRule(str, Enum):
    NOT_NULL = "not_null"
    UNIQUE = "unique"
    REGEX = "regex"
    RANGE = "range"
    ROW_COUNT_ANOMALY = "row_count_anomaly"
    FRESHNESS = "freshness"


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class QualityGate:
    rule: QualityRule
    severity: Severity
    columns: List[str] = field(default_factory=list)
    column: Optional[str] = None
    pattern: Optional[str] = None
    min: Optional[float] = None
    max: Optional[float] = None
    algorithm: Optional[str] = None  # for ROW_COUNT_ANOMALY: "ewma" | "iqr" | "exact"
    sensitivity: Optional[float] = None


@dataclass
class QualityResult:
    passed: List[QualityGate] = field(default_factory=list)
    failed: List[tuple[QualityGate, str]] = field(default_factory=list)
    routed_to_dlq: int = 0
    aborted: bool = False


class AnomalySignal(str, Enum):
    RECORD_COUNT_DROP = "record_count_drop"
    RECORD_COUNT_SURGE = "record_count_surge"
    BYTES_PER_RECORD_OUTLIER = "bytes_per_record_outlier"
    LATENCY_OUTLIER = "latency_outlier"
    DLQ_RATE_SPIKE = "dlq_rate_spike"
    CURSOR_STALLED = "cursor_stalled"
    SCHEMA_FINGERPRINT_CHANGED = "schema_fingerprint_changed"


@dataclass
class AnomalyResult:
    signal: AnomalySignal
    score: float
    severity: Severity
    threshold: Optional[float] = None
    details: Dict[str, Any] = field(default_factory=dict)
