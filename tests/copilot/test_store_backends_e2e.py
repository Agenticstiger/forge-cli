# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""End-to-end coverage for staged-store backends that shipped in v1.1+.

The earlier ``test_store_factory.py`` covers resolution + a smoke
round-trip. This suite exercises the shape the real pipeline hits:

* SqliteBackend — TTL expiry, metadata round-trip, cross-namespace
  clear, fluid_version tagging round-trip.
* Cross-backend — same (put, get) invariants hold for
  FileBackend / SqliteBackend so the pipeline can swap without
  regressions.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path

import pytest

from fluid_build.copilot.store.backends.file import FileBackend
from fluid_build.copilot.store.backends.sqlite import SqliteBackend
from fluid_build.copilot.store.factory import resolve_store

# ---------------------------------------------------------------------------
# SqliteBackend
# ---------------------------------------------------------------------------


class TestSqliteBackendE2E:
    def _make(self, tmp_path: Path) -> SqliteBackend:
        return SqliteBackend(path=tmp_path / "e2e.sqlite3")

    def test_put_get_roundtrip_preserves_value_metadata_and_version(self, tmp_path: Path):
        store = self._make(tmp_path)
        value = {"technique": "dimensional", "facts": ["fact_order_line"]}
        store.put(
            "llm/logical",
            "cache-key-xyz",
            value,
            metadata={"model": "gemini-2.5-pro", "stage": "logical"},
            fluid_version="0.7.2",
        )

        record = store.get("llm/logical", "cache-key-xyz")
        assert record is not None
        assert record.value == value
        assert record.metadata == {"model": "gemini-2.5-pro", "stage": "logical"}
        assert record.fluid_version == "0.7.2"

    def test_ttl_expiry_clears_stale_record_on_get(self, tmp_path: Path):
        store = self._make(tmp_path)
        store.put("llm/logical", "shortlived", {"x": 1}, ttl=1)

        # Not expired yet.
        assert store.get("llm/logical", "shortlived") is not None

        # Force-expire by writing a tz-aware past timestamp via python so the
        # round-trip matches the production put path (which uses
        # ``utc_now().isoformat()`` — tz-aware). Using raw SQLite's
        # ``datetime('now', ...)`` produces a naive string that fails to
        # compare against ``utc_now()``; that would be a test-only bug.
        from datetime import timedelta

        from fluid_build.copilot.store.base import utc_now

        past = (utc_now() - timedelta(seconds=10)).isoformat()
        store.conn.execute(
            "update store set expires_at = ? where namespace = ? and key = ?",
            (past, "llm/logical", "shortlived"),
        )
        store.conn.commit()

        assert store.get("llm/logical", "shortlived") is None

    def test_clear_by_root_namespace_returns_count_across_children(self, tmp_path: Path):
        store = self._make(tmp_path)
        store.put("memory/project", "p", {"x": 1})
        store.put("memory/team", "t", {"x": 2})
        store.put("memory/personal", "u", {"x": 3})
        store.put("llm/logical", "l", {"x": 4})  # different root

        cleared = store.clear("memory")
        assert cleared == 3, "clear(root_ns) must cascade to child namespaces"
        assert store.get("memory/project", "p") is None
        # Sibling root untouched.
        assert store.get("llm/logical", "l") is not None

    def test_query_limit_is_respected(self, tmp_path: Path):
        store = self._make(tmp_path)
        for i in range(5):
            store.put("memory/team", f"k{i}", {"i": i})
        results = store.query("memory/team", limit=3)
        assert len(results) == 3


# ---------------------------------------------------------------------------
# Cross-backend invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(
            lambda p: FileBackend(root=p / "file-store", workspace_root=p),
            id="file",
        ),
        pytest.param(lambda p: SqliteBackend(path=p / "invariant.sqlite3"), id="sqlite"),
    ],
)
def test_backend_roundtrip_invariant(make, tmp_path: Path):
    """Same put / get round-trip must hold across backends."""
    store = make(tmp_path)
    payload = {"technique": "data_vault_2", "hubs": ["hub_customer"]}
    store.put("llm/logical", "rt-key", payload, metadata={"stage": "logical"})
    record = store.get("llm/logical", "rt-key")
    assert record is not None
    # JSON round-trip is acceptable for any backend (some serialise).
    assert json.loads(json.dumps(record.value)) == payload


# ---------------------------------------------------------------------------
# Factory end-to-end (checks env-var driven resolution)
# ---------------------------------------------------------------------------


def test_factory_null_backend_silently_no_ops(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FLUID_STORE_BACKEND", "null")
    store = resolve_store(workspace_root=tmp_path)
    store.put("memory/project", "k", {"x": 1})
    assert store.get("memory/project", "k") is None  # never persisted
