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

"""The engine owns DuckDB's spill directory, so the contract's repository cannot.

DuckDB's default ``temp_directory`` is relative -- ``.tmp`` in the working
directory, or ``<db>.tmp`` beside a file database -- and DuckDB resolves its
temp directory into ``allowed_directories`` like any other grant. A contract
repository that ships ``.tmp -> /`` therefore handed the whole host to its own
SQL, with an EMPTY allowlist, on every version the engine supports (reproduced
on duckdb 1.5.1, 1.5.6 and 2.0.0; the MCP output port's ``query_sql`` runs on
the same helper).

11 of the 14 tests here fail when the production fix is reverted to the previous
code. The 3 that do not are the deliberately non-discriminating guards --
spilling is still enabled, a failed open leaks nothing, and the read-back
accepts every grant shape a real call site uses -- which exist to catch a
future regression rather than to witness this one. Two guards are additionally
mutation-checked, because an assertion about a directory that something else
also removes is worthless: deleting the explicit spill cleanup fails
``test_a_failure_after_the_spill_directory_exists_still_removes_it``, and
disabling :func:`_open_lock` fails
``test_concurrent_opens_of_one_file_database_never_cross_configure`` in 6 runs
out of 6.
"""

from __future__ import annotations

import gc
import os
import stat
from pathlib import Path
from typing import Any, List

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.providers._duckdb_sandbox import (  # noqa: E402
    SPILL_DIR_PREFIX,
    DuckDBAllowlist,
    DuckDBSandboxError,
    secure_duckdb_connect,
)

#: A file every host has, that no test here ever grants.
UNGRANTED_HOST_FILE = "/etc/passwd"


def _setting(con: Any, name: str) -> Any:
    return con.execute(f"SELECT current_setting('{name}')").fetchall()[0][0]


def _reads(con: Any, path: str) -> bool:
    """Whether the SQL on ``con`` can read ``path`` at all."""
    try:
        con.execute(f"SELECT count(*) FROM read_text('{path}')").fetchall()
        return True
    except duckdb.Error:
        return False


# ── the defect: a symlinked spill directory grants its target ────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink to the filesystem root")
def test_a_tmp_symlink_to_root_in_the_working_directory_grants_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.tmp -> /`` beside an in-memory run: DuckDB's default spill path.

    The contract repository is the working directory, so it chooses what ``.tmp``
    is. Before the fix ``allowed_directories`` was ``['/']`` here.
    """
    repo = tmp_path / "contract-repo"
    repo.mkdir()
    (repo / ".tmp").symlink_to("/")
    monkeypatch.chdir(repo)

    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none())
    try:
        granted = list(_setting(con, "allowed_directories"))
        assert granted == [
            d for d in granted if SPILL_DIR_PREFIX in d
        ], f"only the engine's own spill directory may be granted, got {granted}"
        assert not _reads(con, UNGRANTED_HOST_FILE)
        assert not _reads(con, ".tmp/etc/passwd")
    finally:
        con.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink to the filesystem root")
def test_a_db_tmp_symlink_to_root_beside_a_file_database_grants_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``<db>.tmp -> /`` beside a file database: the other default spill path.

    A file database gets ``<db>.tmp``, not ``.tmp``, so the in-memory test above
    does not cover it. The database file itself needs no grant (it is opened
    before the lock), which is why the allowlist here is still empty.
    """
    repo = tmp_path / "contract-repo"
    repo.mkdir()
    db = repo / "work.duckdb"
    Path(str(db) + ".tmp").symlink_to("/")
    monkeypatch.chdir(repo)

    con = secure_duckdb_connect(db, allow=DuckDBAllowlist.none())
    try:
        granted = list(_setting(con, "allowed_directories"))
        assert granted == [
            d for d in granted if SPILL_DIR_PREFIX in d
        ], f"only the engine's own spill directory may be granted, got {granted}"
        assert not _reads(con, UNGRANTED_HOST_FILE)
        assert not _reads(con, "work.duckdb.tmp/etc/passwd")
    finally:
        con.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink")
