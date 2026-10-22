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

"""One engine on DuckDB 1.5.x and 2.x: ``providers/_duckdb_compat.py``.

Each test runs on whichever DuckDB is installed and states what must hold on
both. CI runs the suite on 1.5.x (Python 3.10, which DuckDB 2.0 does not
support) and on 2.x (every newer Python). What 2.0 changed is in the module's
docstring; the cases here are the ones that broke forge-cli's own code.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.providers._duckdb_compat import (  # noqa: E402
    STORAGE_COMPATIBILITY_VERSION,
    first_row,
    one_row,
    query_relation,
    storage_compat_config,
)
from fluid_build.providers._duckdb_sandbox import (  # noqa: E402
    DuckDBAllowlist,
    duckdb_version,
    secure_duckdb_connect,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "fluid_build"
DUCKDB_2 = duckdb_version(duckdb) >= (2, 0, 0)


# ── first_row ────────────────────────────────────────────────────────────


def test_first_row_of_an_execute_result():
    assert first_row(duckdb.connect().execute("SELECT 41 + 1, 'x'")) == (42, "x")


def test_first_row_of_a_relation():
    """DuckDB 2.0 removed ``fetchone()`` from relations."""
    con = duckdb.connect()
    rel = con.sql("SELECT range AS a FROM range(3)")
    assert first_row(rel.aggregate("count(*)")) == (3,)
    assert first_row(rel.count("*")) == (3,)


def test_first_row_is_the_first_of_several_rows():
    con = duckdb.connect()
    assert first_row(con.execute("SELECT range FROM range(3) ORDER BY 1")) == (0,)
    assert first_row(con.sql("SELECT range FROM range(3) ORDER BY 1")) == (0,)


def test_first_row_of_no_rows_is_none():
    con = duckdb.connect()
    assert first_row(con.execute("SELECT 1 WHERE false")) is None
    assert first_row(con.sql("SELECT 1 WHERE false")) is None


def test_one_row_is_the_row_of_an_aggregate():
    con = duckdb.connect()
    assert one_row(con.execute("SELECT count(*) FROM range(0)")) == (0,)
    assert one_row(con.sql("SELECT max(range) FROM range(4)")) == (3,)


def test_one_row_refuses_an_empty_result():
    """An aggregate cannot return no row; a caller indexing [0] must not get None."""
    with pytest.raises(ValueError, match="no row"):
        one_row(duckdb.connect().execute("SELECT 1 WHERE false"))


def test_the_connection_is_free_after_first_row():
    """On DuckDB 2.0 a ``con.execute()`` result read only partway (``fetchone()``
    on a one-row ``COUNT(*)``) keeps the connection busy, and the next relation
    read, ``register`` or ``create_function`` on it raises
    ``ResourceInUseException``. ``first_row`` reads to the end."""
    pd = pytest.importorskip("pandas")
    pytest.importorskip("numpy")  # DuckDB's create_function needs it
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT range AS a FROM range(5)")

    assert first_row(con.execute("SELECT count(*) FROM t")) == (5,)
    assert con.sql("SELECT sum(a) FROM t").fetchall() == [(10,)]
    assert first_row(con.execute("SELECT a FROM t ORDER BY a LIMIT 1")) == (0,)
    con.register("frame", pd.DataFrame({"b": [1, 2]}))
    assert first_row(con.execute("SELECT max(a) FROM t")) == (4,)
    con.create_function("twice", lambda x: x * 2, ["BIGINT"], "BIGINT")
    assert con.sql("SELECT twice(b) FROM frame ORDER BY b").fetchall() == [(2,), (4,)]


@pytest.mark.skipif(not DUCKDB_2, reason="the hazard first_row avoids is DuckDB 2.0's")
def test_on_duckdb_2_a_partial_read_keeps_the_connection_busy():
    """What first_row exists for, pinned: if a later DuckDB relaxes this, the
    module docstring and this test go together."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT range AS a FROM range(5)")
    assert con.execute("SELECT count(*) FROM t").fetchone() == (5,)
    with pytest.raises(duckdb.Error, match="(?i)resource in use|busy|open result"):
        con.sql("SELECT 1").fetchall()


# ── query_relation ───────────────────────────────────────────────────────


def _table_t() -> Any:
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT range AS a FROM range(3)")
    return con


