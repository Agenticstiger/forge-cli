# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The duckdb runner lands a partitioned Glue table in the Hive layout Glue reads.

A Glue table with ``partition_keys`` reads its rows only under
``<location>/<key>=<value>/``. The runner wrote one file directly under the location,
which no partition holds, so it now writes ``COPY ... PARTITION_BY`` into the
location instead: a full refresh replaces each partition it writes, an
``incremental_append`` run adds files, and the shapes a directory of files cannot
take (a late-arrival split, dedup or merge, a stream landing elsewhere) are refused
before anything is written.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.build_runners.duckdb.runner import (  # noqa: E402
    PartitionedWrite,
    _build_copy_destination,
    _landed_relation,
    execute_duckdb_build,
)

from . import _duckdb_moto  # noqa: E402

pytestmark = pytest.mark.unit

PRODUCT = "sales.orders"
BUILD = "land"
ROWS = [
    ("o1", "2024-01-05", "eu", "10"),
    ("o2", "2024-01-05", "us", "20"),
    ("o3", "2024-01-06", "eu", "30"),
]


def _source(tmp_path: Path, rows=ROWS) -> Path:
    lines = ["id,order_date,region,amount"] + [",".join(r) for r in rows]
    path = tmp_path / "orders.csv"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _contract(
    tmp_path: Path,
    *,
    location: Optional[Dict[str, Any]] = None,
    mode: str = "full_refresh",
    streams: Optional[List[str]] = None,
    source_extra: Optional[Dict[str, Any]] = None,
    connection_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    connection: Dict[str, Any] = {"uri": str(_source(tmp_path))}
    connection.update(connection_extra or {})
    source: Dict[str, Any] = {
        "kind": "filesystem",
        "mode": mode,
        "connection": connection,
        "reader": {"format": "csv"},
        "streams": streams or ["orders"],
    }
    source.update(source_extra or {})
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": PRODUCT,
        "name": "Orders",
        "builds": [
            {
                "id": BUILD,
                "pattern": "acquisition",
                "engine": "duckdb",
                "properties": {"source": source, "sink": {"format": "parquet"}},
                "outputs": ["orders"],
            }
        ],
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "parquet",
                    "location": location
                    or {
                        "path": "./lake/orders",
                        "database": "sales",
                        "table": "orders",
                        "partitionBy": ["order_date", "region"],
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "id", "type": "string"},
                        {"name": "order_date", "type": "date"},
                        {"name": "region", "type": "string"},
                        {"name": "amount", "type": "bigint"},
                    ]
                },
            }
        ],
    }


def _run(tmp_path: Path, contract: Dict[str, Any]):
    runs = tmp_path / ".fluid" / "runs" / PRODUCT / BUILD / "runs"
    before = set(runs.glob("*.json")) if runs.is_dir() else set()
    rc = execute_duckdb_build(contract["builds"][0], contract, tmp_path)
    (written,) = set(runs.glob("*.json")) - before
    return rc, json.loads(written.read_text(encoding="utf-8"))


def _files(root: Path) -> List[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.parquet"))


def _rows(root: Path):
    con = duckdb.connect()
    try:
        return con.execute(
            f"SELECT id, CAST(order_date AS VARCHAR), region, amount FROM read_parquet("
            f"'{root}/**/*.parquet', hive_partitioning = true) ORDER BY id"
        ).fetchall()
    finally:
        con.close()


def _columns_in_files(root: Path) -> List[str]:
    con = duckdb.connect()
    try:
        first = _files(root)[0]
        return [
            row[0]
            for row in con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{root / first}', hive_partitioning = false)"
            ).fetchall()
        ]
    finally:
        con.close()


# ── The COPY ────────────────────────────────────────────────────────────


def test_an_unpartitioned_copy_is_written_as_before():
    assert _build_copy_destination("/x/o.parquet", "parquet") == (
        "COPY ({select}) TO '/x/o.parquet' (FORMAT 'parquet')"
    )


def test_a_full_refresh_copy_rewrites_each_partition_under_one_name():
    assert _build_copy_destination(
        "s3://b/p", "parquet", PartitionedWrite(("order_date", "region"), file_stem="orders")
    ).replace("{select}", "S", 1) == (
        "COPY (S) TO 's3://b/p' (FORMAT 'parquet', PARTITION_BY (order_date, region), "
        "OVERWRITE_OR_IGNORE, FILENAME_PATTERN 'orders_{i}')"
    )


def test_an_append_copy_gives_each_file_a_new_name():
    assert _build_copy_destination(
        "s3://b/p", "csv", PartitionedWrite(("region",), append=True)
    ) == ("COPY ({select}) TO 's3://b/p' (FORMAT 'csv', HEADER, PARTITION_BY (region), APPEND)")


def test_braces_in_the_path_reach_the_copy_as_written(tmp_path):
    """The select is put in with ``replace``: a ``{`` in the path is not a format field."""
    path = tmp_path / "lake" / "a{b}"
    assert f"TO '{path}'" in _build_copy_destination(str(path), "parquet")
    location = {
        "path": "./lake/{orders}",
        "database": "sales",
        "table": "orders",
        "partitionBy": ["region"],
    }
    rc, record = _run(tmp_path, _contract(tmp_path, location=location))
    assert rc == 0, record
    assert (tmp_path / "lake" / "{orders}" / "region=eu" / "orders_0.parquet").is_file()


