# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Day-2 operations — full matrix (Slice L).

Status × logs × run-diff × retention × doctor × auth — all six surfaces
get exhaustive unit tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.api.runner import RunState
from fluid_build.api.state import Cursor
from fluid_build.build_runners._state import FileStateStore
from fluid_build.cli.ops.auth import (
    AuthResult,
    InMemoryBackend,
    login,
    rotate,
    verify_secret,
)
from fluid_build.cli.ops.doctor import DoctorScope, Severity, run_doctor
from fluid_build.cli.ops.logs import LogComponent, fetch_logs
from fluid_build.cli.ops.retention import sweep_with_summary
from fluid_build.cli.ops.run_diff import run_diff
from fluid_build.cli.ops.status import build_status_report

# ── Status ──────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_runs(
    store: FileStateStore,
    *,
    product_id: str = "bronze.x",
    build_id: str = "ingest",
    states: List[str],
    finished_offsets_minutes: List[int] = None,
) -> List[str]:
    """Seed N runs with the given states. ``finished_offsets_minutes`` lets
    callers control freshness/error-rate tests. Returns run-ids in write
    order (which is also the chronological order, oldest first).
    """
    finished_offsets_minutes = finished_offsets_minutes or [0] * len(states)
    now = datetime.now(timezone.utc)
    run_ids: List[str] = []
    # Order states by recency (smallest offset first = newest first) so the
    # newest run gets the highest run-id and sorts first under reverse-name
    # ordering used by FileStateStore.list_runs.
    indexed = sorted(
        list(enumerate(zip(states, finished_offsets_minutes))),
        key=lambda t: t[1][1],
        reverse=True,
    )
    # ``indexed[0]`` is the OLDEST entry (largest offset) — gets the lowest
    # numeric run-id; ``indexed[-1]`` is the newest — gets the highest.
    for rank, (_orig_i, (state, offset)) in enumerate(indexed):
        finished = (now - timedelta(minutes=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_id = f"r{rank:05d}"
        store.write_run_record(
            product_id,
            build_id,
            {
                "run_id": run_id,
                "state": state,
                "started_at": finished,
                "finished_at": finished,
                "records_total": 100 if state == "succeeded" else 0,
                "facets": {"engine": "duckdb", "duration_seconds": 1.5},
                "streams": [
                    {"name": "orders", "state": state, "records": 100, "duration_seconds": 1.5}
                ],
                "error": None if state == "succeeded" else f"failure-{_orig_i}",
            },
        )
        run_ids.append(run_id)
    return run_ids


class TestStatusReport:
    def test_orders_runs_newest_first(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        # Three runs at offsets 30 / 20 / 10 minutes — third is newest.
        _seed_runs(
            store,
            states=["succeeded", "succeeded", "failed"],
            finished_offsets_minutes=[30, 20, 10],
        )
        report = build_status_report(store, "bronze.x", "ingest", limit=3)
        assert len(report.runs) == 3
        # The newest seeded run gets the highest numeric run-id; reverse-name
        # sort puts it first.
        assert report.runs[0].run_id == "r00002"
        # And it must be the "failed" state since that was the freshest.
        assert report.runs[0].state == "failed"

    def test_freshness_uses_latest_succeeded_run(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        # Most recent run was 5 minutes ago and succeeded.
        _seed_runs(
            store,
            states=["succeeded", "failed", "succeeded"],
            finished_offsets_minutes=[60, 30, 5],
        )
        report = build_status_report(store, "bronze.x", "ingest", limit=10)
        # Freshness should be ~5 minutes (300 s).
        assert report.freshness_seconds is not None
        assert 250 < report.freshness_seconds < 350

    def test_error_rate_24h(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        _seed_runs(
            store,
            states=["succeeded", "failed", "failed", "succeeded"],
            finished_offsets_minutes=[10, 10, 10, 10],
        )
        report = build_status_report(store, "bronze.x", "ingest", limit=10)
        # 2/4 failures (failed counts; partial counts; succeeded does not).
        assert report.error_rate_24h == 0.5

    def test_no_runs_yields_empty_report(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        report = build_status_report(store, "bronze.x", "ingest", limit=5)
        assert report.runs == []
        assert report.freshness_seconds is None
        assert report.error_rate_24h == 0.0

    def test_streams_carry_through(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        _seed_runs(store, states=["succeeded"])
        report = build_status_report(store, "bronze.x", "ingest", limit=1)
        assert report.runs[0].streams[0]["name"] == "orders"


# ── Logs ────────────────────────────────────────────────────────────────


class TestFetchLogs:
    def test_no_logs_returns_empty(self, tmp_path: Path):
        result = fetch_logs(tmp_path, "bronze.x", component=LogComponent.BUILD)
        assert result == []

    def test_reads_build_log_lines(self, tmp_path: Path):
        log_dir = tmp_path / "logs" / "bronze.x" / "ingest"
        log_dir.mkdir(parents=True)
        (log_dir / "build.log").write_text(
            json.dumps({"timestamp": "2026-01-01T00:00:00Z", "level": "INFO", "message": "started"})
            + "\n"
            + json.dumps({"timestamp": "2026-01-01T00:00:01Z", "level": "INFO", "message": "done"}),
            encoding="utf-8",
        )
        result = fetch_logs(tmp_path, "bronze.x", component=LogComponent.BUILD)
        assert len(result) == 2
        assert result[0].message == "started"
        assert result[1].message == "done"

    def test_grep_filters_messages(self, tmp_path: Path):
        log_dir = tmp_path / "logs" / "bronze.x" / "ingest"
        log_dir.mkdir(parents=True)
        (log_dir / "build.log").write_text("INFO start\nERROR boom\nINFO done\n", encoding="utf-8")
        result = fetch_logs(tmp_path, "bronze.x", component=LogComponent.BUILD, grep="ERROR")
        assert len(result) == 1
        assert "boom" in result[0].message

    def test_dlq_component_reads_ndjson(self, tmp_path: Path):
        dlq = tmp_path / "dlq" / "run-1"
        dlq.mkdir(parents=True)
        (dlq / "orders.ndjson").write_text(
            json.dumps({"reason": "schema_violation", "record": {"id": 1}}) + "\n",
            encoding="utf-8",
        )
        result = fetch_logs(tmp_path, "bronze.x", component=LogComponent.DLQ, run_id="run-1")
        assert len(result) == 1
        assert "schema_violation" in result[0].message

    def test_dlq_requires_run_id(self, tmp_path: Path):
        result = fetch_logs(tmp_path, "bronze.x", component=LogComponent.DLQ)
        assert result == []

    def test_plain_text_logs_passthrough(self, tmp_path: Path):
        log_dir = tmp_path / "logs" / "bronze.x" / "ingest"
        log_dir.mkdir(parents=True)
        (log_dir / "build.log").write_text("not json\nstill not json\n", encoding="utf-8")
        result = fetch_logs(tmp_path, "bronze.x", component=LogComponent.BUILD)
        assert len(result) == 2
        assert result[0].message == "not json"


# ── Run diff ───────────────────────────────────────────────────────────


class TestRunDiff:
    def test_diff_records_total(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        store.write_run_record(
            "bronze.x",
            "ingest",
            {
                "run_id": "rA",
                "state": "succeeded",
                "started_at": _now_iso(),
                "finished_at": _now_iso(),
                "records_total": 100,
                "facets": {"duration_seconds": 1.0},
                "streams": [{"name": "orders", "records": 100}],
            },
        )
        store.write_run_record(
            "bronze.x",
            "ingest",
            {
                "run_id": "rB",
                "state": "succeeded",
                "started_at": _now_iso(),
                "finished_at": _now_iso(),
                "records_total": 250,
                "facets": {"duration_seconds": 1.5},
                "streams": [{"name": "orders", "records": 250}],
            },
        )
        diff = run_diff(store, "bronze.x", "ingest", run_a="rA", run_b="rB")
        assert diff.records_delta == 150
        assert diff.duration_delta == pytest.approx(0.5, rel=0.1)
        assert len(diff.streams) == 1
        assert diff.streams[0].delta == 150

    def test_diff_handles_added_and_removed_streams(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        store.write_run_record(
            "bronze.x",
            "ingest",
            {
                "run_id": "rA",
                "state": "succeeded",
                "streams": [
                    {"name": "orders", "records": 10},
                    {"name": "removed_stream", "records": 5},
                ],
            },
        )
        store.write_run_record(
            "bronze.x",
            "ingest",
            {
                "run_id": "rB",
                "state": "succeeded",
                "streams": [
                    {"name": "orders", "records": 20},
                    {"name": "new_stream", "records": 3},
                ],
            },
        )
        diff = run_diff(store, "bronze.x", "ingest", run_a="rA", run_b="rB")
        names = {s.name for s in diff.streams}
        assert names == {"orders", "removed_stream", "new_stream"}
        removed = next(s for s in diff.streams if s.name == "removed_stream")
        assert removed.records_b == 0 and removed.delta == -5

    def test_diff_missing_run_returns_zeroes(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        diff = run_diff(store, "bronze.x", "ingest", run_a="missing-a", run_b="missing-b")
        assert diff.records_delta == 0
        assert diff.streams == []


# ── Retention ──────────────────────────────────────────────────────────


class TestRetentionSweep:
    def test_sweep_removes_aged_files(self, tmp_path: Path, monkeypatch):
        # Create files in the layout the sweeper expects.
        runs = tmp_path / "runs" / "bronze.x" / "ingest" / "runs"
        runs.mkdir(parents=True)
        old = runs / "old.json"
        old.write_text("{}", encoding="utf-8")
        # Mark file as 60 days old.
        import os
        import time

        sixty_days_ago = time.time() - 60 * 24 * 3600
        os.utime(old, (sixty_days_ago, sixty_days_ago))
        # And a fresh file we expect to keep.
        fresh = runs / "fresh.json"
        fresh.write_text("{}", encoding="utf-8")
        summary = sweep_with_summary(tmp_path)
        # Default runState retention is P30D — old file deleted, fresh kept.
        assert old.exists() is False
        assert fresh.exists() is True
        assert summary.by_category["run_state"] == 1
        assert summary.bytes_freed >= 0


# ── Doctor ─────────────────────────────────────────────────────────────


class TestDoctor:
    def test_authoring_scope_passes(self):
        report = run_doctor(DoctorScope.AUTHORING)
        assert report.scope is DoctorScope.AUTHORING
        # Latest schema is 0.7.3, must be OK.
        assert report.ok, [r.detail for r in report.errors]

    def test_pipeline_scope_passes(self):
        report = run_doctor(DoctorScope.PIPELINE)
        # All six runner modules must import.
        assert all(r.severity is Severity.OK for r in report.results), [
            (r.name, r.severity, r.detail) for r in report.results
        ]

    def test_ingestion_scope_passes_for_installed_extras(self):
        report = run_doctor(DoctorScope.INGESTION)
        # duckdb / dlt / httpx are installed in the dev venv.
        oks = [r for r in report.results if r.severity is Severity.OK]
        assert len(oks) >= 3

    def test_infra_scope_warns_for_missing_binaries(self):
        report = run_doctor(DoctorScope.INFRA)
        # Either binaries are present (OK) or missing (WARN); never ERROR.
        assert all(r.severity in (Severity.OK, Severity.WARN) for r in report.results)

    def test_catalog_scope_passes(self):
        report = run_doctor(DoctorScope.CATALOG)
        assert all(r.severity is Severity.OK for r in report.results)

    def test_all_scope_runs_every_check(self):
        report = run_doctor(DoctorScope.ALL)
        # ALL scope yields more checks than any single scope.
        per_scope_counts = []
        for s in (
            DoctorScope.AUTHORING,
            DoctorScope.PIPELINE,
            DoctorScope.INGESTION,
            DoctorScope.INFRA,
            DoctorScope.CATALOG,
        ):
            per_scope_counts.append(len(run_doctor(s).results))
        assert len(report.results) == sum(per_scope_counts)


# ── Auth ───────────────────────────────────────────────────────────────


class TestAuth:
    def test_login_persists_secret(self):
        backend = InMemoryBackend()
        result = login(
            "vault://x/y",
            obtain_secret=lambda: "secret-value",
            backend=backend,
        )
        assert result.success
        assert backend.fetch("vault://x/y") == "secret-value"

    def test_login_failure_when_obtain_raises(self):
        backend = InMemoryBackend()

        def bad():
            raise RuntimeError("oauth flow cancelled")

        result = login("vault://x/y", obtain_secret=bad, backend=backend)
        assert not result.success
        assert "oauth flow cancelled" in (result.detail or "")

    def test_login_failure_when_obtain_returns_empty(self):
        backend = InMemoryBackend()
        result = login("vault://x/y", obtain_secret=lambda: "", backend=backend)
        assert not result.success

    def test_test_secret_when_present(self):
        backend = InMemoryBackend()
        backend.store("vault://x", "value")
        result = verify_secret("vault://x", backend=backend)
        assert result.success

    def test_test_secret_runs_probe(self):
        backend = InMemoryBackend()
        backend.store("vault://x", "good")
        good_probe = lambda s: s == "good"
        bad_probe = lambda s: s == "WRONG"
        assert verify_secret("vault://x", backend=backend, probe=good_probe).success
        assert not verify_secret("vault://x", backend=backend, probe=bad_probe).success

    def test_test_secret_missing(self):
        backend = InMemoryBackend()
        result = verify_secret("vault://nope", backend=backend)
        assert not result.success
        assert "not found" in (result.detail or "")

    def test_rotate_replaces_only_after_probe_passes(self):
        backend = InMemoryBackend()
        backend.store("vault://x", "old")
        # New secret rejected — old must remain.
        result = rotate("vault://x", new_secret="bad", backend=backend, probe=lambda _: False)
        assert not result.success
        assert backend.fetch("vault://x") == "old"

    def test_rotate_replaces_when_probe_passes(self):
        backend = InMemoryBackend()
        backend.store("vault://x", "old")
        result = rotate("vault://x", new_secret="new", backend=backend, probe=lambda _: True)
        assert result.success
        assert backend.fetch("vault://x") == "new"

    def test_rotate_without_probe_just_overwrites(self):
        backend = InMemoryBackend()
        backend.store("vault://x", "old")
        result = rotate("vault://x", new_secret="new", backend=backend)
        assert result.success
        assert backend.fetch("vault://x") == "new"

    def test_rotate_when_no_prior_secret_indicates_in_detail(self):
        backend = InMemoryBackend()
        result = rotate("vault://fresh", new_secret="newsec", backend=backend)
        assert result.success
        assert "no prior" in (result.detail or "").lower()
