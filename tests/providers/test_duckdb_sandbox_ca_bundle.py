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
the call site declared a remote prefix, so a local read gains nothing. The
allowlist object is left alone: the grant is appended at ``SET`` time, so a
refusal hint still speaks only of what the caller asked for.

Everything here is offline. The real network proof is the CLI run in the PR
description; what CI can check without a network is the read-back, and on 2.0.0
the refusal itself, which arrives before any connection is attempted.

Each mechanism was reverted on duckdb 2.0.0 and the failures recorded, not
predicted. ``_ca_bundle_for_remote`` returning ``None`` fails
``test_a_declared_remote_prefix_grants_one_ca_bundle``,
``test_duckdb_can_read_the_bundle_it_was_granted`` and
``test_the_granted_bundle_is_the_first_candidate_that_exists``. Returning
``dirname(bundle)`` fails those three and
``test_the_grant_is_one_file_and_not_a_directory`` -- which it did NOT, until
that test was made to check the granted entry itself: a directory handed to
``allowed_paths`` never reaches ``allowed_directories``, so reading the latter
back could not see it. Dropping the ``allow.remote_prefixes`` guard fails
``test_a_local_read_grants_no_ca_bundle`` alone.
"""

from __future__ import annotations

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


def test_a_declared_remote_prefix_grants_one_ca_bundle() -> None:
    """The grant that makes a remote read possible on 2.0.0.

    Exactly one entry, and it is a bundle DuckDB named: before the fix
    ``allowed_paths`` was empty here and the read raised PermissionException.
    """
    bundle = _host_bundle()

    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_locations(REMOTE))
    try:
        assert _granted_paths(con) == [sandbox._fold(os.path.realpath(bundle))]
    finally:
        con.close()


def test_duckdb_can_read_the_bundle_it_was_granted() -> None:
    """The grant is the file DuckDB opens, not merely a path it tolerates.

    Reading the bundle as text through the sandbox proves the entry is
    effective -- the probe mirrors DuckDB's own order, so the file granted is
    the one its TLS picks up. The read of an UNGRANTED candidate-shaped path is
    the control: the grant is one file, not its directory.
    """
    bundle = _host_bundle()
    neighbour = os.path.join(os.path.dirname(bundle), "fluid-not-granted.pem")

    con = secure_duckdb_connect(":memory:", allow=DuckDBAllowlist.none().with_locations(REMOTE))
    try:
        rows = con.execute(f"SELECT count(*) FROM read_text('{bundle}')").fetchall()
        assert rows[0][0] == 1
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT count(*) FROM read_text('{neighbour}')").fetchall()
    finally:
        con.close()


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

    So a refusal hint lists what the contract declared and not a trust store
    the operator never mentioned.
    """
    _host_bundle()
    allow = DuckDBAllowlist.none().with_locations(REMOTE)

    con = secure_duckdb_connect(":memory:", allow=allow)
    try:
        assert allow.paths == ()
        assert "ssl" not in sandbox_refusal_hint(allow)
    finally:
        con.close()


# ── which candidate, and in whose order ──────────────────────────────────


def test_the_granted_bundle_is_the_first_candidate_that_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probed in DuckDB's order, so the grant names the file DuckDB opens.

    Two candidates present: the earlier one wins. A host that has both
    ``/etc/ssl/certs/ca-certificates.crt`` and ``/etc/ssl/cert.pem`` is
    ordinary on Debian, and granting the later one would leave the read
    refused on the file DuckDB actually reaches for.
    """
    present = {
        sandbox._CA_BUNDLE_CANDIDATES[0],
        sandbox._CA_BUNDLE_CANDIDATES[-1],
    }
    monkeypatch.setattr(sandbox.os.path, "isfile", lambda p: p in present)

    allow = DuckDBAllowlist.none().with_locations(REMOTE)
    assert sandbox._ca_bundle_for_remote(allow) == sandbox._CA_BUNDLE_CANDIDATES[0]


def test_a_host_with_no_candidate_grants_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A minimal container or Windows: DuckDB's own refusal is the better message.

    Nothing is invented, and the connection still opens -- the failure, if the
    read is attempted, is DuckDB's, naming the paths it looked for.
    """
    monkeypatch.setattr(sandbox.os.path, "isfile", lambda _p: False)

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

    con = secure_duckdb_connect(
        ":memory:",
        allow=DuckDBAllowlist.none().with_locations(UNREACHABLE),
        extensions=["httpfs"],
    )
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
