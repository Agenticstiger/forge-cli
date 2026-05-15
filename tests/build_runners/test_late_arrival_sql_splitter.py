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

"""Pin the SQL late-arrival splitter — the actual enforcement helper.

Phase-3 #15 surfaced ``fluid.late_arrival.*`` as connector config in
session 1; this session adds a Python-side SQL enforcer for the
duckdb / DLT runners. The splitter must:

1. Compute the watermark as ``max(event_time)`` on the source.
2. Move rows older than ``watermark - budget`` into a side-output
   relation.
3. Delete those rows from the source.
4. Be idempotent — second call with no new data produces no change.
5. Reject malformed identifiers (path-traversal / SQL-injection
   shapes) via ``validate_ident``.
"""

from __future__ import annotations

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.build_runners._late_arrival import (
    _detect_event_time_column,
    split_late_events_in_duckdb,
)


@pytest.fixture
def con():
    """Fresh in-memory duckdb connection per test."""
    c = duckdb.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def main_table(con):
    """Pre-populate a small events table.

    Watermark = max(event_time) = 2026-04-15.
    With a 5-day budget, the threshold = 2026-04-10.
    """
    con.execute(
        """
        CREATE TABLE main_events (
            id INTEGER,
            event_time TIMESTAMP,
            payload VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO main_events VALUES
            (1, TIMESTAMP '2026-04-01 12:00:00', 'old'),
            (2, TIMESTAMP '2026-04-08 12:00:00', 'old-but-borderline'),
            (3, TIMESTAMP '2026-04-12 12:00:00', 'on-time'),
            (4, TIMESTAMP '2026-04-15 12:00:00', 'fresh')
        """
    )
    return "main_events"


class TestSplitLateEventsInDuckdb:
    def test_splits_rows_older_than_budget(self, con, main_table):
        # 5 days = 432000 seconds. Threshold = 2026-04-15 - 5d = 2026-04-10.
        # Row #1 (2026-04-01) and #2 (2026-04-08) are older → late.
        # Rows #3 (2026-04-12) and #4 (2026-04-15) → on-time.
        counts = split_late_events_in_duckdb(
            con=con,
            source_relation="main_events",
            side_output_relation="late_events",
            event_time_column="event_time",
            allowed_lateness_seconds=5 * 86400,
        )
        assert counts["late"] == 2
        assert counts["on_time"] == 2

        late_rows = con.execute("SELECT id FROM late_events ORDER BY id").fetchall()
        assert [r[0] for r in late_rows] == [1, 2]

        remaining = con.execute("SELECT id FROM main_events ORDER BY id").fetchall()
        assert [r[0] for r in remaining] == [3, 4]

    def test_idempotent_second_call_is_noop(self, con, main_table):
        first = split_late_events_in_duckdb(
            con=con,
            source_relation="main_events",
            side_output_relation="late_events",
            event_time_column="event_time",
            allowed_lateness_seconds=5 * 86400,
        )
        assert first["late"] == 2

        # Second call: nothing new is older than watermark (the watermark
        # didn't change because we removed only old rows).
        second = split_late_events_in_duckdb(
            con=con,
            source_relation="main_events",
            side_output_relation="late_events",
            event_time_column="event_time",
            allowed_lateness_seconds=5 * 86400,
        )
        assert second["late"] == 0
        assert second["on_time"] == 2

    def test_zero_budget_returns_zero_counts(self, con, main_table):
        counts = split_late_events_in_duckdb(
            con=con,
            source_relation="main_events",
            side_output_relation="late_events",
            event_time_column="event_time",
            allowed_lateness_seconds=0,
        )
        assert counts == {"on_time": 0, "late": 0}

    def test_empty_source_returns_zero_counts(self, con):
        con.execute("CREATE TABLE empty_events (id INT, event_time TIMESTAMP)")
        counts = split_late_events_in_duckdb(
            con=con,
            source_relation="empty_events",
            side_output_relation="late",
            event_time_column="event_time",
            allowed_lateness_seconds=300,
        )
        assert counts == {"on_time": 0, "late": 0}

    def test_creates_side_output_with_matching_schema(self, con, main_table):
        # Side-output table doesn't pre-exist. The splitter must create
        # it with the same schema as the source.
        split_late_events_in_duckdb(
            con=con,
            source_relation="main_events",
            side_output_relation="auto_created",
            event_time_column="event_time",
            allowed_lateness_seconds=5 * 86400,
        )
        # If creation worked, the table exists and has the right shape.
        cols = con.execute("DESCRIBE auto_created").fetchall()
        col_names = [c[0] for c in cols]
        assert col_names == ["id", "event_time", "payload"]

    def test_rejects_malformed_identifiers(self, con):
        con.execute("CREATE TABLE x (event_time TIMESTAMP)")
        with pytest.raises(Exception):
            split_late_events_in_duckdb(
                con=con,
                source_relation="x; DROP TABLE x; --",
                side_output_relation="late",
                event_time_column="event_time",
                allowed_lateness_seconds=300,
            )


class TestDetectEventTimeColumn:
    def test_explicit_wins(self):
        schema = [
            {"name": "ts", "logicalType": "timestamp"},
            {"name": "occurred_at", "logicalType": "timestamp"},
        ]
        assert _detect_event_time_column(schema, explicit="occurred_at") == "occurred_at"

    def test_canonical_name_wins_over_position(self):
        schema = [
            {"name": "first_ts", "logicalType": "timestamp"},
            {"name": "event_time", "logicalType": "timestamp"},
        ]
        assert _detect_event_time_column(schema) == "event_time"

    def test_falls_back_to_first_timestamp(self):
        schema = [
            {"name": "id", "logicalType": "integer"},
            {"name": "weird_name", "logicalType": "timestamp"},
        ]
        assert _detect_event_time_column(schema) == "weird_name"

    def test_returns_none_when_no_timestamp(self):
        schema = [{"name": "id", "logicalType": "integer"}]
        assert _detect_event_time_column(schema) is None

    def test_returns_none_on_garbage(self):
        assert _detect_event_time_column(None) is None  # type: ignore[arg-type]
        assert _detect_event_time_column("not a list") is None  # type: ignore[arg-type]