@pytest.mark.parametrize(
    "sql, columns",
    [
        ("SELECT 1 AS a", ["a"]),
        ("WITH x AS (SELECT 1 AS a) SELECT * FROM x", ["a"]),
        ("FROM t", ["a"]),
        ("CREATE TABLE made AS SELECT 1 AS q; SELECT * FROM made", ["q"]),
        ("INSERT INTO t VALUES (7) RETURNING a", ["a"]),
        ("UPDATE t SET a = a RETURNING a", ["a"]),
        ('SELECT count(*) AS "Count" FROM t', ["Count"]),
        ("PIVOT t ON a USING count(*)", ["0", "1", "2"]),
        ("DESCRIBE t", None),
        ("CALL pragma_version()", None),
        ("EXPLAIN SELECT 1", None),
    ],
)
def test_query_relation_keeps_a_query(sql, columns):
    rel = query_relation(_table_t(), sql)
    assert rel is not None
    if columns is not None:
        assert rel.columns == columns


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE made AS SELECT 1 AS q",
        "CREATE OR REPLACE VIEW v AS SELECT 1 AS q",
        "INSERT INTO t VALUES (7)",
        "UPDATE t SET a = a",
        "DELETE FROM t WHERE a = 100",
        "MERGE INTO t USING (SELECT 1 AS a) s ON t.a = s.a "
        "WHEN NOT MATCHED THEN INSERT VALUES (s.a)",
        "DROP TABLE t",
        "SET threads = 2",
        "SELECT 1 AS x; CREATE TABLE made AS SELECT 1 AS q",
        # DuckDB 2.0 types a PIVOT whose columns it looks up as MULTI, a query
        # or not: as DDL it returns a Success flag.
        "CREATE TABLE p AS PIVOT t ON a USING count(*)",
        # SQL ending in DML after a query: 2.0's con.sql() returns the query's rows.
        "CREATE TABLE o2 AS SELECT 1 AS id; SELECT * FROM o2; INSERT INTO o2 VALUES (3)",
        "SELECT a FROM t; DELETE FROM t WHERE a = 1",
        "SELECT a FROM t; UPDATE t SET a = a",
        "SELECT a FROM t; ALTER TABLE t ADD COLUMN b INTEGER",
        "SELECT 1 AS x; SET threads = 2",
        "SELECT 1 AS x; DROP TABLE t",
        "BEGIN; SELECT * FROM t; COMMIT",
    ],
)
def test_query_relation_is_none_for_a_statement_that_returns_no_rows(sql):
    """DuckDB 1.5 returned None here; 2.0 returns a relation over a row count,
    which an engine writing "the result" would write as a one-row Count file."""
    assert query_relation(_table_t(), sql) is None


def test_query_relation_of_a_copy_is_none(tmp_path):
    con = _table_t()
    assert query_relation(con, f"COPY t TO '{tmp_path / 'x.csv'}'") is None
    assert (tmp_path / "x.csv").read_text(encoding="utf-8").splitlines() == ["a", "0", "1", "2"]


def test_query_relation_runs_each_statement_once():
    con = _table_t()
    assert query_relation(con, "INSERT INTO t VALUES (7)") is None
    assert first_row(con.execute("SELECT count(*) FROM t WHERE a = 7")) == (1,)
    assert query_relation(con, "CREATE TABLE made AS SELECT 1 AS q") is None
    assert con.execute("SELECT q FROM made").fetchall() == [(1,)]
    rel = query_relation(con, "INSERT INTO t VALUES (8); SELECT count(*) AS n FROM t WHERE a = 8")
    assert rel.fetchall() == [(1,)]
    assert query_relation(con, "SELECT a FROM t; DELETE FROM t WHERE a = 8") is None
    assert first_row(con.execute("SELECT count(*) FROM t WHERE a = 8")) == (0,)


# ── the engine, end to end ───────────────────────────────────────────────


