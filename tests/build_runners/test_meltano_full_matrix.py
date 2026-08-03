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

"""Meltano (Singer protocol) engine — full matrix (Slice D).

Synthetic ``tap-fluid-fake`` covers the protocol-level matrix without
needing pipx-installed real taps; an env-gated integration test exists
for ``tap-postgres`` against Testcontainers Postgres.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.api.runner import RunnerCapability
from fluid_build.build_runners.meltano.runner import (
    MeltanoRunner,
    _resolve_tap_binary,
    collect_singer_output,
    execute_meltano_build,
    invoke_tap,
    stream_singer_messages,
    write_records_to_duckdb,
)
from tests._infrastructure.singer_fixtures import fake_singer_tap  # noqa: F401

# ── Helpers ──────────────────────────────────────────────────────────────


def _base_contract(
    *,
    source: Dict[str, Any],
    meltano_props: Dict[str, Any] = None,
    duckdb_path: str = None,
) -> Dict[str, Any]:
    expose: Dict[str, Any] = {
        "exposeId": "data",
        "kind": "table",
        "binding": {"platform": "local", "format": "parquet"},
        "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
    }
    if duckdb_path is not None:
        expose["binding"]["location"] = {"path": duckdb_path}
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.meltano_test",
        "name": "Meltano Test",
        "metadata": {
            "layer": "Bronze",
            "owner": {"team": "data-platform", "email": "dp@co.example"},
        },
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "meltano",
                "capabilities": ["full_refresh"],
                "properties": {
                    "source": source,
                    "sink": {"format": "parquet"},
                    "meltano": meltano_props or {},
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [expose],
    }


def _count_table_rows(duckdb_path: Path, dataset: str, table: str) -> int:
    import duckdb

    if not duckdb_path.exists():
        return 0
    con = duckdb.connect(str(duckdb_path))
    try:
        try:
            cur = con.execute(f"SELECT COUNT(*) FROM {dataset}.{table}").fetchone()
            return int(cur[0]) if cur else 0
        except Exception:
            return 0
    finally:
        con.close()


# ── Singer protocol parser ─────────────────────────────────────────────


class TestSingerProtocolParser:
    def test_stream_messages_filters_blanks(self):
        lines = [
            '{"type":"SCHEMA","stream":"x"}',
            "",
            '{"type":"RECORD","stream":"x","record":{"a":1}}',
        ]
        msgs = list(stream_singer_messages(iter(lines)))
        assert len(msgs) == 2

    def test_collect_groups_by_stream(self):
        msgs = [
            {"type": "SCHEMA", "stream": "orders", "schema": {}},
            {"type": "RECORD", "stream": "orders", "record": {"id": 1}},
            {"type": "RECORD", "stream": "orders", "record": {"id": 2}},
            {"type": "STATE", "value": {"bookmarks": {"orders": {"last_id": 2}}}},
        ]
        out = collect_singer_output(iter(msgs))
        assert "orders" in out["schemas"]
        assert len(out["records"]["orders"]) == 2
        assert out["state"]["bookmarks"]["orders"]["last_id"] == 2

    def test_collect_handles_no_state(self):
        msgs = [
            {"type": "SCHEMA", "stream": "x", "schema": {}},
            {"type": "RECORD", "stream": "x", "record": {"a": 1}},
        ]
        out = collect_singer_output(iter(msgs))
        assert out["state"] == {}

    def test_bad_json_line_is_skipped(self):
        # The function logs a warning but doesn't raise.
        msgs = list(stream_singer_messages(iter(["not json", '{"type":"STATE","value":{}}'])))
        assert len(msgs) == 1


# ── Tap discovery ───────────────────────────────────────────────────────


class TestTapDiscovery:
    def test_resolve_finds_on_path(self, fake_singer_tap):
        binary = _resolve_tap_binary("fluid-fake")
        assert binary == fake_singer_tap["binary"]

    def test_resolve_returns_none_when_missing(self):
        assert _resolve_tap_binary("nonexistent-tap-xyz") is None

    def test_resolve_accepts_already_prefixed_name(self, fake_singer_tap):
        binary = _resolve_tap_binary("tap-fluid-fake")
        assert binary == fake_singer_tap["binary"]


# ── Tap invocation ──────────────────────────────────────────────────────


class TestInvokeTap:
    def test_invoke_with_default_config(self, fake_singer_tap, tmp_path: Path):
        result = invoke_tap(
            fake_singer_tap["binary"],
            config={"n_records": 5},
            workdir=tmp_path / "tap_workdir",
        )
        assert result["exit_code"] == 0
        assert len(result["records"]["orders"]) == 5
        assert result["state"]["bookmarks"]["orders"]["last_id"] == 5

    def test_invoke_multiple_streams(self, fake_singer_tap, tmp_path: Path):
        result = invoke_tap(
            fake_singer_tap["binary"],
            config={"n_records": 2, "streams": ["alpha", "beta"]},
            workdir=tmp_path / "wd",
        )
        assert "alpha" in result["records"] and len(result["records"]["alpha"]) == 2
        assert "beta" in result["records"] and len(result["records"]["beta"]) == 2

    def test_invoke_with_state(self, fake_singer_tap, tmp_path: Path):
        result = invoke_tap(
            fake_singer_tap["binary"],
            config={"n_records": 3},
            state={"bookmarks": {"orders": {"last_id": 100}}},
            workdir=tmp_path / "wd",
        )
        assert result["state"]["bookmarks"]["orders"]["last_id"] == 3

    def test_invoke_failure(self, fake_singer_tap, tmp_path: Path):
        result = invoke_tap(
            fake_singer_tap["binary"],
            config={"fail": True},
            workdir=tmp_path / "wd",
        )
        assert result["exit_code"] != 0
        assert "simulated tap failure" in result["stderr"]


# ── Built-in target → DuckDB ───────────────────────────────────────────


class TestDuckdbTarget:
    def test_writes_records(self, tmp_path: Path):
        records = {
            "orders": [
                {"id": 1, "amount": 10.5},
                {"id": 2, "amount": 20.0},
                {"id": 3, "amount": None},
            ]
        }
        out = tmp_path / "out.duckdb"
        counts = write_records_to_duckdb(records, duckdb_path=out, dataset="bronze")
        assert counts["orders"] == 3
        assert _count_table_rows(out, "bronze", "orders") == 3

    def test_empty_stream_creates_empty_table(self, tmp_path: Path):
        out = tmp_path / "out.duckdb"
        counts = write_records_to_duckdb({"empty": []}, duckdb_path=out, dataset="bronze")
        assert counts["empty"] == 0

    def test_special_characters_in_values(self, tmp_path: Path):
        records = {"x": [{"id": 1, "label": "O'Brien"}, {"id": 2, "label": "value with 'quotes'"}]}
        out = tmp_path / "out.duckdb"
        counts = write_records_to_duckdb(records, duckdb_path=out)
        assert counts["x"] == 2


# ── Capability declarations ────────────────────────────────────────────


class TestCapabilityDeclarations:
    def test_runner_class_attributes(self):
        r = MeltanoRunner()
        assert r.name == "meltano"
        assert "embedded" in r.declared_modes
        assert "bring-your-own" in r.declared_modes
        assert RunnerCapability.FULL_REFRESH in r.declared_capabilities
        assert RunnerCapability.INCREMENTAL_APPEND in r.declared_capabilities
        assert RunnerCapability.INCREMENTAL_DEDUP in r.declared_capabilities


# ── End-to-end via the dispatcher ──────────────────────────────────────


class TestMeltanoDispatcher:
    def test_full_refresh_via_fake_tap(self, fake_singer_tap, tmp_path: Path):
        out = tmp_path / "out.duckdb"
        contract = _base_contract(
            source={
                "kind": "fluid-fake",
                "connection": {"n_records": 4},
                "mode": "full_refresh",
                "streams": ["orders"],
            },
            meltano_props={"tap": "tap-fluid-fake", "dataset_name": "bronze"},
            duckdb_path=str(out),
        )
        rc = execute_meltano_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        assert _count_table_rows(out, "bronze", "orders") == 4

    def test_dry_run_does_not_invoke_tap(self, fake_singer_tap, tmp_path: Path):
        out = tmp_path / "out.duckdb"
        contract = _base_contract(
            source={
                "kind": "fluid-fake",
                "connection": {"n_records": 4},
                "mode": "full_refresh",
                "streams": ["orders"],
            },
            meltano_props={"tap": "tap-fluid-fake"},
            duckdb_path=str(out),
        )
        rc = execute_meltano_build(contract["builds"][0], contract, tmp_path, dry_run=True)
        assert rc == 0
        assert not out.exists()

    def test_incremental_append_writes_state(self, fake_singer_tap, tmp_path: Path):
        out = tmp_path / "out.duckdb"
        contract = _base_contract(
            source={
                "kind": "fluid-fake",
                "connection": {"n_records": 3},
                "mode": "incremental_append",
                "streams": ["orders"],
            },
            meltano_props={"tap": "tap-fluid-fake"},
            duckdb_path=str(out),
        )
        rc = execute_meltano_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc == 0
        # Cursor file persisted under .fluid/runs/.../cursors/_singer.json.
        cursor_file = (
            tmp_path
            / ".fluid"
            / "runs"
            / contract["id"]
            / contract["builds"][0]["id"]
            / "cursors"
            / "_singer.json"
        )
        assert cursor_file.exists()
        cur = json.loads(cursor_file.read_text())
        assert cur["value"]["bookmarks"]["orders"]["last_id"] == 3

    def test_sample_rows_truncates(self, fake_singer_tap, tmp_path: Path):
        out = tmp_path / "out.duckdb"
        contract = _base_contract(
            source={
                "kind": "fluid-fake",
                "connection": {"n_records": 10},
                "mode": "full_refresh",
                "streams": ["orders"],
            },
            meltano_props={"tap": "tap-fluid-fake"},
            duckdb_path=str(out),
        )
        rc = execute_meltano_build(
            contract["builds"][0], contract, tmp_path, dry_run=False, sample_rows=2
        )
        assert rc == 0
        assert _count_table_rows(out, "bronze", "orders") == 2

    def test_tap_failure_returns_nonzero(self, fake_singer_tap, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "fluid-fake",
                "connection": {"fail": True},
                "mode": "full_refresh",
                "streams": ["x"],
            },
            meltano_props={"tap": "tap-fluid-fake"},
            duckdb_path=str(tmp_path / "out.duckdb"),
        )
        rc = execute_meltano_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0

    def test_missing_tap_binary_fails_cleanly(self, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "doesnotexist",
                "connection": {},
                "mode": "full_refresh",
                "streams": ["x"],
            },
            meltano_props={"tap": "tap-doesnotexist-xyz"},
            duckdb_path=str(tmp_path / "out.duckdb"),
        )
        rc = execute_meltano_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        assert rc != 0

    def test_run_record_persisted(self, fake_singer_tap, tmp_path: Path):
        contract = _base_contract(
            source={
                "kind": "fluid-fake",
                "connection": {"n_records": 2},
                "mode": "full_refresh",
                "streams": ["orders"],
            },
            meltano_props={"tap": "tap-fluid-fake"},
            duckdb_path=str(tmp_path / "out.duckdb"),
        )
        execute_meltano_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        runs = list(
            (tmp_path / ".fluid" / "runs" / contract["id"] / "ingest" / "runs").glob("*.json")
        )
        assert len(runs) == 1
        rec = json.loads(runs[0].read_text())
        assert rec["facets"]["engine"] == "meltano"


# ── Base dispatcher integration ────────────────────────────────────────


class TestMeltanoBaseDispatcher:
    def test_base_dispatches_to_meltano(self, fake_singer_tap, tmp_path: Path):
        from fluid_build.build_runners.base import (
            ACQUISITION_ENGINES,
            _execute_acquisition_build,
            is_acquisition_build,
        )

        assert "meltano" in ACQUISITION_ENGINES
        contract = _base_contract(
            source={
                "kind": "fluid-fake",
                "connection": {"n_records": 3},
                "mode": "full_refresh",
                "streams": ["orders"],
            },
            meltano_props={"tap": "tap-fluid-fake"},
            duckdb_path=str(tmp_path / "out.duckdb"),
        )
        build = contract["builds"][0]
        assert is_acquisition_build(build)
        rc = _execute_acquisition_build(build, contract, tmp_path, dry_run=False, sample_rows=None)
        assert rc == 0
