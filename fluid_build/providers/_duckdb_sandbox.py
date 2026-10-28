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

"""The one way the engine opens DuckDB: every connection is sandboxed.

A contract carries SQL (``builds[].properties.sql``, stage SQL, quality
predicates, masking checks) and DuckDB runs it with the privileges of the
process. Unconfined, that SQL reads any file the process can:
``read_csv('/etc/passwd')``, ``read_text('~/.aws/credentials')``, ``glob('/')``,
``ATTACH`` of another database, ``COPY ... TO`` anywhere, an ``https://`` URL.
A service that runs a user's contract would hand that user its host.

:func:`secure_duckdb_connect` applies DuckDB's own sandbox, in the order the
DuckDB docs give ("Securing DuckDB", introduced in 1.2):

1. connect (the database file itself is opened here, before any restriction,
   and needs no grant of its own);
2. ``allow_persistent_secrets = false`` and ``allow_community_extensions =
   false``, then load what the call site needs: its extensions, and anything
   its ``before_lock`` callback sets up (an ``ATTACH`` of a declared source
   database, an object-store secret);
3. ``autoinstall_known_extensions = false`` / ``autoload_known_extensions =
   false``, so a query cannot pull an extension in afterwards;
4. ``temp_directory`` (a directory this helper creates and owns, see below),
   ``home_directory`` (pinned to ``$HOME``, see below), then
   ``allowed_directories`` and ``allowed_paths``: what the call site declares
   it reads or writes, plus -- only when it declared a remote prefix, and only
   when the pin below lands -- a private copy of the host CA bundle DuckDB's TLS
   needs (:func:`_private_ca_bundle`), with ``ca_cert_file`` pinned to that copy
   (``SET GLOBAL``, so a ``con.cursor()`` inherits it);
5. ``enable_external_access = false``;
6. ``lock_configuration = true``, so the SQL cannot ``SET`` any of it back,
   then a read-back that the grants are the ones the call site asked for.

Each setting is a ``SET`` statement on the connection, not the ``config=`` dict:
the dict does not take the list-valued ``allowed_directories``.

What the sandbox can and cannot promise (and why the floor is DuckDB 1.5.0):

* ``allowed_directories`` arrived in DuckDB 1.2. Up to and including 1.4.3 it
  did not resolve ``./..`` before the allowlist check, so ``<dir>/./../x``
  escaped it. Through 1.4.x the check is lexical: a symlink inside an allowed
  directory reads its target anywhere on the host, and a relative path is
  refused outright. 1.5.0 resolves both (each reproduced on 1.4.3 / 1.4.4 and
  refused on 1.5.0; see ``tests/providers/test_duckdb_sandbox.py``).
  :data:`MIN_DUCKDB_VERSION` is therefore 1.5.0, and a connection refuses to
  open on anything older.
* A remote read needs the host's CA bundle, and from DuckDB 2.0 that read goes
  through the sandbox. duckdb/duckdb-httpfs#474 moved it inside DuckDB's file
  system (``CURLOPT_CAINFO_BLOB``, an in-memory store for httplib) where curl
  and OpenSSL used to open it themselves, bypassing ``allowed_paths``
  entirely. So on 1.5.x a remote read works with no CA grant, and on 2.0.0 the
  same read fails before a byte moves with ``Cannot read a CA certificate
  bundle``. Measured, not assumed:
  ``fluid init --discover s3://noaa-ghcn-pds/...`` emitted 8 columns on 1.5.6
  and exited 1 on 2.0.0, measured at ``5f2ec50b`` (and on clean ``7cf5471a``
  when the card was filed).
  What is granted is NOT the host store. A path in ``allowed_paths`` can be
  WRITTEN, so :func:`_ca_bundle_for_remote` only picks which store to copy and
  :func:`_private_ca_bundle` grants a 0400 copy the connection owns, with
  ``ca_cert_file`` pinned to it so DuckDB opens that copy and not its own probe
  result. Granting the store itself was the first version of this and it let
  contract SQL replace the host trust store wherever the process can write it.
  The copy's mode is a speed bump, not the barrier: measured, ``COPY TO`` with
  its default options writes a temporary file and renames it over the target, so
  0400 stops nothing once the containing DIRECTORY is writable by the SQL --
  which is why the grant is dropped outright when the copy would land inside a
  granted directory (``$TMPDIR`` pointed into the workspace, an ordinary CI or
  direnv setup). The barrier that holds is that the host store is never granted;
  the worst an attacker-chosen copy buys is the trust anchors of one connection
  whose remote locations the contract named anyway.
  No verification setting is touched: an expired and a self-signed certificate
  are refused on 1.5.6 and 2.0.0 alike, with and without
  ``enable_server_cert_verification``, which governs the httplib client while
  s3 and https go through curl. 1.5.6 exposes
  ``enable_curl_server_cert_verification`` (default true) and 2.0.0 no longer
  does; duckdb/duckdb-httpfs#378 proposes unifying them and is still open, so
  the behaviour above is what was measured rather than what upstream documents.
  Plain ``http://`` did not need the grant -- duckdb/duckdb-httpfs#499 made a
  failed bundle load non-fatal there, and 2.0.0 carries it -- but it gets one
  anyway, because the declared prefix is what the grant keys on and an http base
  URL that redirects to https still verifies (which is #499's own reason for
  loading the bundle on that path).
* DuckDB folds its ``temp_directory`` into ``allowed_directories``, and its
  default is relative: ``.tmp`` in the working directory, or ``<db>.tmp``
  beside a file database. Both sit where the contract's repository can place a
  symlink, so the engine creates and owns the spill directory instead
  (:func:`_own_spill_dir`), and :func:`_refuse_ungranted_dirs` reads the
  grants back under the lock to check that is all DuckDB took.
* duckdb/duckdb#26064 (open): ``~`` is expanded with ``$HOME`` by the check and
  with the ``home_directory`` setting by the open, so once the two differ they
  name different files and the check passes for one while the other is read.
  ``home_directory`` is therefore pinned to ``$HOME`` itself and locked: both
  expansions agree, and ``~/x`` is readable exactly when ``$HOME/x`` is
  allowed. A ``~`` in an allowlist entry is refused outright.
* DuckDB 1.5 does not resolve ``..`` inside a remote URL, so a remote prefix
  (``s3://bucket/data/``) bounds the bucket or host, not the path within it.
  Remote prefixes are granted only for a location the contract declares.
* A location the contract DECLARES (an input, an output, a source URI) is
  granted to its SQL, so a declaration is itself a request for access.
  :meth:`DuckDBAllowlist.with_declared` grants one only inside the roots its
  call site names (the contract's directory, its workspace, the run's scratch
  directory) or a directory the operator lists in ``FLUID_DUCKDB_ALLOWED_DIRS``
  (:data:`OPERATOR_DIRS_ENV`); a contract field cannot widen that. Both sides
  are compared after ``realpath``, because DuckDB realpaths allowlist entries
  too: an in-repo symlink to ``/`` would otherwise grant the host. A glob is
  granted as the directory above its first wildcard, which must itself resolve
  inside those roots: the contract could declare any file there anyway, and
  DuckDB expands a glob to files a grant-time listing would miss (dotfiles,
  files that land after the grant), then checks each one's realpath against
  the directory, so a matched symlink that leads out is refused at read time.
* ``COPY ... TO`` a local file that already exists writes ``tmp_<name>``
  beside it and renames that into place (DuckDB's ``USE_TMP_FILE``), so a
  call site that grants the file it writes as a single path also grants
  :func:`copy_tmp_path`, confined as the file is. Without it only the first
  write lands, unless a granted directory happens to hold both.
* A directory the engine grants by convention rather than by declaration
  (the local provider's ``./runtime``) sits in a working directory the
  contract's repository may supply, so it may be a symlink to ``$HOME``.
  :func:`unaliased_dir` grants it only where its name says it is, or inside
  the call site's roots.
* The contract's own directory is granted whole, and a contract can be saved
  anywhere: :func:`refuse_broad_root` refuses to open a connection for one
  that is ``$HOME``, a home directory, a system temp directory or ``/``
  (:func:`broad_root_reason`), unless the operator lists it in
  ``FLUID_DUCKDB_ALLOWED_DIRS``.
* The FLUID workspace a contract sits in (the nearest ancestor holding
  ``fluid.workspace.yaml``) is granted so that products can read their
  siblings' files, but that file can sit anywhere above the contract.
  :func:`workspace_grant` does not grant a workspace root that is one of those
  broad directories, nor one above the version-control repository the
  contract is in (``~/src`` holding several clones): the call site then runs
  without it. A workspace with no repository between it and the contract is
  one trust domain: every directory in it may read every other.
* Autoloading is off, so a function from an extension the call site did not
  load (``sqlite_scan``, ``read_xlsx``, ``ST_Read``, ``delta_scan``,
  ``iceberg_scan``) is not in the catalog. :func:`is_sandbox_refusal` treats
  that error as a refusal.
* A database scanner the call site loads widens what SQL on that connection
  reaches, and how far depends on the DuckDB version (each case replayed on
  1.5.0, 1.5.6 and 2.0.0). Through 1.5, once ``postgres`` is loaded,
  ``postgres_scan(dsn, ...)`` connects to any host the process can reach, and
  once ``sqlite`` is loaded, ``sqlite_scan`` / ``ATTACH ... (TYPE sqlite)`` open
  any SQLite file it can read, whatever the allowlist says. DuckDB 2.0 has no
  ``postgres_scan``, and refuses a SQLite file outside the allowlist as it
  refuses any other read. On both, an ``ATTACH`` of a postgres or mysql
  database after the lock is refused, so ``postgres_query`` / ``mysql_query``
  reach only what the engine attached before it. Only the engine's own SQL runs
  on a connection that loads a scanner; contract SQL never does.
* DuckDB shares one database instance per file per process, and the lock is
  instance-wide: a second connection to a file database that is already open
  cannot be sandboxed, and is refused with :class:`DuckDBSandboxError`. Each
  call site closes its connection before the next opens the same file, and
  :func:`_open_lock` serialises the open so that refusal is deterministic
  rather than the outcome of a race.
* ``lock_configuration`` does not hold on DuckDB 1.5.x for the logging
  settings reached as a TABLE FUNCTION. ``SET enable_logging = true`` is
  refused as it should be, but ``SELECT * FROM enable_logging(...)`` is
  accepted, and the sandbox's own path refusal is then thrown from DuckDB's
  logger write path as an uncaught C++ exception that aborts the process
  (SIGABRT; reproduced on 1.5.1 and 1.5.6, refused cleanly on 2.0.0). So on
  1.5.x one statement can end the process rather than raise something
  :func:`is_sandbox_refusal` can classify. It widens no grant -- the failure is
  availability, not confidentiality -- and the engine's own SQL never spells
  it; the exposure is a free-form SQL surface, such as the MCP output port's
  ``query_sql`` under ``policy.allow_free_form_sql``, whose SELECT-only gate a
  table function passes. Denying those functions belongs in that gate, not
  here: no connection setting can take back what the lock does not hold.
* The DuckDB docs call these settings defense-in-depth, "not a substitute for
  proper sandboxing": a multi-tenant host still runs each contract in its own
  container.

References:
    https://duckdb.org/docs/current/operations_manual/securing_duckdb/overview.html
    https://github.com/duckdb/duckdb/issues/26064
    https://github.com/bordumb/dataing/pull/176 (the 1.4.3 ``./..`` escape)
    https://github.com/duckdb/duckdb/blob/v1.4.4/src/main/config.cpp
        (``DBConfig::CanAccessFile``: the 1.4 check is lexical)
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Tuple,
    Union,
)

from fluid_build._errors import FluidUserError, doc_url

from ._duckdb_compat import storage_compat_config
from ._sql_safety import quote_ansi_string_literal, validate_ident

_log = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only
    import duckdb

PathLike = Union[str, "os.PathLike[str]"]

#: Oldest DuckDB whose ``allowed_directories`` resolves ``./..`` and symlinks.
MIN_DUCKDB_VERSION: Tuple[int, int, int] = (1, 5, 0)

#: Operator-only widening: directories (``os.pathsep``-separated) where a
#: contract may also declare inputs and outputs. An environment variable, not a
#: contract field, so the author of the contract cannot set it.
OPERATOR_DIRS_ENV = "FLUID_DUCKDB_ALLOWED_DIRS"

_GLOB_CHARS = frozenset("*?[")
_REMOTE_RE = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<host>[^/?#]+)(?P<path>/[^?#]*)?$")
_REMOTE_SCHEMES = frozenset(
    {"s3", "s3a", "s3n", "gs", "gcs", "r2", "azure", "az", "abfss", "http", "https", "hf"}
)


@dataclass
class DuckDBSandboxError(FluidUserError):
    """A DuckDB connection could not be opened inside the sandbox."""

    code: str = "DuckDBSandboxError"


def _sandbox_error(what: str, why: str, fix: str) -> DuckDBSandboxError:
    return DuckDBSandboxError(what=what, why=why, fix=fix, doc=doc_url())


def is_remote_location(location: str) -> bool:
    """Whether ``location`` is a URL DuckDB reads through an extension."""
    match = _REMOTE_RE.match(str(location))
    return bool(match and match.group("scheme").lower() in _REMOTE_SCHEMES)


def _first_glob(location: str, start: int = 0) -> Optional[int]:
    return next(
        (i for i in range(start, len(location)) if location[i] in _GLOB_CHARS),
        None,
    )


def _static_prefix(location: str) -> Tuple[str, bool]:
    """``location`` cut before its first glob segment, and whether it had one.

    Either separator ends a segment, so a Windows path globs as a POSIX one.
    """
    first = _first_glob(location)
    if first is None:
        return location, False
    cut = max(location.rfind("/", 0, first), location.rfind("\\", 0, first))
    if cut < 0:
        return ".", True  # a relative glob in the working directory
    return location[:cut] or location[: cut + 1], True


@dataclass(frozen=True)
class DuckDBAllowlist:
    """What one DuckDB connection may read and write, and nothing else.

    ``dirs`` are directories (everything below them), ``paths`` single files,
    ``remote_prefixes`` URL prefixes such as ``s3://bucket/landing/``. Build one
    with :meth:`none` and the ``with_*`` methods; every call site names its own.
    """

    dirs: Tuple[str, ...] = ()
    paths: Tuple[str, ...] = ()
    remote_prefixes: Tuple[str, ...] = ()

    @classmethod
    def none(cls) -> "DuckDBAllowlist":
        """No file or network access at all: in-memory work only."""
        return cls()

    def with_dirs(self, *dirs: Optional[PathLike]) -> "DuckDBAllowlist":
        """Also allow everything under each directory in ``dirs`` (``None`` skipped)."""
        added = _new(self.dirs, (d for d in dirs if d is not None and str(d) != ""), _local)
        return DuckDBAllowlist(self.dirs + added, self.paths, self.remote_prefixes)

    def with_paths(self, *paths: Optional[PathLike]) -> "DuckDBAllowlist":
        """Also allow each single file in ``paths`` (``None`` skipped)."""
        added = _new(self.paths, (p for p in paths if p is not None and str(p) != ""), _local)
        return DuckDBAllowlist(self.dirs, self.paths + added, self.remote_prefixes)

    def with_remote(self, *prefixes: Optional[str]) -> "DuckDBAllowlist":
        """Also allow each URL prefix (``s3://bucket/``, ``https://host/data/``)."""
        added = _new(self.remote_prefixes, (p for p in prefixes if p), _remote)
        return DuckDBAllowlist(self.dirs, self.paths, self.remote_prefixes + added)

    def with_locations(
        self, *locations: Optional[PathLike], base: Optional[PathLike] = None
    ) -> "DuckDBAllowlist":
        """Also allow each location a contract DECLARES it reads or writes.

        For a location the operator names (a ``fluid discover`` argument); a
        location a CONTRACT declares goes through :meth:`with_declared`, which
        also confines it. A URL grants its directory prefix (cut before any
        glob); a local glob grants the directory above its first wildcard; an
        existing directory grants itself; anything else grants that one file.
        A leading ``~`` is expanded as DuckDB expands it, and a relative local
        path is resolved against ``base`` (else the working directory, where
        DuckDB would resolve it).
        """
        out = self
        for location in locations:
            if location is None or str(location) == "":
                continue
            raw = os.fspath(location)
            if is_remote_location(raw):
                prefix, globbed = _static_prefix(raw)
                match = _REMOTE_RE.match(prefix)
                if match and not (match.group("path") or "").strip("/"):
                    # a bare bucket or host: the whole of it
                    prefix = f"{match.group('scheme')}://{match.group('host')}/"
                elif globbed:
                    prefix = prefix.rstrip("/") + "/"
                elif not prefix.endswith("/"):
                    prefix = prefix.rsplit("/", 1)[0] + "/"
                out = out.with_remote(prefix)
                continue
            out = out._with_local(_absolute(raw, base), roots=None, declared=raw)
        return out

    def with_declared(
        self,
        *locations: Optional[PathLike],
        within: Iterable[Optional[PathLike]],
        base: Optional[PathLike] = None,
    ) -> "DuckDBAllowlist":
        """Also allow each location a CONTRACT declares, inside ``within`` only.

        A declared input or output is granted to the contract's SQL, so the
        declaration must not be a way to read the host: each local location
        must resolve (``realpath``, as DuckDB resolves allowlist entries) inside
        one of ``within`` (the call site's own roots, such as the contract's
        directory and its workspace) or a directory the operator lists in
        ``FLUID_DUCKDB_ALLOWED_DIRS``; anything else raises
        :class:`DuckDBSandboxError`. A glob is granted as the directory above
        its first wildcard, which must resolve inside those roots: everything
        in it is declarable already, and DuckDB refuses a matched symlink that
        leads out of it. A relative path is resolved against ``base``, else
        the working directory, which is where DuckDB opens it.
        A URL is granted as :meth:`with_locations` grants it.
        """
        roots = _confinement_roots(within)
        out = self
        for location in locations:
            if location is None or str(location) == "":
                continue
            raw = os.fspath(location)
            if is_remote_location(raw):
                out = out.with_locations(raw)
                continue
            out = out._with_local(_absolute(raw, base), roots=roots, declared=raw)
        return out

    def _with_local(
        self, path: str, *, roots: Optional[Tuple[str, ...]], declared: str
    ) -> "DuckDBAllowlist":
        """Grant one absolute local ``path``; with ``roots``, only inside them."""
        prefix, globbed = _local_glob_prefix(path)
        if roots is not None:
            _confine(prefix, roots, declared)
        if globbed or Path(prefix).is_dir():
            # A glob grants the directory above its first wildcard, not the
            # files a listing finds now: DuckDB's own expansion includes
            # dotfiles (``._x.csv``) and files that land after the grant, and
            # checks every one, so a grant of only today's matches fails the
            # whole read. With ``roots``, that directory is confined (above),
            # and DuckDB refuses a file in it whose realpath leads out.
            return self.with_dirs(prefix)
        return self.with_paths(prefix)


def _local_glob_prefix(path: str) -> Tuple[str, bool]:
    """Absolute local ``path`` cut before its first glob segment, and whether it had one.

    A directory that exists under its literal name is a directory, not a
    pattern: ``Proj [old]`` in a contract's directory, the working directory
    or ``$HOME`` is where the contract lives, and the callers join it in
    before this sees the path. Cut there, ``customers.csv`` would be confined
    (and refused) as the directory above the project. DuckDB matches such a
    component as a pattern first and opens it literally when nothing matches;
    a sibling the pattern does match (``Proj o``) is not granted, so that read
    is refused rather than widened. The last component stays a pattern even
    when a file has that literal name: DuckDB reads its matches (``a1.csv``
    for ``a[1].csv``), and the directory granted holds both.
    """
    start = 0
    while True:
        first = _first_glob(path, start)
        if first is None:
            return path, False
        end = min(
            (i for i in (path.find("/", first), path.find("\\", first)) if i >= 0),
            default=-1,
        )
        if end < 0 or not os.path.lexists(path[:end]):
            cut = max(path.rfind("/", 0, first), path.rfind("\\", 0, first))
            if cut < 0:
                return ".", True
            return path[:cut] or path[: cut + 1], True
        start = end


def _absolute(raw: str, base: Optional[PathLike]) -> str:
    """``raw`` with ``~`` expanded as DuckDB expands it, made absolute at ``base``."""
    # '~' as DuckDB itself would expand it (home_directory is $HOME).
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(base if base is not None else os.getcwd()) / path
    # Lexically normalised first, so '<root>/*/../../etc/x' is confined as the
    # '/etc/x' it names rather than as the '<root>' its glob prefix suggests.
    return os.path.normpath(str(path))


def _is_within(real: str, root: str) -> bool:
    return real == root or real.startswith(_with_sep(root))


def operator_allowed_dirs() -> Tuple[str, ...]:
    """The directories ``FLUID_DUCKDB_ALLOWED_DIRS`` adds, each ``realpath``-ed.

    Set by whoever runs the engine (``FLUID_DUCKDB_ALLOWED_DIRS=/shared/ref``),
    never by a contract. A relative entry, or one that resolves to the
    filesystem root, is refused rather than guessed at.
    """
    out: list = []
    for part in os.environ.get(OPERATOR_DIRS_ENV, "").split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        expanded = os.path.expanduser(part)
        if not os.path.isabs(expanded):
            raise _sandbox_error(
                what=f"{OPERATOR_DIRS_ENV} entry {part!r} is not an absolute path",
                why="A relative entry would mean a different directory in every working directory.",
                fix=f"Set {OPERATOR_DIRS_ENV} to absolute directories, separated by {os.pathsep!r}.",
            )
        real = os.path.realpath(expanded)
        if real == os.path.realpath(os.sep):
            raise _sandbox_error(
                what=f"{OPERATOR_DIRS_ENV} entry {part!r} is the filesystem root",
                why="Allowing '/' lets a contract declare, and so read, every file.",
                fix="List the directories contracts may read and write, not '/'.",
            )
        if real not in out:
            out.append(real)
    return tuple(out)


def _confinement_roots(within: Iterable[Optional[PathLike]]) -> Tuple[str, ...]:
    roots: list = []
    for root in [*within, *operator_allowed_dirs()]:
        if root is None or str(root) == "":
            continue
        real = os.path.realpath(os.path.expanduser(os.fspath(root)))
        if real == os.path.realpath(os.sep):
            raise _sandbox_error(
                what="A declared-location root is the filesystem root",
                why="Confining declarations to '/' confines nothing.",
                fix="Confine declarations to the contract's directory and workspace.",
            )
        if real not in roots:
            roots.append(real)
    return tuple(roots)


def _confine(candidate: str, roots: Tuple[str, ...], declared: str) -> None:
    """Refuse ``candidate`` (from the declared ``declared``) outside every root."""
    real = os.path.realpath(candidate)
    if any(_is_within(real, root) for root in roots):
        return
    where = ", ".join(roots) if roots else "none"
    raise _sandbox_error(
        what=(
            f"The contract declares {declared!r} ({real}), outside the directories it may "
            f"read and write ({where}). The operator can allow a directory with "
            f"{OPERATOR_DIRS_ENV}."
        ),
        why=(
            "A declared input, output or source is granted to the contract's SQL. "
            "Granted anywhere, a contract could read any file on this host (credentials, "
            "/proc, another tenant's data) just by declaring it."
        ),
        fix=(
            "Move the file under the contract's directory or its workspace, or, as the "
            f"operator, list its directory in {OPERATOR_DIRS_ENV} "
            f"(separated by {os.pathsep!r})."
        ),
    )


def confine_declared(
    location: PathLike,
    *,
    within: Iterable[Optional[PathLike]],
    base: Optional[PathLike] = None,
) -> str:
    """The ``realpath`` of a declared local ``location``, refused outside ``within``.

    For a location the engine opens itself rather than grants to the SQL, such
    as a SQLite source it ``ATTACH``es before the lock: nothing bounds that open
    (and through DuckDB 1.5 the sqlite scanner's own client library is never
    bounded), so this check is the only one. Confined as
    :meth:`DuckDBAllowlist.with_declared` confines (``within`` plus
    ``FLUID_DUCKDB_ALLOWED_DIRS``); a relative ``location`` is resolved against
    ``base``, else the working directory. Open the returned path, the one that
    was checked.
    """
    raw = os.fspath(location)
    absolute = _absolute(raw, base)
    _confine(absolute, _confinement_roots(within), raw)
    return os.path.realpath(absolute)


def copy_tmp_path(path: PathLike) -> str:
    """The file DuckDB's ``COPY ... TO path`` writes first when ``path`` exists.

    ``tmp_<name>`` in the same directory, renamed over ``path`` once written
    (``plan_copy_to_file.cpp``, DuckDB 1.5). DuckDB does this for a local file
    that exists; never for a URL, ``PARTITION_BY`` or ``PER_THREAD_OUTPUT``.
    """
    head, name = os.path.split(os.fspath(path))
    return os.path.join(head, "tmp_" + name)


def unaliased_dir(path: PathLike, *, within: Iterable[Optional[PathLike]] = ()) -> Optional[str]:
    """``path`` as an absolute directory to grant, or ``None`` if it is an alias.

    For a directory the engine grants by convention, not because the contract
    declared it: the local provider's ``./runtime``. It is resolved in the
    working directory, which is usually the contract's own, so the contract's
    repository can ship ``runtime`` as a symlink (``runtime -> ../../..``) and,
    because DuckDB realpaths allowlist entries, have ``$HOME`` granted. Returned
    only when its realpath is where its name says it is (the realpath of its
    parent, joined with its name) or inside one of ``within``; otherwise
    ``None``, and the caller grants nothing for it.
    """
    lexical = os.path.normpath(os.path.abspath(os.fspath(path)))
    real = os.path.realpath(lexical)
    parent, name = os.path.split(lexical)
    if real == os.path.join(os.path.realpath(parent), name):
        return lexical
    for root in within:
        if root is None or str(root) == "":
            continue
        real_root = os.path.realpath(os.path.expanduser(os.fspath(root)))
        if real_root != os.path.realpath(os.sep) and _is_within(real, real_root):
            return lexical
    return None


#: The default file systems on macOS (APFS, HFS+) and Windows (NTFS) ignore
#: case, so ``/Users/Me`` names ``/users/me`` there; elsewhere it does not.
_CASE_INSENSITIVE_FS = sys.platform == "darwin" or sys.platform.startswith("win")

#: Where every user's scratch files live. ``/private/...`` is what macOS
#: resolves ``/tmp`` and ``/var`` to; ``/private/var/folders`` holds each
#: user's ``$TMPDIR``. :func:`tempfile.gettempdir` adds this process's own.
#: These paths are only compared against, to refuse granting them; nothing is
#: ever created or written under them, so bandit's B108 does not apply.
_SYSTEM_TEMP_DIRS = (
    "/tmp",  # nosec B108
    "/var/tmp",  # nosec B108
    "/private/tmp",
    "/private/var/tmp",
    "/private/var/folders",
    # Linux shared memory: world-writable, like /tmp.
    "/dev/shm",  # nosec B108
    "/run/shm",
)

#: Where accounts' home directories live (Linux, macOS, Fedora Atomic). One of
#: them, or the directory itself, holds other users' files, whoever runs fluid
#: (``root`` has ``HOME=/root``, so ``/home/alice`` is not "your" home).
_HOMES_PARENTS = ("/home", "/Users", "/var/home")

#: The system temp directories that issue each user a nested ``$TMPDIR``
#: (macOS: ``/private/var/folders/<a>/<b>/T``). A directory between one of
#: these and :func:`tempfile.gettempdir` is that user's scratch space too.
_PER_USER_TEMP_PARENTS = ("/private/var/folders",)

#: :func:`broad_root_reason` codes, as a reader would say them.
BROAD_ROOT_REASONS = {
    "filesystem_root": "the filesystem root",
    "home": "your home directory",
    "home_ancestor": "a directory that holds your home directory",
    "temp_root": "a system temp directory, or a directory that holds one",
    "user_home": "an account's home directory, or the directory that holds them",
    # A workspace_grant code, not a broad_root_reason one.
    "outside_repository": "above the version-control repository the contract is in",
}

#: What marks a directory as the root of a version-control checkout. ``.git``
#: is a file in a worktree or a submodule, so either kind counts.
_VCS_MARKERS = (".git", ".hg")


def _fold(real: str) -> str:
    return real.casefold() if _CASE_INSENSITIVE_FS else real


def _home_dirs() -> Tuple[str, ...]:
    """``$HOME`` (as DuckDB's ``~`` reads it) and the account's own home, ``realpath``-ed."""
    homes = [os.path.expanduser("~")]
    try:
        import pwd

        homes.append(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):  # Windows, or a uid with no entry
        pass
    return tuple({os.path.realpath(h) for h in homes if h and os.path.isabs(h)})


def broad_root_reason(path: PathLike) -> Optional[str]:
    """Why granting the directory ``path`` whole would grant the host, else ``None``.

    ``"filesystem_root"``; ``"home"`` for ``$HOME`` and ``"home_ancestor"``
    for a directory that holds it (``/Users``, ``/home``); ``"temp_root"``
    for a system temp directory (``/tmp``, ``/var/tmp``, ``$TMPDIR``, macOS's
    ``/private/var/folders``, ``/dev/shm``) or a directory that holds one;
    ``"user_home"`` for another account's home (``/home/alice`` when root
    runs fluid) or the directory that holds them (``_HOMES_PARENTS``). Every other
    directory, a project below ``$HOME`` or ``/tmp`` included, is ``None``.
    ``$TMPDIR`` counts as held only when the system issued it (below
    ``_PER_USER_TEMP_PARENTS``): one pointed into a project
    (``TMPDIR=~/src/lab/.tmp``) is refused itself, but leaves its project a
    project. Compared after ``realpath`` (DuckDB resolves symlinks in its
    allowlist the same way), and without case where the file system ignores it.
    """
    real = os.path.realpath(os.path.expanduser(os.fspath(path)))
    if os.path.dirname(real) == real:  # '/', or a Windows drive or share root
        return "filesystem_root"
    folded = _fold(real)
    own_temp = _fold(os.path.realpath(tempfile.gettempdir()))
    system_temps = [_fold(os.path.realpath(t)) for t in _SYSTEM_TEMP_DIRS]
    if folded == own_temp or folded in system_temps:  # even when $HOME is below it
        return "temp_root"
    for home in _home_dirs():
        if _is_within(_fold(home), folded):
            return "home" if _fold(home) == folded else "home_ancestor"
    for parent in _HOMES_PARENTS:
        homes = _fold(os.path.realpath(parent))
        if folded == homes or os.path.dirname(folded) == homes:
            return "user_home"
    held = list(system_temps)
    per_user = (_fold(os.path.realpath(p)) for p in _PER_USER_TEMP_PARENTS)
    if any(_is_within(own_temp, parent) for parent in per_user):
        held.append(own_temp)
    if any(_is_within(temp, folded) for temp in held):
        return "temp_root"
    return None


def refuse_broad_root(path: PathLike) -> None:
    """Raise :class:`DuckDBSandboxError` when the contract directory ``path`` is the host.

    A call site grants the contract's own directory whole, and a contract can
    be saved anywhere: one saved straight into ``$HOME`` or ``/tmp`` would have
    its SQL read every file there (``~/.aws/credentials``, another user's
    scratch files). Refused rather than narrowed, because nothing tells the
    contract's own data apart from the rest of that directory. A directory the
    operator lists in ``FLUID_DUCKDB_ALLOWED_DIRS`` (or one below such an
    entry) is the operator's decision, and passes.
    """
    reason = broad_root_reason(path)
    if reason is None:
        return
    real = os.path.realpath(os.path.expanduser(os.fspath(path)))
    if any(_is_within(_fold(real), _fold(d)) for d in operator_allowed_dirs()):
        return
    raise _sandbox_error(
        what=(
            f"The contract's directory {real} is {BROAD_ROOT_REASONS.get(reason, reason)}, "
            "so its SQL is not run there. Move the contract into a directory of its own; "
            f"the operator can allow this one with {OPERATOR_DIRS_ENV}."
        ),
        why=(
            "The contract's directory is opened whole to its SQL. Here that would be every "
            "file below it: credentials, other projects, other users' files."
        ),
        fix=(
            "Move the contract into a directory of its own (for example "
            "~/my-product/contract.fluid.yaml) and run it from there, or, as the operator, "
            f"list this directory in {OPERATOR_DIRS_ENV} to allow it anyway."
        ),
    )


class WorkspaceGrant(NamedTuple):
    """The FLUID workspace above a contract, and whether its sandbox may have it."""

    #: The directory holding the nearest ``fluid.workspace.yaml``, or ``None``.
    found: Optional[Path]
    #: Why ``found`` is not granted, or ``None``: a :func:`broad_root_reason`
    #: code, or ``"outside_repository"``.
    refused: Optional[str]

    @property
    def granted(self) -> Optional[Path]:
        """The workspace root to grant the contract's SQL, or ``None``."""
        return None if self.refused else self.found

    def not_granted_message(self) -> str:
        """The ``local_workspace_not_granted`` warning: what was dropped, why, and the fix."""
        why = BROAD_ROOT_REASONS.get(self.refused or "", self.refused)
        return (
            f"local_workspace_not_granted: {self.found} holds fluid.workspace.yaml, but it is "
            f"{why}, so its files are not opened to contract SQL; the build runs with the "
            "contract's own directory. Move fluid.workspace.yaml into the directory that holds "
            "your data products, or list the directories contracts may declare files in with "
            f"{OPERATOR_DIRS_ENV} in the shell that runs fluid."
        )


def workspace_grant(start: Optional[PathLike]) -> WorkspaceGrant:
    """The FLUID workspace above ``start`` (a contract's directory), vetted for the sandbox.

    A workspace root is granted to contract SQL so a product can read its
    siblings' files, and ``fluid.workspace.yaml`` may sit at any ancestor
    (``fluid init`` writes it where it runs). One at ``$HOME``, above it, at a
    system temp directory or at ``/`` would open that whole tree, so it is
    found but not granted (:attr:`WorkspaceGrant.refused`): the call site runs
    without it and logs :meth:`WorkspaceGrant.not_granted_message`. So is one
    above the version-control repository the contract is in
    (``"outside_repository"``): a ``fluid.workspace.yaml`` in ``~/src`` would
    otherwise let a repository cloned there read every other clone, so the
    walk stops at the contract's own checkout. Rather than
    fail the build: the contract's own directory is still granted, so a
    single-product build is unaffected. The same posture as git's answer to
    CVE-2022-24765, which stops repository discovery from trusting a ``.git``
    in a shared ancestor directory. Workspace defaults (``find_workspace_root``
    for the loader and the CLI) are unchanged; only file access is narrowed.
    """
    if start is None:
        return WorkspaceGrant(None, None)
    # Through the module, so a test can stand in for the search.
    from fluid_build.util import workspace_root

    found = workspace_root.find_workspace_root(Path(start))
    if found is None:
        return WorkspaceGrant(None, None)
    reason = broad_root_reason(found)
    if reason is None and _repository_below(found, Path(start)) is not None:
        reason = "outside_repository"
    return WorkspaceGrant(found, reason)


def _repository_below(found: Path, start: Path) -> Optional[Path]:
    """The nearest checkout root from ``start`` up to, not including, ``found``."""
    current = start.resolve()
    stop = found.resolve()
    for directory in [current, *current.parents]:
        if directory == stop:
            return None
        if any(os.path.lexists(directory / marker) for marker in _VCS_MARKERS):
            return directory
    return None


def _new(
    existing: Tuple[str, ...],
    candidates: Iterable[Any],
    normalize: Callable[[Any], str] = str,
) -> Tuple[str, ...]:
    """``candidates``, each ``normalize``-d, not already in ``existing``; in order, each once.

    Linear: a glob or a long declaration list must not make the allowlist
    quadratic. A candidate already granted verbatim is not normalised again.
    """
    seen = set(existing)
    out: list = []
    for raw in candidates:
        if isinstance(raw, str) and raw in seen:
            continue
        item = normalize(raw)
        if item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def _local(value: PathLike) -> str:
    raw = os.fspath(value)
    if raw.startswith("~"):
        # duckdb/duckdb#26064: '~' is expanded inconsistently by DuckDB itself.
        raise _sandbox_error(
            what=f"DuckDB allowlist entry {raw!r} starts with '~'",
            why=(
                "DuckDB expands '~' differently when it checks a path and when it opens "
                "it (duckdb/duckdb#26064), so a '~' entry does not bound what is read."
            ),
            fix="Pass an absolute path.",
        )
    if is_remote_location(raw) or "://" in raw:
        raise _sandbox_error(
            what=f"DuckDB allowlist entry {raw!r} is a URL, not a local path",
            why="Local directories and remote prefixes are granted separately.",
            fix="Grant it with DuckDBAllowlist.with_remote (or with_locations).",
        )
    resolved = os.path.normpath(os.path.abspath(raw))
    # DuckDB realpaths each entry, so a symlink to '/' is the root too.
    if resolved == os.path.abspath(os.sep) or os.path.realpath(resolved) == os.path.realpath(
        os.sep
    ):
        raise _sandbox_error(
            what="DuckDB allowlist entry is the filesystem root",
            why="Allowing '/' allows every file, which is no sandbox at all.",
            fix="Grant the directory the run actually reads or writes.",
        )
    return resolved


def _remote(prefix: str) -> str:
    match = _REMOTE_RE.match(prefix)
    if not match or match.group("scheme").lower() not in _REMOTE_SCHEMES:
        raise _sandbox_error(
            what=f"DuckDB remote prefix {prefix!r} is not a URL DuckDB reads",
            why=f"Remote prefixes must use one of: {', '.join(sorted(_REMOTE_SCHEMES))}.",
            fix="Grant a local path with with_dirs / with_paths instead.",
        )
    path = match.group("path") or "/"
    if ".." in path.split("/"):
        raise _sandbox_error(
            what=f"DuckDB remote prefix {prefix!r} contains '..'",
            why="DuckDB does not resolve '..' in a URL, so the prefix would not bound it.",
            fix="Name the prefix without '..'.",
        )
    if not prefix.endswith("/"):
        prefix += "/"
    return prefix


def duckdb_version(module: Any) -> Tuple[int, int, int]:
    """``module.__version__`` as ``(major, minor, patch)``; raises when unreadable."""
    raw = getattr(module, "__version__", None)
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", raw) if isinstance(raw, str) else None
    if not match:
        raise _sandbox_error(
            what="Cannot tell which DuckDB is installed",
            why=f"duckdb.__version__ is {raw!r}; the sandbox needs a known version.",
            fix="Reinstall DuckDB: pip install 'duckdb>=1.5.0'.",
        )
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _require_supported(module: Any) -> Tuple[int, int, int]:
    """``module``'s version, once it is one the sandbox can confine."""
    version = duckdb_version(module)
    if version < MIN_DUCKDB_VERSION:
        want = ".".join(str(n) for n in MIN_DUCKDB_VERSION)
        have = ".".join(str(n) for n in version)
        raise _sandbox_error(
            what=f"DuckDB {have} is too old to sandbox contract SQL",
            why=(
                f"Before {want}, DuckDB's allowed_directories could be escaped with "
                "'<dir>/./../' or a symlink, so contract SQL could read any file on "
                "this host."
            ),
            fix=f"pip install 'duckdb>={want}'",
        )
    return version


def _sql_list(values: Iterable[str]) -> str:
    return "[" + ", ".join(quote_ansi_string_literal(v) for v in values) + "]"


def _with_sep(directory: str) -> str:
    # A trailing separator makes the entry a directory, never a name prefix
    # (``/data`` must not admit ``/data-other``).
    return directory if directory.endswith(("/", os.sep)) else directory + os.sep


#: One lock per database FILE, so two threads never configure one DuckDB
#: instance at once (:func:`_open_lock`). The dict is guarded because the
#: read-modify-write that adds a missing key is not atomic.
_OPEN_LOCKS: Dict[str, "threading.Lock"] = {}
_OPEN_LOCKS_GUARD = threading.Lock()


def _open_lock(target: str) -> Any:
    """The lock that serialises opening ``target``, or a no-op for in-memory.

    DuckDB shares one instance per database file per process, and the sandbox's
    settings live on that instance, not on the connection. Two threads opening
    one file therefore race: each ``SET`` lands on the instance the other is
    also configuring, so one connection can end up running under the other's
    allowlist, and :func:`_refuse_ungranted_dirs` reads a list the other thread
    is still writing -- which segfaults DuckDB inside
    ``AllowedDirectoriesSetting::GetSetting`` on 1.5.1 and 1.5.6 (not on
    2.0.0). Serialising the open closes all of it: the second thread finds a
    locked instance and gets the :func:`_already_open` refusal it was always
    meant to get, instead of winning a race. No ``before_lock`` callback opens
    a database through this helper, so holding the lock across one cannot
    deadlock.

    A private in-memory database (``:memory:``, ``""``) is its own instance per
    connection and needs no lock. A NAMED one (``:memory:shared``) is shared
    within the process as a file is, and is keyed by its literal name.
    """
    if target in (":memory:", ""):
        return contextlib.nullcontext()
    key = target if target.startswith(":") else _fold(os.path.realpath(target))
    with _OPEN_LOCKS_GUARD:
        return _OPEN_LOCKS.setdefault(key, threading.Lock())


#: Prefix of the spill directory each connection owns (:func:`_own_spill_dir`).
#: Named so an operator who finds one left behind knows what wrote it.
SPILL_DIR_PREFIX = "fluid-duckdb-spill-"

#: Prefix of the directory holding one connection's private CA-bundle copy
#: (:func:`_private_ca_bundle`). Named for the same reason.
CA_DIR_PREFIX = "fluid-duckdb-ca-"


def _refuse_caller_spill_dir(config: Optional[Mapping[str, Any]]) -> None:
    """Refuse a ``config`` that names ``temp_directory``: the sandbox owns it.

    Silently overriding it would be worse than refusing, because the caller
    would go on believing its own directory was in use.
    """
    if any(str(key).lower() == "temp_directory" for key in (config or {})):
        raise _sandbox_error(
            what="DuckDB config names 'temp_directory'",
            why=(
                "The sandbox owns the spill directory. DuckDB folds its temp directory "
                "into allowed_directories, so a value chosen anywhere else widens what "
                "contract SQL may read."
            ),
            fix="Drop 'temp_directory' from config; the connection sets its own.",
        )


def _own_spill_dir() -> str:
    """A private directory for one connection's spill files, created mode 0700.

    DuckDB's default ``temp_directory`` is RELATIVE: ``.tmp`` in the working
    directory, or ``<db>.tmp`` beside a file database. Either sits in a
    directory a contract's repository supplies, and DuckDB resolves its temp
    directory into ``allowed_directories`` like any other entry, so a
    repository that ships ``.tmp -> /`` grants the whole host to its own SQL
    (reproduced with an empty allowlist on 1.5.1, 1.5.6 and 2.0.0, both
    variants: ``tests/providers/test_duckdb_sandbox_spill.py``). Pointing
    ``temp_directory`` at a directory the engine creates takes that grant away
    from the contract: :func:`tempfile.mkdtemp` creates it 0700 under
    ``$TMPDIR``, outside the contract's tree, under a name no contract can
    predict or pre-place a symlink at.

    Spilling stays on. ``temp_directory = ''`` leaves ``allowed_directories``
    empty, which is tighter still, but a query that outgrows ``memory_limit``
    then fails where it used to finish, and the configuration is locked, so no
    call site could turn spilling back on. The directory this grants instead is
    one empty directory the engine made: contract SQL can read
    ``current_setting('temp_directory')`` and write scratch files there, which
    reaches nothing it did not already have.

    An owned per-connection spill directory, set before the configuration is
    locked and removed on teardown, is the pattern paulosuzart/duckbox (#80),
    nightswatchhq/nuthatch (#1182) and monarch-initiative/monarch-app (#1422)
    each arrived at independently; DuckDB's own "Securing DuckDB" page does not
    mention ``temp_directory`` at all. Those projects' own follow-ups name what
    this does NOT do: no ``max_temp_directory_size`` cap (DuckDB's default is
    90% of the free space on the volume), and a hard kill leaves the directory
    behind for ``$TMPDIR``'s own reaper to collect.
    """
    return tempfile.mkdtemp(prefix=SPILL_DIR_PREFIX)


def _escaping_remote_prefix(prefix: str, cwd: str) -> Optional[str]:
    """The local directory ``prefix`` would grant, or ``None`` when it grants none.

    DuckDB resolves every ``allowed_directories`` entry against the file system.
    A remote prefix has no business naming a local path, but its text can: with
    ``s3: -> /`` in the working directory, ``s3://etc/`` resolves to ``/etc``.

    The honest form is the lexical one -- DuckDB 1.5 reports
    ``s3://bucket/landing/`` as ``<cwd>/s3:/bucket/landing/``, which is what
    joining and normalising gives. Resolving symlinks is what differs, so a
    prefix whose resolution leaves that form is one whose components follow a
    link, and the directory it lands on is what DuckDB will grant. Existence is
    deliberately not the test: DuckDB grants a resolved path that is not there
    yet, and contract SQL can write into it.
    """
    joined = os.path.join(cwd, prefix)
    lexical = _fold(os.path.normpath(joined))
    resolved = os.path.realpath(joined)
    return resolved if _fold(resolved) != lexical else None


def _refuse_escaping_remote_prefixes(allow: DuckDBAllowlist) -> None:
    """Raise before the grant is set, rather than reporting it afterwards.

    :func:`_refuse_ungranted_dirs` would catch this too, and keeps doing so as
    the backstop. Refusing here means the connection never holds the grant at
    all, so the barrier does not rest on a single read-back.
    """
    cwd = os.path.realpath(os.getcwd())
    escaped = [
        (prefix, landed)
        for prefix in allow.remote_prefixes
        if (landed := _escaping_remote_prefix(prefix, cwd)) is not None
    ]
    if escaped:
        # ``what`` is the panel's TITLE, which the renderer truncates to one line,
        # so the prefixes go in ``why``: naming them is the whole point of the
        # message and a cut-off title would hide it.
        raise _sandbox_error(
            what="a declared remote prefix would grant a local directory to the SQL",
            why=(
                "DuckDB resolves every allowed_directories entry against the file "
                "system, so a prefix whose text also names a local path becomes a local "
                "grant, and a link in the working directory is enough. Refused here: "
                + "; ".join(f"{p} resolves to {landed}" for p, landed in escaped)
                + ". A remote prefix must bound remote reads and grant no local "
                "directory."
            ),
            fix=(
                "Remove the link (or the directory) in the working directory whose name "
                "matches the prefix's scheme, or declare the location by a prefix whose "
                "text does not resolve locally. If the local directory is genuinely "
                "needed, declare it as a directory so it is granted deliberately rather "
                "than as a side effect of a URL."
            ),
        )


#: The CA bundles DuckDB's httpfs probes, in the order its own refusal names
#: them ("Add one of ... to allowed_paths"). Mirrored from DuckDB rather than
#: borrowed from ``ssl.get_default_verify_paths()`` or ``certifi`` (which IS
#: installed, via requests and httpx), the way sethmlarson/truststore picks a
#: trust store for OpenSSL: what is copied has to be a store DuckDB itself
#: would have trusted, and DuckDB probes these five, consulting neither
#: OpenSSL's defaults nor ``SSL_CERT_FILE``. certifi's bundle sits inside
#: site-packages, where overwriting it would reach every other tool in the
#: environment -- the opposite of what :func:`_private_ca_bundle` is for.
#: ``tests/providers/test_duckdb_sandbox_ca_bundle.py`` parses the list back out
#: of DuckDB's refusal and fails if upstream's order or contents drift.
_CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/cert.pem",
)