@pytest.fixture
def product(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    contract_dir = tmp_path / "product"
    (contract_dir / "data").mkdir(parents=True)
    (contract_dir / "data" / "orders.csv").write_text("id,amount\n1,10\n2,20\n", encoding="utf-8")
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(contract_dir)
    monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
    return contract_dir


def _local_apply(product: Path, *actions: Dict[str, Any]) -> List[Dict[str, Any]]:
    from fluid_build.providers.local.local import LocalProvider

    provider = LocalProvider(project="local", region="local", anchor_dir=product)
    load = {"op": "load_data", "path": str(product / "data" / "orders.csv"), "table_name": "orders"}
    return provider.apply(actions=[load, *actions])["results"]


def test_local_apply_counts_rows_and_materializes(product):
    """On DuckDB 2.0 the SQL action's row count was lost (``rows: None``) and a
    materialize step fell back to a skipped ``noop`` after writing its file."""
    out = product / "out"
    results = _local_apply(
        product,
        {
            "op": "sql",
            "sql": "SELECT * FROM orders",
            "output_table": "t1",
            "outputs": [str(out / "t1.parquet")],
        },
        {"op": "materialize", "source_table": "t1", "dst": str(out / "copy.csv")},
    )
    assert [r["status"] for r in results] == ["ok", "ok", "ok"], results
    assert results[0]["rows"] == 2
    assert results[1]["rows"] == 2
    assert results[2]["op"] == "materialize"
    assert (out / "copy.csv").read_text(encoding="utf-8").splitlines() == [
        "id,amount",
        "1,10",
        "2,20",
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE made AS SELECT * FROM orders",
        "CREATE TABLE made AS PIVOT orders ON id USING sum(amount)",
        "CREATE TABLE made AS SELECT * FROM orders; SELECT * FROM made; "
        "INSERT INTO made VALUES (3, 30)",
    ],
    ids=["ctas", "pivot-ctas", "dml-after-a-query"],
)
def test_local_sql_that_returns_no_rows_fails_the_same_on_both_versions(product, sql):
    """1.5 failed on ``None.write_csv``; 2.0 wrote a one-row ``Count`` file (or
    a ``Success`` header, or the rows before the INSERT) and reported ok. Both
    now refuse it and say what to write instead."""
    out = product / "out" / "made.csv"
    results = _local_apply(product, {"op": "sql", "sql": sql, "outputs": [str(out)]})
    assert results[1]["status"] == "error", results
    assert "returns no rows" in results[1]["error"]
    assert "write just the SELECT" in results[1]["error"]
    assert not out.exists()


def test_contract_tests_sql_that_returns_no_rows_fails_clearly():
    from fluid_build.contract_tests import LocalProviderError, _execute_sql

    with pytest.raises(LocalProviderError, match="returns no rows"):
        _execute_sql(duckdb.connect(), "CREATE TABLE made AS SELECT 1 AS q")
    assert _execute_sql(duckdb.connect(), "SELECT 1 AS q").fetchall() == [(1,)]


def test_contract_tests_dry_run_counts_a_real_relation(tmp_path):
    from fluid_build.contract_tests import _write_output

    rel = duckdb.connect().sql("SELECT range AS a FROM range(42)")
    out = tmp_path / "out.csv"
    assert _write_output(rel, {"path": str(out), "format": "csv"}, dry_run=True) == (42, None)
    assert not out.exists()


def test_a_remote_landing_is_probed_without_keeping_the_connection_busy(tmp_path, monkeypatch):
    """``_artifact_exists`` probes a remote landing with ``SELECT 1 ... LIMIT 1``
    on the run's own connection; on DuckDB 2.0 a result read only partway keeps
    the connection busy for the next relation read on it."""
    from fluid_build.build_runners.duckdb import runner

    landed = tmp_path / "landed.parquet"
    duckdb.connect().execute(f"COPY (SELECT 1 AS a) TO '{landed}' (FORMAT parquet)")
    monkeypatch.setattr(runner, "_is_remote_uri", lambda _path: True)
    con = duckdb.connect()
    assert runner._artifact_exists(con, str(landed), "read_parquet") is True
    assert con.sql(f"SELECT a FROM read_parquet('{landed}')").fetchall() == [(1,)]
    assert runner._artifact_exists(con, str(tmp_path / "missing.parquet"), "read_parquet") is False


