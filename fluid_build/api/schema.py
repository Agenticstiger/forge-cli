# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Schema policy / fingerprint / evolution decision types."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class SchemaPolicy(str, Enum):
    STRICT = "strict"
    DISCOVER_AND_FREEZE = "discover_and_freeze"
    EVOLVE_SAFE = "evolve_safe"
    EVOLVE_ALL = "evolve_all"


@dataclass(frozen=True)
class SchemaColumn:
    name: str
    type: str
    nullable: bool = True


@dataclass(frozen=True)
class SchemaFingerprint:
    """Canonical sha256 of a sorted column-descriptor list. Stable across
    column reorder; sensitive to add/remove/rename/type-change.
    """

    digest: str
    columns: List[SchemaColumn] = field(default_factory=list)
    captured_at: Optional[str] = None  # ISO-8601

    @classmethod
    def of(
        cls, columns: List[SchemaColumn], captured_at: Optional[str] = None
    ) -> "SchemaFingerprint":
        canonical = sorted(
            ({"name": c.name, "type": c.type, "nullable": c.nullable} for c in columns),
            key=lambda d: str(d["name"]),
        )
        h = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode("utf-8")).hexdigest()
        return cls(digest=f"sha256:{h}", columns=list(columns), captured_at=captured_at)


class EvolutionAction(str, Enum):
    OK = "ok"
    INCLUDE = "include"
    DROP = "drop"
    CAST = "cast"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class SchemaEvolutionDecision:
    """Per-column evolution decision returned by the policy resolver."""

    column: str
    event: str  # "added" | "removed" | "type_widened" | "type_narrowed" | "renamed"
    action: EvolutionAction
    reason: str