def _ca_bundle_for_remote(allow: DuckDBAllowlist) -> Optional[str]:
    """The host CA bundle a remote read needs, if there is one to copy.

    ``None`` when the call site declared no remote prefix -- a purely local
    read must not gain a grant it has no use for -- and ``None`` when the host
    has none of DuckDB's candidates, which is a minimal container or Windows.
    Granting nothing there is what this already did: DuckDB then raises its own
    refusal naming the paths it looked for, which is more accurate than
    anything this could say.

    What reaches ``allowed_paths`` is never this path. It is a copy made by
    :func:`_private_ca_bundle`, because a granted path is WRITABLE -- see that
    function.

    A directory is never returned. ``allowed_paths`` grants one file, so this
    cannot widen what :func:`_refuse_ungranted_dirs` reads back from
    ``allowed_directories`` -- including the remote prefixes that are
    load-bearing on 1.5.x (see :func:`_escaping_remote_prefix`).
    """
    if not allow.remote_prefixes:
        return None
    for candidate in _CA_BUNDLE_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return None


def _private_ca_bundle(bundle: str) -> str:
    """A mode-0400 copy of ``bundle`` in a 0700 directory this connection owns.

    **A path in ``allowed_paths`` is granted for WRITING, not only reading.**
    Measured on 1.5.6 and 2.0.0: with the host store granted directly,
    ``COPY (SELECT 'ATTACKER CA') TO '<store>' (FORMAT CSV, USE_TMP_FILE
    false)`` replaced a 333 KB trust store with 14 bytes, while an ungranted
    sibling in the same directory stayed refused. The OS is the only thing that
    stops it: ``/etc/ssl/cert.pem`` is root-owned, so an unprivileged ``fluid``
    cannot write it -- but ``cli/scaffold_ci.py`` generates a ``python:3.12-slim``
    job, which runs as root, and that job runs ``fluid apply``. Contract SQL
    could have installed its own CA into the trust store of the container it was
    built in. Found by the world-class inspection of the first version of this
    fix, which granted the store itself.

    So the store is never granted. The connection reads a copy that belongs to
    it, under :func:`tempfile.mkdtemp`'s 0700 directory, removed when the
    connection object is finalised.

    What the 0400 is worth, stated exactly: it refuses an unprivileged write
    through a plain open, and nothing more. The allowlist does NOT refuse a
    write to this path -- it is in ``allowed_paths``, which is the premise of
    this whole function -- and measured, ``COPY TO`` with its default options
    writes a temporary file and renames it over the target, so the mode is
    bypassed as soon as the containing directory is writable by the SQL. The
    caller therefore drops the grant entirely when the directory would sit
    inside a granted one (:func:`_ca_dir_within_a_grant`), and root bypasses the
    mode in any case.

    The guarantee that does hold is the one that matters: the host store is not
    in ``allowed_paths``, so nothing outside this process's own temporary
    directory can be reached. An attacker who does replace the copy chooses the
    trust anchors of one connection, over remote locations the contract named
    anyway, and the file is gone when that connection is.

    The cost is one ~330 KB file copy per connection that declared a remote
    prefix, which is why :func:`_ca_bundle_for_remote` returns ``None`` for
    every other connection.
    """
    directory = tempfile.mkdtemp(prefix=CA_DIR_PREFIX)
    try:
        copy = os.path.join(directory, "ca-bundle.pem")
        shutil.copyfile(bundle, copy)
        os.chmod(copy, 0o400)
    except BaseException:
        # The caller registers the remover only once this returns, so a failure
        # here -- ENOSPC, an I/O error, or the candidate disappearing between
        # the isfile probe and the copy -- would leak the directory for good.
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return copy


