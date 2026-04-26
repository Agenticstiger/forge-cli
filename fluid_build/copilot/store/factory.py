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

import os
from pathlib import Path
from typing import Optional

from .backends.file import FileBackend
from .backends.null import NullBackend
from .backends.postgres import PostgresBackend
from .backends.sqlite import SqliteBackend
from .backends.vector import VectorBackend
from .base import Store


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
        return PostgresBackend(resolved_dsn)

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
        return VectorBackend(backing_store)

    raise RuntimeError(f"Unknown staged store backend: {backend_name}")
