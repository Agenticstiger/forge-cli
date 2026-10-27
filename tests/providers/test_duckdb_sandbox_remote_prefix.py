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

"""A declared remote prefix bounds remote reads and grants no local directory.

DuckDB resolves every ``allowed_directories`` entry against the file system, so
a prefix whose text also names a local path becomes a local grant. A link in the
working directory -- which the contract's own repository supplies -- is enough:
with ``s3: -> /`` present, ``s3://etc/`` granted ``/etc`` to contract SQL while
``allow.dirs`` was EMPTY. Reproduced on duckdb 1.5.1, 1.5.6 and 2.0.0
(Trello 1209), and reachable from a real call site: ``providers/local/local.py``
hands a raw ``s3://`` URI to :meth:`DuckDBAllowlist.with_declared`.

The read-back added in #743 could not see it. Its accommodation for DuckDB 1.5 --
which reports ``s3://bucket/landing/`` as ``<cwd>/s3:/bucket/landing/`` -- was
written as ``realpath(prefix)``, and that **follows symlinks inside the prefix**,
so the escape's own resolution entered the granted set and excused the grant.
The prefix is now joined to the resolved cwd lexically, so the honest form still
matches and an escaping one no longer does.

Reverting the production change -- its two call sites and the read-back's
accommodation, with the new helpers left defined so the tests that patch them
still import -- fails 6 of the 8 tests this file had at that measurement, on
duckdb 1.5.6 and 2.0.0 alike. The 2 that survive are deliberate: the
legitimate-prefix guard has to pass before and after, and the defect witness
passes on pre-fix code by construction. A LITERAL revert deletes
``_refuse_escaping_remote_prefixes`` as well, so the tests that patch it error
instead of failing -- still red, by a different route.

There are two barriers, and the tests pin each one alone: the grant is refused
before it is set, and the read-back under the lock refuses it again if that
check is ever bypassed. ``test_with_both_barriers_bypassed_the_grant_is_real``
witnesses the defect itself, so this file proves what it guards rather than
only asserting the guard runs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.providers._duckdb_sandbox import (  # noqa: E402
    SPILL_DIR_PREFIX,
    DuckDBAllowlist,
    DuckDBSandboxError,
    secure_duckdb_connect,
)

# The probes and the ungranted-file constant are the sibling suite's, not copies
# of them: the two files ask DuckDB the same two questions, and a second pair
# that drifted would quietly weaken whichever file still held the old one.
from tests.providers.test_duckdb_sandbox_spill import (  # noqa: E402
    UNGRANTED_HOST_FILE,
    _reads,
    _Reporting,
    _setting,
)

sandbox = sys.modules[DuckDBAllowlist.__module__]

posix_symlink = pytest.mark.skipif(os.name == "nt", reason="POSIX symlink")


def _rendered(error: DuckDBSandboxError) -> str:
    """Every field a reader sees, joined.

    ``str(error)`` is only ``what``, which the renderer uses as the panel title
    and truncates to one line -- which is why the prefixes live in ``why``.
    """
    return " ".join([str(error), error.why or "", error.fix or ""])


def _repo_with_scheme_link(tmp_path: Path, target: str) -> Path:
    """A contract repository shipping ``s3:`` as a link to ``target``."""
    repo = tmp_path / "contract-repo"
    repo.mkdir()
    (repo / "s3:").symlink_to(target)
    return repo


# ── the defect ───────────────────────────────────────────────────────────


@posix_symlink
def test_a_scheme_link_to_root_does_not_turn_a_prefix_into_a_local_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``s3://etc/`` with ``s3: -> /`` in cwd: the whole of /etc, from an empty allowlist.

    Before the fix this connected and ``read_text('/etc/passwd')`` returned a row.
    """
    monkeypatch.chdir(_repo_with_scheme_link(tmp_path, "/"))

    with pytest.raises(DuckDBSandboxError) as excinfo:
        secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_remote("s3://etc/"))

    rendered = _rendered(excinfo.value)
    assert "s3://etc/" in rendered, rendered
    assert os.path.realpath("/etc") in rendered, rendered


@posix_symlink
def test_the_escape_is_refused_even_when_the_target_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grant of a directory that is not there yet is still a grant.

    ``s3://staging-area/`` with ``s3: -> /`` resolves to ``/staging-area``, which
    no host has. DuckDB grants it regardless, so contract SQL could COPY TO a
    path outside everything the contract declared. Existence is the wrong test;
    this is why the fix does not ask whether the resolved path is there.
    """
    monkeypatch.chdir(_repo_with_scheme_link(tmp_path, "/"))

    with pytest.raises(DuckDBSandboxError) as excinfo:
        secure_duckdb_connect(
            ":memory:", allow=DuckDBAllowlist.none().with_remote("s3://staging-area/")
        )

    assert "s3://staging-area/" in _rendered(excinfo.value)


@posix_symlink
def test_a_scheme_link_inside_the_repository_is_refused_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``s3: -> ./sub``: the grant stays under cwd, and is still not what was asked for.

    Deliberately strict. The declaration said "bound remote reads to this
    bucket"; granting a local directory instead is a different thing whether or
    not it escapes the repository, and a reader who sees the refusal can declare
    the directory on purpose.
    """
    repo = tmp_path / "contract-repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "s3:").symlink_to(repo / "sub")
    monkeypatch.chdir(repo)

    with pytest.raises(DuckDBSandboxError) as excinfo:
        secure_duckdb_connect(
            ":memory:", allow=DuckDBAllowlist.none().with_remote("s3://bucket/landing/")
        )

    assert "s3://bucket/landing/" in _rendered(excinfo.value)


