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

"""Pin the JDBC ``--source <jdbc>`` path of ``fluid forge data-model
from-source``.

Phase 2.6 of the world-class plan adds postgres / mysql / sqlite as
sources alongside the existing 7 catalogs. The implementation reuses
the duckdb extension scanner, so this test exercises the SQLite
extension path (no external infra) to pin:

* The introspection helper round-trips: a real SQLite file with two
  tables produces an :class:`IntrospectedDatabase` with the right
  tables + columns + types.
* The CLI dispatcher writes a v0.7.3 contract under ``--output`` with
  one ``exposes[]`` entry per table.
* The JDBC type → logical type mapping is reasonable.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

duckdb = pytest.importorskip("duckdb")


@pytest.fixture
def sqlite_db(tmp_path: Path) -> Path:
    """Create a real SQLite database with two tables to introspect."""
    db_path = tmp_path / "fixtures.sqlite"
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL,
            created_at DATETIME
        )
        """)
    cur.execute("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            amount REAL,
            ordered_at DATETIME
        )
        """)
    con.commit()
    con.close()
    return db_path


class TestIntrospectJdbc:
    def test_sqlite_round_trip(self, sqlite_db: Path):
        from fluid_build.cli.discover._jdbc_introspect import introspect_jdbc

        db = introspect_jdbc(
            source="sqlite",
            uri=f"sqlite:///{sqlite_db}",
        )
        # Two tables enumerated.
        names = sorted(t.name for t in db.tables)
        assert names == ["customers", "orders"]

        customers = next(t for t in db.tables if t.name == "customers")
        cols = sorted(c.name for c in customers.columns)
        assert cols == ["created_at", "email", "id"]

    def test_table_filter_narrows_results(self, sqlite_db: Path):
        from fluid_build.cli.discover._jdbc_introspect import introspect_jdbc

        db = introspect_jdbc(
            source="sqlite",
            uri=f"sqlite:///{sqlite_db}",
            table_filter=["orders"],
        )
        assert [t.name for t in db.tables] == ["orders"]

    def test_unsupported_source_raises(self):
        from fluid_build.cli.discover._jdbc_introspect import introspect_jdbc

        with pytest.raises(ValueError, match="Unsupported"):
            introspect_jdbc(source="oracle", uri="x")

    def test_postgresql_aliases_to_postgres(self):
        from fluid_build.cli.discover._jdbc_introspect import (
            _normalize_kind,
        )

        assert _normalize_kind("postgresql") == "postgres"
        assert _normalize_kind("postgres") == "postgres"


class TestRunFromJdbcSource:
    def test_emits_v073_contract_from_sqlite(self, sqlite_db: Path, tmp_path: Path):
        import logging

        from fluid_build.cli.forge_data_model import _run_from_jdbc_source

        out = tmp_path / "out.fluid.yaml"
        args = SimpleNamespace(
            source="sqlite",
            uri=f"sqlite:///{sqlite_db}",
            schema_name=None,
            tables=None,
            name="forged_sqlite",
            output=str(out),
        )
        rc = _run_from_jdbc_source(args, logging.getLogger("test"))
        assert rc == 0
        assert out.exists()

        import yaml as _yaml

        contract = _yaml.safe_load(out.read_text())
        assert contract["fluidVersion"] == "0.7.3"
        assert contract["id"] == "forged_sqlite"
        assert contract["metadata"]["productType"] == "SDP"
        exposes = {e["id"]: e for e in contract["exposes"]}
        assert set(exposes) == {"customers", "orders"}

        customers_schema = exposes["customers"]["contract"]["schema"]
        col_names = [c["name"] for c in customers_schema]
        assert set(col_names) == {"id", "email", "created_at"}

    def test_missing_uri_returns_error_code(self, tmp_path: Path):
        import logging

        from fluid_build.cli.forge_data_model import _run_from_jdbc_source

        args = SimpleNamespace(
            source="postgres",
            uri=None,
            schema_name=None,
            tables=None,
            name=None,
            output=str(tmp_path / "x.yaml"),
        )
        rc = _run_from_jdbc_source(args, logging.getLogger("test"))
        assert rc == 2


class TestJdbcTypeMapping:
    def test_integer_types(self):
        from fluid_build.cli.forge_data_model import _map_jdbc_type_to_logical

        for t in ("integer", "int", "smallint", "serial", "int8"):
            assert _map_jdbc_type_to_logical(t) == "integer"

    def test_decimal_types(self):
        from fluid_build.cli.forge_data_model import _map_jdbc_type_to_logical

        for t in ("numeric", "decimal", "real", "double precision", "float"):
            assert _map_jdbc_type_to_logical(t) == "decimal"

    def test_string_types(self):
        from fluid_build.cli.forge_data_model import _map_jdbc_type_to_logical

        for t in ("varchar", "text", "char", "uuid", "json"):
            assert _map_jdbc_type_to_logical(t) == "string"

    def test_temporal_types(self):
        from fluid_build.cli.forge_data_model import _map_jdbc_type_to_logical

        assert _map_jdbc_type_to_logical("timestamp") == "timestamp"
        assert _map_jdbc_type_to_logical("datetime") == "timestamp"
        assert _map_jdbc_type_to_logical("date") == "date"
        assert _map_jdbc_type_to_logical("time") == "time"
