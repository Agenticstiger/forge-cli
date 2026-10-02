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

"""Contract SQL cannot read the host: every DuckDB the engine opens is sandboxed.

A contract's ``builds[].properties.sql`` runs on DuckDB with the privileges of
the process. Before ``providers/_duckdb_sandbox.py`` it could read any file the
process could (``read_csv('/etc/passwd')``), fetch any URL, ATTACH any
database and COPY anywhere. These tests run the attacks through the real
embedded-SQL build path (``_execute_embedded_sql_build``), pin the helper's
own guarantees, and fail if any new ``duckdb.connect`` bypasses the helper.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.providers import _duckdb_sandbox as sandbox  # noqa: E402
from fluid_build.providers._duckdb_sandbox import (  # noqa: E402
    MIN_DUCKDB_VERSION,
    DuckDBAllowlist,
    DuckDBSandboxError,
    secure_duckdb_connect,
)

SECRET = "TOP-SECRET-VALUE-0451"
REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "fluid_build"


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Path]:
    """A contract directory with its own data, and a secret beside it.

    ``outside/`` is a sibling of the contract directory (so ``../`` reaches it)
    and stands in for ``$HOME``: whatever the SQL would read from there is what
    the sandbox has to refuse.
    """
    contract_dir = tmp_path / "product"
    data = contract_dir / "data"
    data.mkdir(parents=True)
    (data / "orders.csv").write_text("id,amount\n1,10\n2,20\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.csv").write_text(f"token\n{SECRET}\n", encoding="utf-8")
    other = duckdb.connect(str(outside / "other.duckdb"))
    other.execute(f"CREATE TABLE creds AS SELECT '{SECRET}' AS token")
    other.close()
    (contract_dir / "link.csv").symlink_to(outside / "secret.csv")
    monkeypatch.setenv("HOME", str(outside))
    monkeypatch.chdir(contract_dir)
    monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
    return {"contract_dir": contract_dir, "outside": outside, "tmp": tmp_path}


@pytest.fixture
def printed() -> Iterator[List[str]]:
    lines: List[str] = []
    with patch(
        "fluid_build.build_runners.base.cprint",
        side_effect=lambda *a, **_k: lines.append(" ".join(str(x) for x in a)),
    ):
        yield lines


def _contract(sql: str, out: Path) -> Dict[str, Any]:
    build = {"id": "attack", "engine": "sql", "properties": {"sql": sql}}
    return {
        "id": "silver.sandbox_probe",
        "builds": [build],
        "consumes": [],
        "exposes": [
            {
                "exposeId": "out",
                "binding": {"platform": "local", "format": "csv", "location": {"path": str(out)}},
            }
        ],
    }


def _build(sql: str, layout: Dict[str, Path]) -> int:
    from fluid_build.build_runners.base import _execute_embedded_sql_build

    out = layout["contract_dir"] / "out" / "result.csv"
    contract = _contract(sql, out)
    return _execute_embedded_sql_build(contract["builds"][0], contract, layout["contract_dir"])


def _written_text(root: Path) -> str:
    texts = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.suffix != ".duckdb":
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(texts)


# ── attacks from a contract's embedded SQL ───────────────────────────────


def _attacks(outside: Path, contract_dir: Path) -> Dict[str, str]:
    return {
        "absolute_host_file": "SELECT * FROM read_csv('/etc/passwd')",
        "absolute_secret": f"SELECT * FROM read_csv('{outside}/secret.csv')",
        "read_text": f"SELECT content FROM read_text('{outside}/secret.csv')",
        "read_blob": f"SELECT content FROM read_blob('{outside}/secret.csv')",
        "read_json": f"SELECT * FROM read_json_auto('{outside}/secret.csv')",
        "read_parquet": f"SELECT * FROM read_parquet('{outside}/*.parquet')",
        "glob": "SELECT * FROM glob('/etc/*')",
        "dot_dot": f"SELECT * FROM read_csv('{contract_dir}/../outside/secret.csv')",
        # The DuckDB <= 1.4.3 escape: './..' was not resolved before the check.
        "dot_slash_dot_dot": f"SELECT * FROM read_csv('{contract_dir}/./../outside/secret.csv')",
        "relative_dot_dot": "SELECT * FROM read_csv('../outside/secret.csv')",
        "symlink_out": f"SELECT * FROM read_csv('{contract_dir}/link.csv')",
        # $HOME is ``outside`` (fixture): '~' must not reach it.
        "home_tilde": "SELECT * FROM read_csv('~/secret.csv')",
        "http_url": "SELECT * FROM read_csv('http://127.0.0.1:9/secret.csv')",
        "https_url": "SELECT * FROM read_csv('https://example.invalid/secret.csv')",
        "attach_outside_db": (
            f"ATTACH '{outside}/other.duckdb' AS o (READ_ONLY); SELECT * FROM o.creds"
        ),
        "copy_to_outside": (
            f"COPY (SELECT 1 AS a) TO '{outside}/pwned.csv' (FORMAT csv); SELECT 1 AS a"
        ),
        "copy_from_outside": (
            f"CREATE OR REPLACE TABLE t (token VARCHAR); COPY t FROM '{outside}/secret.csv'; "
            "SELECT * FROM t"
        ),
        "reset_external_access": (
            "SET enable_external_access = true; " f"SELECT * FROM read_csv('{outside}/secret.csv')"
        ),
        "widen_allowlist": (
            f"SET allowed_directories = ['{outside}']; "
            f"SELECT * FROM read_csv('{outside}/secret.csv')"
        ),
        "unlock": "SET lock_configuration = false; SELECT 1 AS a",
        "load_extension": "LOAD httpfs; SELECT 1 AS a",
        "install_extension": "INSTALL spatial; SELECT 1 AS a",
    }


_ATTACK_NAMES = sorted(_attacks(Path("/o"), Path("/c")))


@pytest.mark.parametrize("name", _ATTACK_NAMES)
def test_contract_sql_cannot_reach_outside_the_sandbox(name, layout, printed):
    sql = _attacks(layout["outside"], layout["contract_dir"])[name]

    rc = _build(sql, layout)

    assert rc == 1, f"{name}: the build succeeded; the sandbox let it through"
    assert not (layout["outside"] / "pwned.csv").exists()
    assert SECRET not in _written_text(layout["contract_dir"])
    assert SECRET not in "\n".join(printed)
    # A clear refusal, not some unrelated failure that happens to exit 1.
    errors = "\n".join(printed)
    assert (
        "Permission Error" in errors
        or "configuration has been locked" in errors
        or "disabled through configuration" in errors
        # A URL whose filesystem extension the build did not load: autoloading
        # is off under the lock, so the SQL cannot load it either.
        or "requires the extension" in errors
    ), errors
    if "requires the extension" in errors or "Permission Error" in errors:
        assert "contract SQL may only read and write" in errors


def test_refusal_says_what_the_sql_may_read_instead(layout, printed):
    rc = _build("SELECT * FROM read_csv('/etc/passwd')", layout)

    assert rc == 1
    errors = "\n".join(printed)
    assert "Cannot access file" in errors
    assert "contract SQL may only read and write" in errors
    assert str(layout["contract_dir"]) in errors


# ── legitimate reads keep working ────────────────────────────────────────


def test_contract_sql_reads_its_own_directory_and_writes_its_output(layout, printed):
    rc = _build(
        "SELECT id, amount * 2 AS doubled FROM read_csv('data/orders.csv') ORDER BY id",
        layout,
    )

    assert rc == 0, printed
    out = layout["contract_dir"] / "out" / "result.csv"
    assert out.read_text(encoding="utf-8").splitlines() == ["id,doubled", "1,20", "2,40"]


def test_a_declared_input_outside_the_contract_directory_stays_readable(layout, printed):
    """A file the contract DECLARES is granted (that file, not its directory)."""
    shared = layout["tmp"] / "shared"
    shared.mkdir()
    (shared / "rates.csv").write_text("id,rate\n1,3\n", encoding="utf-8")
    (shared / "private.csv").write_text(f"token\n{SECRET}\n", encoding="utf-8")
    out = layout["contract_dir"] / "out" / "result.csv"
    contract = _contract("SELECT * FROM rates", out)
    contract["builds"][0]["properties"]["parameters"] = {
        "inputs": [{"name": "rates", "path": str(shared / "rates.csv")}]
    }
    from fluid_build.build_runners.base import _execute_embedded_sql_build

    assert _execute_embedded_sql_build(contract["builds"][0], contract, layout["contract_dir"]) == 0
    assert out.read_text(encoding="utf-8").splitlines() == ["id,rate", "1,3"]

    # Its neighbour was not declared, so it is not readable.
    contract["builds"][0]["properties"]["sql"] = f"SELECT * FROM read_csv('{shared}/private.csv')"
    assert _execute_embedded_sql_build(contract["builds"][0], contract, layout["contract_dir"]) == 1


# ── contract-tests local actions ─────────────────────────────────────────


def test_contract_tests_action_reads_its_inputs_and_nothing_else(layout):
    from types import SimpleNamespace

    from fluid_build.contract_tests import LocalProviderError, apply_action

    out = layout["contract_dir"] / "out" / "result.parquet"
    action = {
        "op": "add",
        "resource_type": "sql",
        "id": "orders",
        "inputs": {"orders": {"path": str(layout["contract_dir"] / "data" / "orders.csv")}},
        "outputs": {"path": str(out), "format": "parquet"},
        "sql": "SELECT id, amount FROM orders",
    }
    apply_action(action, SimpleNamespace(dry_run=False))
    assert duckdb.connect().execute(f"SELECT count(*) FROM '{out}'").fetchone() == (2,)

    action["sql"] = f"SELECT * FROM read_csv('{layout['outside']}/secret.csv')"
    with pytest.raises(LocalProviderError) as refused:
        apply_action(action, SimpleNamespace(dry_run=False))
    assert "Permission Error" in str(refused.value)
    assert "contract SQL may only read and write" in str(refused.value)
    assert SECRET not in str(refused.value)


# ── the acquisition runner ───────────────────────────────────────────────


def _acquisition_contract(source_glob: str, out_path: Path) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.sandbox_ingest",
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "duckdb",
                "properties": {
                    "source": {
                        "kind": "filesystem",
                        "connection": {"uri": source_glob},
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
                    "location": {"path": str(out_path)},
                },
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }


def test_acquisition_reads_its_declared_source_and_nothing_beside_it(layout):
    from fluid_build.build_runners._acquisition_common import build_acquisition_run_context
    from fluid_build.build_runners.duckdb.runner import (
        _connect_for_destination,
        execute_duckdb_build,
    )

    landing = layout["tmp"] / "landing"
    landing.mkdir()
    (landing / "orders.csv").write_text("id,amount\n1,10\n", encoding="utf-8")
    out = layout["contract_dir"] / "out" / "orders.parquet"
    contract = _acquisition_contract(str(landing / "*.csv"), out)

    assert execute_duckdb_build(contract["builds"][0], contract, layout["contract_dir"]) == 0
    assert out.exists()

    ctx = build_acquisition_run_context(contract["builds"][0], contract, layout["contract_dir"])
    con = _connect_for_destination(ctx)
    try:
        assert con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone() == (1,)
        with pytest.raises(duckdb.PermissionException):
            con.execute(f"SELECT * FROM read_csv('{layout['outside']}/secret.csv')")
        # The read-back connection is the destination's, not the source's.
        with pytest.raises(duckdb.PermissionException):
            con.execute(f"SELECT * FROM read_csv('{landing}/orders.csv')")
    finally:
        con.close()


def test_a_sandbox_refusal_is_not_retried():
    """A refusal is permanent; its path ('pytest-500/...') must not read as an HTTP 500."""
    from fluid_build.providers.local.util.retry import is_retryable_error

    con = secure_duckdb_connect(allow=DuckDBAllowlist.none())
    with pytest.raises(duckdb.PermissionException) as refused:
        con.execute("SELECT * FROM read_csv('/tmp/pytest-500/503/x.csv')")
    assert not is_retryable_error(refused.value)


def test_a_url_is_refused_even_with_its_extension_loaded():
    con = None
    try:
        con = secure_duckdb_connect(allow=DuckDBAllowlist.none(), extensions=["httpfs"])
    except duckdb.Error:
        pytest.skip("httpfs is not installable here (offline)")
    with pytest.raises(duckdb.PermissionException):
        con.execute("SELECT * FROM read_csv('https://example.invalid/x.csv')")
    with pytest.raises(duckdb.PermissionException):
        con.execute("SELECT * FROM read_csv('http://169.254.169.254/latest/meta-data/')")


# ── the helper's own guarantees ──────────────────────────────────────────


def test_allowed_directory_is_a_directory_not_a_name_prefix(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data-other").mkdir()
    (tmp_path / "data-other" / "x.csv").write_text("a\n1\n", encoding="utf-8")
    con = secure_duckdb_connect(allow=DuckDBAllowlist.none().with_dirs(tmp_path / "data"))
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"SELECT * FROM read_csv('{tmp_path}/data-other/x.csv')")


def test_none_allowlist_reaches_no_file(tmp_path):
    (tmp_path / "x.csv").write_text("a\n1\n", encoding="utf-8")
    con = secure_duckdb_connect(allow=DuckDBAllowlist.none())
    assert con.execute("SELECT 41 + 1").fetchone() == (42,)
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"SELECT * FROM read_csv('{tmp_path}/x.csv')")


def test_tilde_follows_home_and_cannot_disagree_with_it(tmp_path, monkeypatch):
    """duckdb/duckdb#26064: '~' must mean $HOME both when checked and when opened."""
    home, allowed = tmp_path / "home", tmp_path / "allowed"
    home.mkdir()
    allowed.mkdir()
    (home / "s.csv").write_text(f"who\n{SECRET}\n", encoding="utf-8")
    (allowed / "s.csv").write_text("who\nallowed\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    con = secure_duckdb_connect(allow=DuckDBAllowlist.none().with_dirs(allowed))
    with pytest.raises(duckdb.PermissionException):
        con.execute("SELECT * FROM read_csv('~/s.csv')").fetchall()
    with pytest.raises(duckdb.Error):
        con.execute("SET home_directory = '/'")

    # And when $HOME itself is granted, '~' reads $HOME, not something else.
    con = secure_duckdb_connect(allow=DuckDBAllowlist.none().with_dirs(home))
    assert con.execute("SELECT * FROM read_csv('~/s.csv')").fetchall() == [(SECRET,)]


def test_file_database_writes_and_checkpoints_under_the_lock(tmp_path):
    """A file-backed database needs no grant of its own (WAL + checkpoint)."""
    db = tmp_path / "db" / "run.duckdb"
    db.parent.mkdir()
    con = secure_duckdb_connect(db, allow=DuckDBAllowlist.none())
    con.execute("CREATE TABLE t AS SELECT range AS a FROM range(50000)")
    con.execute("INSERT INTO t SELECT * FROM range(10)")
    con.execute("CHECKPOINT")
    con.close()
    con = secure_duckdb_connect(db, allow=DuckDBAllowlist.none(), read_only=True)
    assert con.execute("SELECT count(*) FROM t").fetchone() == (50010,)
    # ...and the database's directory was not granted to the SQL.
    (db.parent / "neighbour.csv").write_text("a\n1\n", encoding="utf-8")
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"SELECT * FROM read_csv('{db.parent}/neighbour.csv')")


