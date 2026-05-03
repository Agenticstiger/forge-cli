# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""dlt engine — full matrix (Slice C).

Filesystem-source matrix (csv / parquet / json) + sql_database via Testcontainers
Postgres + custom @dlt.source module loading + write-disposition mapping per
acquisition mode + capability declarations + DuckDB destination.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from typing import Any, Dict

import pytest

from fluid_build.api.runner import RunnerCapability
from fluid_build.build_runners.dlt.runner import (
    DltRunner,
    _map_mode_to_write_disposition,
    execute_dlt_build,
)

# ── Helpers ──────────────────────────────────────────────────────────────


def _base_contract(
    *, source: Dict[str, Any], dlt_props: Dict[str, Any] = None, expose_path: str = None
) -> Dict[str, Any]:
    expose: Dict[str, Any] = {
        "exposeId": "data",
        "kind": "table",
        "binding": {"platform": "local", "format": "parquet"},
        "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
    }
    if expose_path is not None:
        expose["binding"]["location"] = {"path": expose_path}
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.dlt_test",
        "name": "DLT Test",
        "metadata": {
            "layer": "Bronze",
            "owner": {"team": "data-platform", "email": "dp@co.example"},
        },
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "dlt",
                "capabilities": ["full_refresh"],
                "properties": {
                    "source": source,
                    "sink": {"format": "parquet"},
                    "dlt": dlt_props or {},
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [expose],
    }


def _make_csv(path: Path, *, n: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [f"{i},Person{i},{i * 10.5}" for i in range(1, n + 1)]
    path.write_text("id,name,amount\n" + "\n".join(rows) + "\n", encoding="utf-8")


def _make_jsonl(path: Path, *, n: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            json.dumps({"id": i, "name": f"P{i}", "amount": i * 10.5}) for i in range(1, n + 1)
        ),
        encoding="utf-8",
    )


def _count_table_rows(duckdb_path: Path, dataset: str, table: str) -> int:
    import duckdb

    if not duckdb_path.exists():
        return 0
    con = duckdb.connect(str(duckdb_path))
    try:
        try:
            cur = con.execute(f"SELECT COUNT(*) FROM {dataset}.{table}").fetchone()
            return int(cur[0]) if cur else 0
        except Exception:
            return 0
    finally:
        con.close()


# ── Capability declarations ─────────────────────────────────────────────


class TestDltCapabilityDeclarations:
    def test_runner_class_attributes(self):
        r = DltRunner()
        assert r.name == "dlt"
        assert "embedded" in r.declared_modes
        assert RunnerCapability.FULL_REFRESH in r.declared_capabilities
        assert RunnerCapability.INCREMENTAL_APPEND in r.declared_capabilities
        assert RunnerCapability.INCREMENTAL_MERGE in r.declared_capabilities
        assert RunnerCapability.SCHEMA_EVOLUTION in r.declared_capabilities

    def test_dlt_does_not_declare_streaming_or_cdc(self):
        r = DltRunner()
        assert RunnerCapability.STREAMING not in r.declared_capabilities
        assert RunnerCapability.CDC not in r.declared_capabilities


# ── Mode → write_disposition mapping ────────────────────────────────────


class TestModeMapping:
    @pytest.mark.parametrize(
        "mode, disposition",
        [
            ("full_refresh", "replace"),
            ("incremental_append", "append"),
            ("incremental_dedup", "merge"),
            ("incremental_merge", "merge"),
            ("cdc", "merge"),
            ("streaming", "append"),
            ("unknown", "append"),
        ],
    )
    def test_mode_maps_to_disposition(self, mode: str, disposition: str):
        assert _map_mode_to_write_disposition(mode) == disposition


# ── Filesystem source matrix ────────────────────────────────────────────


class TestDltFilesystemMatrix:
    def test_csv_full_refresh_round_trip(self, tmp_path: Path):
        in_csv = tmp_path / "in" / "orders.csv"
        _make_csv(in_csv, n=4)
        out_db = tmp_path / "out" / "data.duckdb"
        contract = _base_contract(
            source={
                "kind": "filesystem",
                "connection": {"uri": str(in_csv)},
                "mode": "full_refresh",
                "reader": {"format": "csv", "options": {"header": True}},
            },
            expose_path=str(out_db),
        )
        rc = execute_dlt_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        assert out_db.exists()

    def test_jsonl_full_refresh(self, tmp_path: Path):
        in_jsonl = tmp_path / "in" / "events.jsonl"
        _make_jsonl(in_jsonl, n=5)
        out_db = tmp_path / "out" / "data.duckdb"
        contract = _base_contract(
            source={
                "kind": "filesystem",
                "connection": {"uri": str(in_jsonl)},
                "mode": "full_refresh",
                "reader": {"format": "ndjson"},
            },
            expose_path=str(out_db),
        )
        rc = execute_dlt_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        assert out_db.exists()

    def test_dry_run_no_output(self, tmp_path: Path):
        in_csv = tmp_path / "in" / "orders.csv"
        _make_csv(in_csv)
        out_db = tmp_path / "out" / "data.duckdb"
        contract = _base_contract(
            source={
                "kind": "filesystem",
                "connection": {"uri": str(in_csv)},
                "mode": "full_refresh",
                "reader": {"format": "csv", "options": {"header": True}},
            },
            expose_path=str(out_db),
        )
        rc = execute_dlt_build(contract["builds"][0], contract, tmp_path, dry_run=True)
        assert rc == 0
        assert not out_db.exists()


# ── SQL database source via live Postgres ───────────────────────────────


