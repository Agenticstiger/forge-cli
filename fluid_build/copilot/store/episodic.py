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

"""Time-decay ranking helpers for the ``memory/episodic`` namespace.

The episodic namespace stores a timeline of recent forge-cli outcomes —
successful/failed runs, interview resumes, repair decisions. Users want
"recent" to dominate "old" without a hard cutoff, so this module applies
exponential decay with a configurable half-life when ranking query
results.

The decay formula is::

    weight = 2 ** -(age_seconds / half_life_seconds)

A half-life of 14 days means an event from 14 days ago is weighted at
0.5 of a fresh event, 28 days ago at 0.25, etc. Setting
``half_life_days=None`` disables decay entirely (every record gets
weight 1.0) which is useful for tests and for "show me the whole
timeline" queries.

The key format for episodic writes is deliberately chronological when
sorted alphabetically (``YYYYMMDDTHHMMSSmmmZ-<event_type>``) so the
default ``Store.query`` implementation — which iterates
``sorted(rglob("*.json"))`` — returns oldest-first; the decay ranker
then re-sorts by weight descending.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from fluid_build.copilot.store.base import Store, StoreRecord, utc_now
from fluid_build.copilot.store.namespaces import MEMORY_NAMESPACES

EPISODIC_NAMESPACE = "memory/episodic"
"""The canonical namespace string for episodic events."""

assert EPISODIC_NAMESPACE in MEMORY_NAMESPACES, (
    "memory/episodic must be registered in MEMORY_NAMESPACES — "
    "check fluid_build/copilot/store/namespaces.py"
)

DEFAULT_HALF_LIFE_DAYS = 14.0
"""Two weeks — matches the average sprint length and keeps a month of
history visible (weight ≈ 0.25) without drowning in stale entries."""

_SECONDS_PER_DAY = 86_400.0


@dataclass(frozen=True)
class RankedEpisodicRecord:
    """A store record paired with its decay weight."""

    record: StoreRecord
    weight: float


def _ensure_aware(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_event_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return _ensure_aware(datetime.fromisoformat(text))
    except ValueError:
        return None


def _parse_event_key_time(key: str) -> Optional[datetime]:
    if len(key) < 19:
        return None
    stamp = key[:19]
    if stamp[8:9] != "T" or stamp[18:19] != "Z":
        return None
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _event_timestamp(record: StoreRecord) -> Optional[datetime]:
    metadata_time = _parse_event_time((record.metadata or {}).get("event_time"))
    if metadata_time is not None:
        return metadata_time
    key_time = _parse_event_key_time(record.key)
    if key_time is not None:
        return key_time
    created_at = getattr(record, "created_at", None)
    return _ensure_aware(created_at) if created_at is not None else None


def decay_weight(
    record: StoreRecord,
    *,
    half_life_days: Optional[float] = DEFAULT_HALF_LIFE_DAYS,
    now: Optional[datetime] = None,
) -> float:
    """Return the exponential-decay weight for ``record``.

    Parameters
    ----------
    record:
        The store record to weight. Uses the logical episodic event time
        when present, falling back to ``record.created_at``.
    half_life_days:
        The decay half-life in days. ``None`` disables decay and every
        record gets weight ``1.0``. Must be positive when provided.
    now:
        The reference "current" timestamp. Defaults to :func:`utc_now`.
        Explicit values let tests pin deterministic results.

    Returns
    -------
    float
        A value in ``(0, 1]``. A record at ``now`` gets weight ``1.0``;
        a record at ``now - half_life_days`` gets weight ``0.5``; older
        records decay further. Records with no ``created_at`` get
        weight ``0.0`` (treated as infinitely old).
    """

    if half_life_days is None:
        return 1.0
    if half_life_days <= 0:
        raise ValueError(f"half_life_days must be positive or None, got {half_life_days!r}")

    event = _event_timestamp(record)
    if event is None:
        return 0.0

    reference = _ensure_aware(now) if now is not None else utc_now()
    age_seconds = (reference - event).total_seconds()
    # Future-dated events (clock skew / test fixtures) clamp to age=0
    # so they weight as fresh — never boost above 1.0.
    if age_seconds <= 0:
        return 1.0

    half_life_seconds = half_life_days * _SECONDS_PER_DAY
    exponent = -(age_seconds / half_life_seconds)
    # math.exp(ln(2) * exponent) equals 2 ** exponent; the explicit form
    # keeps the math readable and avoids ``0.0 ** 0.0`` edge cases on
    # very large ages where 2 ** exponent underflows to 0.0.
    return math.exp(math.log(2.0) * exponent)


def rank_by_decay(
    records: Iterable[StoreRecord],
    *,
    half_life_days: Optional[float] = DEFAULT_HALF_LIFE_DAYS,
    now: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> List[RankedEpisodicRecord]:
    """Return ``records`` re-sorted by decayed weight, descending.

    Parameters
    ----------
    records:
        Any iterable of :class:`StoreRecord` — typically the result of
        ``store.query("memory/episodic", limit=...)``.
    half_life_days:
        Forwarded to :func:`decay_weight`.
    now:
        Forwarded to :func:`decay_weight`. Pass an explicit datetime to
        make ranking deterministic across runs.
    limit:
        Optional truncation after sorting. ``None`` returns all ranked
        records.

    Returns
    -------
    list of RankedEpisodicRecord
        Stable sort: records with identical weights preserve their
        input order.
    """

    weighted: List[RankedEpisodicRecord] = [
        RankedEpisodicRecord(
            record=record,
            weight=decay_weight(record, half_life_days=half_life_days, now=now),
        )
        for record in records
    ]
    # Python's sort is stable; negating the weight keeps ties in input order.
    weighted.sort(key=lambda item: -item.weight)
    if limit is not None:
        return weighted[: max(limit, 0)]
    return weighted


def _format_event_key(event_type: str, when: datetime) -> str:
    """Build a chronologically-sortable key for an episodic event.

    Format: ``YYYYMMDDTHHMMSSmmmZ-<event_type>`` where ``mmm`` is
    milliseconds. Alphabetic ordering of these keys matches chronological
    ordering, so ``FileBackend.query`` — which uses ``sorted(rglob(...))``
    — naturally yields events in the order they occurred.
    """

    aware = _ensure_aware(when)
    stamp = aware.strftime("%Y%m%dT%H%M%S") + f"{aware.microsecond // 1000:03d}Z"
    safe_event_type = (event_type or "event").strip().replace("/", "_") or "event"
    return f"{stamp}-{safe_event_type}"


def record_episodic_event(
    store: Store,
    *,
    event_type: str,
    payload: Dict[str, Any],
    ttl: Optional[int] = None,
    when: Optional[datetime] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> StoreRecord:
    """Append an event to ``memory/episodic``.

    Parameters
    ----------
    store:
        Any :class:`Store` implementation.
    event_type:
        A short tag identifying the event (``"forge.success"``,
        ``"forge.failure"``, ``"repair.attempt"``, …). Used in the key
        for debuggability; does not affect ranking.
    payload:
        Arbitrary JSON-serialisable dict recorded as the record value.
    ttl:
        Optional TTL in seconds. ``None`` keeps the event forever (decay
        alone down-ranks ancient events).
    when:
        Event timestamp. Defaults to :func:`utc_now`. Tests pass an
        explicit value to pin deterministic keys.
    metadata:
        Optional metadata stored alongside the record.

    Returns
    -------
    StoreRecord
        The record returned by ``store.put``.
    """

    event_when = _ensure_aware(when) if when is not None else utc_now()
    key = _format_event_key(event_type, event_when)
    enriched_metadata: Dict[str, Any] = {
        "event_type": event_type,
        "event_time": event_when.isoformat(),
    }
    if metadata:
        enriched_metadata.update(metadata)
    return store.put(
        EPISODIC_NAMESPACE,
        key,
        payload,
        ttl=ttl,
        metadata=enriched_metadata,
    )


def query_recent_events(
    store: Store,
    *,
    half_life_days: Optional[float] = DEFAULT_HALF_LIFE_DAYS,
    limit: int = 10,
    now: Optional[datetime] = None,
    event_type: Optional[str] = None,
    pool_multiplier: int = 5,
    min_pool: int = 50,
) -> List[RankedEpisodicRecord]:
    """Return the top ``limit`` recent episodic events by decayed weight.

    Parameters
    ----------
    store:
        Any :class:`Store` implementation.
    half_life_days:
        Forwarded to :func:`decay_weight`.
    limit:
        Upper bound on returned results.
    now:
        Reference timestamp; defaults to :func:`utc_now`.
    event_type:
        If given, only records whose ``metadata.event_type`` matches
        are considered. Filtering happens *before* ranking so the
        returned set is always of the requested kind.
    pool_multiplier, min_pool:
        Ranking works over a pool larger than ``limit`` to avoid
        premature truncation by the backend's own ordering. The pool
        size is ``max(limit * pool_multiplier, min_pool)``.

    Returns
    -------
    list of RankedEpisodicRecord
        Trimmed to ``limit``; empty list when the namespace is empty.
    """

    if limit <= 0:
        return []

    pool_size = max(limit * max(pool_multiplier, 1), max(min_pool, limit))
    raw = store.query(EPISODIC_NAMESPACE, limit=pool_size)
    if event_type is not None:
        raw = [record for record in raw if (record.metadata or {}).get("event_type") == event_type]
    return rank_by_decay(
        raw,
        half_life_days=half_life_days,
        now=now,
        limit=limit,
    )


__all__ = [
    "DEFAULT_HALF_LIFE_DAYS",
    "EPISODIC_NAMESPACE",
    "RankedEpisodicRecord",
    "decay_weight",
    "query_recent_events",
    "rank_by_decay",
    "record_episodic_event",
]
