# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""PR5 — KC runner correctness from the OSS spike: task-level failure detection
(§14B) + fq_table late-events naming (§6.7)."""

from __future__ import annotations

from pathlib import Path

import pytest

from fluid_build.build_runners.kafka_connect.runner import (
    _poll_connector_health,
    execute_kafka_connect_build,
)

pytestmark = [pytest.mark.unit]


class _FakeClient:
    def __init__(self, status):
        self._status = status

    def get_status(self, name):
        return self._status


# ── task-level failure detection (§14B) ─────────────────────────────────────


def test_poll_health_connector_and_task_running_is_ok():
    client = _FakeClient(
        {"connector": {"state": "RUNNING"}, "tasks": [{"id": 0, "state": "RUNNING"}]}
    )
    state, ok, traces = _poll_connector_health(client, "n", timeout=1.0, interval=0.01)
    assert state == "RUNNING"
    assert ok is True
    assert traces == []


def test_poll_health_running_connector_failed_task_is_not_ok():
    # the exact spike scenario: connector RUNNING while the task is FAILED
    client = _FakeClient(
        {
            "connector": {"state": "RUNNING"},
            "tasks": [{"id": 0, "state": "FAILED", "trace": "NoSuchNamespace"}],
        }
    )
    state, ok, traces = _poll_connector_health(client, "n", timeout=1.0, interval=0.01)
    assert state == "RUNNING"
    assert ok is False  # would have been reported as success before this fix
    assert traces == ["NoSuchNamespace"]


def test_poll_health_failed_connector_is_not_ok():
    client = _FakeClient({"connector": {"state": "FAILED"}, "tasks": []})
    _, ok, _ = _poll_connector_health(client, "n", timeout=1.0, interval=0.01)
    assert ok is False


# ── fq_table late-events naming (§6.7), gated to the Iceberg path ───────────


def _wm_contract(sink_format="iceberg"):
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.kc_wm",
        "name": "X",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "kafka-connect",
                "capabilities": ["streaming"],
                "properties": {
                    "source": {
                        "kind": "postgres",
                        "connection": {
                            "host": "db",
                            "port": 5432,
                            "database": "x",
                            "user": "u",
                            "password": "p",
                        },
                        "mode": "incremental_append",
                        "streams": ["public.events"],
                        "watermark": {"strategy": "high_water_mark", "allowedLateness": "PT5M"},
                    },
                    "sink": {"format": sink_format},
                    "kafka-connect": {
                        "deployment": {"server_url": "http://kafka-connect.test:8083"},
                        "connector_name": "src",
                    },
                },
                "outputs": ["events"],
            }
        ],
        "exposes": [
            {
                "exposeId": "events",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg" if sink_format == "iceberg" else "parquet",
                    "location": {"database": "bronze", "table": "events", "bucket": "lake"},
                },
                "contract": {"schema": []},
            }
        ],
    }


def test_iceberg_late_events_side_table_uses_fq_table(kafka_connect_mock, tmp_path: Path):
    contract = _wm_contract(sink_format="iceberg")
    rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc == 0
    cfg = kafka_connect_mock.connectors["src"]["config"]
    assert cfg["fluid.late_arrival.side_output_table"] == "bronze.events__late_events"


def test_non_iceberg_late_events_keeps_connector_named(kafka_connect_mock, tmp_path: Path):
    contract = _wm_contract(sink_format="parquet")
    rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc == 0
    cfg = kafka_connect_mock.connectors["src"]["config"]
    side = cfg["fluid.late_arrival.side_output_table"]
    assert side.endswith("__late_events")
    assert "bronze.events" not in side  # back-compat: connector-named, not fq_table
