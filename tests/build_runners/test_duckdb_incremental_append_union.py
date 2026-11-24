# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``incremental_append`` to one file keeps every increment.

DuckDB's ``COPY ... TO file`` overwrites, so a second run left only its own rows while
the cursor had moved past the first run's. The runner now writes the union of the
file and the new rows to a temp file beside it and renames it over the target; the
late-arrival side file is unioned the same way.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.build_runners.duckdb import runner as runner_mod  # noqa: E402
from fluid_build.build_runners.duckdb.runner import (  # noqa: E402
    _union_rewrite,
    execute_duckdb_build,
)

pytestmark = pytest.mark.unit

PRODUCT = "sales.events"
BUILD = "land"


def _source(tmp_path: Path, rows: List[tuple]) -> None:
    lines = ["seq,event_time"] + [f"{s},{t}" for s, t in rows]
    (tmp_path / "events.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _contract(tmp_path: Path, fmt: str, *, lateness: bool = False) -> Dict[str, Any]:
    source: Dict[str, Any] = {
        "kind": "filesystem",
        "mode": "incremental_append",
        "connection": {"uri": str(tmp_path / "events.csv")},
        "reader": {"format": "csv"},
        "streams": ["events"],
        "cursor_field": "seq",
    }
    if lateness:
        source["watermark"] = {"strategy": "high_water_mark", "allowedLateness": "PT30M"}
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": PRODUCT,
        "name": "Events",
        "builds": [
            {
                "id": BUILD,
                "pattern": "acquisition",
                "engine": "duckdb",
                "properties": {"source": source, "sink": {"format": fmt}},
                "outputs": ["events"],
            }
        ],
        "exposes": [
            {
                "exposeId": "events",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": fmt,
                    "location": {"path": f"./lake/events.{fmt}"},
                },
                "contract": {
                    "schema": [
                        {"name": "seq", "type": "bigint"},
                        {"name": "event_time", "type": "timestamp"},
                    ]
                },
            }
        ],
    }


def _run(tmp_path: Path, contract: Dict[str, Any]) -> int:
    return execute_duckdb_build(contract["builds"][0], contract, tmp_path)


def _seqs(path: Path, fmt: str) -> List[int]:
    reader = "read_parquet" if fmt == "parquet" else "read_csv_auto"
    con = duckdb.connect()
    try:
        return [
            r[0] for r in con.execute(f"SELECT seq FROM {reader}('{path}') ORDER BY seq").fetchall()
        ]
    finally:
        con.close()


@pytest.mark.parametrize("fmt", ["parquet", "csv"])
def test_two_runs_hold_the_union_of_both_runs_rows(tmp_path, fmt):
    contract = _contract(tmp_path, fmt)
    _source(tmp_path, [(1, "2024-01-01 12:00:00"), (2, "2024-01-01 12:01:00")])
    assert _run(tmp_path, contract) == 0
    _source(
        tmp_path,
        [(1, "2024-01-01 12:00:00"), (2, "2024-01-01 12:01:00"), (3, "2024-01-01 12:02:00")],
    )
    assert _run(tmp_path, contract) == 0
    lake = tmp_path / "lake"
    assert _seqs(lake / f"events.{fmt}", fmt) == [1, 2, 3]
    assert sorted(p.name for p in lake.iterdir()) == [f"events.{fmt}"]


def test_a_failed_copy_leaves_the_file_intact_and_no_temp_file(tmp_path):
    target = tmp_path / "t.parquet"
    target.write_bytes(b"not a parquet file")
    before = target.read_bytes()
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE new_rows AS SELECT 2 AS seq")
        with pytest.raises(Exception):  # noqa: B017 - the corrupt old file fails the COPY
            _union_rewrite(con, str(target), "parquet", "new_rows")
    finally:
        con.close()
    assert target.read_bytes() == before
    assert os.listdir(tmp_path) == ["t.parquet"]


