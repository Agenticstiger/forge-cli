# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Store backend resolution for staged forge flows."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from .backends.file import FileBackend
from .backends.null import NullBackend
from .backends.sqlite import SqliteBackend
from .base import Store

_log = logging.getLogger(__name__)

# Module-level guard so the "persistence disabled" WARNING fires once
# per process even if ``resolve_store`` is called repeatedly with the
# disabled value (which a long-running forge runtime can easily do).
_LOGGED_NULL: set[str] = set()


def resolve_store(
    *,
    workspace_root: Optional[Path] = None,
    backend: Optional[str] = None,
    path: Optional[str | Path] = None,
) -> Store:
    """Resolve a staged store backend from explicit args or env vars."""
    workspace = (workspace_root or Path.cwd()).resolve()
    backend_name = str(backend or os.environ.get("FLUID_STORE_BACKEND") or "file").strip().lower()

    if backend_name in {"0", "none", "null", "disabled"}:
        # Fire a single WARNING per (process, backend_name) so users
        # who export ``FLUID_STORE_BACKEND=null`` to silence a noisy
        # store see the cost of that choice once — episodic
        # memory will not be retained.
        if backend_name not in _LOGGED_NULL:
            _log.warning(
                "Store persistence disabled (FLUID_STORE_BACKEND=%s). "
                "Episodic memory will not be retained for "
                "this process. Unset the variable to restore the "
                "default FileBackend.",
                backend_name,
            )
            _LOGGED_NULL.add(backend_name)
        return NullBackend()

    if backend_name == "file":
        root = Path(
            path or os.environ.get("FLUID_STORE_ROOT") or (Path.home() / ".fluid" / "store")
        ).expanduser()
        return FileBackend(root=root, workspace_root=workspace)

    if backend_name == "sqlite":
        sqlite_path = Path(
            path
            or os.environ.get("FLUID_STORE_PATH")
            or (Path.home() / ".fluid" / "store" / "store.sqlite3")
        ).expanduser()
        return SqliteBackend(path=sqlite_path)

    if backend_name in {"postgres", "vector"}:
        raise RuntimeError(
            f"FLUID_STORE_BACKEND={backend_name} was removed with the copilot's memory store. "
            "Unset it to use the default file store, or set FLUID_STORE_BACKEND=sqlite."
        )
    raise RuntimeError(f"Unknown staged store backend: {backend_name}")
