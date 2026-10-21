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
from pathlib import Path
from typing import List

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.providers._duckdb_compat import (  # noqa: E402
    STORAGE_COMPATIBILITY_VERSION,
    first_row,
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


def test_first_row_of_no_rows_is_none():
    con = duckdb.connect()
    assert first_row(con.execute("SELECT 1 WHERE false")) is None
    assert first_row(con.sql("SELECT 1 WHERE false")) is None


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
    assert STORAGE_COMPATIBILITY_VERSION == "v1.5.0"


def test_a_database_file_the_engine_creates_is_readable_by_duckdb_1_5(tmp_path):
    """A 2.x install must not write files a 1.5 one (Python 3.10, or a tool still
    pinned below 2) cannot open: 1.5.6 reads storage versions 64 to 68 only."""
    db = tmp_path / "new.duckdb"
    con = secure_duckdb_connect(db, allow=DuckDBAllowlist.none())
    con.execute("CREATE TABLE t AS SELECT 1 AS a")
    if DUCKDB_2:
        assert _storage(con, "new") == "v1.5.0+"
    else:  # 1.5's own default, left alone
        assert _storage(con, "new") == "v1.0.0+"
    assert con.execute("SELECT current_setting('storage_compatibility_version')").fetchall() == [
        (STORAGE_COMPATIBILITY_VERSION if DUCKDB_2 else "v0.10.2",)
    ]


def test_an_attached_new_database_follows_the_same_format(tmp_path):
    con = secure_duckdb_connect(
        allow=DuckDBAllowlist.none(),
        before_lock=lambda c: c.execute(f"ATTACH '{tmp_path / 'att.duckdb'}' AS att"),
    )
    con.execute("CREATE TABLE att.t AS SELECT 1 AS a")
    assert _storage(con, "att") == ("v1.5.0+" if DUCKDB_2 else "v1.0.0+")


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
    assert _storage(con, "latest") != "v1.5.0+"


def test_the_storage_setting_is_locked_with_the_rest(tmp_path):
    con = secure_duckdb_connect(allow=DuckDBAllowlist.none())
    with pytest.raises(duckdb.Error):
        con.execute("SET storage_compatibility_version = 'latest'")


# ── no partial reads in DuckDB code ──────────────────────────────────────

#: Calls DuckDB 2.0 removed from relations or from ``con.execute()`` results, or
#: that leave a result open. ``first_row`` replaces the first two.
_PARTIAL_OR_GONE = frozenset(
    {"fetchone", "fetchmany", "fetch_df_chunk", "fetch_arrow_table", "fetch_record_batch"}
)

#: Modules whose ``fetchone`` belongs to another database's client, never DuckDB's.
_OTHER_CLIENTS = {
    "fluid_build/cli/doctor.py": "sqlite3 / Snowflake connector cursors",
    "fluid_build/cli/workspace.py": "sqlite3 workspace registry",
    "fluid_build/copilot/catalog/snowflake.py": "Snowflake connector cursor",
    "fluid_build/copilot/store/backends/postgres.py": "psycopg cursor",
    "fluid_build/copilot/store/backends/sqlite.py": "sqlite3 connection",
    "fluid_build/copilot/store/backends/vector.py": "sqlite3 + sqlite-vec probe",
    "fluid_build/forge_datamodel/from_ddl/snowflake_dumper.py": "Snowflake connector cursor",
}


def _partial_reads(path: Path) -> List[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    where = path.relative_to(REPO_ROOT).as_posix() if REPO_ROOT in path.parents else path.name
    return [
        f"{where}:{node.lineno} .{node.func.attr}()"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _PARTIAL_OR_GONE
    ]


@pytest.mark.parametrize(
    "shape",
    [
        "con.execute('select 1').fetchone()\n",
        "rel.aggregate('count(*)').fetchone()[0]\n",
        "con.execute('select 1').fetchmany(2)\n",
        "con.execute('select 1').fetch_arrow_table()\n",
        "rel.fetch_df_chunk()\n",
        "con.execute('select 1').fetch_record_batch(10)\n",
    ],
)
def test_the_partial_read_detector_catches_each_shape(shape, tmp_path):
    sample = tmp_path / "probe.py"
    sample.write_text(shape, encoding="utf-8")
    assert _partial_reads(sample), shape


def test_no_partial_reads_in_duckdb_code():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.relative_to(REPO_ROOT).as_posix() in _OTHER_CLIENTS:
            continue
        offenders.extend(_partial_reads(path))
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
        assert _partial_reads(path), f"{module} no longer calls fetchone; drop its exemption"