def test_a_tmp_symlink_cannot_widen_a_connection_that_grants_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The realistic shape: a contract granted its own directory, and nothing more.

    ``DuckDBAllowlist.none()`` is the sharpest baseline, but no real call site
    uses it for a build. This is the one that matters in practice.
    """
    repo = tmp_path / "contract-repo"
    (repo / "data").mkdir(parents=True)
    (repo / "data" / "in.csv").write_text("a\n1\n", encoding="utf-8")
    (repo / ".tmp").symlink_to("/")
    monkeypatch.chdir(repo)

    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_dirs(repo / "data"))
    try:
        assert con.execute("SELECT count(*) FROM read_csv('data/in.csv')").fetchall() == [(1,)]
        assert not _reads(con, UNGRANTED_HOST_FILE)
    finally:
        con.close()


# ── what the engine's own spill directory is ─────────────────────────────


def test_the_spill_directory_is_private_to_the_process_and_outside_the_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "contract-repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none())
    try:
        spill = Path(_setting(con, "temp_directory"))
        assert spill.is_absolute(), "a relative temp_directory is the defect"
        assert spill.name.startswith(SPILL_DIR_PREFIX)
        assert repo.resolve() not in spill.resolve().parents
        if os.name != "nt":
            assert stat.S_IMODE(spill.stat().st_mode) == 0o700
    finally:
        con.close()


def test_two_connections_do_not_share_a_spill_directory() -> None:
    """DuckDB assumes one instance owns its temp directory (monarch-app #1421)."""
    first = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none())
    second = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none())
    try:
        assert _setting(first, "temp_directory") != _setting(second, "temp_directory")
    finally:
        first.close()
        second.close()


def test_spilling_is_still_enabled_under_the_lock() -> None:
    """``temp_directory = ''`` would be tighter still, and is deliberately not used.

    Empty, ``allowed_directories`` stays empty, but spilling is off: a query that
    outgrows ``memory_limit`` fails where it used to finish, and the
    configuration is locked, so no call site could turn it back on. Pin the
    choice rather than a spill volume: how much a given sort spills depends on
    the host's memory and thread count, and a tight ``memory_limit`` makes this
    query OOM on a plain connection with DuckDB's OWN default temp directory too
    (verified on 1.5.1, 1.5.6 and 2.0.0), so a spilled-bytes assertion would
    measure the machine, not the sandbox.
    """
    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none())
    try:
        assert _setting(con, "temp_directory") != "", "spilling must not be switched off"
        assert _setting(con, "max_temp_directory_size") != "0 bytes"
        rows = con.execute(
            "SELECT count(*) FROM ("
            "SELECT i, repeat('x', 200) AS s FROM range(400000) t(i) ORDER BY s, i)"
        ).fetchall()
        assert rows == [(400000,)]
    finally:
        con.close()


def test_the_spill_directory_goes_away_with_the_connection() -> None:
    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none())
    spill = Path(_setting(con, "temp_directory"))
    assert spill.is_dir()
    con.close()
    del con
    gc.collect()
    assert not spill.exists(), "the engine's spill directory outlived its connection"