def _ca_dir_within_a_grant(directory: str, allow: DuckDBAllowlist) -> Optional[str]:
    """The granted directory ``directory`` sits inside, or ``None``.

    ``tempfile.mkdtemp`` honours ``$TMPDIR``, and a workspace-local ``$TMPDIR``
    is an ordinary CI, direnv or Nix arrangement. Where it points inside a
    directory this connection grants, the copy is writable by the SQL whatever
    its mode: measured, ``COPY TO`` with default options renamed a new file over
    the 0400 copy on 1.5.6 and 2.0.0, turning 333 KB of trust anchors into 14
    bytes. Granting a trust store the SQL can replace is worse than granting
    none, so the caller grants none.
    """
    real = _fold(os.path.realpath(directory))
    for granted in allow.dirs:
        root = _fold(os.path.realpath(granted))
        if _is_within(real, root):
            return root
    return None


def _refuse_ungranted_dirs(con: Any, allow: DuckDBAllowlist, spill: str) -> None:
    """Refuse the connection if DuckDB granted a directory ``allow`` does not name.

    Read back under the lock, so it reports what the SQL actually runs with
    rather than what was sent. The sandbox is only as good as DuckDB's own
    bookkeeping, and that bookkeeping has surprised us twice: a relative
    ``temp_directory`` is folded in (the defect above), and ``allowed_paths``
    and ``allowed_directories`` are separate lists that resolve differently.
    A grant that appears from somewhere other than ``allow`` fails the
    connection closed rather than running contract SQL with it.

    Both sides are compared after ``realpath`` and, where the file system
    ignores case, after casefolding, because that is how DuckDB stores them:
    two entries that resolve to one directory are reported once (a grant of
    both ``link`` and its target ``real`` reports only ``real/``).

    A remote prefix is accommodated by its LEXICAL form, which is what DuckDB
    1.5 reports (``s3://bucket/landing/`` comes back as
    ``<cwd>/s3:/bucket/landing/``; 2.0 reports the URL itself). Writing it as
    ``realpath(prefix)`` followed symlinks inside the prefix and so excused the
    escape :func:`_escaping_remote_prefix` refuses. The cwd itself is resolved,
    since DuckDB reports the resolved form.
    """
    reported = tuple(con.execute("SELECT current_setting('allowed_directories')").fetchall()[0][0])
    here = os.path.realpath(os.getcwd())
    granted = {_fold(os.path.realpath(d)) for d in (*allow.dirs, spill)}
    granted |= {_fold(os.path.normpath(os.path.join(here, p))) for p in allow.remote_prefixes}
    extra = [
        entry
        for entry in reported
        if (entry not in allow.remote_prefixes)
        if (is_remote_location(entry) or _fold(os.path.realpath(entry)) not in granted)
    ]
    if extra:
        # An escaping prefix is refused before the SET, so reaching it here means
        # that check was bypassed. Defer to its message: this grant IS something
        # a contract can cause, and the advice below would send the reader off to
        # report a sandbox bug they caused themselves.
        _refuse_escaping_remote_prefixes(allow)
        raise _sandbox_error(
            what=(
                "DuckDB granted directories this connection did not ask for: "
                + ", ".join(repr(e) for e in extra)
            ),
            why=(
                "Under the lock, contract SQL reads everything in "
                "allowed_directories. DuckDB reported a directory that is not in this "
                "call site's allowlist, so what the SQL can read is not what was granted."
            ),
            fix=(
                "This is a bug in the sandbox or a change in DuckDB, not something a "
                "contract can cause. Report it with the DuckDB version "
                "(SELECT version()) and the directories listed above."
            ),
        )