def test_before_lock_attach_is_readable_but_a_new_attach_is_not(tmp_path):
    src = tmp_path / "src.duckdb"
    seed = duckdb.connect(str(src))
    seed.execute("CREATE TABLE t AS SELECT 7 AS v")
    seed.close()
    other = tmp_path / "other.duckdb"
    duckdb.connect(str(other)).close()

    con = secure_duckdb_connect(
        allow=DuckDBAllowlist.none(),
        before_lock=lambda c: c.execute(f"ATTACH '{src}' AS s (READ_ONLY)"),
    )
    assert con.execute("SELECT v FROM s.t").fetchone() == (7,)
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"ATTACH '{other}' AS o")


def test_config_is_applied_before_the_lock_and_frozen_after(tmp_path):
    con = secure_duckdb_connect(allow=DuckDBAllowlist.none(), config={"threads": 3})
    assert con.execute("SELECT current_setting('threads')").fetchone() == (3,)
    for statement in (
        "SET threads = 1",
        "SET enable_external_access = true",
        "SET autoload_known_extensions = true",
        "SET allow_persistent_secrets = true",
        "RESET lock_configuration",
    ):
        with pytest.raises(duckdb.Error):
            con.execute(statement)


def test_settings_read_back_as_locked_down(tmp_path):
    con = secure_duckdb_connect(allow=DuckDBAllowlist.none().with_dirs(tmp_path))
    settings = dict(
        con.execute(
            "SELECT name, value FROM duckdb_settings() WHERE name IN ("
            "'enable_external_access', 'lock_configuration', 'autoload_known_extensions', "
            "'autoinstall_known_extensions', 'allow_persistent_secrets', "
            "'allow_community_extensions', 'allowed_directories')"
        ).fetchall()
    )
    assert settings["enable_external_access"] == "false"
    assert settings["lock_configuration"] == "true"
    assert settings["autoload_known_extensions"] == "false"
    assert settings["autoinstall_known_extensions"] == "false"
    assert settings["allow_persistent_secrets"] == "false"
    assert settings["allow_community_extensions"] == "false"
    assert str(tmp_path) in settings["allowed_directories"]