@pytest.mark.parametrize("function", ["postgres_query", "mysql_query", "sqlite_scan", "read_xlsx"])
def test_a_function_of_an_extension_the_sandbox_did_not_load_reads_as_a_refusal(function):
    from fluid_build.providers._duckdb_sandbox import is_sandbox_refusal, is_unloaded_function

    con = secure_duckdb_connect(allow=DuckDBAllowlist.none())
    with pytest.raises(duckdb.Error) as refused:
        con.execute(f"SELECT * FROM {function}('x', 'y')")
    assert is_unloaded_function(refused.value), refused.value
    assert is_sandbox_refusal(refused.value)


@pytest.mark.skipif(not DUCKDB_2, reason="postgres_scan exists through DuckDB 1.5")
def test_duckdb_2_has_no_postgres_scan_even_with_the_extension_loaded():
    """Why the duckdb runner reads a Postgres source through ATTACH: DuckDB 2.0's
    postgres extension dropped ``postgres_scan``. Not a sandbox refusal either:
    loading the extension would not bring it back."""
    from fluid_build.providers._duckdb_sandbox import is_sandbox_refusal

    try:
        con = secure_duckdb_connect(allow=DuckDBAllowlist.none(), extensions=["postgres"])
    except duckdb.Error as exc:
        raise pytest.skip.Exception("the postgres extension is not installable here") from exc
    with pytest.raises(duckdb.CatalogException) as missing:
        con.execute("SELECT * FROM postgres_scan('host=127.0.0.1 port=9', 'public', 't')")
    assert "does not exist" in str(missing.value)
    assert not is_sandbox_refusal(missing.value)


def test_an_unknown_function_is_not_a_sandbox_refusal():
    from fluid_build.providers._duckdb_sandbox import is_sandbox_refusal

    con = secure_duckdb_connect(allow=DuckDBAllowlist.none())
    with pytest.raises(duckdb.Error) as missing:
        con.execute("SELECT * FROM no_such_function_anywhere()")
    assert not is_sandbox_refusal(missing.value)


def test_the_snowflake_mock_connects(tmp_path):
    """DuckDB 2.0 refuses ``CREATE SCHEMA information_schema`` (a system catalog)."""
    from fluid_build.providers.local.mocks import SnowflakeDuckDBMock

    mock = SnowflakeDuckDBMock(db_path=str(tmp_path / "sf.duckdb"))
    con = mock.connect()
    assert con.execute("SELECT count(*) FROM information_schema.schemata").fetchall()[0][0] > 0
    assert con.execute(
        "SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'PUBLIC'"
    ).fetchall() == [(1,)]
    con.close()


# ── storage format ───────────────────────────────────────────────────────


def _storage(con, name: str) -> str:
    tags = con.execute(
        "SELECT tags FROM duckdb_databases() WHERE database_name = ?", [name]
    ).fetchall()[0][0]
    return dict(tags)["storage_version"]


def test_storage_compat_config_pins_only_from_duckdb_2():
    assert storage_compat_config((1, 5, 0)) == {}
    assert storage_compat_config((1, 99, 99)) == {}
    for version in ((2, 0, 0), (2, 3, 1), (3, 0, 0)):
        assert storage_compat_config(version) == {
            "storage_compatibility_version": STORAGE_COMPATIBILITY_VERSION
        }
    # What DuckDB 1.5 writes by default: storage version 64, "v1.0.0+".
    assert STORAGE_COMPATIBILITY_VERSION == "v1.0.0"


def test_a_database_file_the_engine_creates_is_readable_by_duckdb_1_5(tmp_path):
    """A 2.x install must not write files a 1.5 one (Python 3.10, or a tool still
    pinned below 2) cannot open: 1.5.6 reads storage versions 64 to 68 only."""
    db = tmp_path / "new.duckdb"
    con = secure_duckdb_connect(db, allow=DuckDBAllowlist.none())
    con.execute("CREATE TABLE t AS SELECT 1 AS a")
    # The same format on both: 1.5's own default, which 2.x is pinned to.
    assert _storage(con, "new") == "v1.0.0+"
    assert con.execute("SELECT current_setting('storage_compatibility_version')").fetchall() == [
        (STORAGE_COMPATIBILITY_VERSION if DUCKDB_2 else "v0.10.2",)
    ]


