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

"""An incremental cursor means one instant on either DuckDB version.

The cursor is persisted as text and pasted into ``WHERE <field> > '<text>'`` as
an UNTYPED literal, so its text has to denote one instant whatever the column
resolved to. DuckDB 1.5 reads a JSON timestamp carrying ``Z`` as a naive
TIMESTAMP and 2.0 reads it as TIMESTAMP WITH TIME ZONE, so the two disagree
about an offset-bearing literal. On a host with a non-zero UTC offset that moved
the ``>`` boundary: an end-to-end run that wrote its cursor on 1.5 and continued
on 2.0 landed 5 of 6 rows and advanced the cursor past the row it skipped, at
exit code 0.

Two pieces, and both are needed:

* ``_canonical_cursor_text`` renders the stored text as a naive UTC instant.
  That is what repairs the cursors a shipped DuckDB 2.0 install already wrote --
  they carry an explicit offset -- so no state file has to be rewritten.
* ``_cursor_comparison`` compares INSTANTS (``epoch_us``) for a timestamp
  cursor, so the predicate cannot be shifted by the session time zone at all.
  Measured: normalising the text alone still lost a row, because the coercion of
  a naive literal against a TIMESTAMPTZ column is what moves, not the text.

Pinning the session zone to UTC would also have worked and is deliberately NOT
used: ``TimeZone`` is ICU-backed, the sandbox forbids autoloading extensions,
and without ICU DuckDB does not even recognise the option -- so a pin fails the
connection in exactly the hardened environments the sandbox exists for.
``epoch_us`` needs no extension.

Normalisation runs on both the read and the write side. Only the READ side is
load-bearing, and only that side is mutation-tested: it normalises whatever it
finds, so correctness does not depend on how a value was written. Doing it on
write too keeps newly stored state in one form rather than two.
"""

from __future__ import annotations

import pytest

from fluid_build.build_runners.duckdb.runner import (
    _apply_incremental_filter,
    _canonical_cursor_text,
    _cursor_column_type,
    _cursor_comparison,
)


@pytest.mark.parametrize(
    "stored, expected, why",
    [
        # The migration case: what a shipped 2.0 install at a non-UTC host wrote.
        ("2026-05-01 10:00:00-04:00", "2026-05-01 14:00:00", "legacy offset cursor -> UTC"),
        ("2026-05-01 12:00:00+02:00", "2026-05-01 10:00:00", "positive offset -> UTC"),
        # What 2.0 writes under the pin, and the spellings DuckDB renders.
        ("2026-05-01 14:00:00+00:00", "2026-05-01 14:00:00", "offset zero is still an offset"),
        ("2026-05-01 14:00:00+00", "2026-05-01 14:00:00", "two-digit offset"),
        ("2026-05-01 12:00:00+0200", "2026-05-01 10:00:00", "offset without a colon"),
        ("2026-05-01 18:00:00Z", "2026-05-01 18:00:00", "Z suffix"),
        ("2026-05-01T18:00:00Z", "2026-05-01 18:00:00", "ISO T separator"),
        # Everything that is not an offset-bearing timestamp is left alone.
        ("2026-05-01 14:00:00", "2026-05-01 14:00:00", "already naive"),
        ("42", "42", "an integer cursor"),
        ("order-000123", "order-000123", "an opaque string cursor"),
        ("abc-12:34", "abc-12:34", "offset-shaped but not a timestamp"),
        ("", "", "empty"),
    ],
)
def test_cursor_text_is_canonicalised(stored: str, expected: str, why: str) -> None:
    assert _canonical_cursor_text(stored) == expected, why


def test_a_non_string_cursor_becomes_its_own_text() -> None:
    """The persist side hands in whatever DuckDB's driver materialised."""
    assert _canonical_cursor_text(42) == "42"
    assert _canonical_cursor_text(None) == "None"


def test_canonicalising_is_idempotent() -> None:
    """A cursor written by the fixed code must survive being read by it again."""
    once = _canonical_cursor_text("2026-05-01 10:00:00-04:00")
    assert _canonical_cursor_text(once) == once


def test_the_two_stored_forms_converge_on_one_instant() -> None:
    """The whole point: 1.5's naive text and 2.0's offset text must agree.

    ``2026-05-01 14:00:00`` (what 1.5 persisted for an instant) and
    ``2026-05-01 10:00:00-04:00`` (what a shipped 2.0 install at UTC-4 persisted
    for the SAME instant) have to compare equal after canonicalisation, or an
    install that changes DuckDB version re-reads or skips rows.
    """
    from_15 = _canonical_cursor_text("2026-05-01 14:00:00")
    from_20 = _canonical_cursor_text("2026-05-01 10:00:00-04:00")
    assert from_15 == from_20 == "2026-05-01 14:00:00"


# ── the call site, not just the helper ───────────────────────────────────


class _Cursor:
    def __init__(self, value: str) -> None:
        self.stream = "evt"
        self.value = value
        self.updated_at = "2026-05-01T00:00:00Z"


class _StateStore:
    """Returns one stored cursor, as ``FileStateStore`` would after reading JSON."""

    def __init__(self, value: str) -> None:
        self._value = value

    def get_cursor(self, _product: str, _build: str, _stream: str) -> _Cursor:
        return _Cursor(self._value)


class _Source:
    def __init__(self) -> None:
        from fluid_build.api.source import AcquisitionMode

        self.mode = AcquisitionMode.INCREMENTAL_APPEND
        self.cursor_field = "event_time"


class _Ctx:
    def __init__(self, value: str) -> None:
        self.product_id = "bronze.evt"
        self.build_id = "ingest"
        self.source = _Source()
        self.state_store = _StateStore(value)


