# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Write a file atomically, owner-only from the first byte.

For files that hold a credential or the operator's configuration (the AI
config, ``~/.databrickscfg``, a source's connection file, an MCP artifact).
It came from the Fernet credential store, retired on 2026-11-15, which was
its first user.
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically with restrictive permissions.

    Opens a sibling temp file with ``O_EXCL`` + the target ``mode`` (so the
    file is never briefly world-readable under a permissive umask — the
    create-then-chmod window was SECURITY_REVIEW S-009/B2), ``fsync``s it,
    then atomically ``os.replace``s it over ``path`` and fsyncs the parent
    directory so the rename survives a crash.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            # Best effort: the temp file may be gone already, and the error
            # being re-raised below is the one the caller needs.
            pass
        raise
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Parent-directory fsync is best-effort — not supported on every
        # platform / filesystem (notably Windows).
        pass
