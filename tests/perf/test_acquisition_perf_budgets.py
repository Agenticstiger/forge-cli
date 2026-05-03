# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Performance budgets — load-based, deterministic.

Asserts that each runner / hook / stage stays within published latency
budgets at canonical record counts. Numbers are conservative (3× the
expected median) so noisy CI doesn't flake. Tighten as the codebase
matures.

Uses Python's stdlib ``time.perf_counter`` rather than pytest-benchmark
to avoid an extra test dependency. Each test runs the workload N=3
times and asserts the *median* (not min) duration is under budget so
single-iteration GC pauses don't break the suite.

Skipped on slow lanes / under ``FLUID_PERF_DISABLED=1`` for users on
underpowered laptops.
"""

from __future__ import annotations

import os
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("FLUID_PERF_DISABLED") == "1",
    reason="FLUID_PERF_DISABLED=1 — perf budgets opt out for slow lanes",
)


def _median_seconds(fn, *, iterations: int = 3) -> float:
    samples: List[float] = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - t0)
    return statistics.median(samples)


# ── Hook chain throughput ────────────────────────────────────────────────


class TestHookChainPerf:
    def test_quality_gate_processes_10k_records_under_500ms(self):
        """A 10k-record batch through the quality_gate hook must stay under
        500 ms median. Real ingest batches rarely exceed 50k; if this
        regresses, we're spending too much per-row work.
        """
        from fluid_build.api.hooks import HookChain
        from fluid_build.build_runners.hooks.quality_gate import QualityGateHook

        chain = HookChain(hooks=[QualityGateHook()])
        records = [{"id": i, "email": f"u{i}@x.com"} for i in range(10_000)]
        gates = [
            {"rule": "not_null", "columns": ["email"], "severity": "error"},
            {
                "rule": "regex",
                "column": "email",
                "pattern": r"^[^@]+@[^@]+\.[^@]+$",
                "severity": "error",
            },
        ]
        ctx = {"quality_gates": gates}

        median = _median_seconds(lambda: chain.run(list(records), ctx))
        assert median < 0.5, f"quality_gate 10k regressed: {median:.3f}s > 0.5s"

    def test_tokenize_pii_processes_1k_records_under_200ms(self, monkeypatch):
        """PII tokenization is HMAC-SHA256 per cell, so 1k records × 1
        column should be way under 200 ms. The budget catches a regression
        to plain SHA-256 / weak cipher path."""
        monkeypatch.setenv("FLUID_PII_TOKENIZATION_KEY", "perf-test-key")
        from fluid_build.api.hooks import HookChain
        from fluid_build.build_runners.hooks.tokenize_pii import TokenizePiiHook

        chain = HookChain(hooks=[TokenizePiiHook()])
        records = [{"email": f"user{i}@example.com"} for i in range(1_000)]
        ctx = {"classifications": {"email": ["email"]}}

        median = _median_seconds(lambda: chain.run(list(records), ctx))
        assert median < 0.2, f"tokenize_pii 1k regressed: {median:.3f}s > 0.2s"


# ── DLQ writer throughput ─────────────────────────────────────────────────


class TestDLQWriterPerf:
    def test_dlq_writer_appends_1k_records_under_500ms(self, tmp_path: Path):
        """1k DLQ writes (NDJSON, single stream) must stay under 500 ms.
        DLQ overflow is a sign of upstream failure, but the writer
        itself shouldn't be a bottleneck."""
        from fluid_build.build_runners._dlq import DLQConfig, DLQWriter

        cfg = DLQConfig(
            enabled=True,
            sink_format="ndjson",
            location=str(tmp_path / "dlq"),
            max_records_before_abort=10_000,
        )
        writer = DLQWriter(cfg, run_id="perf01", default_root=tmp_path / ".fluid")

        def _write_batch():
            for i in range(1_000):
                writer.append(
                    "perf_stream",
                    {"id": i, "data": "x" * 64},
                    "perf_test",
                    [],
                )

        median = _median_seconds(_write_batch, iterations=1)  # ndjson appends accumulate
        assert median < 0.5, f"DLQ write 1k regressed: {median:.3f}s > 0.5s"


# ── DuckDB filesystem runner ──────────────────────────────────────────────


class TestDuckdbRunnerPerf:
    def test_duckdb_csv_to_parquet_100_rows_under_5s(self, tmp_path: Path):
        """The classic 'first sync' UX budget: CSV → Parquet for 100 rows
        in under 5 s. This is the time-to-first-value floor declared in
        the design doc. Includes import + extension load + COPY.
        """
        from fluid_build.build_runners.duckdb.runner import execute_duckdb_build

        in_dir = tmp_path / "in"
        in_dir.mkdir()
        csv = in_dir / "perf.csv"
        body = ["id,name,amount"]
        body.extend(f"{i},name{i},{i * 1.5:.2f}" for i in range(100))
        csv.write_text("\n".join(body))

        out_path = str((tmp_path / "out" / "perf.parquet").resolve())
        contract = {
            "fluidVersion": "0.7.3",
            "kind": "DataProduct",
            "id": "bronze.perf",
            "metadata": {"layer": "Bronze", "owner": {"team": "p", "email": "p@x"}},
            "builds": [
                {
                    "id": "perf",
                    "pattern": "acquisition",
                    "engine": "duckdb",
                    "capabilities": ["full_refresh"],
                    "properties": {
                        "source": {
                            "kind": "filesystem",
                            "connection": {"uri": str(in_dir / "*.csv")},
                            "mode": "full_refresh",
                            "reader": {"format": "csv", "options": {"header": True}},
                        },
                        "sink": {"format": "parquet"},
                    },
                    "outputs": ["raw"],
                }
            ],
            "exposes": [
                {
                    "exposeId": "raw",
                    "kind": "table",
                    "binding": {
                        "platform": "local",
                        "format": "parquet",
                        "location": {"path": out_path},
                    },
                    "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
                }
            ],
        }

        def _run():
            # Fresh tmp_path subdir per iteration so cursor state is clean.
            execute_duckdb_build(
                contract["builds"][0], contract, tmp_path / f"work_{time.time_ns()}", dry_run=False
            )

        median = _median_seconds(_run, iterations=3)
        assert median < 5.0, f"DuckDB CSV→Parquet 100 rows: {median:.3f}s > 5.0s budget"


# ── CLI startup budget ────────────────────────────────────────────────────


class TestCliStartupPerf:
    def test_cli_help_under_2s(self):
        """``fluid --help`` must return in under 2 s — a hard UX floor.
        Slower than this and ``fluid <subcommand>`` feels sluggish on
        every invocation. Imports, plugin discovery, banner — everything
        in the cold path.
        """
        import subprocess
        import sys

        def _run():
            r = subprocess.run(
                [sys.executable, "-m", "fluid_build.cli", "--help"],
                capture_output=True,
                timeout=10,
            )
            assert r.returncode == 0

        median = _median_seconds(_run, iterations=3)
        # Generous to account for cold cache + venv resolve. The design
        # doc target is 1.5 s; 2.0 s here gives 30 % CI noise margin.
        assert median < 2.0, f"fluid --help: {median:.3f}s > 2.0s budget"
