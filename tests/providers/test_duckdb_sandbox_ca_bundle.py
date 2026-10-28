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

"""A declared remote prefix grants the one CA bundle its TLS needs, and nothing else.

On DuckDB 2.0.0 every remote read failed before a byte moved. The message, as
DuckDB prints it and as the CLI shows it on its ``why`` line -- the exception
type is ``duckdb.PermissionException``, which is not part of the text::

    Permission Error: Cannot read a CA certificate bundle: file system access
    is restricted by configuration. Add one of /etc/ssl/certs/ca-certificates.crt,
    ... to allowed_paths, point ca_cert_file at an allowed file, or disable
    server certificate verification

duckdb/duckdb-httpfs#474 moved the bundle read inside DuckDB's file system
(``CURLOPT_CAINFO_BLOB``, an in-memory store for httplib) where curl and OpenSSL
used to open it themselves, bypassing ``allowed_paths`` entirely -- so the same
read works with no grant on 1.5.x and is refused on 2.0.0.
``fluid init --discover s3://noaa-ghcn-pds/csv/by_year/2024.csv`` emitted a
contract with 8 columns on 1.5.6 and exited 1 on 2.0.0, measured on clean main
(Trello card C2fT4zj1, P0).

What the grant must not become is the reason this file exists. It is one FILE in
``allowed_paths``, so it cannot widen ``allowed_directories`` -- where the remote
prefixes that are load-bearing on 1.5.x live (#757) -- and it is added only when
the call site declared a remote prefix, so a local read gains nothing.

And it is never the host's trust store. **A path in ``allowed_paths`` is granted
for WRITING**, which the world-class inspection of the first version of this fix
proved by replacing a 333 KB store with 14 bytes of its own PEM through ``COPY
… TO``. What is granted is a mode-0400 copy the connection owns, with
``ca_cert_file`` pinned to it, thrown away on teardown. The allowlist OBJECT is
still left alone -- the grant is appended at ``SET`` time, so every caller that
reads ``allow`` as the declared-inputs list is unaffected -- while the refusal
hint does disclose that a copy of the trust store is readable, because telling
the user otherwise would be false.

Nothing here reads a remote location, but the tests that assert a GRANT do load
httpfs -- the grant is gated on the ``ca_cert_file`` pin, and that setting lives
in the extension -- so they go through :func:`_granting_connection`, which skips
when httpfs cannot be installed, as ``test_duckdb_sandbox.py`` does. On a cold
machine that is a ~19 MB download, once. The tests that assert NO grant need no
extension at all. The real network proof is the CLI run in the PR description;
what CI can check without reading anything remote is the read-back, and on 2.0.0
the refusal itself, which arrives before any connection is attempted.

Each mechanism was reverted on duckdb 2.0.0 and the failures recorded, not
predicted. Re-measured against this file's 20 tests whenever a test was added,
because a table that drifts teaches a maintainer to distrust the count:

=====================================  ==============
reverted                               tests that fail
=====================================  ==============
the probe returns ``None``             11
the probe returns the directory        13
the remote-prefix guard is dropped     1
the copy is replaced by the store      7
``chmod 0400`` is dropped              2
the ``ca_cert_file`` pin is dropped    3
the pin is session-scoped, not GLOBAL  1
the exposed-directory check is dropped 1
the grant is kept when the pin fails   1
the cleanup finalize is dropped        1
a failed copy is not cleaned up        1
=====================================  ==============

Dropping the remote-prefix guard fails only ONE test now, where it used to fail
two: the grant is gated on the pin as well, so a local connection (which loads
no httpfs) gets nothing even with the guard gone, and
``test_a_local_refusal_hint_says_nothing_about_a_trust_store`` is what catches
it. Two mechanisms covering one mistake is the point; the count moving is not a
regression.

Three rows were worth the trouble of checking. Returning the bundle's directory
did NOT fail ``test_the_grant_is_one_file_and_not_a_directory`` until that test
was made to check the granted entry itself -- a directory handed to
``allowed_paths`` never reaches ``allowed_directories``, so reading the latter
back could not see it. Granting the host store did NOT fail the write test until
it asserted the store's absence from ``allowed_paths``: the ``COPY`` is refused
on a developer machine either way, because ``/etc/ssl/cert.pem`` is root-owned,
and in the root container the threat model is about it would not have been
refused at all. And the pin being session-scoped instead of GLOBAL fails exactly
one test, ``test_a_cursor_inherits_the_pin``, which exists because nothing else
in the file would have noticed.
"""