def secure_duckdb_connect(
    database: PathLike = ":memory:",
    *,
    allow: DuckDBAllowlist,
    extensions: Iterable[str] = (),
    read_only: bool = False,
    config: Optional[Mapping[str, Any]] = None,
    before_lock: Optional[Callable[[Any], None]] = None,
) -> "duckdb.DuckDBPyConnection":
    """Open DuckDB with file and network access confined to ``allow``.

    ``allow`` is required: each caller states what it legitimately reads and
    writes. A file-backed ``database`` needs no grant: it is opened before the
    lock, and its own WAL and checkpoint writes are not checked against the
    allowlist (``test_file_database_writes_and_checkpoints_under_the_lock``), so
    granting its directory would only widen what the SQL can read (for the
    local provider's ``persist`` mode, all of ``~/.fluid``). ``extensions`` are installed if missing and loaded before the lock; nothing
    can be loaded after it. ``before_lock(con)`` runs once the extensions are
    in, for set-up that needs access the SQL must not have, such as attaching a
    declared source database or creating an object-store secret. ``config``
    takes scalar start-up options (``threads``, ``TimeZone``), which cannot be
    changed once the configuration is locked. Spill files go to a 0700 directory
    this creates under ``$TMPDIR`` and removes once the connection is gone, not
    to DuckDB's relative default (:func:`_own_spill_dir`). On DuckDB 2.x a database file the
    connection creates is written in the storage format DuckDB 1.5 writes
    (:func:`~fluid_build.providers._duckdb_compat.storage_compat_config`), unless
    ``config`` names ``storage_compatibility_version`` itself.

    Raises :class:`DuckDBSandboxError` for an older DuckDB, a bad allowlist
    entry, or a file ``database`` this process already has open (its one
    shared instance is locked, so this connection could not be confined to
    ``allow``); whatever ``duckdb.connect``, an extension or ``before_lock`` raises
    is raised as it is, with the connection closed.
    """
    import duckdb

    version = _require_supported(duckdb)
    _refuse_caller_spill_dir(config)
    target = os.fspath(database)
    # A database file this creates stays readable by the oldest supported
    # DuckDB (see _duckdb_compat); a caller's own setting wins.
    options = {**storage_compat_config(version), **dict(config or {})}
    # One instance per file per process, and the sandbox's settings live on the
    # instance: without this, two threads opening one file configure each
    # other's connection (see _open_lock).
    with _open_lock(target):
        try:
            con = duckdb.connect(target, read_only=read_only, config=options)
        except duckdb.Error as exc:
            if "same database file with a different configuration" in str(exc):
                raise _already_open(target) from exc
            raise
        # Appended with the spill directory's remover as soon as there is one,
        # so a failure can run it AFTER the connection is closed.
        spill_cleanup: List["weakref.finalize"] = []
        try:
            _confine_connection(
                con,
                duckdb,
                target=target,
                allow=allow,
                extensions=extensions,
                before_lock=before_lock,
                spill_cleanup=spill_cleanup,
            )
        except BaseException:
            con.close()
            # Only now that DuckDB has let go: it may still hold open spill
            # files, and a removal underneath it would fail on Windows.
            for drop in spill_cleanup:
                drop()
            raise
    return con