def test_a_failed_open_leaves_no_spill_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``before_lock`` that raises must not leak the directory it had made.

    Watches the one directory THIS call creates rather than globbing ``$TMPDIR``
    for the prefix: the suite runs under pytest-xdist, so another worker's
    connection can hold a spill directory of its own for the whole test, and a
    count of them would fail for reasons that have nothing to do with the
    call under test.
    """
    import fluid_build.providers._duckdb_sandbox as sandbox

    made: List[str] = []
    real = sandbox._own_spill_dir
    monkeypatch.setattr(
        sandbox, "_own_spill_dir", lambda: made.append(real()) or made[-1]  # type: ignore[func-returns-value]
    )

    def boom(_con: Any) -> None:
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none(), before_lock=boom)
    # before_lock runs BEFORE the spill directory is created, so the expected
    # outcome is that none was made at all; if that order ever changes, the
    # one that was made must still be gone.
    assert [d for d in made if Path(d).exists()] == []


# ── the read-back refuses a grant the call site did not ask for ──────────


def test_a_failure_after_the_spill_directory_exists_still_removes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The except branch, exercised where there IS a directory to clean up.

    ``before_lock`` raises BEFORE the spill directory is created, so the
    failed-open test above cannot reach this: it passes with the cleanup
    deleted. The read-back is the one failure that happens after, and with a
    live traceback holding the frame -- which is what ``pytest.raises`` and a
    real caller both produce -- the ``weakref.finalize`` has not fired, so
    without the explicit cleanup the directory is observably left behind.
    """
    import fluid_build.providers._duckdb_sandbox as sandbox

    made: List[str] = []
    real = sandbox._own_spill_dir

    def record() -> str:
        made.append(real())
        return made[-1]

    monkeypatch.setattr(sandbox, "_own_spill_dir", record)
    # Make the post-lock read-back refuse, so the failure lands after the
    # directory exists.
    monkeypatch.setattr(
        sandbox,
        "_refuse_ungranted_dirs",
        lambda con, allow, spill: (_ for _ in ()).throw(
            sandbox._sandbox_error(what="forced", why="forced", fix="forced")
        ),
    )
    with pytest.raises(DuckDBSandboxError) as caught:
        secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none())
    # Holding ``caught`` keeps the traceback, and so the frame holding the
    # connection, alive: the weakref.finalize therefore has NOT fired, and what
    # is asserted below is the explicit cleanup rather than garbage collection.
    # Without this the test passes with that cleanup deleted.
    assert caught.tb is not None
    assert made, "the spill directory was never created, so this proves nothing"
    assert not Path(made[0]).exists(), "a failed open left its spill directory behind"


def test_a_caller_cannot_choose_the_spill_directory() -> None:
    """``config={'temp_directory': ...}`` is refused rather than silently overridden.

    DuckDB folds its temp directory into ``allowed_directories``, so honouring
    a caller's value would widen what contract SQL reads; ignoring it would
    leave the caller believing its own directory was in use.
    """
    with pytest.raises(DuckDBSandboxError, match="temp_directory"):
        secure_duckdb_connect(
            ":memory:", allow=DuckDBAllowlist.none(), config={"temp_directory": "/tmp"}
        )


def test_the_read_back_refuses_a_remote_prefix_the_call_site_did_not_grant() -> None:
    """The remote branch of the read-back, which the accept-path tests never reach.

    A reported entry that is a URL is refused unless it is literally one of
    ``allow.remote_prefixes`` -- including a prefix reported in some other
    string form, which falls through to the same refusal.
    """
    import fluid_build.providers._duckdb_sandbox as sandbox

    allow = DuckDBAllowlist.none().with_remote("s3://bucket/landing/")
    con = secure_duckdb_connect(":memory:", allow=allow)
    try:
        spill = _setting(con, "temp_directory")
        # _Reporting stands in for DuckDB reporting a prefix nothing granted.
        with pytest.raises(DuckDBSandboxError, match="other-bucket"):
            sandbox._refuse_ungranted_dirs(_Reporting(("s3://other-bucket/",)), allow, spill)
    finally:
        con.close()


class _Reporting:
    """A stand-in connection whose ``allowed_directories`` is whatever we say."""

    def __init__(self, entries: Any) -> None:
        self._entries = list(entries)

    def execute(self, _sql: str) -> "_Reporting":
        return self

    def fetchall(self) -> List[Any]:
        return [(self._entries,)]