from __future__ import annotations

import gc
import os
import posixpath
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, List

import pytest

duckdb = pytest.importorskip("duckdb")

from fluid_build.providers._duckdb_sandbox import (  # noqa: E402
    DuckDBAllowlist,
    duckdb_version,
    sandbox_refusal_hint,
    secure_duckdb_connect,
)

# The probe is the sibling suite's, not a copy: a second one that drifted would
# quietly weaken whichever file still held the old spelling.
from tests.providers.test_duckdb_sandbox_spill import _setting  # noqa: E402

sandbox = sys.modules[DuckDBAllowlist.__module__]

#: A public bucket, used for its TEXT only. Nothing here opens a connection to
#: it: declaring a prefix is what puts the sandbox in its remote shape.
REMOTE = "s3://noaa-ghcn-pds/csv/by_year/2024.csv"

#: An https URL on a port nothing listens on. On 2.0.0 the CA refusal lands
#: before the connection is attempted, which is what makes the drift guard
#: below work with no network.
UNREACHABLE = "https://127.0.0.1:1/nothing.csv"

#: The same bucket as a bare prefix, for the allowlist built by hand.
REMOTE_PREFIX = "s3://noaa-ghcn-pds/csv/by_year/"


def _granted_paths(con: Any) -> List[str]:
    """``allowed_paths`` in the form DuckDB reports it back.

    DuckDB resolves each entry, so a grant of ``/etc/ssl/cert.pem`` is read
    back as ``/private/etc/ssl/cert.pem`` on macOS, where ``/etc`` is a link.
    The case fold is the production helper rather than a second spelling of it:
    whether the comparison is case-sensitive is the sandbox's own judgement.
    """
    return [sandbox._fold(os.path.realpath(p)) for p in _setting(con, "allowed_paths")]


def _with_httpfs(allow: DuckDBAllowlist) -> Any:
    """A sandboxed connection with httpfs loaded, or a skip.

    The grant is gated on the ``ca_cert_file`` pin landing, and that setting
    only exists once httpfs is loaded -- so a connection without the extension
    gets no copy at all, deliberately (there is no TLS for it to serve). Every
    test whose SUBJECT is the grant therefore loads httpfs, whether it expects
    one or not, and skips where the extension cannot be installed, as
    ``test_duckdb_sandbox.py`` does. Going through here rather than calling
    ``secure_duckdb_connect`` with ``extensions=["httpfs"]`` is what makes the
    difference between a skip and a red build on an offline runner -- and this
    file is in the ``duckdb-floor`` CI lane, where a download blip would
    otherwise fail it.
    """
    try:
        return secure_duckdb_connect(":memory:", allow=allow, extensions=["httpfs"])
    except duckdb.Error:
        pytest.skip("httpfs is not installable here (offline)")


def _ca_directories() -> List[str]:
    """The CA copy directories currently in $TMPDIR.

    Only meaningful under :func:`_private_tmpdir`. Counting the SHARED system
    temp directory is what the first version of these tests did, and CI runs
    the unit suite with ``pytest -n auto``: every other worker opening a
    sandboxed connection with a remote prefix creates and tears down one of
    these, so the count moved in both directions and the assertions failed
    about one run in six under concurrency (measured by the review).
    """
    root = tempfile.gettempdir()
    return [d for d in os.listdir(root) if d.startswith(sandbox.CA_DIR_PREFIX)]


