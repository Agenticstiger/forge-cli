# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""The duckdb acquisition runner must emit OpenLineage like every other engine.

PR #467 wired emission at the three chokepoints
(``begin_acquisition_run`` / ``write_run_record_and_finalize`` /
``emit_terminal_lineage_event``) on the premise that all six acquisition
runners pass through them. duckdb did not — it inlined the run-opening
sequence and called ``write_run_record`` + ``finalize_run_result``
separately — so the **default** engine, and the one used in the shipped
example contract, emitted nothing at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.api.conformance.runner import assert_openlineage_shape
from fluid_build.api.lineage import RunEventType
from fluid_build.build_runners._lineage import BufferedLineageEmitter, encode_event
from fluid_build.build_runners.duckdb.runner import execute_duckdb_build

pytestmark = pytest.mark.unit


def _contract(in_path: Path, out_path: Path) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.lineage_test",
        "name": "Lineage Test",
        "metadata": {
            "layer": "Bronze",
            "owner": {"team": "data-platform", "email": "dp@co.example"},
        },
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "duckdb",
                "capabilities": ["full_refresh"],
                "properties": {
                    "source": {
                        "kind": "filesystem",
                        "connection": {"uri": str(in_path)},
                        "mode": "full_refresh",
                        "reader": {"format": "csv", "options": {"header": True}},
                    },
                    "sink": {"format": "parquet"},
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
                    "location": {"path": str(out_path)},
                },
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }


def _run(tmp_path: Path, monkeypatch, *, stream_override: str | None = None):
    """Run one duckdb acquisition with a buffered emitter installed."""
    in_path = tmp_path / "in" / "data.csv"
    in_path.parent.mkdir(parents=True, exist_ok=True)
    in_path.write_text("id,name\n1,a\n2,b\n", encoding="utf-8")
    out_path = tmp_path / "out" / "data.parquet"

    contract = _contract(in_path, out_path)
    if stream_override is not None:
        contract["builds"][0]["properties"]["source"]["connection"]["uri"] = stream_override

    buffered = BufferedLineageEmitter()
    import fluid_build.build_runners._lineage as lin

    monkeypatch.setattr(lin, "resolve_lineage_emitter", lambda: buffered)

    code = execute_duckdb_build(
        contract["builds"][0], contract, tmp_path, state_root=tmp_path / ".fluid"
    )
    return code, buffered.events


def test_successful_run_emits_start_and_complete(tmp_path, monkeypatch):
    code, events = _run(tmp_path, monkeypatch)
    assert code == 0
    assert [e.event_type for e in events] == [RunEventType.START, RunEventType.COMPLETE]


def test_failed_run_emits_start_and_fail(tmp_path, monkeypatch):
    code, events = _run(tmp_path, monkeypatch, stream_override=str(tmp_path / "nope.csv"))
    assert code == 1
    assert [e.event_type for e in events] == [RunEventType.START, RunEventType.FAIL]


def test_emitted_events_are_spec_conformant(tmp_path, monkeypatch):
    _, events = _run(tmp_path, monkeypatch)
    for event in events:
        assert_openlineage_shape(encode_event(event))


def test_run_id_is_stable_across_the_pair(tmp_path, monkeypatch):
    _, events = _run(tmp_path, monkeypatch)
    run_ids = {encode_event(e)["run"]["runId"] for e in events}
    assert len(run_ids) == 1, f"START and COMPLETE must share one runId, got {run_ids}"


def test_dry_run_emits_nothing(tmp_path, monkeypatch):
    """``--dry-run`` plans only; there is no run to report lineage for."""
    in_path = tmp_path / "in" / "data.csv"
    in_path.parent.mkdir(parents=True, exist_ok=True)
    in_path.write_text("id,name\n1,a\n", encoding="utf-8")
    contract = _contract(in_path, tmp_path / "out" / "data.parquet")

    buffered = BufferedLineageEmitter()
    import fluid_build.build_runners._lineage as lin

    monkeypatch.setattr(lin, "resolve_lineage_emitter", lambda: buffered)
    code = execute_duckdb_build(
        contract["builds"][0],
        contract,
        tmp_path,
        dry_run=True,
        state_root=tmp_path / ".fluid",
    )
    assert code == 0
    assert buffered.events == []