def test_a_new_database_file_has_the_header_duckdb_1_5_writes(tmp_path):
    """Byte-level: storage version 64 in the main header, which DuckDB 1.0 and
    later read; 2.0's own default writes 999 there, which no 1.5 opens."""
    db = tmp_path / "header.duckdb"
    con = secure_duckdb_connect(db, allow=DuckDBAllowlist.none())
    con.execute("CREATE TABLE t AS SELECT 1 AS a")
    con.close()
    header = db.read_bytes()[:20]
    assert header[8:12] == b"DUCK"
    assert int.from_bytes(header[12:20], "little") == 64


def test_an_attached_new_database_follows_the_same_format(tmp_path):
    con = secure_duckdb_connect(
        allow=DuckDBAllowlist.none(),
        before_lock=lambda c: c.execute(f"ATTACH '{tmp_path / 'att.duckdb'}' AS att"),
    )
    con.execute("CREATE TABLE att.t AS SELECT 1 AS a")
    assert _storage(con, "att") == "v1.0.0+"


def test_a_database_file_that_exists_keeps_its_format(tmp_path):
    db = tmp_path / "theirs.duckdb"
    raw = duckdb.connect(str(db))
    raw.execute("CREATE TABLE t AS SELECT 1 AS a")
    before = _storage(raw, "theirs")
    raw.close()

    con = secure_duckdb_connect(db, allow=DuckDBAllowlist.none())
    con.execute("INSERT INTO t VALUES (2)")
    assert _storage(con, "theirs") == before
    assert con.execute("SELECT count(*) FROM t").fetchall() == [(2,)]
    con.close()
    con = secure_duckdb_connect(db, allow=DuckDBAllowlist.none(), read_only=True)
    assert con.execute("SELECT count(*) FROM t").fetchall() == [(2,)]


@pytest.mark.skipif(not DUCKDB_2, reason="only 2.x has a newer format to choose")
def test_a_callers_own_storage_setting_wins(tmp_path):
    db = tmp_path / "latest.duckdb"
    con = secure_duckdb_connect(
        db, allow=DuckDBAllowlist.none(), config={"storage_compatibility_version": "latest"}
    )
    con.execute("CREATE TABLE t AS SELECT 1 AS a")
    assert _storage(con, "latest").startswith("v2.")


def test_the_storage_setting_is_locked_with_the_rest(tmp_path):
    con = secure_duckdb_connect(allow=DuckDBAllowlist.none())
    with pytest.raises(duckdb.Error):
        con.execute("SET storage_compatibility_version = 'latest'")


# ── no partial reads in DuckDB code ──────────────────────────────────────

#: Reads that DuckDB 2.0 removed from relations or ``con.execute()`` results, or
#: that leave a result open (the connection stays busy until it is drained):
#: ``fetchone``/``fetchmany`` (``first_row`` replaces them), the Arrow batch
#: readers, ``rel.arrow()``. Caught separately: a relation's argument-less
#: ``execute()`` (gone), and an ``execute()`` of a query whose result is thrown
#: away unread (open until the next ``execute()``). ``rel.pl()`` is not listed:
#: relations keep it in 2.0.
_PARTIAL_OR_GONE = frozenset(
    {"fetchone", "fetchmany", "fetch_df_chunk", "fetch_arrow_table", "fetch_record_batch"}
    | {"fetch_arrow_reader", "record_batch", "arrow"}
)

#: Modules whose ``fetchone`` (or ``execute``) belongs to another client, never
#: DuckDB's; each must keep importing no DuckDB.
_OTHER_CLIENTS = {
    "fluid_build/cli/doctor.py": "sqlite3 / Snowflake connector cursors",
    "fluid_build/cli/workspace.py": "sqlite3 workspace registry",
    "fluid_build/copilot/catalog/snowflake.py": "Snowflake connector cursor",
    "fluid_build/copilot/store/backends/postgres.py": "psycopg cursor",
    "fluid_build/copilot/store/backends/sqlite.py": "sqlite3 connection",
    "fluid_build/copilot/store/backends/vector.py": "sqlite3 + sqlite-vec probe",
    "fluid_build/forge_datamodel/from_ddl/snowflake_dumper.py": "Snowflake connector cursor",
    "fluid_build/providers/gcp/util/auth.py": "googleapiclient request .execute()",
    "fluid_build/output_ports/mcp/drivers/postgres.py": "psycopg cursor health probe",
    "fluid_build/output_ports/mcp/drivers/snowflake.py": "Snowflake cursor health probe",
}