def _confine_connection(
    con: Any,
    duckdb: Any,
    *,
    target: str,
    allow: DuckDBAllowlist,
    extensions: Iterable[str],
    before_lock: Optional[Callable[[Any], None]],
    spill_cleanup: List["weakref.finalize"],
) -> None:
    """Apply the sandbox to an open connection, in the order the module docstring gives.

    Called with that database file's :func:`_open_lock` held, so no other
    thread is configuring this instance while these settings land.
    ``spill_cleanup`` is appended with the spill directory's remover once it
    exists, so the caller runs it after closing the connection.
    """
    try:
        con.execute("SET allow_persistent_secrets = false")
    except duckdb.Error as exc:
        # The first statement on a fresh connection: locked already means
        # this joined another connection's locked instance of the file.
        if "configuration has been locked" in str(exc):
            raise _already_open(target) from exc
        raise
    con.execute("SET allow_community_extensions = false")
    for ext in extensions:
        name = validate_ident(str(ext))
        try:
            con.execute(f"LOAD {name}")
        except duckdb.Error:
            con.execute(f"INSTALL {name}")
            con.execute(f"LOAD {name}")
    if before_lock is not None:
        before_lock(con)
    con.execute("SET autoinstall_known_extensions = false")
    con.execute("SET autoload_known_extensions = false")
    # Before the allowlist, because DuckDB folds its temp directory into
    # allowed_directories: left at its relative default, that is a
    # directory the contract's repository owns (see _own_spill_dir).
    spill = _own_spill_dir()
    spill_cleanup.append(weakref.finalize(con, shutil.rmtree, spill, ignore_errors=True))
    con.execute(f"SET temp_directory = {quote_ansi_string_literal(spill)}")
    home = os.environ.get("HOME")
    if home:
        # duckdb/duckdb#26064: the allowlist check expands '~' with $HOME and
        # the open with this setting. Equal and locked, they cannot disagree.
        con.execute(f"SET home_directory = {quote_ansi_string_literal(home)}")
    _refuse_escaping_remote_prefixes(allow)
    directories = [_with_sep(d) for d in allow.dirs] + list(allow.remote_prefixes)
    con.execute(f"SET allowed_directories = {_sql_list(directories)}")
    # The CA bundle is appended here and NOT folded into ``allow``: the
    # allowlist stays what the call site declared, so the read-back below and
    # every caller that reads ``allow`` as a declared-inputs list are unchanged.
    # What is granted is a private copy, never the host store: a granted path
    # can be written (:func:`_private_ca_bundle`).
    paths = list(allow.paths)
    host_bundle = _ca_bundle_for_remote(allow)
    if host_bundle is not None:
        private = _private_ca_bundle(host_bundle)
        directory = os.path.dirname(private)
        exposed = _ca_dir_within_a_grant(directory, allow)
        if exposed is not None:
            # Writable by the SQL whatever the mode, so worse than no grant.
            shutil.rmtree(directory, ignore_errors=True)
            _log.warning(
                "duckdb_ca_bundle_grant_skipped: the temporary directory would sit inside "
                "the granted directory %s, where contract SQL could replace the trust "
                "store this connection reads. A remote read may fail on DuckDB 2.0 with "
                "'Cannot read a CA certificate bundle'; point TMPDIR outside the granted "
                "tree to restore it.",
                exposed,
            )
        else:
            try:
                # GLOBAL, not the session default: ca_cert_file is a LOCAL-scope
                # setting, so a session-scoped pin leaves con.cursor() with ''
                # and its remote reads failing on 2.0.0 (measured). The lock
                # still refuses a change afterwards, GLOBAL or not.
                con.execute(f"SET GLOBAL ca_cert_file = {quote_ansi_string_literal(private)}")
            except duckdb.Error:
                # httpfs is not loaded, so the setting is not in the catalog and
                # DuckDB will never open this copy. Granting it would leave a
                # writable file in allowed_paths for a benefit that cannot
                # exist -- the azure extension takes exactly this path, and its
                # own TLS does not read the bundle from here.
                shutil.rmtree(directory, ignore_errors=True)
            else:
                spill_cleanup.append(
                    weakref.finalize(con, shutil.rmtree, directory, ignore_errors=True)
                )
                paths.append(private)
    con.execute(f"SET allowed_paths = {_sql_list(paths)}")
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    _refuse_ungranted_dirs(con, allow, spill)


