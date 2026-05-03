# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Public Provider Protocol.

Providers manage **declarative resources** (Airbyte sources/destinations/
connections, Kafka Connect connectors, dlt datasets, …). They implement the
plan/apply lifecycle: ``plan`` diffs declared vs current state and emits
``PlanAction`` instances; ``apply`` executes them and returns ``ApplyResult``.

Each emitted ``PlanAction`` carries BOTH ``op`` and ``action_type`` per
the existing dispatcher contract (see CLAUDE.md): apply.py reads ``op``;
display reads ``action_type``. Drop either and the pipeline silently
breaks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Protocol, Tuple


@dataclass(frozen=True)
class PlanAction:
    """One mutation the provider intends to apply. ``op`` is the dispatch key;
    ``action_type`` is the display key. Both required for any new action.
    """

    op: str  # e.g. "create", "update", "delete", "noop" — what apply dispatches on
    action_type: str  # e.g. "airbyte_source", "kc_connector" — what display renders
    resource_id: str
    payload: Dict[str, Any] = field(default_factory=dict)
    category: str = "provider"  # "infra" | "provider" | "build"
    artifact_digest: Optional[str] = None  # set when this action references a generated artifact


@dataclass
class ApplyResult:
    succeeded: List[PlanAction] = field(default_factory=list)
    failed: List[Tuple[PlanAction, str]] = field(default_factory=list)
    skipped: List[PlanAction] = field(default_factory=list)
    facets: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_partial(self) -> bool:
        return bool(self.failed) and bool(self.succeeded)

    @property
    def is_total_failure(self) -> bool:
        return bool(self.failed) and not self.succeeded


class Provider(Protocol):
    """Provider Protocol — manages declarative resources for one engine class."""

    name: ClassVar[str]
    manages: ClassVar[FrozenSet[str]]  # resource kinds this provider owns

    def plan(self, contract: Dict[str, Any]) -> List[PlanAction]:
        """Diff declared vs current state; return ordered actions."""
        ...

    def apply(self, actions: List[PlanAction]) -> ApplyResult:
        """Execute actions. Idempotency on action.resource_id is required."""
        ...

    def validate_sovereignty(self, contract: Dict[str, Any]) -> List[str]:
        """Return a list of sovereignty violations (empty list = OK)."""
        ...