#: The start of a statement that returns rows, for an ``execute()`` whose result
#: is thrown away.
_QUERY_START = re.compile(
    r"\s*(SELECT|WITH|FROM|TABLE|VALUES|DESCRIBE|SHOW|SUMMARIZE|PRAGMA|CALL|EXPLAIN|PIVOT)\b",
    re.IGNORECASE,
)


def _literal_start(node: ast.expr) -> str:
    """The leading literal text of a ``str`` or f-string argument, else ``""``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return ""


def _unread_query(node: ast.AST) -> bool:
    """``<x>.execute("SELECT ...")`` as a statement of its own: its rows are never read."""
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
        return False
    call = node.value
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "execute"
        and bool(call.args)
        and bool(_QUERY_START.match(_literal_start(call.args[0])))
    )


def _partial_reads(path: Path) -> List[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = path.relative_to(REPO_ROOT).as_posix() if REPO_ROOT in path.parents else path.name
    found = [
        f"{where}:{node.lineno} .{node.func.attr}()"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and (
            node.func.attr in _PARTIAL_OR_GONE
            or (node.func.attr == "execute" and not node.args and not node.keywords)
        )
    ]
    found += [
        f"{where}:{node.lineno} unread .execute()" for node in ast.walk(tree) if _unread_query(node)
    ]
    return found


@pytest.mark.parametrize(
    "shape",
    [
        "con.execute('select 1').fetchone()\n",
        "rel.aggregate('count(*)').fetchone()[0]\n",
        "con.execute('select 1').fetchmany(2)\n",
        "con.execute('select 1').fetch_arrow_table()\n",
        "rel.fetch_df_chunk()\n",
        "con.execute('select 1').fetch_record_batch(10)\n",
        "rel.fetch_arrow_reader(1000)\n",
        "rel.record_batch()\n",
        "rel.arrow()\n",
        "rel.execute()\n",
        "con.execute('SELECT 1')\n",
        "con.execute(f'select count(*) FROM {table}')\n",
        "con.execute('DESCRIBE t')\n",
    ],
)
def test_the_partial_read_detector_catches_each_shape(shape, tmp_path):
    sample = tmp_path / "probe.py"
    sample.write_text(shape, encoding="utf-8")
    assert _partial_reads(sample), shape


@pytest.mark.parametrize(
    "shape",
    [
        "con.execute('SELECT 1').fetchall()\n",
        "rows = con.execute('SELECT 1')\n",
        "con.execute('CREATE TABLE t AS SELECT 1')\n",
        "con.execute(f'COPY ({select}) TO {path}')\n",
        "con.execute(sql)\n",
    ],
)
def test_the_partial_read_detector_leaves_a_read_or_a_write_alone(shape, tmp_path):
    sample = tmp_path / "probe.py"
    sample.write_text(shape, encoding="utf-8")
    assert not _partial_reads(sample), shape


def test_no_partial_reads_in_duckdb_code():
    offenders, scanned = [], 0
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.relative_to(REPO_ROOT).as_posix() in _OTHER_CLIENTS:
            continue
        scanned += 1
        offenders.extend(_partial_reads(path))
    # A fence that scans nothing passes; it must see the DuckDB modules.
    assert scanned > 500, scanned
    assert not offenders, (
        "On DuckDB 2.0 a relation has no fetchone/fetchmany, and a con.execute() "
        "result read partway keeps its connection busy (the next relation read, "
        "register or create_function raises ResourceInUseException). Use "
        "fluid_build.providers._duckdb_compat.first_row with a one-row query; if the "
        "call is another database's client, list the module in _OTHER_CLIENTS:\n  "
        + "\n  ".join(offenders)
    )


def test_every_other_client_module_still_needs_its_exemption():
    """An exemption outlives its reason silently; a stale one would hide DuckDB code."""
    for module in _OTHER_CLIENTS:
        path = REPO_ROOT / module
        assert path.is_file(), module
        assert _partial_reads(path), f"{module} no longer needs its exemption; drop it"
        # The exemption is for another client's calls: DuckDB code there would
        # go unchecked.
        assert not re.search(
            r"import duckdb|from duckdb|secure_duckdb_connect", path.read_text(encoding="utf-8")
        ), f"{module} now uses DuckDB; it cannot stay exempt"
