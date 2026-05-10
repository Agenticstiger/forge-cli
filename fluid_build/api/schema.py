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
    # Marks a fingerprint that the runner could not compute from real source
    # schema (e.g. dlt resolves columns inside ``pipeline.run`` and emits
    # stream names only at fingerprint() time). The shared schema-evolution
    # gate must SKIP comparison for placeholder fingerprints — otherwise
    # stream names get treated as real columns and every contract column
    # shows up as ``removed→fail``. See
    # ``fluid_build.build_runners._acquisition_common.enforce_schema_policy_or_raise``.
    is_placeholder: bool = False

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

    @classmethod
    def placeholder(
        cls,
        streams: List[str],
        *,
        engine: str,
        captured_at: Optional[str] = None,
    ) -> "SchemaFingerprint":
        """Return a placeholder fingerprint for runners that can't introspect
        the source schema cheaply (dlt, debezium, airbyte, meltano,
        kafka_connect — anything code-as-config). The columns slot still
        carries the stream names tagged with the engine, so observability
        tooling keeps per-stream visibility — but ``is_placeholder=True``
        tells the schema-evolution gate to skip the contract-vs-current
        comparison rather than misreading stream names as real columns.

        For runners that DO introspect (e.g. duckdb), use
        :meth:`SchemaFingerprint.of`.
        """
        cols = [SchemaColumn(name=s, type=engine, nullable=True) for s in streams]
        # Reuse `of()` for the digest so the value is still deterministic
        # over the placeholder content (useful for change detection between
        # placeholder snapshots, e.g. when ``streams[]`` changes).
        fp = cls.of(cols, captured_at=captured_at)
        return cls(
            digest=fp.digest,
            columns=fp.columns,
            captured_at=fp.captured_at,
            is_placeholder=True,
        )


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