class TestDltSqlDatabasePostgres:
    def test_full_refresh_postgres(self, seeded_postgres: Dict[str, Any], tmp_path: Path):
        pg = seeded_postgres
        out_db = tmp_path / "out" / "data.duckdb"
        contract = _base_contract(
            source={
                "kind": "postgres",
                "connection": {
                    "host": pg["host"],
                    "port": pg["port"],
                    "user": pg["user"],
                    "password": pg["password"],
                    "database": pg["database"],
                    "drivername": "postgresql+psycopg",
                },
                "mode": "full_refresh",
                "streams": ["public.fluid_test_orders"],
            },
            dlt_props={"dataset_name": "bronze"},
            expose_path=str(out_db),
        )
        rc = execute_dlt_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        assert out_db.exists()
        # The table name in DuckDB is normalized lowercase.
        assert _count_table_rows(out_db, "bronze", "fluid_test_orders") == 5


# ── Custom @dlt.source module ───────────────────────────────────────────


class TestDltCustomSource:
    def test_custom_source_module_loads_and_runs(self, tmp_path: Path):
        # Write a tiny custom dlt source as a module under the contract dir.
        src_dir = tmp_path / "sources"
        src_dir.mkdir(parents=True)
        (src_dir / "my_source.py").write_text(
            dedent(
                """
                import dlt

                @dlt.resource
                def my_resource():
                    yield from [
                        {"id": 1, "label": "alpha"},
                        {"id": 2, "label": "beta"},
                        {"id": 3, "label": "gamma"},
                    ]

                @dlt.source
                def source():
                    return my_resource
                """
            ),
            encoding="utf-8",
        )
        out_db = tmp_path / "out" / "data.duckdb"
        contract = _base_contract(
            source={
                "kind": "custom",
                "connection": {},
                "mode": "full_refresh",
            },
            dlt_props={
                "source_module": "sources/my_source.py",
                "dataset_name": "bronze",
            },
            expose_path=str(out_db),
        )
        rc = execute_dlt_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        assert _count_table_rows(out_db, "bronze", "my_resource") == 3

    def test_custom_source_missing_module_fails(self, tmp_path: Path):
        contract = _base_contract(
            source={"kind": "custom", "connection": {}, "mode": "full_refresh"},
            dlt_props={"source_module": "missing_module.py"},
            expose_path=str(tmp_path / "out.duckdb"),
        )
        rc = execute_dlt_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0


# ── Run-record persistence ──────────────────────────────────────────────


class TestDltRunRecord:
    def test_run_record_persisted_with_facets(self, tmp_path: Path):
        in_csv = tmp_path / "in" / "data.csv"
        _make_csv(in_csv)
        out_db = tmp_path / "out" / "data.duckdb"
        contract = _base_contract(
            source={
                "kind": "filesystem",
                "connection": {"uri": str(in_csv)},
                "mode": "full_refresh",
                "reader": {"format": "csv", "options": {"header": True}},
            },
            expose_path=str(out_db),
        )
        execute_dlt_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        runs_dir = (
            tmp_path / ".fluid" / "runs" / contract["id"] / contract["builds"][0]["id"] / "runs"
        )
        records = list(runs_dir.glob("*.json"))
        assert len(records) == 1
        rec = json.loads(records[0].read_text())
        assert rec["facets"]["engine"] == "dlt"
        assert rec["facets"]["destination"] == "duckdb"


# ── Failure modes ───────────────────────────────────────────────────────


class TestDltFailureModes:
    def test_unsupported_kind_fails(self, tmp_path: Path):
        contract = _base_contract(
            source={"kind": "salesforce", "connection": {}, "mode": "full_refresh"},
        )
        rc = execute_dlt_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0

    def test_filesystem_unsupported_format_fails(self, tmp_path: Path):
        in_path = tmp_path / "in" / "x.weird"
        in_path.parent.mkdir(parents=True)
        in_path.write_text("nope\n")
        contract = _base_contract(
            source={
                "kind": "filesystem",
                "connection": {"uri": str(in_path)},
                "mode": "full_refresh",
                "reader": {"format": "weirdformat"},
            },
        )
        rc = execute_dlt_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0

    def test_missing_source_block_fails(self, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "filesystem",
                "connection": {"uri": "/tmp/nope"},
                "mode": "full_refresh",
            },
        )
        # Strip the required `source` to simulate validator failure at runtime.
        del contract["builds"][0]["properties"]["source"]
        rc = execute_dlt_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0


# ── Dispatcher integration via base ─────────────────────────────────────


class TestDltDispatcher:
    def test_base_dispatches_to_dlt(self, tmp_path: Path):
        """Engine 'dlt' on an acquisition build is routed through the dispatcher."""
        from fluid_build.build_runners.base import (
            ACQUISITION_ENGINES,
            _execute_acquisition_build,
            is_acquisition_build,
        )

        assert "dlt" in ACQUISITION_ENGINES

        in_csv = tmp_path / "in" / "data.csv"
        _make_csv(in_csv)
        out_db = tmp_path / "out" / "data.duckdb"
        contract = _base_contract(
            source={
                "kind": "filesystem",
                "connection": {"uri": str(in_csv)},
                "mode": "full_refresh",
                "reader": {"format": "csv", "options": {"header": True}},
            },
            expose_path=str(out_db),
        )
        build = contract["builds"][0]
        assert is_acquisition_build(build)
        rc = _execute_acquisition_build(build, contract, tmp_path, dry_run=False, sample_rows=None)
        assert rc == 0
