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

"""The single source of truth for the time-grain vocabulary.

This vocabulary used to live in three hand-maintained copies
(``emit/semantic_quality.py``, ``logical_canonicalizer.py``,
``emit/fluid_contract.py``) — and they drifted: the canonicalizer knew
``hr``/``hrs``/``min``/``mins`` that the other two rejected, while they
knew ``hourly``/``minutely``/``ms``/``s``/``millisecond(s)`` the
canonicalizer didn't. A value's fate depended on which module saw it
first. This module is the union of all three tables and the only place
the vocabulary may be defined — pinned by
``tests/forge_datamodel/test_time_grains.py``, which greps the tree for
stray copies.

Sub-minute inputs deliberately coarsen to ``minute``: the contract
schema's ``timeGranularity`` enum bottoms out there, and a coarser-
than-requested grain is honest while a dropped grain is silent metadata
loss.
"""

from __future__ import annotations

import re
from typing import Optional

ALLOWED_TIME_GRAINS: frozenset[str] = frozenset(
    {"day", "week", "month", "quarter", "year", "hour", "minute"}
)
"""Canonical grains — mirrors the contract schema's ``timeGranularity`` enum."""

TIME_GRAIN_ALIASES: dict[str, str] = {
    "days": "day",
    "daily": "day",
    "weeks": "week",
    "weekly": "week",
    "months": "month",
    "monthly": "month",
    "quarters": "quarter",
    "quarterly": "quarter",
    "years": "year",
    "yearly": "year",
    "hours": "hour",
    "hourly": "hour",
    "hr": "hour",
    "hrs": "hour",
    "minutes": "minute",
    "minutely": "minute",
    "min": "minute",
    "mins": "minute",
    # Sub-minute grains coarsen to the schema floor rather than dropping.
    "s": "minute",
    "ms": "minute",
    "sec": "minute",
    "secs": "minute",
    "second": "minute",
    "seconds": "minute",
    "millisecond": "minute",
    "milliseconds": "minute",
}
"""Alias → canonical grain. Union of the three formerly-drifted tables."""

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_time_grain(value: Optional[str]) -> Optional[str]:
    """Normalize free-form grain input to a canonical grain, or ``None``.

    Handles case, surrounding whitespace, ``_``/``-`` separators, a
    ``per `` prefix and `` grain`` suffix (\"per month\", \"day grain\"),
    and every alias in :data:`TIME_GRAIN_ALIASES`. Returns ``None`` for
    anything that doesn't resolve to an allowed grain — callers decide
    whether that is a lint error (semantic quality) or an omit
    (emitters must never write an enum-invalid contract).
    """
    normalized = (value or "").strip().lower().replace("_", " ").replace("-", " ")
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    normalized = normalized.removeprefix("per ").removesuffix(" grain").strip()
    normalized = TIME_GRAIN_ALIASES.get(normalized, normalized)
    return normalized if normalized in ALLOWED_TIME_GRAINS else None


def resolve_grain_alias(value: str) -> str:
    """Canonicalizer-flavoured resolution: alias → canonical, unknown
    values pass through unchanged (lowercased/stripped) so the
    semantic-quality lint can still surface them as errors with the
    author's original spelling."""
    normalized = str(value).strip().lower()
    return TIME_GRAIN_ALIASES.get(normalized, normalized)