@pytest.fixture
def _private_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``$TMPDIR`` at this test's own directory.

    ``tempfile.mkdtemp`` honours ``$TMPDIR``, so the copies this test provokes
    are the only ones :func:`_ca_directories` can see. Deterministic under
    xdist, where the shared temp directory is not.
    """
    private = tmp_path / "tmp"
    private.mkdir()
    monkeypatch.setenv("TMPDIR", str(private) + os.sep)
    monkeypatch.setattr("tempfile.tempdir", None)
    return private


def _host_bundle() -> str:
    """The candidate this host actually has, or skip."""
    for candidate in sandbox._CA_BUNDLE_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    pytest.skip("no CA bundle among DuckDB's candidates on this host")


# ── the defect ───────────────────────────────────────────────────────────


def test_a_declared_remote_prefix_grants_one_private_copy_of_the_bundle() -> None:
    """The grant that makes a remote read possible on 2.0.0.

    Exactly one entry; it carries the host store's bytes, and it is NOT the host
    store. Before the fix ``allowed_paths`` was empty here and the read raised
    PermissionException.
    """
    bundle = _host_bundle()

    con = _with_httpfs(DuckDBAllowlist.none().with_locations(REMOTE))
    try:
        granted = _granted_paths(con)
        assert len(granted) == 1, granted
        copy = granted[0]
        assert sandbox.CA_DIR_PREFIX in copy, copy
        assert copy != sandbox._fold(os.path.realpath(bundle))
        with open(copy, "rb") as made, open(bundle, "rb") as host:
            assert made.read() == host.read()
        assert oct(os.stat(copy).st_mode & 0o777) == "0o400"
        assert oct(os.stat(os.path.dirname(copy)).st_mode & 0o777) == "0o700"
    finally:
        con.close()


def test_duckdb_is_pinned_at_the_copy() -> None:
    """``ca_cert_file`` names the copy, so DuckDB cannot fall back to its probe.

    Without the pin DuckDB would walk its own five candidates and open the host
    store, which is no longer granted -- the read would fail with the very
    refusal this fix exists to remove.

    ``ca_cert_file`` is an httpfs setting and does not exist on a connection
    that did not load the extension, which is why the production code tolerates
    the ``SET`` failing and this test loads httpfs (and skips where it cannot be
    installed).
    """
    _host_bundle()

    con = _with_httpfs(DuckDBAllowlist.none().with_locations(REMOTE))
    try:
        # Both sides through the same fold: _granted_paths() case-folds the way
        # the sandbox does, and the raw setting does not.
        pinned = sandbox._fold(os.path.realpath(_setting(con, "ca_cert_file")))
        assert pinned == _granted_paths(con)[0]
    finally:
        con.close()


def test_duckdb_can_read_the_copy_it_was_granted_and_not_the_host_store() -> None:
    """The grant is the file DuckDB opens, and the host store stays unreadable.

    ``ca_cert_file`` is pinned to the copy, so the file granted is the file its
    TLS picks up -- proven by the live reads elsewhere, and here by reading the
    copy as text through the sandbox. The two controls are what the first
    version of this fix got wrong: the host store itself is refused, and so is
    an ungranted sibling of the copy, so the grant is one file and not its
    directory.
    """
    bundle = _host_bundle()

    con = _with_httpfs(DuckDBAllowlist.none().with_locations(REMOTE))
    try:
        copy = _granted_paths(con)[0]
        assert con.execute(f"SELECT count(*) FROM read_text('{copy}')").fetchall()[0][0] == 1
        neighbour = os.path.join(os.path.dirname(copy), "fluid-not-granted.pem")
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT count(*) FROM read_text('{neighbour}')").fetchall()
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT count(*) FROM read_text('{bundle}')").fetchall()
    finally:
        con.close()


def test_the_granted_copy_cannot_be_written_and_the_host_store_is_not_granted() -> None:
    """The reason the grant is a copy at all.

    **A path in ``allowed_paths`` is granted for writing.** Measured on 1.5.6
    and 2.0.0 against the first version of this fix, which granted the host
    store directly: ``COPY (SELECT 'ATTACKER CA') TO '<store>' (FORMAT CSV,
    USE_TMP_FILE false)`` replaced a 333 KB trust store with 14 bytes, while an
    ungranted sibling stayed refused. Only file ownership stopped it, and
    ``cli/scaffold_ci.py`` generates a root ``python:3.12-slim`` job that runs
    ``fluid apply``.

    Two barriers now, and this asserts both: the host store is not in
    ``allowed_paths`` at all, and the copy that is there is mode 0400, so an
    unprivileged write is refused by the OS as well. Running as root defeats
    the second -- which is why the first one matters, and why the copy is
    per-connection and thrown away.
    """
    bundle = _host_bundle()
    with open(bundle, "rb") as host:
        before = host.read()

    con = _with_httpfs(DuckDBAllowlist.none().with_locations(REMOTE))
    try:
        copy = _granted_paths(con)[0]
        # The barrier that does not depend on who we are. Asserted because the
        # COPY below is refused on this host either way: /etc/ssl/cert.pem is
        # root-owned, so granting the store outright (the first version of this
        # fix) still failed to write it as an unprivileged user -- the test
        # would have passed for the wrong reason, and in the root CI container
        # that this threat model is about, it would not have passed at all.
        assert sandbox._fold(os.path.realpath(bundle)) not in _granted_paths(con)
        with pytest.raises(duckdb.Error):
            con.execute(
                f"COPY (SELECT 'ATTACKER CA' AS c) TO '{bundle}' "
                "(FORMAT CSV, USE_TMP_FILE false)"
            )
        if os.geteuid() != 0:
            with pytest.raises(duckdb.Error):
                con.execute(
                    f"COPY (SELECT 'ATTACKER CA' AS c) TO '{copy}' "
                    "(FORMAT CSV, USE_TMP_FILE false)"
                )
        # The store is byte-identical afterwards. Compared to itself rather than
        # to an expected header: macOS's bundle opens with an OpenBSD comment,
        # so a guess at its first line fails on a file that was never touched.
        with open(bundle, "rb") as host:
            assert host.read() == before
    finally:
        con.close()


def test_the_copy_is_removed_once_the_connection_is_gone() -> None:
    """One ~330 KB file per remote connection, not a leak in $TMPDIR.

    The same ``weakref.finalize`` teardown the spill directory uses, with the
    same limits, measured: ``con.close()`` ALONE does not remove it, because the
    finalizer is keyed on the connection object, so a caller holding a closed
    connection (a cached handle, or a traceback pinning the frame) keeps the
    copy until that reference goes. An unclosed connection is cleaned at normal
    interpreter exit; ``os._exit`` or a kill leaves it for ``$TMPDIR``'s reaper,
    exactly as the spill directory documents.
    """
    _host_bundle()

    con = _with_httpfs(DuckDBAllowlist.none().with_locations(REMOTE))
    directory = os.path.dirname(_granted_paths(con)[0])
    assert os.path.isdir(directory)

    con.close()
    del con
    gc.collect()

    assert not os.path.exists(directory), directory


# ── the grant stays narrow ───────────────────────────────────────────────


def test_a_local_read_grants_no_ca_bundle(tmp_path: Path) -> None:
    """No remote prefix declared, so no CA path appears.

    A local read has no TLS to do, and a grant it cannot use is a grant that
    should not be there.
    """
    local = tmp_path / "data.csv"
    local.write_text("a,b\n1,2\n", encoding="utf-8")

    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_locations(str(local)))
    try:
        assert _granted_paths(con) == [sandbox._fold(os.path.realpath(str(local)))]
    finally:
        con.close()


def test_a_declared_file_and_the_ca_copy_both_reach_allowed_paths(tmp_path: Path) -> None:
    """The shape the changed line assembles: ``allow.paths`` plus the copy.

    Every other test here declares one or the other, so a regression in the
    concatenation -- dropping the caller's own declared files and granting only
    the bundle -- would be invisible.

    Membership, not order: DuckDB's read-back order is its own. Measured with
    three files declared ``zzz, aaa, mmm``, it reported ``mmm, aaa, zzz`` --
    neither sorted nor as declared -- so an assertion on position would be
    pinning an implementation detail that happens to hold today.
    """
    _host_bundle()
    declared = tmp_path / "declared.csv"
    declared.write_text("a,b\n1,2\n", encoding="utf-8")
    allow = DuckDBAllowlist.none().with_paths(str(declared)).with_remote(REMOTE_PREFIX)

    con = _with_httpfs(allow)
    try:
        granted = _granted_paths(con)
        assert len(granted) == 2, granted
        assert sandbox._fold(os.path.realpath(str(declared))) in granted, granted
        assert any(sandbox.CA_DIR_PREFIX in entry for entry in granted), granted
    finally:
        con.close()


def test_the_grant_is_one_file_and_not_a_directory() -> None:
    """``allowed_directories`` is untouched, so #757's prefix grant is unaffected.

    A CA *directory* would be a local grant bolted onto a remote declaration,
    which is the shape that made ``s3://etc/`` dangerous in the first place.
    """
    _host_bundle()
    allow = DuckDBAllowlist.none().with_locations(REMOTE)

    con = _with_httpfs(allow)
    try:
        # Each granted path is a FILE. Asserted on the entry itself and not
        # only on ``allowed_directories``: a directory handed to
        # ``allowed_paths`` stays in ``allowed_paths``, so the read-back below
        # cannot see it. Measured -- granting ``dirname(bundle)`` left the
        # directory assertion green.
        for entry in _setting(con, "allowed_paths"):
            assert os.path.isfile(str(entry)), entry
        directories = [str(d) for d in _setting(con, "allowed_directories")]
        # Matched on the bucket, not on ``s3://``: 1.5.x reports the prefix as
        # ``<cwd>/s3:/noaa-ghcn-pds/csv/by_year/`` and 2.0.0 verbatim, which is
        # the difference #757 turns on.
        assert any("noaa-ghcn-pds" in entry for entry in directories), directories
        assert not any("ssl" in entry or "ca-trust" in entry for entry in directories), directories
    finally:
        con.close()


def test_the_allowlist_the_caller_passed_is_not_widened() -> None:
    """The grant is appended at ``SET`` time, not folded into ``allow``.

    The allowlist object is what every other caller reads, so widening it would
    make a declared-inputs list grow a path nobody declared.

    The refusal hint is the one place that must still own up to it: it tells the
    user what contract SQL may read, and with a remote prefix declared that
    includes a copy of the trust store. Saying "only the locations the contract
    declares" there was false, which the defect scan caught. The host store's
    PATH is deliberately not printed -- SQL is refused that file, so naming it
    would point the reader at the wrong one.
    """
    bundle = _host_bundle()
    allow = DuckDBAllowlist.none().with_locations(REMOTE)

    con = _with_httpfs(allow)
    try:
        assert allow.paths == ()
        hint = sandbox_refusal_hint(allow)
        assert "trust store" in hint
        assert bundle not in hint
        # Not "read-only": the copy's mode is a speed bump, not a write bar
        # (COPY TO renames over it), and this sentence is what the user reads.
        assert "read-only" not in hint
    finally:
        con.close()


def test_a_local_refusal_hint_says_nothing_about_a_trust_store() -> None:
    """No remote prefix, no disclosure: it would be noise on every local refusal."""
    allow = DuckDBAllowlist.none().with_dirs(os.getcwd())

    assert "trust store" not in sandbox_refusal_hint(allow)


# ── which candidate, and in whose order ──────────────────────────────────


def test_the_granted_bundle_is_the_first_candidate_that_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probed in DuckDB's order, so the grant names the file DuckDB opens.

    Two candidates present: the earlier one wins. A host that has both
    ``/etc/ssl/certs/ca-certificates.crt`` and ``/etc/ssl/cert.pem`` is
    ordinary on Debian, and granting the later one would leave the read
    refused on the file DuckDB actually reaches for. An absent candidate ahead
    of both is skipped rather than returned.

    The candidate list is injected rather than ``os.path.isfile`` patched:
    ``sandbox.os`` IS the stdlib module, so patching its ``isfile`` replaces
    ``posixpath.isfile`` for pytest and everything else running at the time.
    With real files on disk this exercises the production probe itself.
    """
    earlier = tmp_path / "earlier-ca-certificates.crt"
    earlier.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    later = tmp_path / "later-cert.pem"
    later.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setattr(
        sandbox,
        "_CA_BUNDLE_CANDIDATES",
        (str(tmp_path / "absent.crt"), str(earlier), str(later)),
    )

    allow = DuckDBAllowlist.none().with_locations(REMOTE)
    assert sandbox._ca_bundle_for_remote(allow) == str(earlier)


