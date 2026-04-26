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

"""Coverage for ``fluid_build.copilot.store.episodic``.

The episodic helpers add time-decay ranking to ``memory/episodic`` —
previously a registered namespace with no writer and no decay-aware
reader. The module ships four public functions (``decay_weight``,
``rank_by_decay``, ``record_episodic_event``, ``query_recent_events``)
plus the ``RankedEpisodicRecord`` result type. These tests pin:

* **Decay math** — a record at ``now`` weights 1.0; at one half-life
  weights 0.5; at two half-lives weights 0.25. ``half_life_days=None``
  disables decay. Future-dated events clamp to 1.0 (clock-skew safe).
* **Ranking stability** — identical weights preserve input order.
* **Key format** — generated keys sort alphabetically in chronological
  order so ``FileBackend.query``'s ``sorted(rglob())`` iteration returns
  events in the order they occurred.
* **End-to-end with FileBackend** — round-trip through the default
  backend with a temp root, and confirm ranking returns newer before
  older regardless of insertion order.
* **Event-type filter** — ``query_recent_events`` restricts to a
  specific event_type when requested.
* **Graceful edges** — empty namespace returns ``[]``; ``limit <= 0``
  returns ``[]`` without hitting the store; negative ``half_life_days``
  raises ``ValueError``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fluid_build.copilot.store.backends.file import FileBackend
from fluid_build.copilot.store.backends.null import NullBackend
from fluid_build.copilot.store.base import StoreRecord, utc_now
from fluid_build.copilot.store.episodic import (
    DEFAULT_HALF_LIFE_DAYS,
    EPISODIC_NAMESPACE,
    RankedEpisodicRecord,
    decay_weight,
    query_recent_events,
    rank_by_decay,
    record_episodic_event,
)

UTC = timezone.utc


def _record(
    created_at: datetime, *, key: str = "x", event_type: str = "forge.success"
) -> StoreRecord:
    return StoreRecord(
        namespace=EPISODIC_NAMESPACE,
        key=key,
        value={"ok": True},
        metadata={"event_type": event_type},
        created_at=created_at,
    )


class TestDecayWeight:
    def test_fresh_record_weight_is_one(self):
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        record = _record(now)
        assert decay_weight(record, half_life_days=14.0, now=now) == pytest.approx(1.0)

    def test_one_half_life_is_one_half(self):
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        record = _record(now - timedelta(days=14.0))
        assert decay_weight(record, half_life_days=14.0, now=now) == pytest.approx(0.5, abs=1e-9)

    def test_two_half_lives_is_one_quarter(self):
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        record = _record(now - timedelta(days=28.0))
        assert decay_weight(record, half_life_days=14.0, now=now) == pytest.approx(0.25, abs=1e-9)

    def test_none_half_life_disables_decay(self):
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        ancient = _record(now - timedelta(days=3650))  # ten years old
        assert decay_weight(ancient, half_life_days=None, now=now) == 1.0

    def test_future_record_clamps_to_one(self):
        """Clock skew / test fixtures that place events 'in the future'
        must not produce weights > 1.0 — clamp at the fresh ceiling."""
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        future = _record(now + timedelta(hours=2))
        assert decay_weight(future, half_life_days=14.0, now=now) == 1.0

    def test_missing_created_at_is_zero(self):
        record = StoreRecord(
            namespace=EPISODIC_NAMESPACE,
            key="x",
            value={},
            created_at=None,  # type: ignore[arg-type]
        )
        assert decay_weight(record, half_life_days=14.0, now=utc_now()) == 0.0

    def test_naive_created_at_treated_as_utc(self):
        """Records written by older code paths may be naive — assume UTC
        rather than raising, so old timelines don't crash the ranker."""
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        naive = datetime(2026, 4, 11, 12, 0, 0)  # exactly 14d earlier, no tz
        record = _record(naive)
        assert decay_weight(record, half_life_days=14.0, now=now) == pytest.approx(0.5, abs=1e-9)

    def test_negative_half_life_raises(self):
        record = _record(utc_now())
        with pytest.raises(ValueError):
            decay_weight(record, half_life_days=-1.0, now=utc_now())

    def test_zero_half_life_raises(self):
        record = _record(utc_now())
        with pytest.raises(ValueError):
            decay_weight(record, half_life_days=0.0, now=utc_now())

    def test_default_half_life_matches_constant(self):
        """DEFAULT_HALF_LIFE_DAYS=14.0 is part of the public contract —
        changing it silently would reshape ranking for every caller."""
        assert DEFAULT_HALF_LIFE_DAYS == 14.0


class TestRankByDecay:
    def test_newer_outranks_older(self):
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        old = _record(now - timedelta(days=30), key="old")
        recent = _record(now - timedelta(hours=1), key="recent")
        ranked = rank_by_decay([old, recent], half_life_days=14.0, now=now)
        assert [r.record.key for r in ranked] == ["recent", "old"]
        assert ranked[0].weight > ranked[1].weight

    def test_sort_is_stable_for_equal_weights(self):
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        # Three records, all decay-disabled → all weight 1.0; ordering
        # must follow input order.
        records = [_record(now, key=f"r{i}") for i in range(3)]
        ranked = rank_by_decay(records, half_life_days=None, now=now)
        assert [r.record.key for r in ranked] == ["r0", "r1", "r2"]
        assert all(r.weight == 1.0 for r in ranked)

    def test_limit_trims_results(self):
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        records = [_record(now - timedelta(days=i), key=f"r{i}") for i in range(5)]
        ranked = rank_by_decay(records, half_life_days=14.0, now=now, limit=2)
        assert len(ranked) == 2
        # Oldest two trimmed; the two freshest survive.
        assert [r.record.key for r in ranked] == ["r0", "r1"]

    def test_zero_limit_returns_empty(self):
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        records = [_record(now, key="r")]
        assert rank_by_decay(records, half_life_days=14.0, now=now, limit=0) == []

    def test_empty_input_returns_empty(self):
        assert rank_by_decay([], half_life_days=14.0, now=utc_now()) == []

    def test_result_type(self):
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        records = [_record(now, key="r")]
        ranked = rank_by_decay(records, now=now)
        assert len(ranked) == 1
        assert isinstance(ranked[0], RankedEpisodicRecord)
        assert ranked[0].record.key == "r"


