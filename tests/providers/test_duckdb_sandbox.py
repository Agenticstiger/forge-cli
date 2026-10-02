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


def test_a_declared_input_outside_the_contract_directory_needs_the_operator(
    layout, printed, monkeypatch
):
    """Outside the contract's directory, a declared file is granted only where the
    operator allows it (FLUID_DUCKDB_ALLOWED_DIRS), and then that file only."""
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

    # The contract alone cannot grant a file outside its directory.
    assert _execute_embedded_sql_build(contract["builds"][0], contract, layout["contract_dir"]) == 1
    assert "FLUID_DUCKDB_ALLOWED_DIRS" in "\n".join(printed), printed
    assert not out.exists()

    monkeypatch.setenv("FLUID_DUCKDB_ALLOWED_DIRS", str(shared))
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


def test_acquisition_reads_its_declared_source_and_nothing_beside_it(layout, monkeypatch):
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

    # A landing directory outside the contract's is the operator's to allow.
    with pytest.raises(DuckDBSandboxError, match="FLUID_DUCKDB_ALLOWED_DIRS"):
        execute_duckdb_build(contract["builds"][0], contract, layout["contract_dir"])
    assert not out.exists()

    monkeypatch.setenv("FLUID_DUCKDB_ALLOWED_DIRS", str(landing))
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

    # The source glob grants the files it matches, not the landing directory.
    from fluid_build.build_runners.duckdb.runner import _run_allowlist, _run_streams

    (landing / "notes.txt").write_text(f"{SECRET}\n", encoding="utf-8")
    src = secure_duckdb_connect(allow=_run_allowlist(ctx, _run_streams(ctx)))
    try:
        assert src.execute(f"SELECT count(*) FROM read_csv('{landing}/*.csv')").fetchone() == (1,)
        with pytest.raises(duckdb.PermissionException):
            src.execute(f"SELECT content FROM read_text('{landing}/notes.txt')")
    finally:
        src.close()


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


# ── a declaration is not a grant of the host ─────────────────────────────
#
# Every declared input and output is granted to the contract's SQL, and the
# contract's author writes the declarations. Before confinement, declaring an
# innocuous glob in $HOME granted all of $HOME, and declaring a credentials file
# granted it directly.


def _build_declaring(layout: Dict[str, Path], sql: str, path: str) -> int:
    from fluid_build.build_runners.base import _execute_embedded_sql_build

    out = layout["contract_dir"] / "out" / "result.csv"
    contract = _contract(sql, out)
    contract["builds"][0]["properties"]["parameters"] = {"inputs": [{"name": "d", "path": path}]}
    return _execute_embedded_sql_build(contract["builds"][0], contract, layout["contract_dir"])


@pytest.fixture
def home_secrets(layout: Dict[str, Path]) -> Path:
    """``$HOME`` (the fixture's ``outside``) with credentials and a decoy CSV."""
    home = layout["outside"]
    (home / ".aws").mkdir()
    (home / ".aws" / "credentials").write_text(f"[default]\nkey = {SECRET}\n", encoding="utf-8")
    (home / "decoy.csv").write_text("a\n1\n", encoding="utf-8")
    return home


_READ_CREDENTIALS = "SELECT content FROM read_text('~/.aws/credentials')"


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param("{home}/*.csv", id="glob_in_home"),
        pytest.param("{home}/.aws/credentials", id="the_file_itself"),
        pytest.param("{home}", id="the_home_directory"),
        pytest.param("{home}/.aws", id="a_directory_outside"),
        pytest.param("{contract}/*/../../outside/.aws/credentials", id="dot_dot_after_glob"),
        pytest.param("../outside/*.csv", id="relative_dot_dot"),
    ],
)
def test_declaring_a_location_outside_the_contract_grants_nothing(
    declared, layout, home_secrets, printed
):
    path = declared.format(home=home_secrets, contract=layout["contract_dir"])

    rc = _build_declaring(layout, _READ_CREDENTIALS, path)

    assert rc == 1, f"{declared}: the build read through a declaration"
    assert SECRET not in _written_text(layout["contract_dir"])
    assert SECRET not in "\n".join(printed)
    assert "FLUID_DUCKDB_ALLOWED_DIRS" in "\n".join(printed), printed


def test_a_relative_declaration_cannot_grant_the_servers_working_directory(
    layout, home_secrets, printed, monkeypatch
):
    """``path: ./*.csv`` resolves where DuckDB opens it; outside the contract, refused."""
    monkeypatch.chdir(home_secrets)  # a server whose working directory is elsewhere

    rc = _build_declaring(layout, _READ_CREDENTIALS, "./*.csv")

    assert rc == 1
    assert SECRET not in _written_text(layout["contract_dir"])
    assert SECRET not in "\n".join(printed)


