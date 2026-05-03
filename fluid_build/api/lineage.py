# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""OpenLineage emission types.

Runners emit RUN_START / RUN_COMPLETE / RUN_FAIL / RUN_ABORT events with
input/output dataset facets and run facets (capabilities, engine version,
image digest, idempotency key template, schema-change deltas, anomalies,
supply-chain, cost, chargeback).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol


class RunEventType(str, Enum):
    START = "START"
    COMPLETE = "COMPLETE"
    FAIL = "FAIL"
    ABORT = "ABORT"
    OTHER = "OTHER"


@dataclass(frozen=True)
class DatasetFacet:
    """One side of a lineage edge: identifies a dataset (source or sink)."""

    namespace: str
    name: str
    facets: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunEvent:
    event_type: RunEventType
    event_time: str  # ISO-8601
    run_id: str
    job_namespace: str
    job_name: str
    inputs: List[DatasetFacet] = field(default_factory=list)
    outputs: List[DatasetFacet] = field(default_factory=list)
    run_facets: Dict[str, Any] = field(default_factory=dict)


class LineageEmitter(Protocol):
    """Emits OpenLineage events to one or more configured backends."""

    def emit(self, event: RunEvent) -> None: ...

    def flush(self, timeout_seconds: float = 5.0) -> bool: ...
