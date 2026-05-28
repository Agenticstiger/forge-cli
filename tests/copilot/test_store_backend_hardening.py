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

"""Hardening pins for #46 / #47 / #52.

* NullBackend now fires a ONE-TIME WARNING when resolved (#46) so
  users who disabled persistence by accident see a signal.
* SqliteBackend uses ``PRAGMA user_version`` for schema migrations
  (#47) so adding a column to ``_MIGRATIONS`` no longer silently
  no-ops on existing stores.
* PostgresBackend connect-time failures degrade to a local FileBackend
  with a WARNING (#52); per-op failures degrade to documented misses
  instead of raising.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from fluid_build.copilot.store import factory as factory_module
from fluid_build.copilot.store.backends.file import FileBackend
from fluid_build.copilot.store.backends.null import NullBackend
from fluid_build.copilot.store.backends.sqlite import (
    _MIGRATIONS,
    _SCHEMA_VERSION,
    SqliteBackend,
)
from fluid_build.copilot.store.factory import resolve_store

# ---------------------------------------------------------------------------
# #46 — NullBackend resolution must WARN the user once.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_null_warning_guard():
    """The factory de-duplicates the disabled-store warning via a
    module-level set. Each test starts with a clean slate so the
    one-time semantics are observable independent of test order."""
    factory_module._LOGGED_NULL.clear()
    yield
    factory_module._LOGGED_NULL.clear()


@pytest.mark.parametrize("value", ["null", "none", "disabled", "0"])
def test_null_backend_warns_on_first_resolve(
    value: str, tmp_path: Path, monkeypatch, caplog
) -> None:
    monkeypatch.setenv("FLUID_STORE_BACKEND", value)
    with caplog.at_level(logging.WARNING, logger="fluid_build.copilot.store.factory"):
        store = resolve_store(workspace_root=tmp_path)
    assert isinstance(store, NullBackend)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert (
        len(warnings) == 1
    ), f"expected exactly one WARNING when null backend resolves; got {len(warnings)}"
    msg = warnings[0].getMessage()
    assert "Store persistence disabled" in msg
    assert "FLUID_STORE_BACKEND" in msg


def test_null_backend_warning_fires_only_once_per_value(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """Long-running forge processes call ``resolve_store`` many times.
    The WARNING must NOT spam — the module-level guard de-duplicates
    on backend-name so the user sees the cost of the choice once."""
    monkeypatch.setenv("FLUID_STORE_BACKEND", "null")
    with caplog.at_level(logging.WARNING, logger="fluid_build.copilot.store.factory"):
        for _ in range(5):
            assert isinstance(resolve_store(workspace_root=tmp_path), NullBackend)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, f"WARNING must de-duplicate across resolves; got {len(warnings)}"


# ---------------------------------------------------------------------------
# #47 — SqliteBackend migration framework (PRAGMA user_version).
# ---------------------------------------------------------------------------


def test_fresh_sqlite_store_stamps_current_schema_version(tmp_path: Path) -> None:
    store = SqliteBackend(path=tmp_path / "fresh.sqlite3")
    version = store.conn.execute("pragma user_version").fetchone()[0]
    assert (
        int(version) == _SCHEMA_VERSION
    ), f"a freshly initialised store must report version {_SCHEMA_VERSION}"


def test_existing_v0_store_migrates_to_current_version(tmp_path: Path) -> None:
    """Simulate the pre-migration shape: a v0 store created without
    PRAGMA user_version (i.e. the historical behaviour). Opening it
    with the migration-aware constructor must walk the migration
    table forward to the current ``_SCHEMA_VERSION``."""
    db_path = tmp_path / "legacy_v0.sqlite3"
    # Build a v0-style store by hand — no user_version, schema only.
    raw = sqlite3.connect(str(db_path))
    raw.execute(
        """
        create table store (
            namespace text not null,
            key text not null,
            value_blob text not null,
            metadata text,
            created_at text not null,
            expires_at text,
            fluid_version text,
            primary key (namespace, key)
        )
        """
    )
    raw.execute(
        "insert into store values (?, ?, ?, ?, ?, ?, ?)",
        (
            "memory/semantic",
            "pre_v1_record",
            "{}",
            "{}",
            "2026-01-01T00:00:00+00:00",
            None,
            None,
        ),
    )
    raw.commit()
    # Sanity: this store reports user_version = 0 before migration.
    assert raw.execute("pragma user_version").fetchone()[0] == 0
    raw.close()

    # Now open via the migration-aware backend.
    store = SqliteBackend(path=db_path)
    new_version = store.conn.execute("pragma user_version").fetchone()[0]
    assert (
        int(new_version) == _SCHEMA_VERSION
    ), "opening a v0 store must walk it forward to current schema version"
    # Pre-existing rows survive the migration intact (the v0→v1 step
    # only ensures the table exists; CREATE TABLE IF NOT EXISTS is a
    # no-op on a populated v0 store).
    record = store.get("memory/semantic", "pre_v1_record")
    assert record is not None
    assert record.value == {}


def test_future_schema_version_logs_warning_does_not_crash(tmp_path: Path, caplog) -> None:
    """A store written by a *newer* CLI (user_version >
    ``_SCHEMA_VERSION``) must NOT be downgraded — log a warning and
    proceed so a forge can at least read it."""
    db_path = tmp_path / "future.sqlite3"
    # Build a v1 store via the normal init, then poke user_version
    # forward to simulate a newer CLI having written it.
    SqliteBackend(path=db_path)  # creates v1 store
    raw = sqlite3.connect(str(db_path))
    future_version = _SCHEMA_VERSION + 5
    raw.execute(f"pragma user_version = {future_version}")
    raw.commit()
    raw.close()

    with caplog.at_level(logging.WARNING, logger="fluid_build.copilot.store.backends.sqlite"):
        store = SqliteBackend(path=db_path)
    # user_version unchanged — we did NOT rewind.
    assert int(store.conn.execute("pragma user_version").fetchone()[0]) == future_version
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "opening a future-schema store must log a WARNING about CLI / schema skew"


def test_migration_table_stays_dense(tmp_path: Path) -> None:
    """``_MIGRATIONS`` must carry an entry for every version from 1
    through ``_SCHEMA_VERSION`` so iteration in ``_init_db`` doesn't
    silently skip a version bump."""
    for version in range(1, _SCHEMA_VERSION + 1):
        assert (
            version in _MIGRATIONS
        ), f"_MIGRATIONS is missing version {version}; bump _SCHEMA_VERSION only when the migration exists"


# ---------------------------------------------------------------------------
# #52 — PostgresBackend graceful degrade.
# ---------------------------------------------------------------------------


def test_postgres_construct_failure_falls_back_to_filebackend(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """When ``FLUID_STORE_BACKEND=postgres`` is set but Postgres is
    unreachable, the factory must NOT crash a forge — degrade to a
    local FileBackend with a single WARNING so the user notices but
    the run completes."""

    # Replace the PostgresBackend constructor with one that raises
    # ``psycopg.OperationalError``-shaped exception so we exercise the
    # graceful path without needing a real DB on the test runner.
    class _SimulatedConnectionRefused(Exception):
        pass

    def fake_constructor(self, dsn):  # noqa: ANN001
        raise _SimulatedConnectionRefused("connection refused")

    from fluid_build.copilot.store.backends import postgres as pg_mod

    monkeypatch.setattr(pg_mod.PostgresBackend, "__init__", fake_constructor)
    monkeypatch.setenv("FLUID_STORE_BACKEND", "postgres")
    monkeypatch.setenv("FLUID_STORE_DSN", "postgresql://user:secret@nowhere:1234/x")
    monkeypatch.setenv("FLUID_STORE_ROOT", str(tmp_path / "fallback-root"))

    with caplog.at_level(logging.WARNING, logger="fluid_build.copilot.store.factory"):
        store = resolve_store(workspace_root=tmp_path)

    assert isinstance(store, FileBackend), (
        "PostgresBackend construct failure must fall back to FileBackend, "
        "not surface as a raw exception"
    )
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "fallback must emit a WARNING so the operator notices the degrade"
    joined = " ".join(r.getMessage() for r in warnings)
    assert "PostgresBackend" in joined or "Postgres" in joined


def test_postgres_per_op_failures_degrade_to_misses(tmp_path: Path, monkeypatch) -> None:
    """Connection drops between resolve-time and per-op time must not
    surface as raw exceptions. ``get`` returns None, ``query`` /
    ``search`` return [], ``clear`` returns 0."""

    from fluid_build.copilot.store.backends.postgres import PostgresBackend

    # Build a PostgresBackend with a connection that explodes on every
    # cursor call. We bypass ``__init__`` so we don't touch psycopg.
    instance = PostgresBackend.__new__(PostgresBackend)
    instance.dsn = "postgresql://***@host:5432/db"

    class _DeadConnection:
        def cursor(self):
            raise RuntimeError("connection dropped")

        def commit(self):
            pass

        def rollback(self):
            pass

    instance.conn = _DeadConnection()

    # All four read/write ops must degrade quietly.
    assert instance.get("memory/semantic", "k") is None
    record = instance.put("memory/semantic", "k", {"x": 1})
    # ``put`` still returns the in-memory record so callers don't
    # branch on None — but the failure was logged at DEBUG, not raised.
    assert record.value == {"x": 1}
    assert instance.query("memory/semantic") == []
    assert instance.search("memory/semantic", "needle") == []
    assert instance.clear("memory/semantic") == 0


def test_safe_store_init_uses_explicit_fallback(tmp_path: Path, caplog) -> None:
    """``_safe_store_init`` must run the fallback factory when the
    primary raises — pinning the shared helper directly so other
    backends (Snowflake, etc.) can reuse it later."""
    from fluid_build.copilot.store.factory import _safe_store_init

    def boom() -> Any:
        raise RuntimeError("primary unavailable")

    def fb() -> Any:
        return FileBackend(root=tmp_path / "fb-root", workspace_root=tmp_path)

    with caplog.at_level(logging.WARNING):
        store = _safe_store_init(boom, fallback_factory=fb, label="ThingThatFails")
    assert isinstance(store, FileBackend)


def test_safe_store_init_returns_null_when_no_fallback() -> None:
    """No fallback factory + primary failure → NullBackend so the
    forge can still complete (better than crashing on init)."""
    from fluid_build.copilot.store.factory import _safe_store_init

    def boom() -> Any:
        raise RuntimeError("primary unavailable")

    store = _safe_store_init(boom, label="NoFallback")
    assert isinstance(store, NullBackend)
