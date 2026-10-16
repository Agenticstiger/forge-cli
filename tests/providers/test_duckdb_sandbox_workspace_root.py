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

"""A FLUID workspace widens the DuckDB sandbox only when it is a project tree.

A contract inside a workspace (``fluid.workspace.yaml``) may read its sibling
products' files, so the local provider grants the workspace root to the
contract's SQL. The root is the nearest ancestor holding the file, so a
``fluid.workspace.yaml`` at ``$HOME``, above it, at a system temp directory or
at ``/`` would grant that whole tree. Such a root is not granted: the build
runs under the narrower sandbox and a ``local_workspace_not_granted`` warning
says why and what to do.
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Dict

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.providers import _duckdb_sandbox as sandbox  # noqa: E402
from fluid_build.providers._duckdb_sandbox import DuckDBSandboxError  # noqa: E402
from tests.build_runners.test_consumes_resolution import _workspace  # noqa: E402
from tests.providers.test_duckdb_sandbox import _acquisition_contract, _contract  # noqa: E402

SECRET = "WORKSPACE-ROOT-TEST-VALUE-7731"  # pragma: allowlist secret


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def home_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Path]:
    """``$HOME`` with a test credential file, and a contract directory inside it."""
    home_dir = tmp_path / "home"
    (home_dir / ".aws").mkdir(parents=True)
    (home_dir / ".aws" / "credentials.csv").write_text(f"token\n{SECRET}\n", encoding="utf-8")
    contract_dir = home_dir / "proj"
    (contract_dir / "data").mkdir(parents=True)
    (contract_dir / "data" / "orders.csv").write_text("id\n1\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.chdir(contract_dir)
    monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
    monkeypatch.delenv(sandbox.OPERATOR_DIRS_ENV, raising=False)
    return {"tmp": tmp_path, "home": home_dir, "contract_dir": contract_dir}


def _build(sql: str, contract_dir: Path) -> int:
    from fluid_build.build_runners.base import _execute_embedded_sql_build

    contract = _contract(sql, contract_dir / "runtime" / "out" / "out.csv")
    return _execute_embedded_sql_build(contract["builds"][0], contract, contract_dir)


def _written(root: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in root.rglob("*")
        if p.is_file() and not p.is_symlink() and p.suffix != ".duckdb"
    )


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list:
    return [r for r in caplog.records if r.getMessage().startswith(name)]


@pytest.fixture
def cli_debug(caplog: pytest.LogCaptureFixture):
    """Logging as ``fluid --debug`` leaves it: the root at DEBUG, while the
    local provider's own logger stays pinned at INFO (``providers/base.py``)."""
    provider_log = logging.getLogger("fluid_build.providers.local.local")
    before = provider_log.level
    provider_log.setLevel(logging.INFO)
    try:
        with caplog.at_level(logging.DEBUG):
            yield caplog
    finally:
        provider_log.setLevel(before)


# ── the contract's SQL, through the real embedded-SQL build ──────────────


@pytest.mark.parametrize("where", ["home", "home_ancestor"])
def test_a_workspace_file_at_or_above_home_does_not_grant_home(where, home_tree, caplog):
    root = home_tree["home"] if where == "home" else home_tree["tmp"]
    _workspace(root)
    secret = home_tree["home"] / ".aws" / "credentials.csv"

    with caplog.at_level(logging.DEBUG):
        rc = _build(f"SELECT * FROM read_csv('{secret}')", home_tree["contract_dir"])

    assert rc == 1, "the build read a file under $HOME through a workspace at or above it"
    assert SECRET not in _written(home_tree["contract_dir"])
    warned = _events(caplog, "local_workspace_not_granted")
    assert warned, "no local_workspace_not_granted warning"
    message = warned[0].getMessage()
    assert warned[0].levelno == logging.WARNING
    assert str(root.resolve()) in message
    assert "fluid.workspace.yaml" in message and sandbox.OPERATOR_DIRS_ENV in message