@posix_symlink
def test_the_read_back_still_refuses_when_the_pre_set_check_is_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two barriers, independently sufficient. This one is the backstop.

    With the pre-set refusal neutralised the grant does reach DuckDB, and the
    read-back under the lock has to catch it -- which it only does because its
    accommodation for the DuckDB 1.5 form stopped following symlinks.
    """
    monkeypatch.chdir(_repo_with_scheme_link(tmp_path, "/"))
    monkeypatch.setattr(sandbox, "_refuse_escaping_remote_prefixes", lambda *a, **k: None)

    with pytest.raises(DuckDBSandboxError) as excinfo:
        secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_remote("s3://etc/"))

    assert "did not ask for" in str(excinfo.value), str(excinfo.value)


@posix_symlink
def test_the_pre_set_check_still_refuses_when_the_read_back_is_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: the grant is never set in the first place."""
    monkeypatch.chdir(_repo_with_scheme_link(tmp_path, "/"))
    monkeypatch.setattr(sandbox, "_refuse_ungranted_dirs", lambda *a, **k: None)

    with pytest.raises(DuckDBSandboxError) as excinfo:
        secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_remote("s3://etc/"))

    assert "s3://etc/" in _rendered(excinfo.value), _rendered(excinfo.value)


@posix_symlink
def test_with_both_barriers_bypassed_the_grant_is_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Witness the defect itself, so this file proves what it is guarding against.

    Neither barrier runs here, which is the pre-fix state: ``allowed_directories``
    holds the link's target and contract SQL reads the host out of an allowlist
    that granted no directory at all. If a future change makes this test fail,
    the escape has been closed further upstream and this file should say so.
    """
    monkeypatch.chdir(_repo_with_scheme_link(tmp_path, "/"))
    monkeypatch.setattr(sandbox, "_refuse_escaping_remote_prefixes", lambda *a, **k: None)
    monkeypatch.setattr(sandbox, "_refuse_ungranted_dirs", lambda *a, **k: None)

    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_remote("s3://etc/"))
    try:
        granted = list(_setting(con, "allowed_directories"))
        local = [d for d in granted if SPILL_DIR_PREFIX not in d and "://" not in d]
        assert local, f"expected the escape to be visible, got {granted}"
        assert _reads(con, UNGRANTED_HOST_FILE)
    finally:
        con.close()


@posix_symlink
def test_the_read_back_defers_to_the_escape_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deferral needs the read-back driven directly to be exercised at all.

    In ``test_the_read_back_still_refuses_when_the_pre_set_check_is_bypassed``
    the patched no-op is the SAME function the read-back defers to, so that test
    witnesses the generic "did not ask for" message, not this one. Here the real
    check runs inside the read-back, against a stand-in connection reporting the
    grant DuckDB would have made.
    """
    monkeypatch.chdir(_repo_with_scheme_link(tmp_path, "/"))
    allow = DuckDBAllowlist.none().with_remote("s3://etc/")

    with pytest.raises(DuckDBSandboxError) as excinfo:
        sandbox._refuse_ungranted_dirs(
            _Reporting((os.path.realpath("/etc") + os.sep,)), allow, str(tmp_path)
        )

    rendered = _rendered(excinfo.value)
    assert "s3://etc/" in rendered, rendered
    assert "did not ask for" not in rendered, rendered


# ── what must keep working ───────────────────────────────────────────────


def test_a_legitimate_remote_prefix_still_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No link in cwd: the shape every real call site uses.

    DuckDB 1.5 reports this as ``<cwd>/s3:/bucket/landing/`` and 2.0 as the URL
    itself; both are accepted, and neither grants anything that exists. The
    bounding this entry provides is live network behaviour, verified out of band
    against a public bucket on 1.5.1 and 1.5.6: ungranted and wrong-bucket reads
    are refused, the granted prefix reads.
    """
    monkeypatch.chdir(tmp_path)

    con = secure_duckdb_connect(
        ":memory:", allow=DuckDBAllowlist.none().with_remote("s3://bucket/landing/")
    )
    try:
        granted = list(_setting(con, "allowed_directories"))
        existing = [
            d for d in granted if SPILL_DIR_PREFIX not in d and "://" not in d and os.path.exists(d)
        ]
        assert existing == [], f"nothing local should exist in the grant, got {existing}"
        assert not _reads(con, UNGRANTED_HOST_FILE)
    finally:
        con.close()


@posix_symlink
def test_the_refusal_does_not_blame_the_sandbox_for_a_contract_s_doing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generic read-back message sends the reader to report a sandbox bug.

    That advice is right for a DuckDB bookkeeping surprise and wrong here: a
    contract plus its repository is all it takes, so the message has to name the
    prefix and what to change instead.
    """
    monkeypatch.chdir(_repo_with_scheme_link(tmp_path, "/"))

    with pytest.raises(DuckDBSandboxError) as excinfo:
        secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_remote("s3://etc/"))

    error = excinfo.value
    rendered = _rendered(error)
    assert "bug in the sandbox" not in rendered, rendered
    assert "working directory" in rendered, rendered
    assert "s3://etc/" in rendered, rendered
    # The title is what a terminal truncates, so it must stand alone.
    assert "s3://" not in str(error), str(error)
