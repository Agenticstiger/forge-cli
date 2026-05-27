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

"""Store backend resolution for staged forge flows."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Optional

from .backends.file import FileBackend
from .backends.null import NullBackend
from .backends.postgres import PostgresBackend
from .backends.sqlite import SqliteBackend
from .backends.vector import VectorBackend, is_sqlite_vec_available
from .base import Store

_log = logging.getLogger(__name__)

# Module-level guard so the "persistence disabled" WARNING fires once
# per process even if ``resolve_store`` is called repeatedly with the
# disabled value (which a long-running forge runtime can easily do).
_LOGGED_NULL: set[str] = set()


def _safe_store_init(
    backend_factory: Callable[[], Store],
    *,
    fallback_factory: Optional[Callable[[], Store]] = None,
    label: str = "backend",
) -> Store:
    """Construct a store, falling back if construction raises.

    Mirrors the swallow-and-warn shape used elsewhere in the codebase
    (e.g. ``providers/__init__.py::_discover_entrypoints`` and
    ``semantic_writer.py``) so a transient DB outage or a missing
    optional dep doesn't crash a forge mid-flight.

    On failure: logs a single WARNING, then either returns
    ``fallback_factory()`` (typically ``FileBackend``) or, if no
    fallback was provided, falls all the way through to
    ``NullBackend`` so a forge can still complete.
    """

    try:
        return backend_factory()
    except Exception as exc:  # noqa: BLE001
        if fallback_factory is None:
            _log.warning(
                "Store %s init failed (%s: %s); using NullBackend — "
                "memory will not be persisted this run.",
                label,
                type(exc).__name__,
                exc,
            )
            return NullBackend()
        _log.warning(
            "Store %s init failed (%s: %s); falling back to a local "
            "FileBackend so the forge can continue. Re-check your "
            "FLUID_STORE_* config before the next run.",
            label,
            type(exc).__name__,
            exc,
        )
        try:
            return fallback_factory()
        except Exception as fb_exc:  # noqa: BLE001 - last-resort guard
            _log.warning(
                "Fallback store init also failed (%s: %s); using "
                "NullBackend so the forge can still complete.",
                type(fb_exc).__name__,
                fb_exc,
            )
            return NullBackend()


def _file_fallback_factory(*, path: Optional[str | Path], workspace: Path) -> Callable[[], Store]:
    """Construct a FileBackend factory for use as a graceful fallback."""

    def _build() -> Store:
        root = Path(
            path or os.environ.get("FLUID_STORE_ROOT") or (Path.home() / ".fluid" / "store")
        ).expanduser()
        return FileBackend(root=root, workspace_root=workspace)

    return _build


def resolve_store(
    *,
    workspace_root: Optional[Path] = None,
    backend: Optional[str] = None,
    path: Optional[str | Path] = None,
    dsn: Optional[str] = None,
    vector_backing: Optional[str] = None,
) -> Store:
    """Resolve a staged store backend from explicit args or env vars."""
    workspace = (workspace_root or Path.cwd()).resolve()
    backend_name = str(backend or os.environ.get("FLUID_STORE_BACKEND") or "file").strip().lower()

    if backend_name in {"0", "none", "null", "disabled"}:
        # Fire a single WARNING per (process, backend_name) so users
        # who export ``FLUID_STORE_BACKEND=null`` to silence a noisy
        # store see the cost of that choice once — episodic +
        # semantic memory will not be retained, retrieval will turn
        # up empty, and ``fluid memory show`` will be useless.
        if backend_name not in _LOGGED_NULL:
            _log.warning(
                "Store persistence disabled (FLUID_STORE_BACKEND=%s). "
                "Episodic + semantic memory will not be retained for "
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

    if backend_name == "postgres":
        resolved_dsn = str(dsn or os.environ.get("FLUID_STORE_DSN") or "").strip()
        if not resolved_dsn:
            raise RuntimeError("FLUID_STORE_DSN is required for PostgresBackend")
        # Postgres-down used to surface raw ``psycopg.OperationalError``
        # mid-forge. Wrap the construct so the same outage now degrades
        # to a local FileBackend with a single WARNING — the forge
        # completes, the operator sees the message, and re-running once
        # the DB is back picks up where they left off.
        return _safe_store_init(
            lambda: PostgresBackend(resolved_dsn),
            fallback_factory=_file_fallback_factory(path=path, workspace=workspace),
            label="PostgresBackend",
        )

    if backend_name == "vector":
        backing_name = (
            str(vector_backing or os.environ.get("FLUID_STORE_VECTOR_BACKING") or "file")
            .strip()
            .lower()
        )
        if backing_name == "vector":
            backing_name = "file"
        backing_store = resolve_store(
            workspace_root=workspace,
            backend=backing_name,
            path=path,
            dsn=dsn,
        )
        # When the user installed the ``[vector]`` extra, light up
        # the embedded ranking automatically. Previously this was
        # hard-coded to ``False`` so installing the extra produced
        # no behaviour change — the canonical "dead code on the
        # wire" bug. ``VectorBackend.__init__`` still guards against
        # the extra being missing, so this stays graceful.
        use_embeddings = is_sqlite_vec_available()
        return VectorBackend(backing_store, use_embeddings=use_embeddings)

    raise RuntimeError(f"Unknown staged store backend: {backend_name}")
