# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from fluid_build.copilot.store.backends.file import FileBackend
from fluid_build.copilot.store.backends.sqlite import SqliteBackend
from fluid_build.copilot.store.factory import resolve_store


def test_resolve_store_defaults_to_file(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("FLUID_STORE_BACKEND", raising=False)
    store = resolve_store(workspace_root=tmp_path, path=tmp_path / "store")
    assert isinstance(store, FileBackend)


def test_sqlite_store_supports_root_namespace_query_and_clear(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "store.sqlite3"
    monkeypatch.setenv("FLUID_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("FLUID_STORE_PATH", str(db_path))
    store = resolve_store(workspace_root=tmp_path)
    assert isinstance(store, SqliteBackend)

    store.put("memory/project", "project", {"kind": "project"})
    store.put("memory/team", "team", {"kind": "team"})

    records = store.query("memory", limit=10)
    assert {record.namespace for record in records} == {"memory/project", "memory/team"}

    cleared = store.clear("memory")
    assert cleared == 2
    assert store.query("memory", limit=10) == []


def test_a_removed_backend_falls_back_to_the_file_store_with_one_warning(
    tmp_path, monkeypatch, caplog
) -> None:
    import logging

    from fluid_build.copilot.store import factory
    from fluid_build.copilot.store.backends.file import FileBackend

    monkeypatch.setattr(factory, "_LOGGED_NULL", set())
    with caplog.at_level(logging.WARNING):
        first = factory.resolve_store(backend="postgres", path=tmp_path, workspace_root=tmp_path)
        factory.resolve_store(backend="postgres", path=tmp_path, workspace_root=tmp_path)
    assert isinstance(first, FileBackend)
    assert sum("was removed" in r.getMessage() for r in caplog.records) == 1
