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


def refusal(archive: str, release_dir: Optional[Path]) -> Optional[str]:
    """Why ``--release-archive`` cannot run, before any stage, or ``None``: no
    ``--release-dir``, a path inside it, a folder or a link, or a path the CLI's path rules
    refuse (``cli.security.validate_cli_path``)."""
    if not archive:
        return None
    if release_dir is None:
        return f"--release-archive {archive} writes the sealed release: add --release-dir DIR"
    from fluid_build.cli.core import FluidCLIError
    from fluid_build.cli.security import validate_cli_path

    try:
        validate_cli_path(archive, mode="write", must_exist=False, file_type="archive")
    except FluidCLIError as exc:
        return f"--release-archive {archive}: {exc.message}"
    path = Path(os.path.abspath(archive))
    root = Path(os.path.abspath(release_dir))
    if path == root or root in path.parents:
        return f"--release-archive {archive} lies inside --release-dir {release_dir}"
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
    for top, dirs, files in os.walk(release_dir, followlinks=False):
        here = Path(top)
        for name in sorted(dirs) + sorted(files):
            path = here / name
            mode = os.lstat(path).st_mode
            rel = path.relative_to(release_dir).as_posix()
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise ArchiveError(f"{rel} in the sealed release is not a regular file")
            out.append((rel, os.lstat(path).st_size, functools.partial(_open, path)))
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
    from fluid_build.forge.core.bundle import write_tgz_stream

    target = Path(os.path.abspath(archive))
    temporary = target.with_name(f".{target.name}.{os.getpid()}.partial")
    try:
        entries = _entries(release_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as raw:
            hashing = _Hashing(raw)
            write_tgz_stream(hashing, entries)  # type: ignore[arg-type]
            raw.flush()
            os.fsync(raw.fileno())
        if os.path.islink(target) or os.path.isdir(target):
            raise ArchiveError(f"{archive} became a link or a folder while it was written")
        os.replace(temporary, target)
    except BaseException as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass  # not made yet, or gone already: the error below is the one to report
        if isinstance(exc, ArchiveError):
            raise
        if isinstance(exc, (OSError, ValueError)):
            raise ArchiveError(f"{archive} cannot be written: {exc}") from None
        raise
    return hashing.digest.hexdigest()