def test_a_workspace_file_at_a_temp_root_does_not_grant_it(tmp_path, monkeypatch, caplog):
    """``/tmp`` holds every user's scratch files: a workspace there grants none of them."""
    temp_root = tmp_path / "systemp"
    other = temp_root / "someone-else"
    other.mkdir(parents=True)
    (other / "secret.csv").write_text(f"token\n{SECRET}\n", encoding="utf-8")
    contract_dir = temp_root / "proj"
    contract_dir.mkdir()
    _workspace(temp_root)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_root))
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    monkeypatch.chdir(contract_dir)
    monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)

    with caplog.at_level(logging.DEBUG):
        rc = _build(f"SELECT * FROM read_csv('{other}/secret.csv')", contract_dir)

    assert rc == 1
    assert SECRET not in _written(contract_dir)
    assert _events(caplog, "local_workspace_not_granted")


def test_a_workspace_at_the_filesystem_root_degrades_instead_of_failing(
    home_tree, monkeypatch, caplog
):
    """A ``/fluid.workspace.yaml`` drops the grant; the contract's own data still builds."""
    import fluid_build.util.workspace_root as workspace_root

    monkeypatch.setattr(workspace_root, "find_workspace_root", lambda start=None: Path(os.sep))

    with caplog.at_level(logging.DEBUG):
        rc = _build("SELECT * FROM read_csv('data/orders.csv')", home_tree["contract_dir"])

    assert rc == 0
    assert _events(caplog, "local_workspace_not_granted")


def test_a_workspace_inside_a_project_tree_still_grants_its_products(
    tmp_path, monkeypatch, cli_debug
):
    """The reason workspaces are granted at all: one product reads a sibling's file."""
    ws = tmp_path / "home" / "src" / "lab"
    sibling = ws / "orders" / "data"
    sibling.mkdir(parents=True)
    (sibling / "orders.csv").write_text("id\nsibling-row\n", encoding="utf-8")
    contract_dir = ws / "report"
    contract_dir.mkdir()
    _workspace(ws)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(contract_dir)
    monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)

    rc = _build(f"SELECT * FROM read_csv('{sibling}/orders.csv')", contract_dir)

    assert rc == 0
    assert "sibling-row" in (contract_dir / "runtime" / "out" / "out.csv").read_text()
    assert not _events(cli_debug, "local_workspace_not_granted")
    roots = _events(cli_debug, "local_sandbox_roots")
    assert roots, "the resolved sandbox roots do not reach the CLI's DEBUG output"
    assert f"workspace={ws.resolve()}" in roots[0].getMessage()


def test_the_sandbox_roots_are_logged_once_per_run(home_tree, cli_debug):
    assert _build("SELECT * FROM read_csv('data/orders.csv')", home_tree["contract_dir"]) == 0

    roots = _events(cli_debug, "local_sandbox_roots")
    assert len(roots) == 1, [r.getMessage() for r in roots]
    assert roots[0].levelno == logging.DEBUG, "the roots line is for --debug, not every run"
    assert f"anchor={home_tree['contract_dir']}" in roots[0].getMessage()
    assert "workspace=" not in roots[0].getMessage()


# ── the DuckDB acquisition runner's declared-location roots ──────────────


def test_acquisition_cannot_declare_a_home_file_through_a_workspace_at_home(home_tree, caplog):
    from fluid_build.build_runners.duckdb.runner import execute_duckdb_build

    _workspace(home_tree["home"])
    out = home_tree["contract_dir"] / "out" / "raw.parquet"
    contract = _acquisition_contract(str(home_tree["home"] / ".aws" / "credentials.csv"), out)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(DuckDBSandboxError, match=sandbox.OPERATOR_DIRS_ENV):
            execute_duckdb_build(contract["builds"][0], contract, home_tree["contract_dir"])

    assert not out.exists()
    assert _events(caplog, "local_workspace_not_granted")


# ── the rule itself ──────────────────────────────────────────────────────


