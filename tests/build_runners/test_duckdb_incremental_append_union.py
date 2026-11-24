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