def test_a_candidate_that_is_not_a_file_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory and a dangling symlink at a candidate path are passed over.

    DuckDB opens a file. Returning a directory would hand ``allowed_paths`` an
    entry that grants nothing and leave the read refused, and a dangling link
    the same. ``os.path.isfile`` follows links and answers False for both --
    pinned here because the fix depends on it.
    """
    directory = tmp_path / "dir-shaped.crt"
    directory.mkdir()
    dangling = tmp_path / "dangling.pem"
    dangling.symlink_to(tmp_path / "gone.pem")
    real = tmp_path / "real-cert.pem"
    real.write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    monkeypatch.setattr(
        sandbox, "_CA_BUNDLE_CANDIDATES", (str(directory), str(dangling), str(real))
    )

    allow = DuckDBAllowlist.none().with_locations(REMOTE)
    assert sandbox._ca_bundle_for_remote(allow) == str(real)


def test_a_connection_without_httpfs_gets_no_grant_and_leaves_nothing(
    _private_tmpdir: Path,
) -> None:
    """No pin, no grant -- and no copy left on disk either.

    ``ca_cert_file`` is an httpfs setting, so on a connection that did not load
    the extension the pin cannot land and DuckDB will never open the copy.
    Granting it anyway would put a writable file in ``allowed_paths`` for a
    benefit that cannot exist. The azure extension takes exactly this path
    (``cli/discover/filesystem.py`` loads ``azure``, not ``httpfs``, for an
    ``azure://`` URL), which is how the review found it.
    """
    before = len(_ca_directories())

    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_locations(REMOTE))
    try:
        assert _granted_paths(con) == []
        assert len(_ca_directories()) == before
    finally:
        con.close()


def test_a_tmpdir_inside_a_granted_directory_gets_no_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A trust store the SQL could replace is worse than no trust store.

    ``tempfile.mkdtemp`` honours ``$TMPDIR``, and a workspace-local ``$TMPDIR``
    is an ordinary CI, direnv or Nix arrangement. Measured there: ``COPY TO``
    with its DEFAULT options writes a temporary file and renames it over the
    target, so the copy's 0400 stops nothing once its directory is writable by
    the SQL -- 333 KB of trust anchors became 14 bytes on 1.5.6 and 2.0.0. The
    grant is dropped instead, loudly, and nothing is left behind.
    """
    _host_bundle()
    granted_dir = tmp_path / "workspace"
    (granted_dir / ".tmp").mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(granted_dir / ".tmp") + os.sep)
    monkeypatch.setattr("tempfile.tempdir", None)
    allow = DuckDBAllowlist.none().with_dirs(str(granted_dir)).with_remote(REMOTE_PREFIX)

    with caplog.at_level("WARNING"):
        con = _with_httpfs(allow)
    try:
        assert _granted_paths(con) == []
        assert not list((granted_dir / ".tmp").glob(sandbox.CA_DIR_PREFIX + "*"))
        assert "duckdb_ca_bundle_grant_skipped" in caplog.text
    finally:
        con.close()


