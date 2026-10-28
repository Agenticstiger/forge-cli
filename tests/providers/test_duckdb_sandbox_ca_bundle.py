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

On DuckDB 2.0.0 every remote read failed before a byte moved::

    PermissionException: Cannot read a CA certificate bundle: file system access
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

Every test here but one is offline, and that one
(``test_the_candidate_list_matches_the_one_duckdb_names``) needs httpfs, so it
skips when the extension cannot be installed, as its sibling in
``test_duckdb_sandbox.py`` does. The real network proof is the CLI run in the PR
description; what CI can check without a network is the read-back, and on 2.0.0
the refusal itself, which arrives before any connection is attempted.

Each mechanism was reverted on duckdb 2.0.0 and the failures recorded, not
predicted:

===================================  ====================================
reverted                             tests that fail
===================================  ====================================
the probe returns ``None``           8
the probe returns the directory      9
the remote-prefix guard is dropped   2 (both local-read tests)
the copy is replaced by the store    4
``chmod 0400`` is dropped            2
the ``ca_cert_file`` pin is dropped  1 -- AND a live ``s3://`` read on
                                     2.0.0 fails with the original
                                     ``Cannot read a CA certificate
                                     bundle``, which is what makes the pin
                                     load-bearing rather than belt-and-braces
the cleanup finalize is dropped      1
===================================  ====================================

Two of those were worth the trouble of checking. Returning the bundle's
directory did NOT fail ``test_the_grant_is_one_file_and_not_a_directory`` until
that test was made to check the granted entry itself -- a directory handed to
``allowed_paths`` never reaches ``allowed_directories``, so reading the latter
back could not see it. And granting the host store did NOT fail the write test
until it asserted the store's absence from ``allowed_paths``: the ``COPY`` is
refused on a developer machine either way, because ``/etc/ssl/cert.pem`` is
root-owned. In the root container the threat model is about, it would not have
been refused at all.
"""

from __future__ import annotations

import gc
import os
import re
import sys
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


def _granted_paths(con: Any) -> List[str]:
    """``allowed_paths`` in the form DuckDB reports it back.

    DuckDB resolves each entry, so a grant of ``/etc/ssl/cert.pem`` is read
    back as ``/private/etc/ssl/cert.pem`` on macOS, where ``/etc`` is a link.
    The case fold is the production helper rather than a second spelling of it:
    whether the comparison is case-sensitive is the sandbox's own judgement.
    """
    return [sandbox._fold(os.path.realpath(p)) for p in _setting(con, "allowed_paths")]


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

    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_locations(REMOTE))
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

    try:
        con = secure_duckdb_connect(
            ":memory:",
            allow=DuckDBAllowlist.none().with_locations(REMOTE),
            extensions=["httpfs"],
        )
    except duckdb.Error:
        pytest.skip("httpfs is not installable here (offline)")
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

    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_locations(REMOTE))
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

    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_locations(REMOTE))
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

    The same ``weakref.finalize`` teardown the spill directory uses.
    """
    _host_bundle()

    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_locations(REMOTE))
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


def test_the_grant_is_one_file_and_not_a_directory() -> None:
    """``allowed_directories`` is untouched, so #757's prefix grant is unaffected.

    A CA *directory* would be a local grant bolted onto a remote declaration,
    which is the shape that made ``s3://etc/`` dangerous in the first place.
    """
    _host_bundle()
    allow = DuckDBAllowlist.none().with_locations(REMOTE)

    con = secure_duckdb_connect(":memory:", allow=allow)
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

    con = secure_duckdb_connect(":memory:", allow=allow)
    try:
        assert allow.paths == ()
        hint = sandbox_refusal_hint(allow)
        assert "trust store" in hint
        assert bundle not in hint
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


def test_every_candidate_is_an_absolute_file_path() -> None:
    """A relative entry would be refused by DuckDB and a directory would widen the grant."""
    assert sandbox._CA_BUNDLE_CANDIDATES
    for candidate in sandbox._CA_BUNDLE_CANDIDATES:
        assert os.path.isabs(candidate), candidate
        assert not candidate.endswith(("/", os.sep)), candidate
        assert not os.path.isdir(candidate), candidate


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
