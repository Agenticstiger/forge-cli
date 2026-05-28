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

"""Tests for the audit_trail.write_audit_event dual-write strategy.

MEMORY-E2E-A finding #54: until this slice ``write_audit_event``
only wrote files under ``~/.fluid/store/audit/`` and the configured
:class:`Store` backend (Postgres / Sqlite / Vector) never saw a
single event. The fix routes through ``store.put`` in addition to
the file write so the AUDIT namespace stops being dead weight.

Contract pinned by these tests:

* When ``FLUID_STORE_BACKEND=sqlite``, ``write_audit_event`` (called
  with no explicit ``root``) writes BOTH a file under the default
  audit dir AND a record under the ``audit`` namespace in the
  configured store.
* When the store backend is the file default (``FLUID_STORE_BACKEND``
  unset or ``file``), the Store hop is skipped — the file write is
  the only path and there's no double-up.
* When ``root`` is passed explicitly (sandboxed callers, tests),
  the Store hop is skipped — the caller is asking for file-only
  isolation.
* Store errors are swallowed — a flaky DSN never breaks audit
  capture; the file write still lands.
* The key shape is chronologically sortable and includes the event
  name so on-disk + in-store ordering match.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fluid_build.copilot.store.audit_trail import _build_event_key, write_audit_event
from fluid_build.copilot.store.backends.sqlite import SqliteBackend
from fluid_build.copilot.store.namespaces import AUDIT_NAMESPACE

# ── key shape ─────────────────────────────────────────────────────────


class TestEventKeyShape:
    def test_key_carries_event_name_and_iso_prefix(self):
        from datetime import datetime, timezone

        when = datetime(2026, 5, 27, 12, 34, 56, tzinfo=timezone.utc)
        key = _build_event_key("mcp_update_entity", when)
        # Lexicographically sortable timestamp prefix.
        assert key.startswith("20260527T123456Z_")
        # Event name preserved (slashes flattened — see comment in source).
        assert key.endswith("_mcp_update_entity")

    def test_slashes_in_event_name_are_flattened(self):
        from datetime import datetime, timezone

        when = datetime(2026, 5, 27, 0, 0, 0, tzinfo=timezone.utc)
        key = _build_event_key("forge/data-model/from-intent", when)
        # Source flattens "/" → "_" so FileBackend's path mapping
        # doesn't blow up.
        assert "/" not in key

    def test_naive_datetime_treated_as_utc(self):
        from datetime import datetime

        when = datetime(2026, 5, 27, 0, 0, 0)  # naive
        key = _build_event_key("x", when)
        assert key.startswith("20260527T000000Z_")


# ── Store-routing behavior ────────────────────────────────────────────


class TestStoreRouting:
    def test_sqlite_backend_receives_audit_event(self, monkeypatch, tmp_path: Path):
        # Wire the SQLite store via env vars exactly the way
        # ``resolve_store`` would. ``write_audit_event`` is then
        # called with NO explicit root so the dual-write path fires.
        db_path = tmp_path / "store.sqlite3"
        # We can't use a custom default audit root because the
        # canonical fallback lands under ~/.fluid; use a tmp HOME so
        # the file write doesn't pollute the user's machine either.
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("FLUID_STORE_BACKEND", "sqlite")
        monkeypatch.setenv("FLUID_STORE_PATH", str(db_path))

        path = write_audit_event(
            "mcp_update_entity",
            payload={"path": "contract.fluid.yaml", "entity": "orders"},
        )
        # File write still lands (canonical fallback).
        assert path.exists()
        assert path.suffix == ".json"

        # Store mirror written under AUDIT_NAMESPACE.
        store = SqliteBackend(path=db_path)
        records = store.query(AUDIT_NAMESPACE, limit=10)
        assert len(records) == 1
        record = records[0]
        assert record.namespace == AUDIT_NAMESPACE
        assert record.value["event"] == "mcp_update_entity"
        assert record.value["payload"] == {
            "path": "contract.fluid.yaml",
            "entity": "orders",
        }
        # Metadata mirrors event + timestamp for downstream filters.
        assert record.metadata.get("event") == "mcp_update_entity"
        assert record.metadata.get("timestamp_utc")

    def test_explicit_root_skips_store_hop(self, monkeypatch, tmp_path: Path):
        # When the caller passes ``root=tmp_path`` (the standard test
        # isolation pattern), we honour that contract and DON'T fan
        # out to the user's global store — that would surprise the
        # caller and pollute their machine.
        db_path = tmp_path / "store.sqlite3"
        monkeypatch.setenv("FLUID_STORE_BACKEND", "sqlite")
        monkeypatch.setenv("FLUID_STORE_PATH", str(db_path))

        path = write_audit_event(
            "test_event",
            payload={"i": 1},
            root=tmp_path / "audit",
        )
        assert path.exists()

        # No DB file should have been created — the store hop was skipped.
        assert not db_path.exists()

    def test_default_file_backend_does_not_double_write(self, monkeypatch, tmp_path: Path):
        # FileBackend would just duplicate the file write we already
        # do; routing through it is wasted I/O. Verify the env-unset
        # path skips Store entirely.
        monkeypatch.delenv("FLUID_STORE_BACKEND", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        # Patch resolve_store so any accidental dispatch raises loudly.
        called = {"yes": False}

        def _boom(*a, **k):
            called["yes"] = True
            raise AssertionError("resolve_store must not be invoked on default file backend")

        monkeypatch.setattr("fluid_build.copilot.store.factory.resolve_store", _boom)

        path = write_audit_event("file_only", payload={"i": 1})
        assert path.exists()
        assert called["yes"] is False

    def test_null_backend_selector_skips_store_hop(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "null")
        monkeypatch.setenv("HOME", str(tmp_path / "home"))

        # Same guard as above — null selector means "do nothing", not
        # "resolve the null backend and put". We can spare the import.
        called = {"yes": False}

        def _boom(*a, **k):
            called["yes"] = True
            raise AssertionError("resolve_store must not be invoked on null backend")

        monkeypatch.setattr("fluid_build.copilot.store.factory.resolve_store", _boom)

        path = write_audit_event("null_event", payload={"i": 1})
        assert path.exists()
        assert called["yes"] is False

    def test_store_failure_is_swallowed_file_still_lands(self, monkeypatch, tmp_path: Path):
        # Simulate a broken Store backend (bad DSN, transient outage).
        # The file write must still succeed and no exception should
        # propagate from ``write_audit_event``.
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("FLUID_STORE_BACKEND", "postgres")
        # Bad DSN — psycopg.connect will raise inside resolve_store.
        monkeypatch.setenv("FLUID_STORE_DSN", "postgresql://no-such-host:1/nodb")

        path = write_audit_event("postgres_should_fail", payload={"i": 1})
        assert path.exists()
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["event"] == "postgres_should_fail"

    def test_audit_namespace_constant_matches_routing_target(self):
        # The audit_trail module uses a private string ``_AUDIT_NAMESPACE``
        # to avoid an import cycle. Pin that the value matches the
        # canonical constant exported by ``copilot.store.namespaces``.
        from fluid_build.copilot.store import audit_trail

        assert audit_trail._AUDIT_NAMESPACE == AUDIT_NAMESPACE


# ── round-trip ────────────────────────────────────────────────────────


class TestRoundTripWithReportGenerator:
    def test_file_artefact_still_readable_by_existing_report_generator(
        self, monkeypatch, tmp_path: Path
    ):
        # The AuditReportGenerator walks the on-disk audit dir. After
        # the dual-write change, the file artefact must still match
        # the format the generator already parses.
        from fluid_build.copilot.store.audit_trail import AuditReportGenerator

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        # Unset backend so we don't try to talk to a store.
        monkeypatch.delenv("FLUID_STORE_BACKEND", raising=False)

        write_audit_event("roundtrip_check", payload={"k": "v"})
        # Default audit root lives under HOME/.fluid/store/audit/.
        report = AuditReportGenerator(
            root=tmp_path / "home" / ".fluid" / "store" / "audit"
        ).generate_report()
        assert len(report.events) == 1
        assert report.events[0].event == "roundtrip_check"
        assert report.events[0].payload == {"k": "v"}


# ── No accidental double-write on store success ───────────────────────


class TestNoDoubleWrite:
    def test_each_event_produces_exactly_one_file_and_one_store_row(
        self, monkeypatch, tmp_path: Path
    ):
        db_path = tmp_path / "store.sqlite3"
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("FLUID_STORE_BACKEND", "sqlite")
        monkeypatch.setenv("FLUID_STORE_PATH", str(db_path))

        for i in range(3):
            write_audit_event(f"event_{i}", payload={"i": i})

        # On-disk: one file per call.
        audit_dir = tmp_path / "home" / ".fluid" / "store" / "audit"
        on_disk = list(audit_dir.glob("*.json"))
        assert len(on_disk) == 3

        # Store: one row per call.
        store = SqliteBackend(path=db_path)
        rows = store.query(AUDIT_NAMESPACE, limit=10)
        assert len(rows) == 3
        events = sorted(row.value["event"] for row in rows)
        assert events == ["event_0", "event_1", "event_2"]
