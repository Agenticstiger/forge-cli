# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Pre-land hook chain. Hooks run on each batch before destination write.

Built-ins: dlp_scan, tokenize_pii, quality_gate, emit_lineage_input.
Third parties may register additional hooks via the entry-point group
``fluid_build.preland_hooks``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class HookResult:
    """Outcome of one hook over one batch."""

    records: List[Dict[str, Any]]
    classifications: Dict[str, List[str]] = field(default_factory=dict)  # column -> labels
    dropped: List[Dict[str, Any]] = field(default_factory=list)
    dlq: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class PreLandHook(Protocol):
    """Pre-land hook contract. Hooks are pure functions of (records, ctx)."""

    name: str

    def apply(self, records: List[Dict[str, Any]], ctx: Dict[str, Any]) -> HookResult: ...


@dataclass
class HookChain:
    """Ordered chain of pre-land hooks. Runner invokes ``run(records)`` per batch."""

    hooks: List[PreLandHook]

    def run(
        self, records: List[Dict[str, Any]], ctx: Optional[Dict[str, Any]] = None
    ) -> HookResult:
        ctx = ctx or {}
        result = HookResult(records=records)
        for hook in self.hooks:
            step = hook.apply(result.records, ctx)
            result.records = step.records
            for col, labels in step.classifications.items():
                existing = result.classifications.setdefault(col, [])
                for lbl in labels:
                    if lbl not in existing:
                        existing.append(lbl)
            result.dropped.extend(step.dropped)
            result.dlq.extend(step.dlq)
            result.metadata.update(step.metadata)
        return result