def test_a_symlink_in_the_contract_to_root_grants_nothing(layout, printed):
    """DuckDB realpaths allowlist entries, so a declared in-repo symlink to '/'
    would grant the whole host while looking like a path inside the contract."""
    from fluid_build.providers.local.local import LocalProvider

    contract_dir = layout["contract_dir"]
    (contract_dir / "rootlink").symlink_to("/")
    provider = LocalProvider(project="local", region="local", anchor_dir=contract_dir)
    for declared in ("rootlink", str(contract_dir / "rootlink"), "rootlink/etc/*"):
        with pytest.raises(DuckDBSandboxError):
            provider._allowlist([{"path": declared}])
    with pytest.raises(DuckDBSandboxError):
        DuckDBAllowlist.none().with_dirs(contract_dir / "rootlink")

    # Through the build: a file reached via the symlink is outside, so refused.
    via_link = f"rootlink{layout['outside']}/secret.csv"
    assert _build_declaring(layout, "SELECT * FROM d", via_link) == 1
    assert SECRET not in _written_text(contract_dir)
    assert SECRET not in "\n".join(printed)


def test_an_operator_allowed_glob_grants_its_files_not_its_directory(
    layout, home_secrets, printed, monkeypatch
):
    monkeypatch.setenv("FLUID_DUCKDB_ALLOWED_DIRS", str(home_secrets))

    assert _build_declaring(layout, "SELECT * FROM d", f"{home_secrets}/decoy*.csv") == 0, printed
    out = layout["contract_dir"] / "out" / "result.csv"
    assert out.read_text(encoding="utf-8").splitlines() == ["a", "1"]

    assert _build_declaring(layout, _READ_CREDENTIALS, f"{home_secrets}/decoy*.csv") == 1
    assert SECRET not in _written_text(layout["contract_dir"])


def test_a_declared_glob_inside_the_contract_still_builds(layout, printed):
    rc = _build_declaring(layout, "SELECT sum(amount) AS s FROM d", "data/*.csv")
    assert rc == 0, printed
    out = layout["contract_dir"] / "out" / "result.csv"
    assert out.read_text(encoding="utf-8").splitlines() == ["s", "30"]


def test_a_declared_output_outside_the_contract_is_refused(layout, printed):
    """An output is a write grant: declaring one outside would COPY anywhere."""
    from fluid_build.build_runners.base import _execute_embedded_sql_build

    target = layout["outside"] / "pwned.csv"
    contract = _contract("SELECT 1 AS a", target)
    assert _execute_embedded_sql_build(contract["builds"][0], contract, layout["contract_dir"]) == 1
    assert not target.exists()


def test_with_declared_confines_each_glob_match(tmp_path, monkeypatch):
    """A match that is a symlink out of the roots is refused, not granted."""
    monkeypatch.delenv("FLUID_DUCKDB_ALLOWED_DIRS", raising=False)
    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "data").mkdir(parents=True)
    outside.mkdir()
    (root / "data" / "a.csv").write_text("a\n1\n", encoding="utf-8")
    (outside / "s.csv").write_text(f"s\n{SECRET}\n", encoding="utf-8")

    allow = DuckDBAllowlist.none().with_declared("data/*.csv", within=[root], base=root)
    assert allow.paths == (f"{root}/data/*.csv", f"{root}/data/a.csv")
    assert allow.dirs == ()
    con = secure_duckdb_connect(allow=allow)
    assert con.execute(f"SELECT * FROM read_csv('{root}/data/*.csv')").fetchall() == [(1,)]

    (root / "data" / "b.csv").symlink_to(outside / "s.csv")
    with pytest.raises(DuckDBSandboxError, match="outside the directories"):
        DuckDBAllowlist.none().with_declared("data/*.csv", within=[root], base=root)


@pytest.mark.parametrize("entry", ["relative/dir", "/"])
def test_bad_operator_dirs_are_refused(entry, tmp_path, monkeypatch):
    monkeypatch.setenv("FLUID_DUCKDB_ALLOWED_DIRS", entry)
    with pytest.raises(DuckDBSandboxError, match="FLUID_DUCKDB_ALLOWED_DIRS"):
        DuckDBAllowlist.none().with_declared(tmp_path / "x.csv", within=[tmp_path])