def test_a_failed_copy_leaves_no_directory_behind(
    _private_tmpdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remover is registered by the caller, so the copy has to clean up itself.

    ``mkdtemp`` runs first and the ``weakref.finalize`` only once
    :func:`_private_ca_bundle` has returned, so a failure in between -- ENOSPC,
    an I/O error, or the candidate disappearing between the ``isfile`` probe and
    the copy -- used to leak the directory for the lifetime of ``$TMPDIR``.
    """
    bundle = _host_bundle()
    before = len(_ca_directories())

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sandbox.shutil, "copyfile", _boom)

    with pytest.raises(OSError):
        sandbox._private_ca_bundle(bundle)

    assert len(_ca_directories()) == before


def test_a_cursor_inherits_the_pin(tmp_path: Path) -> None:
    """A plain SET lands in LOCAL scope, so the pin has to be GLOBAL.

    Measured with a session-scoped ``SET``: ``con.cursor()`` reported
    ``ca_cert_file = ''`` and its remote read failed on 2.0.0 with the original
    ``Cannot read a CA certificate bundle`` -- ``allowed_paths`` is GLOBAL so
    the cursor saw the copy, but nothing pointed DuckDB at it, and the
    configuration is locked so no caller could set it either.
    """
    _host_bundle()

    con = _with_httpfs(DuckDBAllowlist.none().with_locations(REMOTE))
    try:
        # NB the setting REPORTS scope GLOBAL in duckdb_settings() until a plain
        # SET puts a local value there, measured on 1.5.1, 1.5.6 and 2.0.0 -- it
        # is SET's own scope resolution that makes the GLOBAL keyword necessary,
        # not a declared LOCAL scope.
        copy = _granted_paths(con)[0]
        cursor = con.cursor()
        pinned = sandbox._fold(os.path.realpath(_setting(cursor, "ca_cert_file")))
        assert pinned == copy
    finally:
        con.close()


def test_a_host_with_no_candidate_grants_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A minimal container or Windows: DuckDB's own refusal is the better message.

    Nothing is invented, and the connection still opens -- the failure, if the
    read is attempted, is DuckDB's, naming the paths it looked for.
    """
    monkeypatch.setattr(sandbox, "_CA_BUNDLE_CANDIDATES", (str(tmp_path / "absent.crt"),))

    allow = DuckDBAllowlist.none().with_locations(REMOTE)
    assert sandbox._ca_bundle_for_remote(allow) is None

    con = secure_duckdb_connect(":memory:", allow=allow)
    try:
        assert _granted_paths(con) == []
    finally:
        con.close()


def test_every_candidate_is_an_absolute_posix_file_path() -> None:
    """A relative entry would be refused by DuckDB and a directory would widen the grant.

    Judged with ``posixpath``, not ``os.path``: these are POSIX paths, and from
    Python 3.13 ``ntpath.isabs`` answers False for a path with a single leading
    slash (it is drive-relative on Windows). Measured: ``ntpath.isabs(
    '/etc/ssl/cert.pem')`` is True on 3.12 and False on 3.14, so ``os.path``
    would fail this test for a contributor on Windows under 3.13 or later --
    about their platform, not about the list.

    The directory check only bites for a candidate that exists; what keeps a
    non-existent one honest is :func:`_ca_bundle_for_remote`'s own
    ``os.path.isfile``, pinned by ``test_a_candidate_that_is_not_a_file_is_skipped``.
    """
    assert sandbox._CA_BUNDLE_CANDIDATES
    for candidate in sandbox._CA_BUNDLE_CANDIDATES:
        assert posixpath.isabs(candidate), candidate
        assert not candidate.endswith("/"), candidate
        assert not os.path.isdir(candidate), candidate


#: What :data:`sandbox._CA_BUNDLE_CANDIDATES` must hold, spelled out here so an
#: edit to it has to be made twice, deliberately.
EXPECTED_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/cert.pem",
)


