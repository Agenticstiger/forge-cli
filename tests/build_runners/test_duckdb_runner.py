# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""DuckDB acquisition runner — unit + integration tests.

Filesystem-based tests run unconditionally; postgres tests require a live
Postgres (docker-compose / Testcontainers) and are gated by an env var.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.api.runner import RunState
from fluid_build.build_runners.duckdb.runner import DuckdbRunner, execute_duckdb_build

# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def sample_csv(tmp_path: Path) -> Path:
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    p = in_dir / "orders.csv"
    p.write_text(
        "id,customer,amount\n1,Alice,100.50\n2,Bob,250.00\n3,Carol,42.00\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def csv_contract(sample_csv: Path, tmp_path: Path) -> Dict[str, Any]:
    out_path = str((tmp_path / "out" / "orders.parquet").resolve())
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.orders_csv",
        "name": "Orders CSV ingest",
        "metadata": {
            "layer": "Bronze",
            "owner": {"team": "data-platform", "email": "dp@co.example"},
        },
        "builds": [
            {
                "id": "ingest_orders",
                "pattern": "acquisition",
                "engine": "duckdb",
                "capabilities": ["full_refresh"],
                "properties": {
                    "source": {
                        "kind": "filesystem",
                        "connection": {"uri": str(sample_csv.parent / "*.csv")},
                        "mode": "full_refresh",
                        "reader": {"format": "csv", "options": {"header": True}},
                    },
                    "sink": {"format": "parquet"},
                },
                "outputs": ["orders_raw"],
            }
        ],
        "exposes": [
            {
                "exposeId": "orders_raw",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": out_path},
                },
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }


# ── filesystem CSV ────────────────────────────────────────────────────────


class TestDuckdbFilesystemCsv:
    def test_runs_end_to_end(self, csv_contract, tmp_path):
        rc = execute_duckdb_build(
            csv_contract["builds"][0],
            csv_contract,
            tmp_path,
            dry_run=False,
        )
        assert rc == 0, "expected DuckDB acquisition to exit 0"
        out = Path(csv_contract["exposes"][0]["binding"]["location"]["path"])
        assert out.exists(), f"expected output parquet at {out}"
        assert out.stat().st_size > 0

    def test_dry_run_no_output(self, csv_contract, tmp_path):
        out = Path(csv_contract["exposes"][0]["binding"]["location"]["path"])
        rc = execute_duckdb_build(
            csv_contract["builds"][0],
            csv_contract,
            tmp_path,
            dry_run=True,
        )
        assert rc == 0
        assert not out.exists(), "dry-run must not write output"

    def test_sample_rows_truncates(self, csv_contract, tmp_path):
        rc = execute_duckdb_build(
            csv_contract["builds"][0],
            csv_contract,
            tmp_path,
            dry_run=False,
            sample_rows=2,
        )
        assert rc == 0
        # Verify row count via DuckDB.
        import duckdb  # type: ignore

        con = duckdb.connect(":memory:")
        try:
            n = con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{csv_contract['exposes'][0]['binding']['location']['path']}')"
            ).fetchone()[0]
        finally:
            con.close()
        assert n == 2

    def test_run_record_persisted(self, csv_contract, tmp_path):
        execute_duckdb_build(csv_contract["builds"][0], csv_contract, tmp_path, dry_run=False)
        runs_dir = (
            tmp_path
            / ".fluid"
            / "runs"
            / csv_contract["id"]
            / csv_contract["builds"][0]["id"]
            / "runs"
        )
        records = list(runs_dir.glob("*.json"))
        assert len(records) == 1
        import json

        rec = json.loads(records[0].read_text())
        assert rec["state"] == "succeeded"
        assert rec["records_total"] == 3

    def test_runner_protocol_attributes(self):
        r = DuckdbRunner()
        assert r.name == "duckdb"
        assert "embedded" in r.declared_modes
        from fluid_build.api.runner import RunnerCapability

        assert RunnerCapability.FULL_REFRESH in r.declared_capabilities
        assert RunnerCapability.SCHEMA_DISCOVERY in r.declared_capabilities


# ── filesystem failure modes ──────────────────────────────────────────────


class TestDuckdbFilesystemFailures:
    def test_missing_uri_fails(self, tmp_path):
        contract = {
            "fluidVersion": "0.7.3",
            "id": "bronze.bad",
            "name": "x",
            "metadata": {"layer": "Bronze"},
            "builds": [
                {
                    "id": "b",
                    "pattern": "acquisition",
                    "engine": "duckdb",
                    "properties": {
                        "source": {
                            "kind": "filesystem",
                            "connection": {"uri": str(tmp_path / "does-not-exist-*.csv")},
                            "mode": "full_refresh",
                            "reader": {"format": "csv"},
                        },
                        "sink": {"format": "parquet"},
                    },
                    "outputs": ["x"],
                }
            ],
            "exposes": [
                {
                    "exposeId": "x",
                    "kind": "table",
                    "binding": {
                        "platform": "local",
                        "format": "parquet",
                        "location": {"path": str(tmp_path / "out.parquet")},
                    },
                    "contract": {"schema": []},
                }
            ],
        }
        rc = execute_duckdb_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0


