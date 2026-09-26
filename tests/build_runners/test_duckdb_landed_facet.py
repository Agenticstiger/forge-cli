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

"""The duckdb run record says what landed, where, and how it was counted.

``fluid verify`` holds an S3+Glue table to the build's run record. The record
described neither what the destination held nor which destination it was:

* ``records_total`` came from a second ``COUNT(*)`` over the select, which
  re-reads the source after the COPY. Against a live source it counted rows
  that arrived after the write: a run landed 3,305,000 rows and recorded
  3,935,000, and verify then failed the correct table as CRITICAL.
* nothing named the destination, so a local run from the contract directory
  (which every overlay shares) was held against the cloud table.

Every test runs the real ``execute_duckdb_build`` against a CSV source.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.build_runners.duckdb.runner import execute_duckdb_build  # noqa: E402

pytestmark = pytest.mark.unit

PRODUCT = "bronze.subs"
BUILD = "ingest"


def _contract(source: Path, *, mode: str = "full_refresh", extra_source: str = "") -> str:
    cursor = "        cursor_field: id\n" if mode != "full_refresh" else ""
    return (
        'fluidVersion: "0.7.5"\n'
        "kind: DataProduct\n"
        f"id: {PRODUCT}\n"
        "name: Subs\n"
        "builds:\n"
        f"  - id: {BUILD}\n"
        "    pattern: acquisition\n"
        "    engine: duckdb\n"
        "    properties:\n"
        "      source:\n"
        "        kind: filesystem\n"
        f"        mode: {mode}\n"
        f"{cursor}"
        f"        connection: {{uri: {source}}}\n"
        "        reader: {format: csv}\n"
        "        streams: [subs]\n"
        f"{extra_source}"
        "      sink: {format: parquet}\n"
        "    outputs: [subs]\n"
        "exposes:\n"
        "  - exposeId: subs\n"
        "    kind: table\n"
        "    binding:\n"
        "      platform: local\n"
        "      format: parquet\n"
        "      location: {path: ./out/subs.parquet}\n"
        "    contract:\n"
        "      schema:\n"
        "        - {name: id, type: INTEGER}\n"
        "        - {name: event_time, type: TIMESTAMP}\n"
    )


def _run(tmp_path: Path, contract_text: str) -> Dict[str, Any]:
    """Run the build once; return the run record it wrote."""
    import yaml

    contract = yaml.safe_load(contract_text)
    runs = tmp_path / ".fluid" / "runs" / PRODUCT / BUILD / "runs"
    before = set(runs.glob("*.json")) if runs.is_dir() else set()
    assert execute_duckdb_build(contract["builds"][0], contract, tmp_path) == 0
    (written,) = set(runs.glob("*.json")) - before
    return json.loads(written.read_text(encoding="utf-8"))


def _rows_in(path: Path) -> int:
    return duckdb.connect().execute(f"SELECT COUNT(*) FROM '{path}'").fetchone()[0]


def _source(tmp_path: Path, rows: int) -> Path:
    source = tmp_path / "subs.csv"
    source.write_text(
        "id,event_time\n" + "".join(f"{i},2026-09-25 10:00:00\n" for i in range(rows)),
        encoding="utf-8",
    )
    return source


def test_records_total_is_what_the_copy_wrote_not_a_second_read(tmp_path, monkeypatch):
    """The source gains rows between the COPY and anything after it, as a live
    database does. The record must count the rows the file holds."""
    source = _source(tmp_path, 100)
    real_connect = duckdb.connect

    class _LiveSource:
        def __init__(self, con: Any) -> None:
            self._con = con

        def __getattr__(self, name: str) -> Any:
            return getattr(self._con, name)

        def execute(self, sql: str, *args: Any) -> Any:
            result = self._con.execute(sql, *args)
            if sql.startswith("COPY"):
                with source.open("a", encoding="utf-8") as fh:
                    fh.write("".join(f"{i},2026-09-25 10:00:01\n" for i in range(100, 105)))
            return result

    monkeypatch.setattr(duckdb, "connect", lambda *a, **k: _LiveSource(real_connect(*a, **k)))

    record = _run(tmp_path, _contract(source))

    monkeypatch.setattr(duckdb, "connect", real_connect)
    assert _rows_in(tmp_path / "out" / "subs.parquet") == 100
    assert record["records_total"] == 100
    assert record["streams"][0]["records"] == 100
    assert record["facets"]["landed"]["rows_from"] == "write"


@pytest.mark.parametrize("mode", ["full_refresh", "incremental_append"])
def test_the_run_record_says_where_it_landed_and_in_which_mode(tmp_path, mode):
    source = _source(tmp_path, 7)

    record = _run(tmp_path, _contract(source, mode=mode))

    assert record["facets"]["landed"] == {
        "mode": mode,
        "rows_from": "write",
        # Relative to the contract directory: the facets also go out in the
        # OpenLineage event, and an absolute path carries the home directory.
        "destinations": {"subs": "out/subs.parquet"},
    }


def test_rows_moved_out_by_the_late_arrival_split_are_not_claimed_as_landed(tmp_path):
    """The split rewrites the file without the late rows, so the COPY's count
    is no longer what the destination holds."""
    source = tmp_path / "subs.csv"
    source.write_text(
        "id,event_time\n1,2026-09-25 10:00:00\n2,2026-09-25 10:00:00\n3,2026-09-20 10:00:00\n",
        encoding="utf-8",
    )
    watermark = "        watermark: {strategy: high_water_mark, allowedLateness: PT1H}\n"

    record = _run(tmp_path, _contract(source, extra_source=watermark))

    assert _rows_in(tmp_path / "out" / "subs.parquet") == 2
    assert record["records_total"] == 3
    assert record["facets"]["landed"]["rows_from"] == "write_before_late_arrival_split"
