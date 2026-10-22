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

"""DuckDB acquisition runner — unit + integration tests.

Filesystem-based tests run unconditionally; the live postgres and mysql round
trips need a server and run only when their FLUID_TEST_* variables are set (no
CI job sets them).
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


# ── postgres, no server ───────────────────────────────────────────────────


_UNSET_SECRET_REF = "env://FLUID_TEST_UNSET_SOURCE_SECRET"  # pragma: allowlist secret
_REFUSED_SECRET_REF = "file://a/b"  # pragma: allowlist secret

_CONNECT_FAILED = {
    "postgres": "Unable to connect to Postgres",
    "mysql": "Failed to connect to MySQL",
}


def _require_extension(kind: str) -> None:
    duckdb = pytest.importorskip("duckdb")
    try:
        probe = duckdb.connect()
        probe.execute(f"INSTALL {kind}; LOAD {kind}")
        probe.close()
    except duckdb.Error:
        pytest.skip(f"the {kind} extension is not installable here (offline)")


def _database_source_contract(kind: str, connection: Dict[str, Any], out_path: Path) -> dict:
    """One acquisition build reading ``app.orders`` from a ``kind`` database."""
    return {
        "fluidVersion": "0.7.3",
        "id": "bronze.db_orders",
        "builds": [
            {
                "id": "ingest_db",
                "pattern": "acquisition",
                "engine": "duckdb",
                "properties": {
                    "source": {
                        "kind": kind,
                        "connection": connection,
                        "mode": "full_refresh",
                        "streams": ["app.orders"],
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


@pytest.mark.parametrize("with_password", [True, False], ids=["password", "no-password"])
@pytest.mark.parametrize("kind", ["postgres", "mysql"])
def test_an_unreachable_database_source_fails_the_build_not_the_process(
    tmp_path, caplog, kind, with_password
):
    """The source is attached as the run's connection opens (DuckDB 2.0 has no
    ``postgres_scan``), so its connect error is reported as the build's failure,
    with the password masked. The error is a connection failure: not a function
    DuckDB 2.0 lacks, nor the "Unknown secret storage found: 'local_file'" that
    an ATTACH without a named secret hits under the sandbox."""
    _require_extension(kind)
    password = "runner-unreachable-db-pw-6T"  # pragma: allowlist secret
    out_path = tmp_path / "orders.parquet"
    connection = {
        "host": "127.0.0.1",
        "port": 9,  # discard: nothing listens
        "user": "fluid",
        **({"password": password} if with_password else {}),
        "database": "app",
    }
    contract = _database_source_contract(kind, connection, out_path)
    caplog.set_level("DEBUG")
    rc = execute_duckdb_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc == 1
    assert not out_path.exists()
    assert _CONNECT_FAILED[kind] in caplog.text, caplog.text[-2000:]
    assert password not in caplog.text
    for written in tmp_path.rglob("*"):
        if written.is_file():
            assert password not in written.read_text(encoding="utf-8", errors="replace"), written


@pytest.mark.parametrize(
    "refused, reason",
    [
        ({"port": "abc"}, "connection.port must be numeric"),
        # The run record's redactor reads "<NAME>_SECRET: environment" as an
        # assignment and masks its value, so the record says "variable not set".
        ({"secretRef": _UNSET_SECRET_REF}, "variable not set"),
        # The file backend refuses a name with a separator: a ConfigurationError,
        # as every secret backend but env:// raises, not a ValueError.
        ({"secretRef": _REFUSED_SECRET_REF}, "Invalid secret name"),
    ],
    ids=["port", "unset-secret-ref", "secret-backend-error"],
)
@pytest.mark.parametrize("kind", ["postgres", "mysql"])
def test_a_source_the_runner_cannot_set_up_fails_the_build_with_a_run_record(
    tmp_path, monkeypatch, kind, refused, reason
):
    """A connection block refused before any attach (a port that is not a
    number, a secretRef that does not resolve) fails the build as a connect
    error does: exit 1 and a failed run record, not an exception out of the
    runner that ends `fluid apply` before its other builds run."""
    import json

    _require_extension(kind)
    monkeypatch.delenv("FLUID_TEST_UNSET_SOURCE_SECRET", raising=False)
    out_path = tmp_path / "orders.parquet"
    connection = {"host": "127.0.0.1", "port": 9, "user": "fluid", "database": "app", **refused}
    contract = _database_source_contract(kind, connection, out_path)
    rc = execute_duckdb_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc == 1
    assert not out_path.exists()
    runs = tmp_path / ".fluid" / "runs" / "bronze.db_orders" / "ingest_db" / "runs"
    (record,) = [json.loads(p.read_text(encoding="utf-8")) for p in runs.glob("*.json")]
    assert record["state"] == "failed"
    (stream,) = record["streams"]
    assert stream["state"] == "failed"
    assert stream["error"].startswith("source: ") and reason in stream["error"], stream


def test_a_postgres_enum_column_reads_as_text_as_postgres_scan_read_it():
    """``postgres_scan`` typed a Postgres enum ``VARCHAR``; the ``ATTACH``-ed
    catalog types it ``ENUM(...)``, which the schema gate took for a type
    change against a declared ``VARCHAR``. DuckDB's own enum stands in here
    for the attached one: both describe as ``ENUM('a', 'b')``."""
    import types

    duckdb = pytest.importorskip("duckdb")
    from fluid_build.build_runners.duckdb.runner import (
        _postgres_alias_for_build,
        _postgres_enums_as_varchar,
        _select_for_stream,
    )

    con = duckdb.connect()
    # Under the alias the runner attaches the source as.
    alias = _postgres_alias_for_build("b1")
    con.execute(f"ATTACH ':memory:' AS {alias}")
    con.execute(f"CREATE SCHEMA {alias}.app")
    con.execute(
        f"CREATE TABLE {alias}.app.src (id INTEGER, mood ENUM('sad', 'ok', 'happy'), "
        "moods ENUM('sad', 'ok', 'happy')[], \"Odd \"\"Name\" ENUM('a)b', 'c'))"
    )
    con.execute(f"INSERT INTO {alias}.app.src VALUES (1, 'ok', ['sad', 'happy'], 'a)b')")
    ctx = types.SimpleNamespace(
        build_id="b1",
        source=types.SimpleNamespace(kind="postgres", connection=types.SimpleNamespace(raw={})),
    )
    select_sql = _select_for_stream(con, ctx, "app.src")
    assert [(row[0], row[1]) for row in con.execute(f"DESCRIBE {select_sql}").fetchall()] == [
        ("id", "INTEGER"),
        ("mood", "VARCHAR"),
        ("moods", "VARCHAR[]"),
        ('Odd "Name', "VARCHAR"),
    ]
    assert con.execute(select_sql).fetchall() == [(1, "ok", ["sad", "happy"], "a)b")]
    # Text order, not declaration order: 'ok' < 'sad' as text, not as this enum.
    assert con.execute(f"SELECT mood < 'sad' FROM ({select_sql})").fetchall() == [(True,)]
    no_enum = f"SELECT id FROM {alias}.app.src"
    assert _postgres_enums_as_varchar(con, no_enum) == no_enum


def test_a_database_source_that_drifts_from_its_declared_schema_is_refused(tmp_path):
    """The schema gate fingerprints the source on a connection of its own,
    which must attach the source as the run's does: a database source exists
    only through that ``ATTACH``. The gate swallows a fingerprint that fails,
    so a fingerprint that cannot see the source would land drifted data."""
    import sqlite3

    from fluid_build._errors import SchemaDriftError

    _require_extension("sqlite")
    db = tmp_path / "src.sqlite"
    source = sqlite3.connect(db)
    source.execute("CREATE TABLE orders (id INTEGER, name TEXT)")
    source.execute("INSERT INTO orders VALUES (1, 'a'), (2, 'b')")
    source.commit()
    source.close()
    out = tmp_path / "orders.parquet"
    contract = {
        "fluidVersion": "0.7.3",
        "id": "bronze.sqlite_orders",
        "builds": [
            {
                "id": "ingest_sqlite",
                "pattern": "acquisition",
                "engine": "duckdb",
                "properties": {
                    "source": {
                        "kind": "sqlite",
                        "connection": {"path": str(db)},
                        "mode": "full_refresh",
                        "streams": ["orders"],
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
                    "location": {"path": str(out)},
                },
                "contract": {
                    "schemaPolicy": "strict",
                    "schema": [
                        {"name": "id", "type": "integer"},
                        {"name": "not_in_the_source", "type": "string"},
                    ],
                },
            }
        ],
    }
    with pytest.raises(SchemaDriftError):
        execute_duckdb_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert not out.exists()


# ── postgres (docker-required) ────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("FLUID_TEST_POSTGRES_HOST") is None,
    reason="Set FLUID_TEST_POSTGRES_HOST/PORT/USER/PASSWORD/DB to run (no CI job sets them).",
)
class TestDuckdbPostgresLive:
    def test_postgres_round_trip(self, tmp_path):
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
    reason="Set FLUID_TEST_MYSQL_HOST/PORT/USER/PASSWORD/DB to run (no CI job sets them).",
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
