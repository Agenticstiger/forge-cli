# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""``fluid init --discover`` full matrix tests (Slice K).

Filesystem discovery exercised pure-locally; Postgres + MySQL behind
Testcontainers. The contract emitter is a deterministic pure function and
gets exhaustive unit coverage independent of the discoverers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.cli.discover import (
    DiscoveredStream,
    FilesystemDiscoverer,
    PostgresDiscoverer,
    get_discoverer,
)
from fluid_build.cli.discover.emitter import emit_contract
from fluid_build.cli.discover.registry import DiscoveredColumn
from fluid_build.schema_manager import FluidSchemaManager

# ── Discoverer registry ─────────────────────────────────────────────────


class TestDiscovererRegistry:
    def test_postgres_uri_routes_to_postgres(self):
        d = get_discoverer("postgres://u:p@h/db")
        assert isinstance(d, PostgresDiscoverer)

    def test_mysql_uri_routes_to_mysql(self):
        d = get_discoverer("mysql://u:p@h/db")
        assert d is not None and d.scheme == "mysql"

    def test_https_uri_routes_to_filesystem(self):
        d = get_discoverer("https://example.com/data.csv")
        assert isinstance(d, FilesystemDiscoverer)

    def test_s3_uri_routes_to_filesystem(self):
        d = get_discoverer("s3://bucket/prefix/")
        assert isinstance(d, FilesystemDiscoverer)

    def test_bare_path_routes_to_filesystem(self):
        d = get_discoverer("/tmp/data.csv")
        assert isinstance(d, FilesystemDiscoverer)


# ── Filesystem discoverer ──────────────────────────────────────────────


class TestFilesystemDiscoverer:
    def test_discovers_csv_columns(self, tmp_path: Path):
        f = tmp_path / "orders.csv"
        f.write_text("id,name,amount\n1,a,1.0\n2,b,2.0\n", encoding="utf-8")
        streams = FilesystemDiscoverer().discover(str(f))
        assert len(streams) == 1
        cols = [c.name for c in streams[0].columns]
        assert cols == ["id", "name", "amount"]

    def test_discovers_parquet_columns(self, tmp_path: Path):
        import duckdb

        out = tmp_path / "data.parquet"
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                f"COPY (SELECT 1 AS id, 'x' AS name, 1.5 AS amount) TO '{out}' (FORMAT 'parquet')"
            )
        finally:
            con.close()
        streams = FilesystemDiscoverer().discover(str(out))
        assert {c.name for c in streams[0].columns} == {"id", "name", "amount"}

    def test_discovers_json_columns(self, tmp_path: Path):
        f = tmp_path / "events.jsonl"
        f.write_text(
            json.dumps({"id": 1, "label": "a"}) + "\n" + json.dumps({"id": 2, "label": "b"}),
            encoding="utf-8",
        )
        streams = FilesystemDiscoverer().discover(str(f))
        cols = {c.name for c in streams[0].columns}
        assert {"id", "label"} <= cols

    def test_directory_yields_one_stream_per_file(self, tmp_path: Path):
        (tmp_path / "a.csv").write_text("id\n1\n", encoding="utf-8")
        (tmp_path / "b.csv").write_text("id\n2\n", encoding="utf-8")
        streams = FilesystemDiscoverer().discover(str(tmp_path))
        names = sorted(s.name for s in streams)
        assert names == ["a", "b"]

    def test_missing_path_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            FilesystemDiscoverer().discover(str(tmp_path / "nope.csv"))


# ── Postgres discoverer (Testcontainers) ───────────────────────────────


class TestPostgresDiscoverer:
    def test_discovers_seeded_tables(self, seeded_postgres: Dict[str, Any]):
        pg = seeded_postgres
        uri = f"postgres://{pg['user']}:{pg['password']}@{pg['host']}:{pg['port']}/{pg['database']}"
        streams = PostgresDiscoverer().discover(uri)
        names = {s.name for s in streams}
        assert "public.fluid_test_orders" in names
        orders = next(s for s in streams if s.name == "public.fluid_test_orders")
        col_names = {c.name.lower() for c in orders.columns}
        assert {"id", "customer", "amount"} <= col_names

    def test_invalid_uri_raises(self):
        with pytest.raises(ValueError):
            PostgresDiscoverer().discover("https://not-postgres")


# ── Contract emitter ───────────────────────────────────────────────────