# ── postgres (docker-required) ────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("FLUID_TEST_POSTGRES_HOST") is None,
    reason="Set FLUID_TEST_POSTGRES_HOST/PORT/USER/PASSWORD/DB to run; CI uses docker-compose.",
)
class TestDuckdbPostgresLive:
    def test_postgres_scan_round_trip(self, tmp_path):
        host = os.environ["FLUID_TEST_POSTGRES_HOST"]
        port = int(os.environ.get("FLUID_TEST_POSTGRES_PORT", "5432"))
        user = os.environ.get("FLUID_TEST_POSTGRES_USER", "postgres")
        password = os.environ.get("FLUID_TEST_POSTGRES_PASSWORD", "postgres")
        database = os.environ.get("FLUID_TEST_POSTGRES_DB", "postgres")

        # Seed a tiny table.
        import duckdb  # type: ignore

        con = duckdb.connect(":memory:")
        try:
            con.execute("INSTALL postgres; LOAD postgres;")
            con.execute(
                f"ATTACH 'host={host} port={port} user={user} password={password} dbname={database}' AS pg (TYPE postgres)"
            )
            con.execute("CREATE SCHEMA IF NOT EXISTS pg.public")
            con.execute("DROP TABLE IF EXISTS pg.public.fluid_test_orders")
            con.execute(
                "CREATE TABLE pg.public.fluid_test_orders AS "
                "SELECT * FROM (VALUES (1,'Alice',100.5), (2,'Bob',250.0), (3,'Carol',42.0)) t(id, customer, amount)"
            )
        finally:
            con.close()

        out_path = tmp_path / "orders.parquet"
        contract = {
            "fluidVersion": "0.7.3",
            "id": "bronze.pg_orders",
            "name": "Postgres ingest",
            "metadata": {"layer": "Bronze"},
            "builds": [
                {
                    "id": "ingest_pg",
                    "pattern": "acquisition",
                    "engine": "duckdb",
                    "properties": {
                        "source": {
                            "kind": "postgres",
                            "connection": {
                                "host": host,
                                "port": port,
                                "user": user,
                                "password": password,
                                "database": database,
                            },
                            "mode": "full_refresh",
                            "streams": ["public.fluid_test_orders"],
                        },
                        "sink": {"format": "parquet"},
                    },
                    "outputs": ["orders"],
                }
            ],
            "exposes": [
                {
                    "exposeId": "orders",
                    "kind": "table",
                    "binding": {
                        "platform": "local",
                        "format": "parquet",
                        "location": {"path": str(out_path)},
                    },
                    "contract": {"schema": []},
                }
            ],
        }
        rc = execute_duckdb_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        assert out_path.exists()

        con = duckdb.connect(":memory:")
        try:
            n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out_path}')").fetchone()[0]
        finally:
            con.close()
        assert n == 3


# ── mysql (docker-required) ───────────────────────────────────────────────
#
# Pin: source.kind == 'mysql' must produce a working SDP via the
# duckdb mysql extension (ATTACH-then-reference). This pre-existed as
# a documented kind in the runner header but the dispatch raised
# ``ValueError: unsupported source.kind 'mysql'`` until the SDP/ADP
# E2E pass surfaced it. Don't let that regress.


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("FLUID_TEST_MYSQL_HOST") is None,
    reason="Set FLUID_TEST_MYSQL_HOST/PORT/USER/PASSWORD/DB to run; CI uses docker-compose.",
)
class TestDuckdbMysqlLive:
    def test_mysql_scan_round_trip(self, tmp_path):
        host = os.environ["FLUID_TEST_MYSQL_HOST"]
        port = int(os.environ.get("FLUID_TEST_MYSQL_PORT", "3306"))
        user = os.environ.get("FLUID_TEST_MYSQL_USER", "root")
        password = os.environ.get("FLUID_TEST_MYSQL_PASSWORD", "")
        database = os.environ.get("FLUID_TEST_MYSQL_DB", "test")

        import duckdb  # type: ignore

        con = duckdb.connect(":memory:")
        try:
            con.execute("INSTALL mysql; LOAD mysql;")
            con.execute(
                f"ATTACH 'host={host} port={port} user={user} "
                f"password={password} database={database}' AS my (TYPE mysql)"
            )
            con.execute("DROP TABLE IF EXISTS my.fluid_test_widgets")
            con.execute(
                "CREATE TABLE my.fluid_test_widgets AS "
                "SELECT * FROM (VALUES (1,'A',9.99), (2,'B',19.99)) t(id, sku, price)"
            )
        finally:
            con.close()

        out_path = tmp_path / "widgets.parquet"
        contract = {
            "fluidVersion": "0.7.3",
            "id": "bronze.my_widgets",
            "name": "MySQL ingest",
            "metadata": {"layer": "Bronze"},
            "builds": [
                {
                    "id": "ingest_my",
                    "pattern": "acquisition",
                    "engine": "duckdb",
                    "properties": {
                        "source": {
                            "kind": "mysql",
                            "connection": {
                                "host": host,
                                "port": port,
                                "user": user,
                                "password": password,
                                "database": database,
                            },
                            "mode": "full_refresh",
                            "streams": [f"{database}.fluid_test_widgets"],
                        },
                        "sink": {"format": "parquet"},
                    },
                    "outputs": ["widgets"],
                }
            ],
            "exposes": [
                {
                    "exposeId": "widgets",
                    "kind": "table",
                    "binding": {
                        "platform": "local",
                        "format": "parquet",
                        "location": {"path": str(out_path)},
                    },
                    "contract": {"schema": []},
                }
            ],
        }
        rc = execute_duckdb_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        assert out_path.exists()

        con = duckdb.connect(":memory:")
        try:
            n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{out_path}')").fetchone()[0]
        finally:
            con.close()
        assert n == 2