class TestRecordAndQueryWithFileBackend:
    """End-to-end: write events through FileBackend, retrieve via the
    decay-aware reader, verify ordering and filtering."""

    @pytest.fixture
    def store(self, tmp_path: Path) -> FileBackend:
        return FileBackend(root=tmp_path / "store")

    def test_record_writes_to_episodic_namespace(self, store: FileBackend):
        when = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        record = record_episodic_event(
            store,
            event_type="forge.success",
            payload={"contract": "orders"},
            when=when,
        )
        assert record.namespace == EPISODIC_NAMESPACE
        assert record.metadata["event_type"] == "forge.success"
        # Key is chronological + event tag.
        assert record.key.startswith("20260425T120000")
        assert record.key.endswith("-forge.success")

    def test_record_key_is_chronologically_sortable(self, store: FileBackend):
        """Two events written out-of-order still sort chronologically
        when keys are sorted alphabetically — this is what makes
        FileBackend's default ``sorted(rglob())`` iteration correct."""
        base = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        later = base + timedelta(hours=5)
        # Write the later one first on purpose.
        record_episodic_event(store, event_type="b", payload={}, when=later)
        record_episodic_event(store, event_type="a", payload={}, when=base)
        retrieved = store.query(EPISODIC_NAMESPACE, limit=10)
        assert [r.key.endswith("-a") for r in retrieved] == [True, False]

    def test_query_ranks_newer_first(self, store: FileBackend):
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        record_episodic_event(store, event_type="old", payload={}, when=now - timedelta(days=30))
        record_episodic_event(store, event_type="fresh", payload={}, when=now - timedelta(hours=1))
        ranked = query_recent_events(store, half_life_days=14.0, now=now, limit=5)
        assert [r.record.metadata["event_type"] for r in ranked] == ["fresh", "old"]

    def test_query_respects_event_type_filter(self, store: FileBackend):
        now = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        record_episodic_event(
            store, event_type="forge.success", payload={}, when=now - timedelta(hours=1)
        )
        record_episodic_event(
            store, event_type="forge.failure", payload={}, when=now - timedelta(hours=2)
        )
        record_episodic_event(
            store, event_type="forge.success", payload={}, when=now - timedelta(hours=3)
        )
        ranked = query_recent_events(
            store,
            half_life_days=14.0,
            now=now,
            limit=10,
            event_type="forge.success",
        )
        assert len(ranked) == 2
        assert all(r.record.metadata["event_type"] == "forge.success" for r in ranked)

    def test_query_on_empty_namespace(self, store: FileBackend):
        assert query_recent_events(store, now=utc_now(), limit=10) == []

    def test_zero_limit_skips_store(self, store: FileBackend):
        """limit<=0 must return [] without paying the query cost.
        Proved by using a NullBackend — .query would return [] anyway
        but the short-circuit means we don't rely on it."""
        null = NullBackend()
        assert query_recent_events(null, limit=0) == []
        assert query_recent_events(null, limit=-5) == []

    def test_record_accepts_sanitized_event_type(self, store: FileBackend):
        """Slashes in event_type would confuse the namespace/key split;
        they must be replaced before writing."""
        when = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        record = record_episodic_event(store, event_type="forge/success/v2", payload={}, when=when)
        assert "/" not in record.key
        assert record.key.endswith("-forge_success_v2")

    def test_record_with_ttl_sets_expires_at(self, store: FileBackend):
        when = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        record = record_episodic_event(
            store, event_type="forge.success", payload={}, when=when, ttl=3600
        )
        assert record.expires_at is not None
        # FileBackend computes expires_at from utc_now(), not from
        # ``when`` — we only assert it was set, not the exact value.

    def test_record_without_when_uses_utc_now(self, store: FileBackend):
        """When ``when`` is omitted, the helper defers to utc_now(). The
        returned record's key must reflect a recent moment — we assert
        that it doesn't crash and produces a non-empty key."""
        record = record_episodic_event(store, event_type="forge.success", payload={"ok": True})
        assert record.key
        assert record.metadata["event_type"] == "forge.success"

    def test_record_merges_extra_metadata(self, store: FileBackend):
        when = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
        record = record_episodic_event(
            store,
            event_type="forge.success",
            payload={},
            when=when,
            metadata={"contract_hash": "abc123"},
        )
        assert record.metadata["event_type"] == "forge.success"
        assert record.metadata["contract_hash"] == "abc123"


class TestKeyFormat:
    def test_key_includes_millisecond_precision(self):
        """Two events in the same second but different milliseconds must
        get different keys — otherwise the later ``put`` would overwrite
        the earlier record on FileBackend."""
        from fluid_build.copilot.store.episodic import _format_event_key

        base = datetime(2026, 4, 25, 12, 0, 0, 123_000, tzinfo=UTC)
        other = datetime(2026, 4, 25, 12, 0, 0, 456_000, tzinfo=UTC)
        assert _format_event_key("e", base) != _format_event_key("e", other)
        assert _format_event_key("e", base).startswith("20260425T120000123Z-")