class TestContractEmitter:
    def test_emits_validatable_contract(self):
        streams = [
            DiscoveredStream(
                name="public.orders",
                columns=[
                    DiscoveredColumn(name="id", type="bigint", nullable=False),
                    DiscoveredColumn(name="amount", type="decimal", nullable=True),
                ],
            ),
        ]
        contract = emit_contract(
            product_id="bronze.test_orders",
            name="Test Orders",
            domain="sales",
            owner_team="data-platform",
            owner_email="dp@x.y",
            engine="duckdb",
            source_kind="postgres",
            connection={"host": "x"},
            streams=streams,
        )
        result = FluidSchemaManager().validate_contract(contract, "0.7.3", offline_only=True)
        assert result.is_valid, result.errors

    def test_safe_id_for_dotted_stream_names(self):
        streams = [
            DiscoveredStream(
                name="public.orders-v2",
                columns=[DiscoveredColumn(name="id", type="bigint")],
            ),
        ]
        contract = emit_contract(
            product_id="bronze.test",
            name="X",
            domain="d",
            owner_team="t",
            owner_email="x@y.z",
            engine="duckdb",
            source_kind="postgres",
            connection={"host": "x"},
            streams=streams,
        )
        # exposeId/outputs must be safe identifiers (no dots, no hyphens).
        assert contract["exposes"][0]["exposeId"] == "public_orders_v2"
        assert contract["builds"][0]["outputs"] == ["public_orders_v2"]

    def test_multi_stream_emits_one_expose_each(self):
        streams = [
            DiscoveredStream(name="orders", columns=[DiscoveredColumn(name="id", type="int")]),
            DiscoveredStream(name="customers", columns=[DiscoveredColumn(name="id", type="int")]),
        ]
        contract = emit_contract(
            product_id="bronze.multi",
            name="Multi",
            domain="d",
            owner_team="t",
            owner_email="x@y.z",
            engine="meltano",
            source_kind="fake-fluid",
            connection={},
            streams=streams,
        )
        assert len(contract["exposes"]) == 2
        assert len(contract["builds"][0]["outputs"]) == 2

    def test_required_flag_inverts_nullable(self):
        streams = [
            DiscoveredStream(
                name="x",
                columns=[
                    DiscoveredColumn(name="id", type="bigint", nullable=False),
                    DiscoveredColumn(name="opt", type="varchar", nullable=True),
                ],
            ),
        ]
        contract = emit_contract(
            product_id="bronze.x",
            name="X",
            domain="d",
            owner_team="t",
            owner_email="x@y.z",
            engine="duckdb",
            source_kind="postgres",
            connection={"host": "x"},
            streams=streams,
        )
        cols = contract["exposes"][0]["contract"]["schema"]
        id_col = next(c for c in cols if c["name"] == "id")
        opt_col = next(c for c in cols if c["name"] == "opt")
        assert id_col["required"] is True
        assert opt_col["required"] is False

    def test_emitter_is_deterministic(self):
        streams = [DiscoveredStream(name="x", columns=[DiscoveredColumn(name="id", type="int")])]
        kwargs = dict(
            product_id="bronze.x",
            name="X",
            domain="d",
            owner_team="t",
            owner_email="x@y.z",
            engine="duckdb",
            source_kind="postgres",
            connection={"host": "x"},
            streams=streams,
        )
        a = emit_contract(**kwargs)
        b = emit_contract(**kwargs)
        assert a == b

    def test_engine_choice_is_propagated(self):
        contract = emit_contract(
            product_id="bronze.x",
            name="X",
            domain="d",
            owner_team="t",
            owner_email="x@y.z",
            engine="airbyte",
            source_kind="salesforce",
            connection={"instance_url": "https://x.salesforce.com"},
            streams=[DiscoveredStream(name="Account", columns=[])],
        )
        assert contract["builds"][0]["engine"] == "airbyte"
        assert contract["builds"][0]["properties"]["source"]["kind"] == "salesforce"


# ── Postgres → emitter end-to-end ──────────────────────────────────────


class TestPostgresDiscoverToContractE2E:
    def test_e2e_validates_against_schema(self, seeded_postgres: Dict[str, Any]):
        pg = seeded_postgres
        uri = f"postgres://{pg['user']}:{pg['password']}@{pg['host']}:{pg['port']}/{pg['database']}"
        streams = PostgresDiscoverer().discover(uri)
        contract = emit_contract(
            product_id="bronze.discovered_pg",
            name="Discovered Postgres",
            domain="imported",
            owner_team="data-platform",
            owner_email="dp@x.y",
            engine="duckdb",
            source_kind="postgres",
            connection={
                "host": pg["host"],
                "port": pg["port"],
                "user": pg["user"],
                "password": pg["password"],
                "database": pg["database"],
            },
            streams=streams,
        )
        result = FluidSchemaManager().validate_contract(contract, "0.7.3", offline_only=True)
        assert result.is_valid, result.errors