def test_broad_roots_are_named(home_tree, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))

    assert sandbox.broad_root_reason(home_tree["home"]) == "home"
    assert sandbox.broad_root_reason(home_tree["tmp"]) == "home_ancestor"
    assert sandbox.broad_root_reason(os.sep) == "filesystem_root"
    assert sandbox.broad_root_reason(tmp_path / "systemp") == "temp_root"
    # A temp directory is named as one even with $HOME below it (as here).
    assert sandbox.broad_root_reason("/tmp") == "temp_root"
    assert sandbox.broad_root_reason("/var/tmp") == "temp_root"
    assert sandbox.broad_root_reason("/var") == "temp_root"
    # Below those, a directory is a project tree like any other.
    assert sandbox.broad_root_reason(home_tree["contract_dir"]) is None
    assert sandbox.broad_root_reason(tmp_path / "systemp" / "proj") is None


def test_a_tmpdir_inside_a_project_does_not_make_the_project_a_temp_root(tmp_path, monkeypatch):
    """``TMPDIR=<project>/.tmp`` (direnv, Nix, CI) is refused itself; its project is not."""
    lab = tmp_path / "work" / "lab"
    (lab / ".tmp").mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(lab / ".tmp"))
    monkeypatch.setattr(sandbox, "_SYSTEM_TEMP_DIRS", (str(tmp_path / "systemp"),))
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))

    assert sandbox.broad_root_reason(lab / ".tmp") == "temp_root"
    assert sandbox.broad_root_reason(lab) is None
    assert sandbox.workspace_grant(lab).refused is None

    # The same for a CI job's project checked out below /tmp.
    ci_lab = tmp_path / "systemp" / "ci-1234" / "lab"
    (ci_lab / ".tmp").mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(ci_lab / ".tmp"))
    assert sandbox.broad_root_reason(ci_lab) is None
    assert sandbox.broad_root_reason(tmp_path / "systemp") == "temp_root"


def test_a_directory_holding_an_os_issued_tmpdir_is_still_a_temp_root(tmp_path, monkeypatch):
    """macOS gives each user ``$TMPDIR=/private/var/folders/<a>/<b>/T``: the dirs above it are scratch."""
    folders = tmp_path / "private" / "var" / "folders"
    per_user_tmp = folders / "ab" / "cd" / "T"
    per_user_tmp.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(per_user_tmp))
    monkeypatch.setattr(sandbox, "_SYSTEM_TEMP_DIRS", (str(folders),))
    monkeypatch.setattr(sandbox, "_PER_USER_TEMP_PARENTS", (str(folders),))
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))

    assert sandbox.broad_root_reason(folders / "ab" / "cd") == "temp_root"
    assert sandbox.broad_root_reason(folders / "ab") == "temp_root"
    assert sandbox.broad_root_reason(per_user_tmp / "proj") is None


def test_broad_roots_are_compared_after_resolving_symlinks(home_tree, monkeypatch):
    alias = home_tree["tmp"] / "alias-of-home"
    alias.symlink_to(home_tree["home"])

    # The workspace named through a symlink is still $HOME ...
    assert sandbox.broad_root_reason(alias) == "home"
    # ... and so is $HOME named through one.
    monkeypatch.setenv("HOME", str(alias))
    assert sandbox.broad_root_reason(home_tree["home"]) == "home"


def test_broad_roots_ignore_case_where_the_file_system_does(home_tree, monkeypatch):
    upper = Path(str(home_tree["home"]).upper())
    monkeypatch.setattr(sandbox, "_CASE_INSENSITIVE_FS", True)
    assert sandbox.broad_root_reason(upper) == "home"
    monkeypatch.setattr(sandbox, "_CASE_INSENSITIVE_FS", False)
    assert sandbox.broad_root_reason(upper) is None


def test_workspace_grant_keeps_what_it_found_and_why_it_was_dropped(home_tree):
    _workspace(home_tree["home"])

    grant = sandbox.workspace_grant(home_tree["contract_dir"])

    assert grant.found == home_tree["home"].resolve()
    assert grant.refused == "home"
    assert grant.granted is None


