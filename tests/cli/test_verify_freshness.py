# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The freshness dimension of ``fluid verify`` (``cli/_verify_freshness.py``), without AWS.

The module reads no account: it turns the contract's ``qos.freshnessSLO`` and the
one result row of the ``$snapshots`` query into a dimension. The end-to-end
behavior, with the queries answered by stubbed Athena clients, is pinned in
``test_verify_athena.py``.
"""

from __future__ import annotations

from typing import Any, List, Optional

import pytest

from fluid_build.cli._verify_freshness import (
    FIX,
    LAKE_FORMATION_HINT,
    SnapshotReading,
    declared_objective,
    error_dimension,
    explain_failure,
    freshness_dimension,
    objective_seconds,
    read_snapshot_row,
    severity_problem,
    skipped_dimension,
    snapshot_sql,
)

pytestmark = pytest.mark.unit

HOUR = 3600
LAST_COMMIT_MS = 1_763_712_000_000  # 2025-11-21T08:00:00+00:00


def _reading(age_seconds: float, *, snapshot_id: int = 4242, operation: str = "append"):
    return SnapshotReading(
        last_commit_ms=LAST_COMMIT_MS,
        age_ms=int(age_seconds * 1000),
        snapshot_id=snapshot_id,
        operation=operation,
        counted=3,
    )


NO_WRITE = SnapshotReading(None, None, None, None, 0)


def _rows(*cells: Optional[str]) -> List[Any]:
    header = {"Data": [{"VarCharValue": f"_col{i}"} for i in range(5)]}
    return [header, {"Data": [{} if cell is None else {"VarCharValue": cell} for cell in cells]}]


# ── The objective ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "expose, expected",
    [
        pytest.param({"qos": {"freshnessSLO": "PT6H"}}, "PT6H", id="declared"),
        pytest.param({"qos": {"freshnessSLO": "  PT6H "}}, "PT6H", id="trimmed"),
        pytest.param({"qos": {"availability": "99.9%"}}, None, id="a-qos-without-it"),
        pytest.param({"qos": {"freshnessSLO": ""}}, None, id="blank"),
        pytest.param({"qos": {"freshnessSLO": 6}}, None, id="not-text"),
        pytest.param({"qos": "PT6H"}, None, id="qos-not-a-mapping"),
        pytest.param({}, None, id="no-qos"),
    ],
)
def test_the_declared_objective(expose, expected):
    assert declared_objective(expose) == expected


@pytest.mark.parametrize(
    "objective, seconds",
    [
        ("PT6H", 6 * HOUR),
        ("P1D", 86400),
        ("PT90M", 5400),
        ("PT1H30M", 5400),
        ("PT30S", 30),
        ("P1W", 7 * 86400),
        ("P2W", 14 * 86400),
        ("P1W2D", 9 * 86400),
        ("P1WT6H", 7 * 86400 + 6 * HOUR),
        ("P1W0D", 7 * 86400),  # a zero tail adds nothing: the schema takes it
        ("P1WT0S", 7 * 86400),
        ("P1WT0H0M0S", 7 * 86400),
        ("P1W0DT0H", 7 * 86400),
        ("P1WT0.0S", 7 * 86400),
        ("P1WT", None),
        ("P1WT0X", None),
        ("P0W", None),
        ("P0W0D", None),
        ("P1M", None),  # a month has no length without a calendar
        ("P1Y", None),
        ("PT0S", None),
        ("6 hours", None),
        ("", None),
        ("P1.5W", None),
        ("P1Y2W", None),
        ("P" + "9" * 30 + "D", None),  # more than a timedelta holds
        ("P" + "9" * 30 + "W", None),
        ("PT" + "9" * 400 + "S", None),
        ("P1WP1W", None),
    ],
)
def test_the_objective_as_seconds(objective, seconds):
    assert objective_seconds(objective) == seconds


# ── The query and its answer ────────────────────────────────────────────


def test_the_query_excludes_compactions_and_quotes_the_metadata_table_as_one_name():
    sql = snapshot_sql("demo_bronze", "customer_subscriptions")

    assert sql == (
        "SELECT CAST(to_unixtime(max(committed_at)) * 1000 AS bigint), "
        "date_diff('millisecond', max(committed_at), current_timestamp), "
        "max_by(snapshot_id, committed_at), max_by(operation, committed_at), COUNT(*) "
        'FROM "demo_bronze"."customer_subscriptions$snapshots" '
        "WHERE operation IS DISTINCT FROM 'replace'"
    )


def test_a_reading_from_the_result_rows():
    reading = read_snapshot_row(_rows("1763712000000", "7200000", "4242", "append", "3"))

    assert reading == SnapshotReading(1_763_712_000_000, 7_200_000, 4242, "append", 3)


def test_no_write_is_a_row_of_nulls_and_a_zero_count():
    assert read_snapshot_row(_rows(None, None, None, None, "0")) == NO_WRITE


def test_a_row_with_no_snapshot_id_is_still_a_reading():
    reading = read_snapshot_row(_rows("1763712000000", "7200000", None, "append", "1"))

    assert reading.snapshot_id is None and reading.last_commit_ms == 1_763_712_000_000


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param([], id="no-rows"),
        pytest.param(_rows("1", "2", "3", "append", "4")[:1], id="header-only"),
        pytest.param(_rows("1", "2", "3", "append"), id="four-cells"),
        pytest.param(_rows("1763712000000", "soon", "4242", "append", "3"), id="age-not-a-number"),
        pytest.param(_rows("1763712000000", None, "4242", "append", "3"), id="time-without-age"),
        pytest.param(_rows("1763712000000", "7200000", "4242", "append", None), id="no-count"),
        pytest.param([{}, {}], id="no-data"),
        # What a writer of the table can make Athena return is bounded, not trusted:
        pytest.param(_rows("1763712000000000", "7200000", "1", "append", "1"), id="micros-as-ms"),
        pytest.param(_rows("253402300800000", "7200000", "1", "append", "1"), id="year-10000"),
        pytest.param(_rows("-62135596800001", "7200000", "1", "append", "1"), id="year-0"),
        pytest.param(_rows("9" * 40, "7200000", "1", "append", "1"), id="absurd-commit-time"),
        pytest.param(_rows("1763712000000", "9" * 40, "1", "append", "1"), id="absurd-age"),
        pytest.param(
            _rows("1763712000000", str(2**53 + 1), "1", "append", "1"), id="age-over-2-53"
        ),
    ],
)
def test_a_result_that_is_not_a_reading_is_refused(rows):
    with pytest.raises(ValueError):
        read_snapshot_row(rows)


@pytest.mark.parametrize(
    "operation, kept",
    [
        ("append", "append"),
        ("overwrite", "overwrite"),
        ("delete", "delete"),
        ("custom-op_2.1", "custom-op_2.1"),
        ("[/bold]x[red]y", None),  # Rich markup
        ("append\x1b[31m", None),  # an escape sequence
        ("append\n   PASS - forged", None),  # a forged line
        ("\x1b]0;title\x07", None),
        ("x" * 33, None),
        ("", None),
        (None, None),
    ],
)
def test_the_operation_is_kept_only_when_it_looks_like_one(operation, kept):
    """An operation is data a writer of the table chose; it is printed, so it is bounded."""
    reading = read_snapshot_row(_rows("1763712000000", "7200000", "4242", operation, "3"))

    assert reading.operation == kept
    assert reading.last_commit_ms == 1_763_712_000_000


# ── The verdict ─────────────────────────────────────────────────────────


def test_a_table_within_its_objective_passes():
    dimension = freshness_dimension("PT6H", 6 * HOUR, _reading(2 * HOUR))

    assert dimension["status"] == "pass"
    assert dimension["message"] == "last write 2h ago (snapshot 4242, append), within PT6H"
    assert dimension["age_seconds"] == 2 * HOUR
    assert dimension["last_commit"] == "2025-11-21T08:00:00+00:00"
    assert (dimension["snapshot_id"], dimension["operation"]) == (4242, "append")
    assert dimension["snapshots_counted"] == 3
    assert (dimension["objective"], dimension["objective_seconds"]) == ("PT6H", 6 * HOUR)
    assert "fix" not in dimension


def test_the_limit_itself_passes_and_a_second_over_fails():
    assert freshness_dimension("PT6H", 6 * HOUR, _reading(6 * HOUR))["status"] == "pass"
    assert freshness_dimension("PT6H", 6 * HOUR, _reading(6 * HOUR + 1))["status"] == "fail"


def test_a_table_older_than_its_objective_fails_with_the_age_and_a_fix():
    dimension = freshness_dimension(
        "PT6H", 6 * HOUR, _reading(2 * 86400 + 5 * HOUR, operation="overwrite")
    )

    assert dimension["status"] == "fail"
    assert dimension["message"] == (
        "last write 2d 5h ago (snapshot 4242, overwrite); the objective is PT6H"
    )
    assert dimension["fix"] == FIX


@pytest.mark.parametrize(
    "seconds, text",
    [
        (0, "0s"),
        (45, "45s"),
        (60, "1m"),
        (61, "1m 1s"),
        (3601, "1h"),
        (3661, "1h 1m"),
        (7205, "2h"),
        (7380, "2h 3m"),
        (86400, "1d"),
        (90061, "1d 1h"),
        (2 * 86400 + 5, "2d"),
    ],
)
def test_the_age_is_written_in_its_largest_unit_and_the_one_below_it(seconds, text):
    message = freshness_dimension("P30D", 30 * 86400, _reading(seconds))["message"]

    assert message.startswith(f"last write {text} ago")


def test_a_snapshot_with_no_id_or_operation_is_still_a_write():
    reading = SnapshotReading(LAST_COMMIT_MS, 2 * HOUR * 1000, None, None, 1)

    dimension = freshness_dimension("PT6H", 6 * HOUR, reading)

    assert dimension["status"] == "pass"
    assert dimension["message"] == "last write 2h ago, within PT6H"
    assert dimension["snapshot_id"] is None and dimension["operation"] is None


def test_only_what_is_known_of_the_snapshot_is_named():
    only_id = SnapshotReading(LAST_COMMIT_MS, 1000, 7, None, 1)
    only_operation = SnapshotReading(LAST_COMMIT_MS, 1000, None, "append", 1)

    assert freshness_dimension("PT6H", 6 * HOUR, only_id)["message"].startswith(
        "last write 1s ago (snapshot 7),"
    )
    assert freshness_dimension("PT6H", 6 * HOUR, only_operation)["message"].startswith(
        "last write 1s ago (append),"
    )


def test_a_commit_dated_after_athenas_clock_has_the_age_zero():
    """A writer's clock ahead of Athena's gives a negative age; it is not fresher than now."""
    dimension = freshness_dimension("PT6H", 6 * HOUR, _reading(-30))

    assert dimension["status"] == "pass" and dimension["age_seconds"] == 0


def test_no_write_fails():
    dimension = freshness_dimension("PT6H", 6 * HOUR, NO_WRITE)

    assert dimension["status"] == "fail" and dimension["fix"] == FIX
    assert "no snapshot records a write" in dimension["message"]
    assert "expired down to compactions" in dimension["message"]
    assert dimension["snapshots_counted"] == 0
    assert "age_seconds" not in dimension and "last_commit" not in dimension


def test_no_write_on_a_reference_only_table_is_skipped():
    dimension = freshness_dimension("PT6H", 6 * HOUR, NO_WRITE, reference_only=True)

    assert dimension["status"] == "skip"
    assert "reference-only" in dimension["message"] and "fix" not in dimension


def test_reference_only_does_not_excuse_an_old_write():
    assert (
        freshness_dimension("PT6H", 6 * HOUR, _reading(9 * HOUR), reference_only=True)["status"]
        == "fail"
    )


# ── The other dimensions and the grading ────────────────────────────────


def test_a_skipped_and_an_errored_dimension_name_the_objective():
    skipped = skipped_dimension("P1M", "not a fixed length")
    errored = error_dimension("PT6H", "Athena could not read the snapshots")

    assert skipped == {
        "status": "skip",
        "objective": "P1M",
        "message": "not checked: not a fixed length",
    }
    assert errored == {
        "status": "error",
        "objective": "PT6H",
        "message": "Athena could not read the snapshots",
    }


@pytest.mark.parametrize(
    "message",
    [
        "Athena could not read the snapshots of d.t: query q-2 FAILED: AccessDeniedException: x",
        "Access denied by Lake Formation",
        "ACCESS DENIED",
        "Insufficient Lake Formation permission(s) on t",
    ],
)
def test_a_refusal_that_looks_like_lake_formation_says_what_to_do(message):
    assert explain_failure(message) == f"{message}. {LAKE_FORMATION_HINT}"
    assert explain_failure(message + ".").endswith(f". {LAKE_FORMATION_HINT}")
    assert ".." not in explain_failure(message + ".")
    assert "Lake Formation" in LAKE_FORMATION_HINT and "every column" in LAKE_FORMATION_HINT


@pytest.mark.parametrize(
    "message",
    [
        "Athena could not read the snapshots of d.t: query q-2 FAILED: TABLE_NOT_FOUND: x",
        "Athena query q-2 did not finish within 300s",
        "",
    ],
)
def test_any_other_failure_is_left_as_it_was(message):
    assert explain_failure(message) == message


def test_only_a_missed_objective_is_a_severity_problem():
    missed = freshness_dimension("PT6H", 6 * HOUR, _reading(9 * HOUR))

    reason, action = severity_problem(missed)
    assert reason == f"freshness objective missed: {missed['message']}"
    assert action == FIX
    for dimension in (
        freshness_dimension("PT6H", 6 * HOUR, _reading(HOUR)),
        freshness_dimension("PT6H", 6 * HOUR, NO_WRITE, reference_only=True),
        skipped_dimension("P1M", "x"),
        error_dimension("PT6H", "x"),
        None,
        {},
    ):
        assert severity_problem(dimension) is None