def test_a_failure_after_the_copy_removes_the_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "t.parquet"
    con = duckdb.connect()
    try:
        con.execute(f"COPY (SELECT 1 AS seq) TO '{target}' (FORMAT 'parquet')")
        con.execute("CREATE TABLE new_rows AS SELECT 2 AS seq")
        before = target.read_bytes()

        def boom(*_a, **_k):
            raise OSError("replace failed")

        monkeypatch.setattr(runner_mod.os, "replace", boom)
        with pytest.raises(OSError):
            _union_rewrite(con, str(target), "parquet", "new_rows")
    finally:
        con.close()
    assert target.read_bytes() == before
    assert os.listdir(tmp_path) == ["t.parquet"]


def test_late_events_keep_every_runs_late_rows_and_a_clean_run_does_not_truncate(tmp_path):
    contract = _contract(tmp_path, "parquet", lateness=True)
    lake = tmp_path / "lake"
    side = lake / "events__late_events.parquet"

    # Run 1: seq 2 is 1h behind the newest event, so it is late.
    run1 = [(1, "2024-01-01 12:00:00"), (2, "2024-01-01 11:00:00")]
    _source(tmp_path, run1)
    assert _run(tmp_path, contract) == 0
    assert _seqs(side, "parquet") == [2]

    # Run 2: seq 4 is late against 12:10.
    run2 = run1 + [(3, "2024-01-01 12:10:00"), (4, "2024-01-01 11:20:00")]
    _source(tmp_path, run2)
    assert _run(tmp_path, contract) == 0
    assert _seqs(side, "parquet") == [2, 4]
    assert _seqs(lake / "events.parquet", "parquet") == [1, 3]

    # Run 3: nothing late, the side file is left alone.
    _source(tmp_path, run2 + [(5, "2024-01-01 12:20:00")])
    assert _run(tmp_path, contract) == 0
    assert _seqs(side, "parquet") == [2, 4]
    assert _seqs(lake / "events.parquet", "parquet") == [1, 3, 5]
    assert sorted(p.name for p in lake.iterdir()) == [
        "events.parquet",
        "events__late_events.parquet",
    ]


def test_a_failing_side_file_union_leaves_the_late_rows_in_the_main_file(tmp_path):
    contract = _contract(tmp_path, "parquet", lateness=True)
    lake = tmp_path / "lake"
    side = lake / "events__late_events.parquet"
    run1 = [(1, "2024-01-01 12:00:00"), (2, "2024-01-01 11:00:00")]
    _source(tmp_path, run1)
    assert _run(tmp_path, contract) == 0
    side.write_bytes(b"corrupt")
    _source(tmp_path, run1 + [(3, "2024-01-01 12:10:00"), (4, "2024-01-01 11:20:00")])
    _run(tmp_path, contract)
    assert side.read_bytes() == b"corrupt"
    assert 4 in _seqs(lake / "events.parquet", "parquet")


@pytest.mark.parametrize("old_ids", [("007", "01234"), ("1.50", "2.50")])
def test_csv_text_columns_round_trip_verbatim(tmp_path, old_ids):
    src = tmp_path / "events.parquet"

    def land(values: str) -> None:
        con = duckdb.connect()
        try:
            con.execute(
                f"COPY (SELECT * FROM (VALUES {values}) t(seq, event_time)) "
                f"TO '{src}' (FORMAT 'parquet')"
            )
        finally:
            con.close()

    contract = _contract(tmp_path, "csv")
    source = contract["builds"][0]["properties"]["source"]
    source.update(
        {
            "connection": {"uri": str(src)},
            "reader": {"format": "parquet"},
            "cursor_field": "event_time",
        }
    )
    contract["exposes"][0]["contract"]["schema"][0]["type"] = "string"
    first = (
        f"('{old_ids[0]}', TIMESTAMP '2024-01-01 12:00:00'), "
        f"('{old_ids[1]}', TIMESTAMP '2024-01-01 12:01:00')"
    )
    land(first)
    assert _run(tmp_path, contract) == 0
    land(first + ", ('5', TIMESTAMP '2024-01-01 12:02:00')")
    assert _run(tmp_path, contract) == 0
    text = (tmp_path / "lake" / "events.csv").read_text()
    assert all(f"{v}," in text for v in (*old_ids, "5"))