def test_workspace_grant_without_a_workspace_file(home_tree):
    grant = sandbox.workspace_grant(home_tree["contract_dir"])

    assert grant.found is None and grant.refused is None and grant.granted is None


# ── the contract's own directory is held to the same rule ────────────────


def _home_contract(home_tree: Dict[str, Path]) -> Path:
    """A contract saved directly in ``$HOME`` (not in a project directory of its own)."""
    home_dir = home_tree["home"]
    (home_dir / "data").mkdir()
    (home_dir / "data" / "orders.csv").write_text("id\n1\n", encoding="utf-8")
    return home_dir


def test_a_contract_directly_in_home_is_refused_not_granted_home(home_tree, monkeypatch, capsys):
    contract_dir = _home_contract(home_tree)
    monkeypatch.chdir(contract_dir)
    secret = home_tree["home"] / ".aws" / "credentials.csv"

    rc = _build(f"SELECT * FROM read_csv('{secret}')", contract_dir)

    assert rc == 1, "a contract saved in $HOME was granted the whole of $HOME"
    assert SECRET not in _written(contract_dir / "runtime")
    printed = " ".join(capsys.readouterr().out.split())  # the console wraps long lines
    assert "your home directory" in printed and sandbox.OPERATOR_DIRS_ENV in printed


def test_a_contract_directly_in_home_is_refused_even_for_its_own_data(home_tree, monkeypatch):
    """Refused, not narrowed: its own data and ``~/.aws`` are the same directory to the sandbox."""
    contract_dir = _home_contract(home_tree)
    monkeypatch.chdir(contract_dir)

    assert _build("SELECT * FROM read_csv('data/orders.csv')", contract_dir) == 1


def test_a_contract_directly_in_a_temp_root_is_refused(tmp_path, monkeypatch):
    temp_root = tmp_path / "systemp"
    (temp_root / "someone-else").mkdir(parents=True)
    (temp_root / "someone-else" / "s.csv").write_text(f"token\n{SECRET}\n", encoding="utf-8")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_root))
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    monkeypatch.chdir(temp_root)
    monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
    monkeypatch.delenv(sandbox.OPERATOR_DIRS_ENV, raising=False)

    rc = _build(f"SELECT * FROM read_csv('{temp_root}/someone-else/s.csv')", temp_root)

    assert rc == 1
    out = temp_root / "runtime" / "out" / "out.csv"
    assert not out.exists() or SECRET not in out.read_text(encoding="utf-8")


def test_the_operator_can_allow_a_broad_contract_directory(home_tree, monkeypatch):
    """``FLUID_DUCKDB_ALLOWED_DIRS`` naming the directory is the operator saying yes."""
    contract_dir = _home_contract(home_tree)
    monkeypatch.chdir(contract_dir)
    monkeypatch.setenv(sandbox.OPERATOR_DIRS_ENV, str(home_tree["home"]))

    assert _build("SELECT * FROM read_csv('data/orders.csv')", contract_dir) == 0


def test_a_working_directory_at_home_is_not_a_confinement_root(home_tree, monkeypatch):
    """A bare ``apply`` (no contract directory) in ``$HOME``: a declaration there is refused."""
    from fluid_build.providers.local.local import LocalProvider

    monkeypatch.chdir(home_tree["home"])
    provider = LocalProvider(project="local", region="local")
    secret = str(home_tree["home"] / ".aws" / "credentials.csv")

    with pytest.raises(DuckDBSandboxError):
        provider._allowlist([{"path": secret}])
    assert str(home_tree["home"].resolve()) not in provider._allowlist([]).dirs


def test_acquisition_from_a_contract_directory_at_home_is_refused(home_tree):
    from fluid_build.build_runners.duckdb.runner import execute_duckdb_build

    contract_dir = _home_contract(home_tree)
    out = contract_dir / "out" / "raw.parquet"
    contract = _acquisition_contract(str(contract_dir / "data" / "orders.csv"), out)

    with pytest.raises(DuckDBSandboxError, match="home directory"):
        execute_duckdb_build(contract["builds"][0], contract, contract_dir)
    assert not out.exists()


