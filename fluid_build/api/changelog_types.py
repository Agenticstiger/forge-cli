# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Shared types for the contract-vs-contract diff engine.

Split from ``changelog.py`` / ``changelog_rules.py`` to keep the rule helpers
free of orchestrator imports — the rules emit ``Change`` objects, the
orchestrator collects them into a ``ChangelogReport``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, List

# Severity strings — keep these stable; downstream JSON consumers index by
# the exact label.
SEV_BREAKING = "breaking"
SEV_NON_BREAKING = "non_breaking"
SEV_INFO = "info"


@dataclass
class Change:
    """A single classified change between two contract versions."""

    path: str
    kind: str
    severity: str
    description: str
    before: Any = None
    after: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChangelogReport:
    """A structured envelope grouping all detected changes by severity."""

    breaking: List[Change] = field(default_factory=list)
    non_breaking: List[Change] = field(default_factory=list)
    info: List[Change] = field(default_factory=list)

    @property
    def has_breaking(self) -> bool:
        return bool(self.breaking)

    @property
    def total(self) -> int:
        return len(self.breaking) + len(self.non_breaking) + len(self.info)

    def add(self, change: Change) -> None:
        if change.severity == SEV_BREAKING:
            self.breaking.append(change)
        elif change.severity == SEV_NON_BREAKING:
            self.non_breaking.append(change)
        elif change.severity == SEV_INFO:
            self.info.append(change)
        else:
            # Unknown severity — surface in info to avoid silent drops.
            self.info.append(change)

    def extend(self, changes: list[Change]) -> None:
        for c in changes:
            self.add(c)

    def to_dict(self) -> dict[str, Any]:
        return {
            "breaking": [c.to_dict() for c in self.breaking],
            "non_breaking": [c.to_dict() for c in self.non_breaking],
            "info": [c.to_dict() for c in self.info],
            "summary": {
                "breaking": len(self.breaking),
                "non_breaking": len(self.non_breaking),
                "info": len(self.info),
                "total": self.total,
            },
        }
