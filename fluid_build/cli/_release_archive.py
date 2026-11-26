# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid ship --release-dir DIR --release-archive PATH``: the sealed release as one ``.tgz``,
for a platform that takes a file rather than a directory.

The archive holds the sealed directory as it is, ``release.json`` at its root and no folder
around it, written only after the seal succeeded. Its bytes are the same for the same sealed
release: entries in sorted order, a fixed mtime, owner 0 with no owner names, modes 0o644 and
0o755, and a gzip header with no time or name (``forge.core.bundle.write_tgz_stream``, the rules
``bundle.tgz`` already follows, which the Reproducible Builds project gives for archives). It is
written to a temporary file beside PATH and renamed into place, so a failure leaves no archive,
and its SHA-256 is printed for the receiving platform to check.

Standard library only at module scope: ``fluid ship --help`` imports ship's modules.
"""

from __future__ import annotations

import functools
import hashlib
import os
import stat
from pathlib import Path
from typing import Any, BinaryIO, List, Optional, Tuple


class ArchiveError(RuntimeError):
    """The archive could not be written; none was left at its path."""


#: Longest file name the temporary file beside the archive leaves room for.
_MAX_NAME = 200
#: Largest file a USTAR header can describe.
_MAX_MEMBER = 8**11 - 1


def refusal(archive: str, release_dir: Optional[Path]) -> Optional[str]:
    """Why ``--release-archive`` cannot run, before any stage, or ``None``: no
    ``--release-dir``, a path inside it (links resolved), a folder or a link, a name too long,
    or a path the CLI's path rules refuse (traversal, a system path). Nothing is created here,
    so a refused run or ``--explain`` leaves the disk as it was; the folder the archive goes in
    is made, and checked writable (``cli.security.validate_cli_path``), once the seal passed."""
    if not archive:
        return None
    if release_dir is None:
        return f"--release-archive {archive} writes the sealed release: add --release-dir DIR"
    from fluid_build.cli.core import FluidCLIError
    from fluid_build.cli.security import SecurePathValidator, get_security_context

    validator = SecurePathValidator(get_security_context())
    try:
        validator._reject_raw_traversal(archive, "write")
        validator._validate_path_security(Path(archive).resolve(), "write")
    except FluidCLIError as exc:
        return f"--release-archive {archive}: {exc.message}"
    real, root = Path(os.path.realpath(archive)), Path(os.path.realpath(release_dir))
    if real == root or root in real.parents:
        return f"--release-archive {archive} lies inside --release-dir {release_dir}"
    path = Path(os.path.abspath(archive))  # the name as given: a link there is refused below
    if len(path.name) > _MAX_NAME:
        return f"--release-archive: a file name of at most {_MAX_NAME} characters"
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"--release-archive {archive} cannot be read ({type(exc).__name__})"
    if stat.S_ISLNK(mode):
        return f"--release-archive {archive} is a link; name the file itself"
    if not stat.S_ISREG(mode):
        return f"--release-archive {archive} is not a file"
    return None


def _entries(release_dir: Path) -> List[Tuple[str, int, Any]]:
    """Every file of the sealed release, as ``(path, size, opener)``. A link or anything but a
    regular file or a folder is no part of a sealed release."""
    out: List[Tuple[str, int, Any]] = []

    def unreadable(exc: OSError) -> None:  # a folder left out would make the archive partial
        raise ArchiveError(f"the sealed release cannot be read whole: {exc}")

    for top, dirs, files in os.walk(release_dir, followlinks=False, onerror=unreadable):
        here = Path(top)
        for name in sorted(dirs) + sorted(files):
            path = here / name
            info = os.lstat(path)
            rel = path.relative_to(release_dir).as_posix()
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ArchiveError(f"{rel} in the sealed release is not a regular file")
            if info.st_size > _MAX_MEMBER:
                raise ArchiveError(f"{rel} is larger than a .tgz entry can hold (8 GiB)")
            out.append((rel, info.st_size, functools.partial(_open, path)))
    if not any(rel == "release.json" for rel, _, _ in out):
        raise ArchiveError(f"{release_dir} holds no release.json: it is no sealed release")
    return out


def _open(path: Path) -> BinaryIO:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    return os.fdopen(fd, "rb")


class _Hashing:
    """A file object that hashes what is written through it."""

    def __init__(self, raw: BinaryIO) -> None:
        self.raw = raw
        self.digest = hashlib.sha256()

    def write(self, data: bytes) -> int:
        self.digest.update(data)
        return self.raw.write(data)

    def flush(self) -> None:
        self.raw.flush()


def write_archive(release_dir: Path, archive: str) -> str:
    """Write the sealed release in ``release_dir`` to ``archive``; return its SHA-256 (hex).

    Raises:
        ArchiveError: nothing is left at ``archive`` (and no temporary file beside it).
    """
    import tempfile

    from fluid_build.cli.core import FluidCLIError
    from fluid_build.cli.security import validate_cli_path
    from fluid_build.forge.core.bundle import write_tgz_stream

    target = Path(os.path.abspath(archive))
    temporary: Optional[Path] = None
    try:
        entries = _entries(release_dir)
        try:  # makes the folder and checks it is writable
            validate_cli_path(archive, mode="write", must_exist=False, file_type="archive")
        except FluidCLIError as exc:
            raise ArchiveError(f"{archive}: {exc.message}") from None
        # A name of its own beside the archive (owner-only, never an existing file), so a
        # leftover of an earlier, killed run is never taken or removed.
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".partial", dir=target.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as raw:
            hashing = _Hashing(raw)
            write_tgz_stream(hashing, entries)  # type: ignore[arg-type]
            raw.flush()
            os.fsync(raw.fileno())
        if os.path.islink(target) or os.path.isdir(target):
            raise ArchiveError(f"{archive} became a link or a folder while it was written")
        os.replace(temporary, target)
    except BaseException as exc:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass  # gone already: the error below is the one to report
        if isinstance(exc, ArchiveError):
            raise
        if isinstance(exc, (OSError, ValueError)):
            raise ArchiveError(f"{archive} cannot be written: {exc}") from None
        raise
    return hashing.digest.hexdigest()  # the bytes renamed into place