@pytest.mark.parametrize(
    "entry",
    ["~", "~/data", "/", "s3://bucket/data/"],
)
def test_bad_local_allowlist_entries_are_refused(entry):
    with pytest.raises(DuckDBSandboxError):
        DuckDBAllowlist.none().with_dirs(entry)


@pytest.mark.parametrize(
    "prefix",
    ["/local/path", "ftp://host/x/", "s3://bucket/a/../b/"],
)
def test_bad_remote_prefixes_are_refused(prefix):
    with pytest.raises(DuckDBSandboxError):
        DuckDBAllowlist.none().with_remote(prefix)


def test_declared_locations_grant_the_narrowest_thing(tmp_path):
    (tmp_path / "dir").mkdir()
    allow = DuckDBAllowlist.none().with_locations(
        tmp_path / "file.csv",
        tmp_path / "dir",
        f"{tmp_path}/landing/*/part-*.parquet",
        "s3://bucket/bronze/orders/*.parquet",
        "https://host/data/file.csv",
        "gs://whole-bucket",
        "s3://b2/*.parquet",
    )
    assert allow.paths == (str(tmp_path / "file.csv"),)
    assert allow.dirs == (str(tmp_path / "dir"), str(tmp_path / "landing"))
    assert allow.remote_prefixes == (
        "s3://bucket/bronze/orders/",
        "https://host/data/",
        "gs://whole-bucket/",
        "s3://b2/",
    )


