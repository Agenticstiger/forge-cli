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
A Command Center that runs a user's contract would hand that user its host.

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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, Optional, Tuple, Union

from fluid_build._errors import FluidUserError, doc_url

from ._sql_safety import quote_ansi_string_literal, validate_ident

if TYPE_CHECKING:  # pragma: no cover - typing only
    import duckdb

PathLike = Union[str, "os.PathLike[str]"]

#: Oldest DuckDB whose ``allowed_directories`` resolves ``./..`` and symlinks.
MIN_DUCKDB_VERSION: Tuple[int, int, int] = (1, 5, 0)

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


def _static_prefix(location: str) -> Tuple[str, bool]:
    """``location`` cut before its first glob segment, and whether it had one.

    Either separator ends a segment, so a Windows path globs as a POSIX one.
    """
    first = next((i for i, ch in enumerate(location) if ch in _GLOB_CHARS), None)
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
        added = _new(self.dirs, (_local(d) for d in dirs if d is not None and str(d) != ""))
        return DuckDBAllowlist(self.dirs + added, self.paths, self.remote_prefixes)

    def with_paths(self, *paths: Optional[PathLike]) -> "DuckDBAllowlist":
        """Also allow each single file in ``paths`` (``None`` skipped)."""
        added = _new(self.paths, (_local(p) for p in paths if p is not None and str(p) != ""))
        return DuckDBAllowlist(self.dirs, self.paths + added, self.remote_prefixes)

    def with_remote(self, *prefixes: Optional[str]) -> "DuckDBAllowlist":
        """Also allow each URL prefix (``s3://bucket/``, ``https://host/data/``)."""
        added = _new(self.remote_prefixes, (_remote(p) for p in prefixes if p))
        return DuckDBAllowlist(self.dirs, self.paths, self.remote_prefixes + added)

    def with_locations(
        self, *locations: Optional[PathLike], base: Optional[PathLike] = None
    ) -> "DuckDBAllowlist":
        """Also allow each location a contract DECLARES it reads or writes.

        A URL grants its directory prefix (cut before any glob); a local glob
        grants the directory above its first wildcard; an existing directory
        grants itself; anything else grants that one file. A leading ``~`` is
        expanded as DuckDB expands it, and a relative local path is resolved
        against ``base`` (else the working directory, where DuckDB would
        resolve it). This is how a declared input stays readable
        while the SQL next to it reaches nothing else.
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
            # '~' as DuckDB itself would expand it (home_directory is $HOME).
            path = Path(raw).expanduser()
            if base is not None and not path.is_absolute():
                path = Path(base) / path
            prefix, globbed = _static_prefix(str(path))
            if globbed:
                out = out.with_dirs(prefix)
            elif Path(prefix).is_dir():
                out = out.with_dirs(prefix)
            else:
                out = out.with_paths(prefix)
        return out


def _new(existing: Tuple[str, ...], candidates: Iterable[str]) -> Tuple[str, ...]:
    """``candidates`` not already in ``existing``, in order, each once."""
    out: list = []
    for item in candidates:
        if item not in existing and item not in out:
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
    if resolved == os.path.abspath(os.sep):
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


def _require_supported(module: Any) -> None:
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
    changed once the configuration is locked.

    Raises :class:`DuckDBSandboxError` for an older DuckDB or a bad allowlist
    entry; whatever ``duckdb.connect``, an extension or ``before_lock`` raises
    is raised as it is, with the connection closed.
    """
    import duckdb

    _require_supported(duckdb)
    target = os.fspath(database)
    con = duckdb.connect(target, read_only=read_only, config=dict(config or {}))
    try:
        con.execute("SET allow_persistent_secrets = false")
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
    text = str(exc)
    return isinstance(exc, duckdb.Error) and (
        "configuration has been locked" in text or "requires the extension" in text
    )


def sandbox_refusal_hint(allow: DuckDBAllowlist) -> str:
    """One sentence naming what the refused SQL could have read instead."""
    granted = [*allow.dirs, *allow.paths, *allow.remote_prefixes]
    where = ", ".join(granted) if granted else "nothing (in-memory only)"
    return (
        "DuckDB refused it: contract SQL may only read and write the locations the "
        f"contract declares and its own directory ({where}). Declare the file as an "
        "input, or move it under the contract's directory."
    )


__all__ = [
    "MIN_DUCKDB_VERSION",
    "DuckDBAllowlist",
    "DuckDBSandboxError",
    "duckdb_version",
    "is_remote_location",
    "is_sandbox_refusal",
    "sandbox_refusal_hint",
    "secure_duckdb_connect",
]
