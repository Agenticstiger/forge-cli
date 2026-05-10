# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Engine-uniformity UX tests.

For every engine asserts: (a) the runner's class attributes follow the
public Protocol; (b) ``execute_<engine>_build`` returns 0 on dry-run for a
minimum-viable contract; (c) on bogus input every engine returns a
non-zero exit code rather than raising; (d) the run-record JSON shape is
the same across engines (state / records_total / facets.engine).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.api.runner import RunnerCapability

# ── One engine entry per test row ──────────────────────────────────────


@pytest.fixture(
    scope="module",
    params=[
        "duckdb",
        "dlt",
        "meltano",
        "airbyte",
        "kafka-connect",
        "debezium",
    ],
)
def engine_name(request) -> str:
    return request.param


def _runner_module(engine: str):
    return importlib.import_module(f"fluid_build.build_runners.{engine.replace('-', '_')}.runner")


def _execute_fn(engine: str):
    module = _runner_module(engine)
    return getattr(module, f"execute_{engine.replace('-', '_')}_build")


def _runner_class(engine: str):
    module = _runner_module(engine)
    cls_name = {
        "duckdb": "DuckdbRunner",
        "dlt": "DltRunner",
        "meltano": "MeltanoRunner",
        "airbyte": "AirbyteRunner",
        "kafka-connect": "KafkaConnectRunner",
        "debezium": "DebeziumRunner",
    }[engine]
    return getattr(module, cls_name)


def _minimal_contract(engine: str) -> Dict[str, Any]:
    source = {"kind": "filesystem", "connection": {"uri": "/tmp/x.csv"}, "mode": "full_refresh"}
    if engine in ("kafka-connect", "debezium"):
        source = {
            "kind": "postgres",
            "connection": {
                "host": "x",
                "port": 5432,
                "database": "x",
                "user": "u",
                "password": "p",
            },
            "mode": "cdc" if engine == "debezium" else "streaming",
            "streams": ["public.x"],
        }
    elif engine == "airbyte":
        source = {"kind": "faker", "connection": {}, "mode": "full_refresh", "streams": ["users"]}
    elif engine == "meltano":
        source = {"kind": "fake-fluid", "connection": {}, "mode": "full_refresh", "streams": ["x"]}

    engine_block: Dict[str, Any] = {"deployment": {"mode": "embedded"}}
    if engine in ("kafka-connect", "debezium", "airbyte"):
        engine_block["deployment"] = {
            "mode": "bring-your-own",
            "server_url": "http://x.test:8083",
        }
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": f"bronze.{engine.replace('-', '_')}",
        "name": f"{engine} test",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": engine,
                "capabilities": [
                    (
                        "cdc"
                        if engine == "debezium"
                        else "streaming" if engine == "kafka-connect" else "full_refresh"
                    )
                ],
                "properties": {
                    "source": source,
                    "sink": {"format": "parquet"},
                    engine: engine_block,
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [
            {
                "exposeId": "data",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "out.duckdb"},
                },
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }


# ── Runner Protocol surface ────────────────────────────────────────────


class TestRunnerProtocolUniform:
    def test_runner_has_name(self, engine_name: str):
        cls = _runner_class(engine_name)
        assert cls.name == engine_name

    def test_runner_declares_capabilities(self, engine_name: str):
        cls = _runner_class(engine_name)
        assert isinstance(cls.declared_capabilities, frozenset)
        assert len(cls.declared_capabilities) > 0
        for cap in cls.declared_capabilities:
            assert isinstance(cap, RunnerCapability)

    def test_runner_declares_modes(self, engine_name: str):
        cls = _runner_class(engine_name)
        assert isinstance(cls.declared_modes, frozenset)
        assert cls.declared_modes <= {"embedded", "bring-your-own", "managed"}

    def test_runner_methods_present(self, engine_name: str):
        cls = _runner_class(engine_name)
        for method in ("plan", "run", "replay", "fingerprint"):
            assert hasattr(cls, method)
            assert callable(getattr(cls, method))


# ── Dry-run uniformity ─────────────────────────────────────────────────


class TestDryRunUniform:
    def test_dry_run_returns_zero(self, engine_name: str, tmp_path: Path):
        contract = _minimal_contract(engine_name)
        rc = _execute_fn(engine_name)(
            contract["builds"][0], contract, tmp_path, dry_run=True, sample_rows=None
        )
        assert rc == 0, f"{engine_name} dry-run did not return 0"


# ── Failure uniformity ────────────────────────────────────────────────


class TestFailureUniform:
    def test_missing_source_returns_nonzero(self, engine_name: str, tmp_path: Path):
        contract = _minimal_contract(engine_name)
        del contract["builds"][0]["properties"]["source"]
        rc = _execute_fn(engine_name)(
            contract["builds"][0], contract, tmp_path, dry_run=False, sample_rows=None
        )
        assert rc != 0, f"{engine_name} failed to surface missing-source error"


# ── Run-record shape uniformity ────────────────────────────────────────


class TestRunRecordShapeUniform:
    """When a runner persists a run record, the JSON shape must include
    ``run_id``, ``state``, ``records_total``, ``facets.engine``, and
    ``streams`` (for engines that have stream-level granularity).
    """

    def test_duckdb_record_shape(self, tmp_path: Path):
        # Use a real CSV so DuckDB succeeds and writes a record.
        in_csv = tmp_path / "in" / "x.csv"
        in_csv.parent.mkdir(parents=True)
        in_csv.write_text("id,name\n1,A\n", encoding="utf-8")
        contract = _minimal_contract("duckdb")
        contract["builds"][0]["properties"]["source"]["connection"]["uri"] = str(in_csv)
        contract["builds"][0]["properties"]["source"]["reader"] = {
            "format": "csv",
            "options": {"header": True},
        }
        contract["exposes"][0]["binding"]["location"]["path"] = str(tmp_path / "out.parquet")
        from fluid_build.build_runners.duckdb.runner import execute_duckdb_build

        execute_duckdb_build(contract["builds"][0], contract, tmp_path, dry_run=False)
        runs_dir = tmp_path / ".fluid" / "runs" / contract["id"] / "ingest" / "runs"
        records = list(runs_dir.glob("*.json"))
        assert records, "duckdb run record not persisted"
        rec = json.loads(records[0].read_text())
        for required in ("run_id", "state", "records_total", "facets", "streams"):
            assert required in rec, f"duckdb record missing {required}"
        assert rec["facets"]["engine"] == "duckdb"