@pytest.mark.parametrize("version", ["1.4.4", "1.4.3", "1.2.2", "0.10.0", "garbage", None])
def test_a_duckdb_older_than_the_floor_is_refused(version, monkeypatch):
    monkeypatch.setattr(duckdb, "__version__", version, raising=False)
    with pytest.raises(DuckDBSandboxError):
        secure_duckdb_connect(allow=DuckDBAllowlist.none())


def test_the_floor_matches_the_declared_dependency():
    """pyproject's duckdb floor and the runtime floor are the same number."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    floor = ".".join(str(n) for n in MIN_DUCKDB_VERSION)
    assert f'"duckdb>={floor}"' in pyproject


def test_the_installed_duckdb_meets_the_floor():
    assert sandbox.duckdb_version(duckdb) >= MIN_DUCKDB_VERSION


# ── no connection bypasses the helper ────────────────────────────────────


_HELPER = PACKAGE / "providers" / "_duckdb_sandbox.py"


def _duckdb_aliases(tree: ast.AST) -> tuple:
    """Names bound to the duckdb module, and to anything imported from it."""
    modules, members = {"duckdb"}, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "duckdb":
                    modules.add(alias.asname or "duckdb")
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and (node.module or "").split(".")[0] == "duckdb"
        ):
            for alias in node.names:
                members.add(alias.asname or alias.name)
    return modules, members


def _bypasses(path: Path) -> List[str]:
    """Each place ``path`` opens or queries DuckDB without the helper.

    * any call on the duckdb module (``duckdb.connect``, ``duckdb.sql`` and the
      other module-level functions run on an unsandboxed default connection);
    * a function imported from duckdb (``from duckdb import connect``);
    * ``<anything named like duck>.connect(...)``, which catches the module
      held in a variable (``self._duckdb.connect``, ``_Duck.get().connect``).
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    modules, members = _duckdb_aliases(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        where = (
            f"{path.relative_to(REPO_ROOT) if REPO_ROOT in path.parents else path}:{node.lineno}"
        )
        if isinstance(func, ast.Attribute):
            receiver = func.value
            if isinstance(receiver, ast.Name) and receiver.id in modules:
                found.append(f"{where} duckdb.{func.attr}(...)")
            elif func.attr == "connect" and "duck" in ast.unparse(receiver).lower():
                found.append(f"{where} {ast.unparse(func)}(...)")
        elif isinstance(func, ast.Name) and func.id in members:
            found.append(f"{where} {func.id}(...) imported from duckdb")
    return found


def test_the_bypass_detector_sees_the_helpers_own_connect():
    """A guard that has never fired proves nothing: it must see the one real call."""
    assert [b for b in _bypasses(_HELPER) if "duckdb.connect(" in b]


@pytest.mark.parametrize(
    "shape",
    [
        "import duckdb\nduckdb.connect(':memory:')\n",
        "def f():\n    import duckdb\n    return duckdb.connect()\n",
        "import duckdb as ddb\nddb.sql('select 1')\n",
        "from duckdb import connect\nconnect()\n",
        "from duckdb import connect as c\nc(':memory:')\n",
        "class X:\n    def f(self):\n        return self._duckdb.connect(':memory:')\n",
        "def f():\n    return _Duck.get().connect(database='x.db')\n",
    ],
)
def test_the_bypass_detector_catches_each_shape(shape, tmp_path):
    sample = tmp_path / "probe.py"
    sample.write_text(shape, encoding="utf-8")
    assert _bypasses(sample), shape


def test_every_duckdb_connection_goes_through_the_sandbox():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path == _HELPER:
            continue
        offenders.extend(_bypasses(path))
    assert not offenders, (
        "DuckDB opened or queried without fluid_build.providers._duckdb_sandbox."
        "secure_duckdb_connect; contract SQL on these connections can read any host "
        "file:\n  " + "\n  ".join(offenders)
    )


def test_no_raw_connect_in_the_helper_beyond_its_one_call():
    calls = [b for b in _bypasses(_HELPER) if "duckdb." in b]
    assert len(calls) == 1, calls


if os.name == "nt":  # pragma: no cover - the sandbox tests assume POSIX paths
    pytestmark = pytest.mark.skip(reason="POSIX paths")