def _already_open(target: str) -> DuckDBSandboxError:
    return _sandbox_error(
        what=f"DuckDB database {target!r} is already open in this process",
        why=(
            "DuckDB shares one instance per database file within a process, and the "
            "sandbox locks that instance's configuration, so a second connection to an "
            "open file cannot be sandboxed with its own allowlist."
        ),
        fix=(
            "Close the other connection to this file first, or give this run its own "
            "database file."
        ),
    )


def is_unloaded_function(exc: BaseException) -> bool:
    """Whether ``exc`` is a function whose extension autoloading would have loaded.

    DuckDB's catalog error: ``Table Function with name "sqlite_scan" is not in
    the catalog, but it exists in the sqlite_scanner extension``.
    """
    text = str(exc)
    return "but it exists in the" in text and "extension" in text


def is_sandbox_refusal(exc: BaseException) -> bool:
    """Whether ``exc`` is DuckDB refusing a path or setting the sandbox denies."""
    try:
        import duckdb
    except ImportError:  # pragma: no cover - only reachable without duckdb
        return False
    if isinstance(exc, duckdb.PermissionException):
        return True
    # Under the lock no SQL can SET a setting back, nor pull in an extension
    # (autoloading is off): DuckDB's own advice to do either cannot be taken.
    # ``read_csv('https://...')`` says "requires the extension"; a function such
    # as ``sqlite_scan`` "is not in the catalog, but it exists in the
    # sqlite_scanner extension".
    text = str(exc)
    return isinstance(exc, duckdb.Error) and (
        "configuration has been locked" in text
        or "requires the extension" in text
        or is_unloaded_function(exc)
    )