def test_an_unasked_for_grant_fails_the_connection_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If DuckDB ever grants something else, the connection is refused, not used.

    Stand-in for a future DuckDB (or a sandbox bug) folding a directory in:
    the only way to reach the check is to widen the list behind its back, so the
    allowlist is narrowed after it is sent.
    """
    extra = tmp_path / "not-granted"
    extra.mkdir()
    granted = tmp_path / "granted"
    granted.mkdir()
    allow = DuckDBAllowlist.none().with_dirs(granted, extra)
    narrowed = DuckDBAllowlist(dirs=(str(granted),))

    import fluid_build.providers._duckdb_sandbox as sandbox

    real = sandbox._refuse_ungranted_dirs
    monkeypatch.setattr(
        sandbox,
        "_refuse_ungranted_dirs",
        lambda con, _allow, spill: real(con, narrowed, spill),
    )
    with pytest.raises(DuckDBSandboxError) as caught:
        secure_duckdb_connect(":memory:", allow=allow)
    assert "not-granted" in str(caught.value)


def test_the_read_back_accepts_every_shape_a_call_site_grants(tmp_path: Path) -> None:
    """Dirs, single files, remote prefixes and a symlinked duplicate: no false refusal.

    DuckDB reports grants after ``realpath``, so two entries naming one
    directory come back once; and DuckDB 1.5 resolves a remote prefix as a
    relative local path (``<cwd>/s3:/bucket/landing/``) where 2.0 reports it
    verbatim. A read-back that did not know both would refuse every real build.
    """
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.csv").write_text("a\n1\n", encoding="utf-8")
    allow = (
        DuckDBAllowlist.none()
        .with_dirs(real)
        .with_paths(real / "a.csv")
        .with_remote("s3://bucket/landing/", "https://host/data/")
    )
    if os.name != "nt":
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        allow = allow.with_dirs(link)

    con = secure_duckdb_connect(":memory:", allow=allow)
    try:
        assert con.execute(f"SELECT count(*) FROM read_csv('{real / 'a.csv'}')").fetchall() == [
            (1,)
        ]
    finally:
        con.close()


# ── one instance per file means one configurer at a time ─────────────────


def test_concurrent_opens_of_one_file_database_never_cross_configure(tmp_path: Path) -> None:
    """Two threads opening one file DB: each connection gets its own allowlist, or a refusal.

    DuckDB shares one instance per file per process and the sandbox's settings
    live on the instance, so without ``_open_lock`` the two opens interleave:
    one connection runs under the other's allowlist, and the post-lock
    read-back inspects a list the other thread is still writing, which
    segfaults DuckDB 1.5.x in ``AllowedDirectoriesSetting::GetSetting``. What
    must never happen is a RETURNED connection that can read a directory it
    did not grant.
    """
    import threading

    granted = tmp_path / "granted"
    granted.mkdir()
    (granted / "secret.csv").write_text("a\n1\n", encoding="utf-8")
    db = tmp_path / "shared.duckdb"

    results: List[Any] = []
    lock = threading.Lock()

    def open_with(allow: DuckDBAllowlist, wide: bool) -> None:
        try:
            con = secure_duckdb_connect(db, allow=allow)
        except DuckDBSandboxError:
            with lock:
                results.append(("refused", None, wide))
            return
        try:
            reads = _reads(con, str(granted / "secret.csv"))
            with lock:
                results.append(("open", reads, wide))
        finally:
            con.close()

    for _ in range(12):
        results.clear()
        wide = DuckDBAllowlist.none().with_dirs(granted)
        narrow = DuckDBAllowlist.none()
        threads = [
            threading.Thread(target=open_with, args=(wide, True)),
            threading.Thread(target=open_with, args=(narrow, False)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 2, f"a thread neither opened nor was refused: {results}"
        # The invariant is not "one at a time" -- serialising the opens makes
        # both succeed in turn, which is correct. It is that a connection
        # which granted NOTHING can never read the other's directory.
        assert ("open", True) not in [
            (kind, leaked) for kind, leaked, wide in results if not wide
        ], f"a connection read a directory it did not grant: {results}"