def test_a_partition_key_that_is_not_an_identifier_is_refused():
    with pytest.raises(ValueError):
        _build_copy_destination("s3://b/p", "parquet", PartitionedWrite(("region; DROP",)))


def test_the_landed_relation_reads_the_partition_paths_back():
    assert _landed_relation("s3://b/p/", "read_parquet", ("region",)) == (
        "read_parquet('s3://b/p/**/*', hive_partitioning = true)"
    )
    assert _landed_relation("/x/o.parquet", "read_parquet", ()) == "read_parquet('/x/o.parquet')"


# ── A real run ──────────────────────────────────────────────────────────


def test_the_rows_land_in_the_hive_layout_without_the_key_columns(tmp_path):
    rc, record = _run(tmp_path, _contract(tmp_path))
    assert rc == 0, record
    root = tmp_path / "lake" / "orders"
    assert _files(root) == [
        "order_date=2024-01-05/region=eu/orders_0.parquet",
        "order_date=2024-01-05/region=us/orders_0.parquet",
        "order_date=2024-01-06/region=eu/orders_0.parquet",
    ]
    assert _columns_in_files(root) == ["id", "amount"]
    assert _rows(root) == [
        ("o1", "2024-01-05", "eu", 10),
        ("o2", "2024-01-05", "us", 20),
        ("o3", "2024-01-06", "eu", 30),
    ]
    assert record["records_total"] == 3


def test_a_second_full_refresh_replaces_the_partitions_it_writes(tmp_path):
    contract = _contract(tmp_path)
    assert _run(tmp_path, contract)[0] == 0
    _source(tmp_path, rows=[("o1", "2024-01-05", "eu", "11")])
    assert _run(tmp_path, contract)[0] == 0
    rows = _rows(tmp_path / "lake" / "orders")
    # eu/2024-01-05 is replaced; the partitions the run did not write are kept.
    assert rows == [
        ("o1", "2024-01-05", "eu", 11),
        ("o2", "2024-01-05", "us", 20),
        ("o3", "2024-01-06", "eu", 30),
    ]


def test_incremental_append_adds_files_and_reads_its_cursor_across_partitions(tmp_path):
    contract = _contract(
        tmp_path, mode="incremental_append", source_extra={"cursor_field": "amount"}
    )
    assert _run(tmp_path, contract)[0] == 0
    _source(tmp_path, rows=[("o4", "2024-01-06", "eu", "40")])
    rc, record = _run(tmp_path, contract)
    assert rc == 0, record
    root = tmp_path / "lake" / "orders"
    assert [r[0] for r in _rows(root)] == ["o1", "o2", "o3", "o4"]
    assert len(_files(root)) == 4


@pytest.mark.parametrize(
    "shape, says",
    [
        (
            {"mode": "incremental_dedup", "source_extra": {"cursor_field": "amount"}},
            "incremental_dedup",
        ),
        (
            {
                "source_extra": {
                    "watermark": {"strategy": "high_water_mark", "allowedLateness": "PT1H"}
                }
            },
            "late-arrival split",
        ),
        ({"streams": ["orders", "refunds"]}, "outside its location"),
    ],
)
def test_a_shape_a_directory_of_files_cannot_take_is_refused_before_it_writes(
    tmp_path, shape, says
):
    rc, record = _run(tmp_path, _contract(tmp_path, **shape))
    assert rc != 0
    text = json.dumps(record)
    assert "partitioned table: expose 'orders' is a Glue table partitioned by" in text
    assert says in text
    assert not (tmp_path / "lake").exists()


def test_an_unpartitioned_aws_table_still_lands_one_file(tmp_path):
    location = {"path": "./lake/orders.parquet", "database": "sales", "table": "orders"}
    rc, record = _run(tmp_path, _contract(tmp_path, location=location))
    assert rc == 0, record
    assert (tmp_path / "lake" / "orders.parquet").is_file()


# ── S3 (moto) ───────────────────────────────────────────────────────────


@pytest.fixture
def moto_s3(monkeypatch):
    yield from _duckdb_moto.moto_s3(monkeypatch, "orders-partition-lake")


@pytest.mark.emulated
def test_the_hive_layout_lands_on_s3(tmp_path, moto_s3):
    endpoint, s3 = moto_s3
    location = {
        "bucket": "orders-partition-lake",
        "path": "silver/orders/",
        "database": "sales",
        "table": "orders",
        "region": "us-east-1",
        "partitionBy": ["order_date", "region"],
    }
    contract = _contract(
        tmp_path, location=location, connection_extra={"s3": _duckdb_moto.duckdb_secret(endpoint)}
    )
    rc, record = _run(tmp_path, contract)
    assert rc == 0, record
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="orders-partition-lake")["Contents"])
    assert keys == [
        "silver/orders/order_date=2024-01-05/region=eu/orders_0.parquet",
        "silver/orders/order_date=2024-01-05/region=us/orders_0.parquet",
        "silver/orders/order_date=2024-01-06/region=eu/orders_0.parquet",
    ]
    assert _run(tmp_path, contract)[0] == 0  # a second run overwrites, on S3 too
    assert len(s3.list_objects_v2(Bucket="orders-partition-lake")["Contents"]) == 3