def test_contract_tests_action_cannot_declare_a_file_outside(layout):
    from types import SimpleNamespace

    from fluid_build.contract_tests import LocalProviderError, apply_action

    action = {
        "op": "add",
        "resource_type": "sql",
        "id": "steal",
        "inputs": {"s": {"path": str(layout["outside"] / "secret.csv")}},
        "outputs": {"path": str(layout["contract_dir"] / "out" / "s.csv"), "format": "csv"},
        "sql": "SELECT * FROM s",
    }
    with pytest.raises(LocalProviderError, match="FLUID_DUCKDB_ALLOWED_DIRS"):
        apply_action(action, SimpleNamespace(dry_run=False))
    assert not (layout["contract_dir"] / "out" / "s.csv").exists()


# ── one failure does not poison the run's later actions ──────────────────
#
# Every action of one apply shares one session database file. DuckDB shares one
# instance per file per process and the sandbox locks it, so a connection left
# open by a failure (its traceback keeps it alive) made every later action fail
# with "the configuration has been locked".


def _apply_after_a_failure(provider: Any, data: Path, out: Path) -> List[Dict[str, Any]]:
    actions = [
        {"op": "load_data", "path": str(data / "orders.csv"), "table_name": "orders"},
        {
            "op": "sql",
            "sql": "SELECT * FROM orders",
            "output_table": "t1",
            "outputs": [str(out / "1.csv")],
        },
        {"op": "sql", "sql": "SELECT * FROM no_such_table", "outputs": [str(out / "2.csv")]},
        {"op": "sql", "sql": "SELECT count(*) AS n FROM t1", "outputs": [str(out / "3.csv")]},
        {"op": "load_data", "path": str(data / "missing_*.csv"), "table_name": "m"},
        {"op": "sql", "sql": "SELECT count(*) AS n FROM orders", "outputs": [str(out / "4.csv")]},
        {"op": "materialize", "source_table": "t1", "dst": str(out / "5.csv")},
    ]
    return provider.apply(actions=actions)["results"]


def test_a_failing_action_does_not_fail_the_actions_after_it(layout):
    from fluid_build.providers.local.local import LocalProvider

    provider = LocalProvider(project="local", region="local", anchor_dir=layout["contract_dir"])
    out = layout["contract_dir"] / "out"

    results = _apply_after_a_failure(provider, layout["contract_dir"] / "data", out)

    assert [r["status"] for r in results] == ["ok", "ok", "error", "ok", "error", "ok", "ok"], [
        r.get("error") for r in results
    ]
    assert "no_such_table" in results[2]["error"]
    assert "No files found" in results[4]["error"]
    assert (out / "3.csv").read_text(encoding="utf-8").splitlines() == ["n", "2"]
    assert (out / "5.csv").read_text(encoding="utf-8").splitlines()[1:] == ["1,10", "2,20"]


def test_persist_mode_applies_one_after_another_after_a_failure(layout):
    """``persist=True`` shares ~/.fluid/local.db across runs in one process."""
    from fluid_build.providers.local.local import LocalProvider

    data, out = layout["contract_dir"] / "data", layout["contract_dir"] / "out"
    for _ in range(2):
        provider = LocalProvider(
            project="local", region="local", anchor_dir=layout["contract_dir"], persist=True
        )
        statuses = [r["status"] for r in _apply_after_a_failure(provider, data, out)]
        assert statuses == ["ok", "ok", "error", "ok", "error", "ok", "ok"]


def test_contract_tests_actions_share_a_file_database_after_a_failure(layout, monkeypatch):
    from types import SimpleNamespace

    from fluid_build.contract_tests import LocalProviderError, apply_action

    monkeypatch.setenv("FLUID_LOCAL_DUCKDB_PATH", str(layout["contract_dir"] / "ct.duckdb"))
    action = {
        "op": "add",
        "resource_type": "sql",
        "id": "orders",
        "inputs": {"orders": {"path": str(layout["contract_dir"] / "data" / "orders.csv")}},
        "outputs": {"path": str(layout["contract_dir"] / "out" / "r.csv"), "format": "csv"},
        "sql": "SELECT * FROM no_such_table",
    }
    # The caller keeps the failure (as a CLI reporting it does): its traceback
    # must not keep the file's locked instance open.
    with pytest.raises(LocalProviderError, match="no_such_table") as failed:
        apply_action(action, SimpleNamespace(dry_run=False))
    action["sql"] = "SELECT id FROM orders"
    apply_action(action, SimpleNamespace(dry_run=False))
    assert failed.value is not None
    assert (layout["contract_dir"] / "out" / "r.csv").exists()


