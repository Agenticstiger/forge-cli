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
4. ``home_directory`` (pinned to ``$HOME``, see below), then
   ``allowed_directories`` and ``allowed_paths``: only what the call site
   declares it reads or writes;
5. ``enable_external_access = false``;
6. ``lock_configuration = true``, so the SQL cannot ``SET`` any of it back.

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
* The ``postgres`` and ``mysql`` scanners open sockets through their own
  client libraries, not through DuckDB's file system, so neither
  ``allowed_directories`` nor ``enable_external_access`` bounds them: once
  ``postgres`` is loaded, ``postgres_scan`` reaches any host the process can.
  Through DuckDB 1.5 the ``sqlite`` scanner opens files that way too, so
  ``sqlite_scan`` / ``ATTACH ... (TYPE sqlite)`` open any SQLite file the
  process can read; from 2.0 it goes through DuckDB's file system and the
  allowlist bounds it. Only the engine's own SQL runs on a connection that
  loads a scanner; contract SQL never does.
* DuckDB shares one database instance per file per process, and the lock is
  instance-wide: a second connection to a file database that is already open
  cannot be sandboxed, and is refused with :class:`DuckDBSandboxError`. Each
  call site closes its connection before the next opens the same file.
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

import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterable,
    Mapping,
    NamedTuple,
    Optional,
    Tuple,
    Union,
)

from fluid_build._errors import FluidUserError, doc_url

from ._duckdb_compat import storage_compat_config
from ._sql_safety import quote_ansi_string_literal, validate_ident

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
    as a SQLite source it ``ATTACH``es before the lock: the sqlite scanner opens
    files through its own client library, so ``allowed_directories`` never
    bounds it, and this check is the only one. Confined as
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
    changed once the configuration is locked. On DuckDB 2.x a database file the
    connection creates is written in DuckDB 1.5's storage format
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
    target = os.fspath(database)
    # A database file this creates stays readable by the oldest supported
    # DuckDB (see _duckdb_compat); a caller's own setting wins.
    options = {**storage_compat_config(version), **dict(config or {})}
    try:
        con = duckdb.connect(target, read_only=read_only, config=options)
    except duckdb.Error as exc:
        if "same database file with a different configuration" in str(exc):
            raise _already_open(target) from exc
        raise
    try:
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
        home = os.environ.get("HOME")
        if home:
            # duckdb/duckdb#26064: the allowlist check expands '~' with $HOME and
            # the open with this setting. Equal and locked, they cannot disagree.
            con.execute(f"SET home_directory = {quote_ansi_string_literal(home)}")
        directories = [_with_sep(d) for d in allow.dirs] + list(allow.remote_prefixes)
        con.execute(f"SET allowed_directories = {_sql_list(directories)}")
        con.execute(f"SET allowed_paths = {_sql_list(allow.paths)}")
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
    except BaseException:
        con.close()
        raise
    return con


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
    return (
        "DuckDB refused it: contract SQL may only read and write the locations the "
        f"contract declares and its own directory ({where}). Declare the file as an "
        "input under the contract's directory or workspace, or move it there "
        f"(the operator can allow another directory with {OPERATOR_DIRS_ENV})."
    )


__all__ = [
    "BROAD_ROOT_REASONS",
    "MIN_DUCKDB_VERSION",
    "OPERATOR_DIRS_ENV",
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
