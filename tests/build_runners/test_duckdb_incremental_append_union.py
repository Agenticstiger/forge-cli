# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``incremental_append`` to one file keeps every increment.

DuckDB's ``COPY ... TO file`` overwrites, so a second run left only its own rows while
the cursor had moved past the first run's. The runner now writes the union of the
file and the new rows to a temp file beside it and renames it over the target; the
late-arrival side file is unioned the same way.
"""

from __future__ import annotations

import json
import os
import types
from pathlib import Path
from typing import Any, Dict, List

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.build_runners.duckdb import runner as runner_mod  # noqa: E402
from fluid_build.build_runners.duckdb.runner import (  # noqa: E402
    JsonAppendSchemaError,
    _json_append_to_local_file,
    _union_rewrite,
    execute_duckdb_build,
)

pytestmark = pytest.mark.unit

PRODUCT = "sales.events"
BUILD = "land"


def _source(tmp_path: Path, rows: List[tuple]) -> None:
    lines = ["seq,event_time"] + [f"{s},{t}" for s, t in rows]
    (tmp_path / "events.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _contract(
    tmp_path: Path, fmt: str, *, lateness: bool = False, mode: str = "incremental_append"
) -> Dict[str, Any]:
    source: Dict[str, Any] = {
        "kind": "filesystem",
        "mode": mode,
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
    reader = {"parquet": "read_parquet", "csv": "read_csv_auto"}.get(fmt, "read_json_auto")
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


def test_a_failing_side_file_union_fails_the_run_and_loses_no_row(tmp_path):
    contract = _contract(tmp_path, "parquet", lateness=True)
    lake = tmp_path / "lake"
    side = lake / "events__late_events.parquet"
    run1 = [(1, "2024-01-01 12:00:00"), (2, "2024-01-01 11:00:00")]
    _source(tmp_path, run1)
    assert _run(tmp_path, contract) == 0
    good_side = side.read_bytes()
    main_before = (lake / "events.parquet").read_bytes()
    side.write_bytes(b"corrupt")
    run2 = run1 + [(3, "2024-01-01 12:10:00"), (4, "2024-01-01 11:20:00")]
    _source(tmp_path, run2)
    assert _run(tmp_path, contract) != 0
    # The side file goes first: the main file is as it was, the cursor did not move.
    assert (lake / "events.parquet").read_bytes() == main_before
    side.write_bytes(good_side)
    assert _run(tmp_path, contract) == 0
    assert _seqs(side, "parquet") == [2, 4]
    assert _seqs(lake / "events.parquet", "parquet") == [1, 3]


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


# ── json and ndjson: the old rows are read back by the declared types ────────

JSON_SCHEMA = [
    {"name": "seq", "type": "bigint"},
    {"name": "event_time", "type": "timestamp"},
    {"name": "amount", "type": "decimal(10,2)"},
    {"name": "zip", "type": "string"},
]
RUN1 = "(1::BIGINT, TIMESTAMP '2024-01-01 12:00:00', 1.50::DECIMAL(10,2), '01234')"
RUN2 = "(2::BIGINT, TIMESTAMP '2024-01-01 12:01:00', 20.00::DECIMAL(10,2), '007')"


def _json_contract(tmp_path: Path, fmt: str, *, schema=JSON_SCHEMA, lateness=False):
    contract = _contract(tmp_path, fmt, lateness=lateness)
    source = contract["builds"][0]["properties"]["source"]
    source.update(
        {
            "connection": {"uri": str(tmp_path / "events.parquet")},
            "reader": {"format": "parquet"},
        }
    )
    contract["exposes"][0]["contract"]["schema"] = schema
    return contract


def _land(tmp_path: Path, *rows: str) -> None:
    con = duckdb.connect()
    try:
        con.execute(
            f"COPY (SELECT * FROM (VALUES {', '.join(rows)}) t(seq, event_time, amount, zip)) "
            f"TO '{tmp_path / 'events.parquet'}' (FORMAT 'parquet')"
        )
    finally:
        con.close()


def _typed_rows(path: Path):
    con = duckdb.connect()
    try:
        return con.execute(
            "SELECT seq, CAST(event_time AS VARCHAR), CAST(amount AS VARCHAR), zip, "
            "typeof(event_time), typeof(amount) FROM read_json("
            f"'{path}', format = 'newline_delimited', columns = {{'seq': 'BIGINT', "
            "'event_time': 'TIMESTAMP', 'amount': 'DECIMAL(10,2)', 'zip': 'VARCHAR'}) "
            "ORDER BY seq"
        ).fetchall()
    finally:
        con.close()


@pytest.mark.parametrize("fmt", ["json", "ndjson"])
def test_json_sinks_keep_both_runs_with_declared_types(tmp_path, fmt):
    contract = _json_contract(tmp_path, fmt)
    _land(tmp_path, RUN1)
    assert _run(tmp_path, contract) == 0
    _land(tmp_path, RUN1, RUN2)
    assert _run(tmp_path, contract) == 0
    target = tmp_path / "lake" / f"events.{fmt}"
    assert _typed_rows(target) == [
        (1, "2024-01-01 12:00:00", "1.50", "01234", "TIMESTAMP", "DECIMAL(10,2)"),
        (2, "2024-01-01 12:01:00", "20.00", "007", "TIMESTAMP", "DECIMAL(10,2)"),
    ]
    assert '"zip":"01234"' in target.read_text()
    assert sorted(p.name for p in target.parent.iterdir()) == [f"events.{fmt}"]


@pytest.mark.parametrize("fmt", ["json", "ndjson"])
def test_a_corrupt_old_json_file_stays_and_leaves_no_temp(tmp_path, fmt):
    target = tmp_path / f"t.{fmt}"
    target.write_bytes(b"{not json")
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE new_rows AS SELECT 2 AS seq")
        with pytest.raises(Exception):  # noqa: B017 - the corrupt old file fails the COPY
            _union_rewrite(con, str(target), fmt, "new_rows", {"seq": "BIGINT"})
    finally:
        con.close()
    assert target.read_bytes() == b"{not json"
    assert os.listdir(tmp_path) == [f"t.{fmt}"]


@pytest.mark.parametrize("fmt", ["json", "ndjson"])
def test_an_old_json_column_the_schema_no_longer_declares_is_refused_not_dropped(tmp_path, fmt):
    target = tmp_path / f"t.{fmt}"
    old_rows = b'{"seq":1,"legacy":"keep me"}\n'
    target.write_bytes(old_rows)
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE new_rows AS SELECT 2 AS seq")
        with pytest.raises(JsonAppendSchemaError) as caught:
            _union_rewrite(con, str(target), fmt, "new_rows", {"seq": "BIGINT"})
    finally:
        con.close()
    assert caught.value.code == "incremental_append_json_undeclared_column"
    assert "legacy" in caught.value.what
    assert target.read_bytes() == old_rows
    assert os.listdir(tmp_path) == [f"t.{fmt}"]


@pytest.mark.parametrize("fmt", ["json", "ndjson"])
@pytest.mark.parametrize("schema", [[], None])
def test_a_json_append_without_a_declared_schema_is_refused_before_any_write(tmp_path, fmt, schema):
    contract = _json_contract(tmp_path, fmt)
    if schema is None:
        del contract["exposes"][0]["contract"]["schema"]
    else:
        contract["exposes"][0]["contract"]["schema"] = schema
    _land(tmp_path, RUN1)
    with pytest.raises(JsonAppendSchemaError) as caught:
        _run(tmp_path, contract)
    assert caught.value.code == "incremental_append_json_needs_schema"
    assert not [f for f in (tmp_path / "lake").rglob("*") if f.is_file()]


def test_a_json_type_with_no_duckdb_mapping_is_refused(tmp_path):
    schema = JSON_SCHEMA[:3] + [{"name": "zip", "type": "geometry"}]
    contract = _json_contract(tmp_path, "json", schema=schema)
    _land(tmp_path, RUN1)
    with pytest.raises(JsonAppendSchemaError) as caught:
        _run(tmp_path, contract)
    assert caught.value.code == "incremental_append_json_unmapped_type"
    assert not [f for f in (tmp_path / "lake").rglob("*") if f.is_file()]


@pytest.mark.parametrize("fmt", ["json", "ndjson"])
def test_json_late_events_keep_both_runs_late_rows(tmp_path, fmt):
    contract = _json_contract(tmp_path, fmt, lateness=True)

    def row(seq: int, at: str) -> str:
        return f"({seq}::BIGINT, TIMESTAMP '2024-01-01 {at}', 1.50::DECIMAL(10,2), '0{seq}')"

    run1 = [row(1, "12:00:00"), row(2, "11:00:00")]
    _land(tmp_path, *run1)
    assert _run(tmp_path, contract) == 0
    _land(tmp_path, *run1, row(3, "12:10:00"), row(4, "11:20:00"))
    assert _run(tmp_path, contract) == 0
    lake = tmp_path / "lake"
    assert [r[0] for r in _typed_rows(lake / f"events__late_events.{fmt}")] == [2, 4]
    assert [r[0] for r in _typed_rows(lake / f"events.{fmt}")] == [1, 3]


@pytest.mark.parametrize("declared", ["float", "double"])
def test_a_json_float_is_a_double_and_keeps_its_value(tmp_path, declared):
    schema = [{"name": "seq", "type": "bigint"}, {"name": "ratio", "type": declared}]
    contract = _json_contract(tmp_path, "json", schema=schema)

    def land(*rows: str) -> None:
        con = duckdb.connect()
        try:
            con.execute(
                f"COPY (SELECT * FROM (VALUES {', '.join(rows)}) t(seq, ratio)) "
                f"TO '{tmp_path / 'events.parquet'}' (FORMAT 'parquet')"
            )
        finally:
            con.close()

    land("(1::BIGINT, 0.1::DOUBLE)")
    assert _run(tmp_path, contract) == 0
    land("(1::BIGINT, 0.1::DOUBLE)", "(2::BIGINT, 0.2::DOUBLE)")
    assert _run(tmp_path, contract) == 0
    text = (tmp_path / "lake" / "events.json").read_text()
    assert '"ratio":0.1}' in text and '"ratio":0.2}' in text
    assert "0.1000" not in text


def _stub_ctx(tmp_path: Path, location_path: str, **over: Any) -> Any:
    expose = {
        "exposeId": "events",
        "binding": {"platform": "local", "location": {"path": location_path}},
    }
    return types.SimpleNamespace(
        source=types.SimpleNamespace(
            mode=over.get("mode", runner_mod.AcquisitionMode.INCREMENTAL_APPEND),
            streams=["events"],
        ),
        sink=types.SimpleNamespace(format="json"),
        contract={"exposes": [expose], "builds": [{"id": "b", "outputs": ["events"]}]},
        build_id="b",
        workdir=str(tmp_path),
    )


def test_the_early_json_refusal_skips_a_remote_sink(tmp_path):
    local = _stub_ctx(tmp_path, "./lake/events.json")
    remote = _stub_ctx(tmp_path, "s3://bucket/events.json")
    assert _json_append_to_local_file(local, ["events"], ())
    assert not _json_append_to_local_file(remote, ["events"], ())
    full = _stub_ctx(tmp_path, "./lake/events.json", mode=runner_mod.AcquisitionMode.FULL_REFRESH)
    assert not _json_append_to_local_file(full, ["events"], ())


# ── late arrival: only this run's rows are judged ─────────────────────────────

ALL_FORMATS = ["parquet", "csv", "json", "ndjson"]


def _late_run(tmp_path: Path, contract, rows) -> None:
    _source(tmp_path, rows)
    assert _run(tmp_path, contract) == 0


def _ids(path: Path, fmt: str):
    return _seqs(path, fmt) if path.exists() else None


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_a_later_run_does_not_reclassify_earlier_rows_as_late(tmp_path, fmt):
    contract = _contract(tmp_path, fmt, lateness=True)
    run1 = [(1, "2024-01-01 12:00:00"), (2, "2024-01-01 12:01:00")]
    _late_run(tmp_path, contract, run1)
    _late_run(tmp_path, contract, run1 + [(3, "2024-01-01 14:00:00")])
    lake = tmp_path / "lake"
    assert _ids(lake / f"events.{fmt}", fmt) == [1, 2, 3]
    assert not (lake / f"events__late_events.{fmt}").exists()


@pytest.mark.parametrize("fmt", ALL_FORMATS)
def test_late_rows_go_to_the_side_file_only_and_every_run_keeps_its_own(tmp_path, fmt):
    contract = _contract(tmp_path, fmt, lateness=True)
    run1 = [(1, "2024-01-01 12:00:00"), (2, "2024-01-01 12:01:00")]
    _late_run(tmp_path, contract, run1)
    run2 = run1 + [(3, "2024-01-01 12:10:00"), (4, "2024-01-01 11:00:00")]
    _late_run(tmp_path, contract, run2)
    lake = tmp_path / "lake"
    side = lake / f"events__late_events.{fmt}"
    assert _ids(lake / f"events.{fmt}", fmt) == [1, 2, 3]
    assert _ids(side, fmt) == [4]
    run3 = run2 + [(5, "2024-01-01 12:20:00"), (6, "2024-01-01 11:30:00")]
    _late_run(tmp_path, contract, run3)
    assert _ids(lake / f"events.{fmt}", fmt) == [1, 2, 3, 5]
    assert _ids(side, fmt) == [4, 6]


def test_a_late_row_with_the_highest_cursor_still_moves_the_cursor_past_it(tmp_path):
    contract = _contract(tmp_path, "parquet", lateness=True)
    run1 = [(1, "2024-01-01 12:00:00")]
    _late_run(tmp_path, contract, run1)
    run2 = run1 + [(9, "2024-01-01 10:00:00"), (2, "2024-01-01 12:05:00")]
    _late_run(tmp_path, contract, run2)
    _late_run(tmp_path, contract, run2)  # nothing new: the late row 9 is not read twice
    assert _seqs(tmp_path / "lake" / "events__late_events.parquet", "parquet") == [9]


@pytest.mark.parametrize("fmt", ["parquet", "csv"])
def test_a_full_refresh_without_late_rows_removes_a_stale_side_file(tmp_path, fmt):
    contract = _contract(tmp_path, fmt, lateness=True, mode="full_refresh")
    lake = tmp_path / "lake"
    _late_run(tmp_path, contract, [(1, "2024-01-01 12:00:00"), (2, "2024-01-01 11:00:00")])
    side = lake / f"events__late_events.{fmt}"
    assert _ids(side, fmt) == [2]
    _late_run(tmp_path, contract, [(3, "2024-01-01 12:00:00")])
    assert not side.exists()
    assert _ids(lake / f"events.{fmt}", fmt) == [3]


# ── overlapping rewrites ─────────────────────────────────────────────────────


def test_overlapping_rewrites_of_one_target_never_corrupt_it_or_share_a_temp(tmp_path):
    import threading

    target = tmp_path / "t.parquet"
    seed = duckdb.connect()
    seed.execute(f"COPY (SELECT 0 AS seq) TO '{target}' (FORMAT 'parquet')")
    seed.close()
    errors: List[BaseException] = []

    def work(worker: int) -> None:
        con = duckdb.connect()
        try:
            con.execute(f"CREATE TABLE new_rows AS SELECT {worker} AS seq")
            for _ in range(10):
                _union_rewrite(con, str(target), "parquet", "new_rows")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            con.close()

    threads = [threading.Thread(target=work, args=(i,)) for i in (1, 2, 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    rows = _seqs(target, "parquet")
    assert 0 in rows and len(rows) >= 11
    assert os.listdir(tmp_path) == ["t.parquet"]


def test_each_rewrite_uses_its_own_temp_name(tmp_path):
    from fluid_build.build_runners.duckdb.runner import _append_tmp_path

    path = str(tmp_path / "t.parquet")
    assert _append_tmp_path(path) != _append_tmp_path(path)
    assert _append_tmp_path(path, "run-1") == _append_tmp_path(path, "run-1")
    assert _append_tmp_path(path, "run-1") != _append_tmp_path(path, "run-2")


# ── a build's outputs must resolve ───────────────────────────────────────────


def _second_expose(contract):
    other = json.loads(json.dumps(contract["exposes"][0]))
    other["exposeId"] = "other"
    other["binding"]["location"]["path"] = "./lake/other.parquet"
    contract["exposes"].append(other)


@pytest.mark.parametrize(
    "outputs, code",
    [
        (None, "build_outputs_undeclared"),
        (["nope"], "build_output_unknown_expose"),
    ],
)
def test_a_build_whose_outputs_do_not_resolve_is_refused_before_any_write(tmp_path, outputs, code):
    from fluid_build.util.contract import BuildOutputsError

    contract = _contract(tmp_path, "parquet")
    _second_expose(contract)
    if outputs is None:
        del contract["builds"][0]["outputs"]
    else:
        contract["builds"][0]["outputs"] = outputs
    _source(tmp_path, [(1, "2024-01-01 12:00:00")])
    with pytest.raises(BuildOutputsError) as caught:
        _run(tmp_path, contract)
    assert caught.value.code == code
    assert not [f for f in (tmp_path / "lake").rglob("*") if f.is_file()]


def test_the_first_resolved_output_is_the_one_landed(tmp_path):
    contract = _contract(tmp_path, "parquet")
    _second_expose(contract)
    contract["builds"][0]["outputs"] = ["other", "events"]
    _source(tmp_path, [(1, "2024-01-01 12:00:00")])
    assert _run(tmp_path, contract) == 0
    lake = tmp_path / "lake"
    assert (lake / "other.parquet").is_file() and not (lake / "events.parquet").exists()


# ── review round: grants, ordering, atomic side file, stale temps ────────────

import logging  # noqa: E402
import stat as _stat  # noqa: E402
import time  # noqa: E402

from fluid_build.build_runners.duckdb.runner import _append_to_file  # noqa: E402


def _old_parquet(path: Path, select: str) -> None:
    con = duckdb.connect()
    try:
        con.execute(f"COPY ({select}) TO '{path}' (FORMAT 'parquet')")
    finally:
        con.close()


NEW_LATE = (
    "SELECT * FROM (VALUES (3::BIGINT, TIMESTAMP '2024-01-01 12:10:00'), "
    "(4::BIGINT, TIMESTAMP '2024-01-01 11:00:00')) t(seq, event_time)"
)
OLD_ROW = "SELECT 1::BIGINT AS seq, TIMESTAMP '2024-01-01 12:00:00' AS event_time"


def test_the_post_land_side_union_runs_inside_the_sandbox(tmp_path, monkeypatch):
    # Landing outside the contract directory: the sandbox grants file by file, so a temp
    # name the grant does not name is refused.
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "data"
    monkeypatch.setenv("FLUID_DUCKDB_ALLOWED_DIRS", str(outside))
    contract = _contract(project, "parquet", lateness=True)
    contract["exposes"][0]["binding"]["location"]["path"] = str(outside / "events.parquet")
    run1 = [(1, "2024-01-01 12:00:00"), (2, "2024-01-01 11:00:00")]
    _late_run(project, contract, run1)
    (outside / "events.parquet").unlink()  # the next run is a first run for the main file
    _late_run(project, contract, run1 + [(3, "2024-01-01 12:10:00"), (4, "2024-01-01 11:20:00")])
    assert _seqs(outside / "events__late_events.parquet", "parquet") == [2, 4]
    assert _seqs(outside / "events.parquet", "parquet") == [3]


def test_a_typed_refusal_of_the_main_rewrite_writes_no_side_file(tmp_path):
    main = tmp_path / "events.json"
    main.write_text('{"seq":1,"event_time":"2024-01-01 12:00:00","extra":7}\n')
    before = main.read_bytes()
    con = duckdb.connect()
    try:
        with pytest.raises(JsonAppendSchemaError):
            _append_to_file(
                con,
                str(main),
                "json",
                NEW_LATE,
                {"seq": "BIGINT", "event_time": "TIMESTAMP"},
                late=("event_time", 1800.0),
            )
    finally:
        con.close()
    assert main.read_bytes() == before
    assert sorted(os.listdir(tmp_path)) == ["events.json"]


def test_an_old_file_without_the_event_column_does_not_stick_the_run(tmp_path):
    main = tmp_path / "events.parquet"
    _old_parquet(main, "SELECT 1::BIGINT AS seq")
    con = duckdb.connect()
    try:
        info: dict = {}
        _append_to_file(con, str(main), "parquet", NEW_LATE, late=("event_time", 1800.0), info=info)
    finally:
        con.close()
    assert info["late_counts"] == {"on_time": 1, "late": 1}
    assert _seqs(tmp_path / "events__late_events.parquet", "parquet") == [4]
    assert _seqs(main, "parquet") == [1, 3]


def test_a_non_timestamp_event_column_warns_and_lands_every_row(tmp_path, caplog):
    main = tmp_path / "events.parquet"
    _old_parquet(main, "SELECT 1::BIGINT AS seq, '2024-01-01' AS event_time")
    con = duckdb.connect()
    try:
        with caplog.at_level(logging.WARNING):
            _append_to_file(
                con,
                str(main),
                "parquet",
                "SELECT 2::BIGINT AS seq, '2020-01-01' AS event_time",
                late=("event_time", 1800.0),
            )
    finally:
        con.close()
    assert _seqs(main, "parquet") == [1, 2]
    assert "late_arrival_split_skipped" in caplog.text and "event_time" in caplog.text


def test_the_event_column_is_matched_without_regard_to_case(tmp_path):
    main = tmp_path / "events.parquet"
    _old_parquet(main, OLD_ROW)
    con = duckdb.connect()
    try:
        info: dict = {}
        _append_to_file(con, str(main), "parquet", NEW_LATE, late=("Event_Time", 1800.0), info=info)
    finally:
        con.close()
    assert info["late_counts"] == {"on_time": 1, "late": 1}


def test_a_missing_event_column_is_logged_not_silent(tmp_path, caplog):
    main = tmp_path / "events.parquet"
    _old_parquet(main, OLD_ROW)
    con = duckdb.connect()
    try:
        with caplog.at_level(logging.WARNING):
            _append_to_file(con, str(main), "parquet", NEW_LATE, late=("nope", 1800.0))
    finally:
        con.close()
    assert "late_arrival_split_skipped" in caplog.text and "nope" in caplog.text


def test_a_first_time_side_file_is_written_through_a_temp_and_a_rename(tmp_path, monkeypatch):
    main = tmp_path / "events.parquet"
    _old_parquet(main, OLD_ROW)
    side = tmp_path / "events__late_events.parquet"
    replaced: List[str] = []
    real = os.replace
    monkeypatch.setattr(
        runner_mod.os, "replace", lambda a, b: (replaced.append(str(b)), real(a, b))[1]
    )
    con = duckdb.connect()
    try:
        _append_to_file(con, str(main), "parquet", NEW_LATE, late=("event_time", 1800.0))
    finally:
        con.close()
    assert str(side) in replaced and side.is_file()


def test_a_crash_before_the_rename_leaves_no_partial_side_file(tmp_path, monkeypatch):
    main = tmp_path / "events.parquet"
    _old_parquet(main, OLD_ROW)

    def boom(_path: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(runner_mod, "_fsync_path", boom)
    con = duckdb.connect()
    try:
        with pytest.raises(OSError):
            _append_to_file(con, str(main), "parquet", NEW_LATE, late=("event_time", 1800.0))
    finally:
        con.close()
    assert sorted(os.listdir(tmp_path)) == ["events.parquet"]


def test_a_directory_fsync_failure_after_the_rename_does_not_fail_the_rewrite(
    tmp_path, monkeypatch
):
    import errno

    target = tmp_path / "t.parquet"
    _old_parquet(target, "SELECT 1 AS seq")
    real = os.fsync

    def fsync(fd: int) -> None:
        if _stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "io error")
        real(fd)

    monkeypatch.setattr(runner_mod.os, "fsync", fsync)
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE new_rows AS SELECT 2 AS seq")
        _union_rewrite(con, str(target), "parquet", "new_rows")
    finally:
        con.close()
    assert _seqs(target, "parquet") == [1, 2]
    assert os.listdir(tmp_path) == ["t.parquet"]


def test_only_this_targets_old_temps_are_removed(tmp_path):
    target = tmp_path / "t.parquet"
    _old_parquet(target, "SELECT 1 AS seq")
    old_mine = tmp_path / ".t.parquet.deadrun.fluid-append"
    fresh_mine = tmp_path / ".t.parquet.liverun.fluid-append"
    old_other = tmp_path / ".other.parquet.deadrun.fluid-append"
    old_odd = tmp_path / ".t.parquet.deadrun.keep"
    for f in (old_mine, fresh_mine, old_other, old_odd):
        f.write_bytes(b"x")
    long_ago = time.time() - 48 * 3600
    for f in (old_mine, old_other, old_odd):
        os.utime(f, (long_ago, long_ago))
    con = duckdb.connect()
    try:
        con.execute("CREATE TABLE new_rows AS SELECT 2 AS seq")
        _union_rewrite(con, str(target), "parquet", "new_rows", run_id="now")
    finally:
        con.close()
    assert not old_mine.exists()
    assert fresh_mine.exists() and old_other.exists() and old_odd.exists()