def test_a_second_connection_to_an_open_file_is_refused_clearly(tmp_path):
    db = tmp_path / "shared.duckdb"
    first = secure_duckdb_connect(db, allow=DuckDBAllowlist.none())
    try:
        for config in (None, {"threads": 2}):
            with pytest.raises(DuckDBSandboxError, match="already open in this process"):
                secure_duckdb_connect(db, allow=DuckDBAllowlist.none(), config=config)
    finally:
        first.close()
    # Closed, the file opens again.
    secure_duckdb_connect(db, allow=DuckDBAllowlist.none()).close()


# ── extensions: what autoload-off costs, and what a loaded scanner reaches ─


def test_a_function_from_an_unloaded_extension_reads_as_a_sandbox_refusal(layout, printed):
    """``sqlite_scan`` (and read_xlsx, ST_Read, delta_scan, iceberg_scan) worked
    through autoloading; under the sandbox they are not in the catalog, and the
    error must say why rather than advise a SET the lock refuses."""
    from fluid_build.providers._duckdb_sandbox import is_sandbox_refusal, sandbox_refusal_hint
    from fluid_build.providers.local.util.retry import is_retryable_error

    con = secure_duckdb_connect(allow=DuckDBAllowlist.none())
    with pytest.raises(duckdb.CatalogException) as refused:
        con.execute("SELECT * FROM sqlite_scan('data/app.sqlite', 't')")
    assert "exists in the sqlite_scanner extension" in str(refused.value)
    assert is_sandbox_refusal(refused.value)
    assert not is_retryable_error(refused.value)
    assert "extension autoloading off" in sandbox_refusal_hint(
        DuckDBAllowlist.none(), refused.value
    )

    rc = _build("SELECT * FROM sqlite_scan('data/app.sqlite', 't')", layout)
    assert rc == 1
    assert "extension autoloading off" in "\n".join(printed), printed


def test_contract_sql_runs_on_a_connection_with_no_database_scanner(layout, printed):
    """sqlite/postgres/mysql scanners are not bounded by the allowlist (next
    test), so the connection contract SQL runs on must never have one loaded."""
    rc = _build(
        "SELECT count(*) AS n FROM duckdb_functions() WHERE function_name IN "
        "('sqlite_scan', 'sqlite_attach', 'postgres_scan', 'postgres_query', 'mysql_query')",
        layout,
    )
    assert rc == 0, printed
    out = layout["contract_dir"] / "out" / "result.csv"
    assert out.read_text(encoding="utf-8").splitlines() == ["n", "0"]


def _with_extension(name: str) -> Any:
    try:
        return secure_duckdb_connect(allow=DuckDBAllowlist.none(), extensions=[name])
    except duckdb.Error:
        pytest.skip(f"the {name} extension is not installable here (offline)")


def test_a_loaded_sqlite_scanner_is_not_bounded_by_the_allowlist(tmp_path):
    """Pins a documented limit: once ``sqlite`` is loaded, any SQLite file the
    process can read is reachable, whatever the allowlist says. A call site that
    loads it must keep contract SQL off the connection."""
    db = tmp_path / "elsewhere.sqlite"
    seed = duckdb.connect()
    try:
        seed.execute("INSTALL sqlite; LOAD sqlite")
    except duckdb.Error:
        pytest.skip("the sqlite extension is not installable here (offline)")
    seed.execute(f"ATTACH '{db}' AS s (TYPE sqlite)")
    seed.execute(f"CREATE TABLE s.t AS SELECT '{SECRET}' AS v")
    seed.close()

    con = _with_extension("sqlite")
    assert con.execute(f"SELECT v FROM sqlite_scan('{db}', 't')").fetchall() == [(SECRET,)]


def test_a_loaded_postgres_scanner_is_not_bounded_by_the_allowlist():
    """Pins a documented limit: ``postgres_scan`` opens its own socket, so the
    failure is the server's (nothing listens), not the sandbox's refusal."""
    con = _with_extension("postgres")
    with pytest.raises(duckdb.Error) as failed:
        con.execute(
            "SELECT * FROM postgres_scan('host=127.0.0.1 port=9 connect_timeout=2', 'public', 't')"
        )
    assert not isinstance(failed.value, duckdb.PermissionException), failed.value
    assert "Permission Error" not in str(failed.value)


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
    # A glob grants its own pattern and what it matches (nothing yet), never
    # the directory above its first wildcard.
    assert allow.paths == (
        str(tmp_path / "file.csv"),
        f"{tmp_path}/landing/*/part-*.parquet",
    )
    assert allow.dirs == (str(tmp_path / "dir"),)
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
