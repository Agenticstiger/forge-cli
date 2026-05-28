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

"""Tests for the ``fluid doctor`` store-backend inspector.

MEMORY-E2E-A finding #55: operators had no way to confirm which
memory-store backend was actually wired (``FLUID_STORE_BACKEND``
silently propagates without a status line). ``_inspect_store_backend``
+ ``_print_store_backend_status`` close that gap; this test file
pins the contract:

* ``_inspect_store_backend`` returns a dict keyed by ``env`` /
  ``backend`` / ``class`` / ``location`` / ``status`` / ``ok``.
* Each supported backend selector (file / sqlite / postgres /
  vector / null / unknown) renders the right class name and a
  bounded status string — no exceptions escape.
* ``run(args)`` surfaces the section in the default doctor output
  and the JSON contract on ``--json`` only carries documented keys.
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fluid_build.cli import doctor

LOG = logging.getLogger(__name__)


# ── _inspect_store_backend ────────────────────────────────────────────


class TestInspectStoreBackend:
    def test_unset_env_treated_as_file_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FLUID_STORE_BACKEND", raising=False)
        monkeypatch.setenv("FLUID_STORE_ROOT", str(tmp_path / "store"))
        info = doctor._inspect_store_backend()
        assert info["env"] == "(unset)"
        assert info["backend"] == "file"
        assert info["class"] == "FileBackend"
        assert info["location"] == str(tmp_path / "store")
        # Path missing is OK — auto-create on first write.
        assert info["ok"] == "true"

    def test_file_backend_ready_when_dir_exists(self, monkeypatch, tmp_path):
        root = tmp_path / "store"
        root.mkdir()
        monkeypatch.setenv("FLUID_STORE_BACKEND", "file")
        monkeypatch.setenv("FLUID_STORE_ROOT", str(root))
        info = doctor._inspect_store_backend()
        assert info["class"] == "FileBackend"
        assert info["status"].startswith("ready")
        assert info["ok"] == "true"

    def test_sqlite_backend_probes_user_version_pragma(self, monkeypatch, tmp_path):
        db_path = tmp_path / "store.sqlite3"
        # Pre-create the DB so the inspector hits the live PRAGMA probe
        # rather than the "file missing" branch.
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("PRAGMA user_version = 42")
            conn.commit()
        finally:
            conn.close()
        monkeypatch.setenv("FLUID_STORE_BACKEND", "sqlite")
        monkeypatch.setenv("FLUID_STORE_PATH", str(db_path))
        info = doctor._inspect_store_backend()
        assert info["class"] == "SqliteBackend"
        assert info["location"] == str(db_path)
        assert info["schema_version"] == "42"
        assert info["status"].startswith("reachable")
        assert info["ok"] == "true"

    def test_sqlite_backend_handles_missing_file(self, monkeypatch, tmp_path):
        db_path = tmp_path / "missing.sqlite3"
        monkeypatch.setenv("FLUID_STORE_BACKEND", "sqlite")
        monkeypatch.setenv("FLUID_STORE_PATH", str(db_path))
        info = doctor._inspect_store_backend()
        assert info["class"] == "SqliteBackend"
        assert "missing" in info["status"]
        # Missing file is non-fatal — auto-create.
        assert info["ok"] == "true"

    def test_postgres_backend_without_dsn_reports_not_ok(self, monkeypatch):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "postgres")
        monkeypatch.delenv("FLUID_STORE_DSN", raising=False)
        info = doctor._inspect_store_backend()
        assert info["class"] == "PostgresBackend"
        assert info["ok"] == "false"
        assert "DSN missing" in info["status"]
        # DSN field must be redacted-shape even when missing —
        # never a raw connection string in the visible payload.
        assert info["location"].startswith("(") or "***" in info["location"]

    def test_postgres_backend_with_bad_dsn_does_not_raise(self, monkeypatch):
        # Use a bogus DSN; psycopg.connect will throw — our wrapper
        # must swallow into ``ok=false`` rather than crashing doctor.
        monkeypatch.setenv("FLUID_STORE_BACKEND", "postgres")
        monkeypatch.setenv(
            "FLUID_STORE_DSN",
            "postgresql://postgres:wrong@127.0.0.1:1/nodb",
        )
        info = doctor._inspect_store_backend()
        assert info["class"] == "PostgresBackend"
        # Either psycopg unavailable OR connect failed — both flow to ok=false.
        assert info["ok"] == "false"
        assert info["status"]  # non-empty
        # Password must never leak into the visible location.
        assert "wrong" not in info["location"]

    def test_vector_backend_describes_backing_store(self, monkeypatch):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "vector")
        monkeypatch.setenv("FLUID_STORE_VECTOR_BACKING", "sqlite")
        info = doctor._inspect_store_backend()
        assert info["class"] == "VectorBackend"
        assert "backing=sqlite" in info["location"]
        assert info["ok"] == "true"

    def test_null_backend_reports_no_persistence(self, monkeypatch):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "null")
        info = doctor._inspect_store_backend()
        assert info["class"] == "NullBackend"
        assert info["location"] == "(no persistence)"
        assert info["ok"] == "true"

    def test_unknown_backend_renders_friendly_error(self, monkeypatch):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "made-up-backend")
        info = doctor._inspect_store_backend()
        assert info["ok"] == "false"
        assert "unrecognised" in info["status"].lower()
        assert info["class"] == "(unknown)"

    def test_required_keys_present_on_every_branch(self, monkeypatch):
        for backend in ("file", "sqlite", "null", "vector", "made-up"):
            monkeypatch.setenv("FLUID_STORE_BACKEND", backend)
            info = doctor._inspect_store_backend()
            for key in ("env", "backend", "class", "location", "status", "ok"):
                assert key in info, f"missing key {key!r} from inspector output for {backend}"
            assert info["ok"] in {"true", "false"}


# ── _print_store_backend_status ───────────────────────────────────────


class TestPrintStoreBackendStatus:
    def test_renders_status_class_and_location(self, capsys, monkeypatch):
        info = {
            "env": "sqlite",
            "backend": "sqlite",
            "class": "SqliteBackend",
            "location": "/tmp/foo.sqlite3",
            "status": "reachable (PRAGMA ok)",
            "ok": "true",
            "schema_version": "0",
        }
        doctor._print_store_backend_status(info)
        out = capsys.readouterr().out
        # Title shows.
        assert "Store Backend" in out or "Memory Store" in out
        # All required fields surface.
        assert "SqliteBackend" in out
        assert "/tmp/foo.sqlite3" in out
        assert "reachable" in out

    def test_renders_warning_icon_when_ok_false(self, capsys):
        info = {
            "env": "postgres",
            "backend": "postgres",
            "class": "PostgresBackend",
            "location": "(FLUID_STORE_DSN unset)",
            "status": "DSN missing — PostgresBackend cannot connect",
            "ok": "false",
            "schema_version": "",
        }
        doctor._print_store_backend_status(info)
        out = capsys.readouterr().out
        # Action-needed icon must be visible.
        assert "Action needed" in out or "⚠" in out


# ── run() dispatch ────────────────────────────────────────────────────


class TestRunStoreBackendSection:
    def test_default_run_includes_store_backend_section(self, monkeypatch, capsys):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "file")
        # Stub the heavier downstream — we're only verifying the store
        # section threads through ``run``.
        monkeypatch.setattr(doctor, "_check_fluid_features", lambda: (True, []))
        monkeypatch.setattr(
            doctor,
            "_check_copilot_readiness",
            lambda: SimpleNamespace(
                ready=True,
                provider="x",
                model="y",
                endpoint="z",
                auth_available=True,
                error=None,
            ),
        )
        monkeypatch.setattr(doctor, "_resolve_extended_diagnostic_script", lambda: None)
        monkeypatch.setattr(doctor, "_print_doctor_summary", lambda **kw: None)
        monkeypatch.setattr(doctor, "_print_copilot_readiness", lambda *a, **k: None)
        monkeypatch.setattr(doctor, "_print_doctor_next_steps", lambda **kw: None)
        args = SimpleNamespace(
            env=False,
            json=False,
            scope=None,
            features_only=False,
            extended=False,
            verbose=False,
        )
        rc = doctor.run(args, LOG)
        assert rc == 0
        out = capsys.readouterr().out
        # FileBackend label or the section title — Rich rendering may
        # split into multiple lines.
        assert "FileBackend" in out or "Store" in out

    def test_run_with_json_emits_store_backend_payload(self, monkeypatch):
        monkeypatch.setenv("FLUID_STORE_BACKEND", "file")
        monkeypatch.setattr(doctor, "_check_fluid_features", lambda: (True, []))
        monkeypatch.setattr(
            doctor,
            "_check_copilot_readiness",
            lambda: SimpleNamespace(
                ready=True,
                provider="x",
                model="y",
                endpoint="z",
                auth_available=True,
                error=None,
            ),
        )
        args = SimpleNamespace(
            env=False,
            json=True,
            scope=None,
            features_only=False,
            extended=False,
            verbose=False,
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = doctor.run(args, LOG)
        assert rc == 0
        payload = json.loads(buf.getvalue())
        assert "store_backend" in payload
        sb = payload["store_backend"]
        assert {"env", "backend", "class", "location", "status", "ok"} <= sb.keys()
        assert sb["backend"] == "file"
        assert sb["class"] == "FileBackend"

    def test_run_json_does_not_dispatch_to_scope_or_env(self, monkeypatch):
        # The default --json path must NOT slip into the --scope/--env
        # helpers; those carry their own JSON contracts. Pin that by
        # ensuring those helpers stay unmocked-but-uncalled.
        called = {"scope": False, "env": False}

        def _fake_scope(*a, **k):
            called["scope"] = True
            return 0

        def _fake_env(*a, **k):
            called["env"] = True
            return 0

        monkeypatch.setattr(doctor, "_run_scoped", _fake_scope)
        monkeypatch.setattr(doctor, "_run_env_listing", _fake_env)
        monkeypatch.setattr(doctor, "_check_fluid_features", lambda: (True, []))
        monkeypatch.setattr(
            doctor,
            "_check_copilot_readiness",
            lambda: SimpleNamespace(
                ready=True,
                provider="x",
                model="y",
                endpoint="z",
                auth_available=True,
                error=None,
            ),
        )
        args = SimpleNamespace(
            env=False,
            json=True,
            scope=None,
            features_only=False,
            extended=False,
            verbose=False,
        )
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            doctor.run(args, LOG)
        assert called == {"scope": False, "env": False}


# ── Schema-version probe correctness ──────────────────────────────────


class TestSchemaVersionProbe:
    def test_sqlite_schema_version_zero_when_pragma_unset(self, monkeypatch, tmp_path: Path):
        # Fresh SQLite file with no user_version set returns 0 — make
        # sure the inspector renders that as a string rather than
        # silently dropping the field.
        db_path = tmp_path / "fresh.sqlite3"
        sqlite3.connect(str(db_path)).close()
        monkeypatch.setenv("FLUID_STORE_BACKEND", "sqlite")
        monkeypatch.setenv("FLUID_STORE_PATH", str(db_path))
        info = doctor._inspect_store_backend()
        assert info["schema_version"] == "0"