def test_the_candidate_list_is_pinned_literally() -> None:
    """The one check on the list that runs on every host and every engine.

    ``test_the_candidate_list_matches_the_one_duckdb_names`` is the authority on
    what DuckDB actually probes, but it needs 2.0.0 and httpfs, so it skips on
    the ``duckdb-floor`` lane -- which installs MIN_DUCKDB_VERSION, 1.5.0, the
    very lane this file was added to. The probe tests skip too where the host
    has none of the five. Measured on the first version of this file: narrowing
    the production tuple to one absent path left duckdb 1.5.6 fully green, so
    total drift in OUR copy of the list was invisible everywhere but one lane.
    This assertion has no host or engine dependency at all.
    """
    assert sandbox._CA_BUNDLE_CANDIDATES == EXPECTED_CANDIDATES


def test_the_candidate_list_matches_the_one_duckdb_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The drift guard: upstream's list parsed back out of its own refusal.

    DuckDB names its candidates in the refusal, in probe order, and on 2.0.0
    that refusal arrives before any connection is attempted -- so this needs no
    network. If duckdb-httpfs adds, drops or reorders a path, this fails with
    both lists in the message instead of the fix silently granting a file the
    engine no longer reads.

    Skipped where the refusal does not exist: on 1.5.x curl opens the bundle
    itself, outside the sandbox, so the read fails on the connection instead.
    """
    if duckdb_version(duckdb) < (2, 0, 0):
        pytest.skip("the CA bundle is read outside the sandbox before 2.0")
    monkeypatch.setattr(sandbox, "_ca_bundle_for_remote", lambda _allow: None)

    try:
        con = secure_duckdb_connect(
            ":memory:",
            allow=DuckDBAllowlist.none().with_locations(UNREACHABLE),
            extensions=["httpfs"],
        )
    except duckdb.Error:
        pytest.skip("httpfs is not installable here (offline)")
    try:
        with pytest.raises(duckdb.Error) as excinfo:
            con.execute(f"SELECT * FROM read_csv_auto('{UNREACHABLE}') LIMIT 0")
    finally:
        con.close()

    message = str(excinfo.value)
    if "CA certificate bundle" not in message:
        pytest.skip(f"no CA refusal from this build: {message.splitlines()[0]}")
    named = re.findall(r"/[\w./-]+\.(?:crt|pem)", message)
    assert named == list(sandbox._CA_BUNDLE_CANDIDATES), (named, sandbox._CA_BUNDLE_CANDIDATES)