def test_refuse_broad_root_names_the_directory_and_both_fixes(home_tree):
    with pytest.raises(DuckDBSandboxError) as caught:
        sandbox.refuse_broad_root(home_tree["home"])
    text = str(caught.value)
    assert str(home_tree["home"].resolve()) in text
    assert "your home directory" in text and sandbox.OPERATOR_DIRS_ENV in text
    sandbox.refuse_broad_root(home_tree["contract_dir"])  # a project directory passes


# ── a workspace above the contract's own repository ──────────────────────


@pytest.mark.parametrize("marker", [".git", ".hg", ".git-file"])
def test_a_workspace_above_the_contracts_repository_is_not_granted(
    marker, tmp_path, monkeypatch, caplog
):
    """``~/src/fluid.workspace.yaml`` with each repository cloned below it: one
    repository's SQL does not read another's files."""
    src = tmp_path / "home" / "src"
    (src / "other").mkdir(parents=True)
    (src / "other" / "env.csv").write_text(f"token\n{SECRET}\n", encoding="utf-8")
    contract_dir = src / "cloned"
    contract_dir.mkdir()
    if marker == ".git-file":  # a worktree or submodule checkout
        (contract_dir / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    else:
        (contract_dir / marker).mkdir()
    _workspace(src)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(contract_dir)
    monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)
    monkeypatch.delenv(sandbox.OPERATOR_DIRS_ENV, raising=False)

    with caplog.at_level(logging.DEBUG):
        rc = _build(f"SELECT * FROM read_csv('{src}/other/env.csv')", contract_dir)

    assert rc == 1
    assert SECRET not in _written(contract_dir)
    grant = sandbox.workspace_grant(contract_dir)
    assert grant.found == src.resolve() and grant.refused == "outside_repository"
    warned = _events(caplog, "local_workspace_not_granted")
    assert warned and "repository" in warned[0].getMessage()


def test_a_workspace_at_the_root_of_the_repository_is_still_granted(tmp_path, monkeypatch):
    """A monorepo: ``fluid.workspace.yaml`` beside ``.git``, products below it."""
    repo = tmp_path / "home" / "src" / "lab"
    (repo / ".git").mkdir(parents=True)
    sibling = repo / "orders" / "data"
    sibling.mkdir(parents=True)
    (sibling / "orders.csv").write_text("id\nsibling-row\n", encoding="utf-8")
    contract_dir = repo / "report"
    contract_dir.mkdir()
    _workspace(repo)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(contract_dir)
    monkeypatch.delenv("FLUID_UPSTREAM_CONTRACTS", raising=False)

    assert sandbox.workspace_grant(contract_dir).granted == repo.resolve()
    assert _build(f"SELECT * FROM read_csv('{sibling}/orders.csv')", contract_dir) == 0


# ── other users' homes and shared memory ─────────────────────────────────


def test_other_accounts_home_directories_are_broad_roots(tmp_path, monkeypatch):
    """Run as root (``HOME=/root``), ``/home/alice`` is still somebody's home."""
    monkeypatch.setenv("HOME", str(tmp_path / "root-home"))
    homes = tmp_path / "home"
    (homes / "alice" / "proj").mkdir(parents=True)
    monkeypatch.setattr(sandbox, "_HOMES_PARENTS", (str(homes),))

    assert sandbox.broad_root_reason(homes / "alice") == "user_home"
    assert sandbox.broad_root_reason(homes) == "user_home"
    assert sandbox.broad_root_reason(homes / "alice" / "proj") is None
    _workspace(homes / "alice")
    assert sandbox.workspace_grant(homes / "alice" / "proj").refused == "user_home"


def test_the_well_known_shared_directories_are_broad_roots(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert sandbox.broad_root_reason("/dev/shm") == "temp_root"
    assert sandbox.broad_root_reason("/home/alice") == "user_home"
    assert sandbox.broad_root_reason("/Users/alice") == "user_home"