def test_the_emitted_where_clause_compares_instants() -> None:
    """Exercises the wiring, which a direct call to the helpers does not.

    Given a real connection, so the column probe answers: without this,
    removing either helper from ``_apply_incremental_filter`` leaves every
    other test in this file passing.
    """
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(":memory:")
    try:
        base = "SELECT * FROM (VALUES (TIMESTAMP '2026-05-01 20:00:00')) AS t(event_time)"
        sql, last = _apply_incremental_filter(_Ctx("2026-05-01 10:00:00-04:00"), "evt", base, con)
        assert "epoch_us(" in sql, f"a timestamp cursor must compare instants: {sql}"
        assert "'2026-05-01 14:00:00'" in sql, sql
        assert "-04:00" not in sql, f"the raw offset-bearing cursor reached the SQL: {sql}"
        # The predicate has to execute, not merely read correctly.
        assert con.execute(f"SELECT count(*) FROM ({sql})").fetchall() == [(1,)]
        # The returned value stays the value that was stored, so a caller that
        # records it sees what it persisted.
        assert last == "2026-05-01 10:00:00-04:00"
    finally:
        con.close()


def test_an_integer_cursor_keeps_the_plain_comparison() -> None:
    """``epoch_us`` of an integer column would fail, so a scalar must not take it."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(":memory:")
    try:
        base = "SELECT * FROM (VALUES (41), (43)) AS t(event_time)"
        sql, last = _apply_incremental_filter(_Ctx("42"), "evt", base, con)
        assert "epoch_us(" not in sql, sql
        assert "'42'" in sql, sql
        assert con.execute(f"SELECT count(*) FROM ({sql})").fetchall() == [(1,)]
        assert last == "42"
    finally:
        con.close()


def test_without_a_connection_the_predicate_stays_as_it_was() -> None:
    """No probe, no instant path: the pre-change comparison, never a broken one."""
    sql, _ = _apply_incremental_filter(_Ctx("2026-05-01 14:00:00"), "evt", "SELECT * FROM src")
    assert "epoch_us(" not in sql, sql
    assert "'2026-05-01 14:00:00'" in sql, sql


@pytest.mark.parametrize(
    "column_type, instant",
    [
        # epoch_us accepts exactly these, measured identically on 1.5.6 and 2.0.0.
        ("TIMESTAMP", True),
        ("TIMESTAMP WITH TIME ZONE", True),
        ("TIMESTAMPTZ", True),
        ("TIMESTAMP_MS", True),
        ("TIMESTAMP_NS", True),
        ("DATE", True),
        ("TIME", True),
        # Everything else raises BinderException under epoch_us, so it must not
        # be emitted. VARCHAR is the one that matters: a VARCHAR column of
        # timestamp-looking strings yields a cursor indistinguishable from a
        # real timestamp's, so deciding from the VALUE would break every
        # incremental run on such a column.
        ("VARCHAR", False),
        ("INTEGER", False),
        ("BIGINT", False),
        ("DECIMAL(18,2)", False),
        ('"NULL"', False),
        (None, False),
    ],
)
def test_only_an_epoch_capable_column_takes_the_instant_path(
    column_type: object, instant: bool
) -> None:
    predicate = _cursor_comparison("ts", "2026-05-01 10:00:00", column_type)
    assert ("epoch_us(" in predicate) is instant, predicate


def test_a_varchar_cursor_column_still_runs_on_real_duckdb() -> None:
    """The regression the column probe exists for.

    ``epoch_us(VARCHAR)`` is a BinderException on 1.5.6 and 2.0.0, so emitting
    the instant comparison for a VARCHAR cursor column would fail every
    incremental run on it. The probe has to report VARCHAR and the predicate
    has to stay a plain comparison that actually executes.
    """
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(":memory:")
    try:
        base = "SELECT * FROM (VALUES ('2026-05-01 10:00:00'), ('2026-05-01 20:00:00')) AS t(ts)"
        assert _cursor_column_type(con, base, "ts") == "VARCHAR"
        predicate = _cursor_comparison("ts", "2026-05-01 14:00:00", "VARCHAR")
        assert "epoch_us(" not in predicate, predicate
        rows = con.execute(f"SELECT count(*) FROM ({base}) WHERE {predicate}").fetchall()
        assert rows == [(1,)], rows
    finally:
        con.close()


def test_the_probe_reads_a_real_timestamp_column_and_the_predicate_executes() -> None:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(":memory:")
    try:
        base = (
            "SELECT * FROM (VALUES (TIMESTAMP '2026-05-01 10:00:00'), "
            "(TIMESTAMP '2026-05-01 20:00:00')) AS t(ts)"
        )
        assert _cursor_column_type(con, base, "ts") == "TIMESTAMP"
        predicate = _cursor_comparison("ts", "2026-05-01 14:00:00", "TIMESTAMP")
        assert "epoch_us(" in predicate, predicate
        assert con.execute(f"SELECT count(*) FROM ({base}) WHERE {predicate}").fetchall() == [(1,)]
    finally:
        con.close()


def test_the_probe_answers_for_an_empty_source() -> None:
    """A first incremental run over a source with no rows must still bind."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(":memory:")
    try:
        base = "SELECT CAST(NULL AS TIMESTAMP) AS ts WHERE false"
        assert _cursor_column_type(con, base, "ts") == "TIMESTAMP"
    finally:
        con.close()


def test_the_probe_returns_none_rather_than_raising_on_a_bad_source() -> None:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(":memory:")
    try:
        assert _cursor_column_type(con, "SELECT * FROM nope_does_not_exist", "ts") is None
    finally:
        con.close()
