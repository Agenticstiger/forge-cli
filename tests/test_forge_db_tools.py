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

"""Pins for the opt-in ``fetch_sample_rows`` live-DB agent tool.

Covers:
  * the FLUID_FORGE_DB_TOOLS gate (tool ABSENT when unset, PRESENT when =1 —
    mirrors the dbt-MCP / web-tools gates);
  * env-sourced connection resolution (the LLM never supplies a URI/creds);
  * the typed "not configured" result when no connection env is set;
  * a real duckdb+sqlite round-trip proving a capped, redacted sample;
  * the row cap (LLM-supplied limit is clamped to the hard max);
  * output redaction — credential-shaped VALUES and credential-named COLUMNS
    are masked before the sample reaches the model;
  * identifier validation (a non-identifier table name is refused, no SQL run);
  * the forge_copilot_tools integration (surface + route).

The sqlite path needs no network: duckdb's bundled sqlite extension attaches a
local file. Postgres/MySQL share the exact same code path (only the URI scheme
and duckdb extension differ), so the sqlite round-trip exercises the whole tool.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

from fluid_build.cli import forge_copilot_tools, forge_db_tools
from fluid_build.cli.forge_db_tools import (
    TOOL_NAME,
    db_tool_definitions,
    dispatch_db_tool,
    is_db_tool,
    is_enabled,
)

_ON = {"FLUID_FORGE_DB_TOOLS": "1"}


def _make_sqlite(tmp_path: Path) -> str:
    """Create a small sqlite db and return a ``sqlite:///`` URI for it."""
    db = tmp_path / "sample.sqlite"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE customers (id INTEGER, name TEXT, notes TEXT, password TEXT)")
    con.executemany(
        "INSERT INTO customers VALUES (?, ?, ?, ?)",
        [
            (1, "Alice", "key is sk-ant-api03-" + "A" * 40, "hunter2plaintext"),
            (2, "Bob", "nothing secret here", "swordfish-secret-value"),
            (3, "Carol", "ok", "correct-horse-battery"),
        ],
    )
    con.commit()
    con.close()
    # sqlite:/// + an absolute path (starts with /) yields the sqlite://// form
    # that _parse_uri decodes back to the absolute path.
    return f"sqlite:///{db}"


# ── gate ─────────────────────────────────────────────────────────────────────
class TestGate:
    def test_is_enabled_reads_env_flag(self):
        assert is_enabled({"FLUID_FORGE_DB_TOOLS": "1"}) is True
        assert is_enabled({"FLUID_FORGE_DB_TOOLS": "true"}) is True
        assert is_enabled({"FLUID_FORGE_DB_TOOLS": "0"}) is False
        assert is_enabled({}) is False

    def test_is_db_tool_requires_name_and_enabled(self):
        assert is_db_tool(TOOL_NAME, _ON) is True
        assert is_db_tool(TOOL_NAME, {}) is False  # disabled
        assert is_db_tool("propose_contract", _ON) is False  # not a db tool

    def test_definitions_empty_when_disabled(self):
        assert db_tool_definitions(env={}) == []

    def test_definitions_present_when_enabled(self):
        defs = db_tool_definitions(env=_ON)
        by_name = {d["name"]: d for d in defs}
        assert set(by_name) == {TOOL_NAME}
        schema = by_name[TOOL_NAME]["input_schema"]
        assert schema["properties"]["table"]["type"] == "string"
        assert schema["additionalProperties"] is False


# ── get_tool_definitions integration ─────────────────────────────────────────
class TestToolListingIntegration:
    def test_absent_when_unset(self, monkeypatch):
        monkeypatch.delenv("FLUID_FORGE_DB_TOOLS", raising=False)
        names = {t["name"] for t in forge_copilot_tools.get_tool_definitions()}
        assert TOOL_NAME not in names

    def test_present_when_enabled(self, monkeypatch):
        monkeypatch.setenv("FLUID_FORGE_DB_TOOLS", "1")
        names = {t["name"] for t in forge_copilot_tools.get_tool_definitions()}
        assert TOOL_NAME in names