def sandbox_refusal_hint(allow: DuckDBAllowlist, refusal: Optional[BaseException] = None) -> str:
    """One sentence naming what the refused SQL could have read instead.

    With the ``refusal`` itself, a function from an extension that was not
    loaded is named as such rather than as a path to declare.
    """
    if refusal is not None and is_unloaded_function(refusal):
        return (
            "DuckDB refused it: contract SQL runs with extension autoloading off and "
            "cannot INSTALL or LOAD one, so functions from extensions the engine does not "
            "load (sqlite_scan, read_xlsx, ST_Read, delta_scan, iceberg_scan) are not "
            "available. Read the data as CSV, Parquet or JSON, or land it with an "
            "acquisition build first."
        )
    granted = [*allow.dirs, *allow.paths, *allow.remote_prefixes]
    where = ", ".join(granted) if granted else "nothing (in-memory only)"
    # Named because it is readable, not because anyone declared it: a remote read
    # also grants the host's trust store (:func:`_ca_bundle_for_remote`), so
    # "only the locations the contract declares" would be false here. Left out
    # when there is no remote prefix, where it would be noise on every refusal.
    # Named without its path: what SQL can read is the private copy
    # :func:`_private_ca_bundle` made for this connection, not the host store
    # this returns, and printing the store would point the reader at a file the
    # SQL was in fact refused. It can overstate by one line -- the grant is
    # dropped when the pin cannot land or the copy would be exposed -- which is
    # the safe direction for a sentence about what SQL may read.
    also = (
        ", and a private copy of the host's TLS trust store, which a remote read needs"
        if _ca_bundle_for_remote(allow)
        else ""
    )
    return (
        "DuckDB refused it: contract SQL may only read and write the locations the "
        f"contract declares and its own directory ({where}){also}. Declare the file as "
        "an input under the contract's directory or workspace, or move it there "
        f"(the operator can allow another directory with {OPERATOR_DIRS_ENV})."
    )


__all__ = [
    "BROAD_ROOT_REASONS",
    "CA_DIR_PREFIX",
    "MIN_DUCKDB_VERSION",
    "OPERATOR_DIRS_ENV",
    "SPILL_DIR_PREFIX",
    "DuckDBAllowlist",
    "DuckDBSandboxError",
    "WorkspaceGrant",
    "broad_root_reason",
    "confine_declared",
    "copy_tmp_path",
    "duckdb_version",
    "is_remote_location",
    "is_sandbox_refusal",
    "is_unloaded_function",
    "operator_allowed_dirs",
    "refuse_broad_root",
    "sandbox_refusal_hint",
    "secure_duckdb_connect",
    "unaliased_dir",
    "workspace_grant",
]
