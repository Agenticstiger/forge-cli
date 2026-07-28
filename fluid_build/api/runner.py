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

"""Runner Protocol — the contract every ingestion runtime implements.

A Runner ingests data from an external source (described by a SourceSpec)
and lands it at a destination (described by a SinkSpec), per the contract's
acquisition pattern. State, lineage, metrics, and hooks come in via the
RunContext; the runner is otherwise a pure function of inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Dict, FrozenSet, List, Optional, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from .cost import CostTracker
    from .hooks import HookChain
    from .lineage import LineageEmitter
    from .schema import SchemaFingerprint
    from .source import SinkSpec, SourceSpec
    from .state import StateStore


class RunnerCapability(str, Enum):
    """Capabilities a runner can declare. Contract.builds[].capabilities is checked
    against runner.declared_capabilities; missing capabilities surface a typed error.
    """

    FULL_REFRESH = "full_refresh"
    INCREMENTAL_APPEND = "incremental_append"
    INCREMENTAL_DEDUP = "incremental_dedup"
    INCREMENTAL_MERGE = "incremental_merge"
    CDC = "cdc"
    STREAMING = "streaming"
    SCHEMA_DISCOVERY = "schema_discovery"
    SCHEMA_EVOLUTION = "schema_evolution"
    DLP_SCAN = "dlp_scan"
    AT_MOST_ONCE = "at_most_once"
    AT_LEAST_ONCE = "at_least_once"
    EXACTLY_ONCE = "exactly_once"


class RunState(str, Enum):
    """Run lifecycle states. Persisted; used by status/replay/retention."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"

    @property
    def terminal(self) -> bool:
        return self in {
            RunState.SUCCEEDED,
            RunState.PARTIAL,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.ARCHIVED,
        }


@dataclass(frozen=True)
class StreamResult:
    """Per-stream outcome inside a run."""

    name: str
    state: RunState
    records: int = 0
    bytes_read: int = 0
    bytes_written: int = 0
    duration_seconds: float = 0.0
    error: Optional[str] = None
    cursor_advanced: bool = False


@dataclass(frozen=True)
class RunResult:
    """Aggregate outcome of a single run."""

    run_id: str
    state: RunState
    streams: List[StreamResult] = field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    records_total: int = 0
    bytes_total: int = 0
    dlq_records: int = 0
    error: Optional[str] = None
    facets: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunPlan:
    """Dry-run preview returned by Runner.plan(ctx)."""

    streams_planned: List[str]
    estimated_records: Optional[int] = None
    estimated_bytes: Optional[int] = None
    estimated_duration_seconds: Optional[float] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class RunContext:
    """Everything a runner needs to execute one window. Built by cli/apply.py
    and passed to every runner. No global mutable state in runners.
    """

    run_id: str
    product_id: str
    build_id: str
    contract: Dict[str, Any]
    source: "SourceSpec"
    sink: "SinkSpec"
    state_store: "StateStore"
    hook_chain: "HookChain"
    lineage: "LineageEmitter"
    cost_tracker: "CostTracker"
    workdir: str
    env: Dict[str, str] = field(default_factory=dict)
    sample_rows: Optional[int] = None
    backfill_window: Optional[Dict[str, str]] = None  # {"from": ISO, "to": ISO}
    extras: Dict[str, Any] = field(default_factory=dict)


class Runner(Protocol):
    """Public Runner Protocol.

    Implementations declare a `name` (registration handle) and the set of
    capabilities they can satisfy. They expose three idempotent methods:
    ``plan``, ``run``, ``replay`` — and a ``fingerprint`` snapshot of the
    source schema.
    """

    name: ClassVar[str]
    declared_capabilities: ClassVar[FrozenSet[RunnerCapability]]
    declared_modes: ClassVar[FrozenSet[str]]  # subset of {"embedded", "bring-your-own", "managed"}

    def plan(self, ctx: RunContext) -> RunPlan:
        """Return a dry-run preview of what ``run(ctx)`` would do. Idempotent."""
        ...

    def run(self, ctx: RunContext) -> RunResult:
        """Execute one ingestion window. Idempotent under the same ``ctx.run_id``."""
        ...

    def replay(self, ctx: RunContext, run_id: str) -> RunResult:
        """Re-execute a prior run by id. Output must be byte-identical to the original
        when the source and sink state are unchanged.
        """
        ...

    def fingerprint(self, ctx: RunContext) -> "SchemaFingerprint":
        """Snapshot of the source schema for drift detection."""
        ...