# ── connection resolution ────────────────────────────────────────────────────
class TestConnectionResolution:
    def test_not_configured_when_no_env(self):
        out = dispatch_db_tool(TOOL_NAME, {"table": "customers"}, env=dict(_ON))
        assert out["error"] == "DatabaseNotConfigured"

    def test_missing_table_typed_error(self, tmp_path):
        env = {**_ON, "FLUID_FORGE_DB_URI": _make_sqlite(tmp_path)}
        out = dispatch_db_tool(TOOL_NAME, {"table": "   "}, env=env)
        assert out["error"] == "InvalidArgs"

    def test_named_connection_env(self, tmp_path):
        env = {**_ON, "FLUID_FORGE_DB_URI_ANALYTICS": _make_sqlite(tmp_path)}
        out = dispatch_db_tool(
            TOOL_NAME, {"table": "customers", "connection": "analytics"}, env=env
        )
        assert "rows" in out
        assert out["connection"] == "analytics"

    def test_bad_connection_name_rejected(self, tmp_path):
        env = {**_ON, "FLUID_FORGE_DB_URI": _make_sqlite(tmp_path)}
        out = dispatch_db_tool(TOOL_NAME, {"table": "customers", "connection": "../etc"}, env=env)
        assert out["error"] == "InvalidConnection"


# ── the real duckdb+sqlite round-trip ────────────────────────────────────────
class TestFetchSampleRows:
    def test_happy_path_capped_and_shaped(self, tmp_path):
        env = {**_ON, "FLUID_FORGE_DB_URI": _make_sqlite(tmp_path)}
        out = dispatch_db_tool(TOOL_NAME, {"table": "customers", "limit": 2}, env=env)
        assert out["columns"] == ["id", "name", "notes", "password"]
        assert out["row_count"] == 2
        assert len(out["rows"]) == 2
        # Non-secret scalar values survive.
        assert out["rows"][0][0] == 1
        assert out["rows"][0][1] == "Alice"

    def test_limit_clamped_to_hard_max(self, tmp_path):
        env = {**_ON, "FLUID_FORGE_DB_URI": _make_sqlite(tmp_path)}
        out = dispatch_db_tool(TOOL_NAME, {"table": "customers", "limit": 10_000}, env=env)
        # Only 3 rows exist; the point is the effective limit is the hard cap,
        # surfaced so the model knows the sample is bounded.
        assert out["applied_limit"] == forge_db_tools.MAX_ROWS
        assert out["row_count"] <= forge_db_tools.MAX_ROWS

    def test_secret_shaped_value_is_redacted(self, tmp_path):
        env = {**_ON, "FLUID_FORGE_DB_URI": _make_sqlite(tmp_path)}
        out = dispatch_db_tool(TOOL_NAME, {"table": "customers", "limit": 1}, env=env)
        notes = out["rows"][0][2]
        assert "sk-ant-api03-" + "A" * 40 not in notes
        assert "REDACTED" in notes

    def test_sensitive_named_column_fully_masked(self, tmp_path):
        env = {**_ON, "FLUID_FORGE_DB_URI": _make_sqlite(tmp_path)}
        out = dispatch_db_tool(TOOL_NAME, {"table": "customers", "limit": 3}, env=env)
        pw_idx = out["columns"].index("password")
        for row in out["rows"]:
            # Every value in a credential-named column is wholesale-masked,
            # even when the raw value doesn't match a known token shape.
            assert row[pw_idx] == "***REDACTED***"
            assert "hunter2plaintext" != row[pw_idx]

    def test_non_identifier_table_refused_no_sql(self, tmp_path):
        env = {**_ON, "FLUID_FORGE_DB_URI": _make_sqlite(tmp_path)}
        out = dispatch_db_tool(TOOL_NAME, {"table": "customers; DROP TABLE customers"}, env=env)
        assert out["error"] == "InvalidIdentifier"

    def test_unknown_table_typed_error_no_leak(self, tmp_path):
        env = {**_ON, "FLUID_FORGE_DB_URI": _make_sqlite(tmp_path)}
        out = dispatch_db_tool(TOOL_NAME, {"table": "does_not_exist"}, env=env)
        assert "error" in out
        # No raw duckdb exception text (which can echo paths / the DSN).
        assert "see server logs" in out["message"]


# ── dispatch_tool_call integration (the real wiring) ─────────────────────────
class TestDispatchIntegration:
    def test_routes_when_enabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLUID_FORGE_DB_TOOLS", "1")
        monkeypatch.setenv("FLUID_FORGE_DB_URI", _make_sqlite(tmp_path))
        out = forge_copilot_tools.dispatch_tool_call(TOOL_NAME, {"table": "customers", "limit": 1})
        assert out["row_count"] == 1

    def test_unknown_when_disabled(self, monkeypatch):
        monkeypatch.delenv("FLUID_FORGE_DB_TOOLS", raising=False)
        out = forge_copilot_tools.dispatch_tool_call(TOOL_NAME, {"table": "customers"})
        assert "error" in out and "Unknown tool" in out["error"]
