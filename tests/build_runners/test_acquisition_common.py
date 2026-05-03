# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the shared acquisition runtime modules."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.api.cost import BudgetCap
from fluid_build.api.schema import SchemaColumn, SchemaPolicy
from fluid_build.api.state import Cursor, Watermark
from fluid_build.build_runners._acquisition_common import generate_run_id, utc_now_iso
from fluid_build.build_runners._anomaly import (
    EwmaState,
    ewma_update,
    ewma_z_score,
    iqr_score,
)
from fluid_build.build_runners._cost import (
    BudgetExceededError,
    InMemoryCostTracker,
    gate_or_raise,
    parse_bytes,
)
from fluid_build.build_runners._dlq import DLQConfig, DLQOverflowError, DLQWriter
from fluid_build.build_runners._fingerprint import fingerprint_from_columns
from fluid_build.build_runners._idempotency import DEFAULT_KEY_TEMPLATE, format_key
from fluid_build.build_runners._retention import RetentionConfig, parse_iso_duration
from fluid_build.build_runners._retry import RetryPolicy, is_retryable, with_retry
from fluid_build.build_runners._schema_evolution import EvolutionAction, resolve
from fluid_build.build_runners._state import FileStateStore, LockHeldError

# ── _acquisition_common ──────────────────────────────────────────────────


class TestRunIds:
    def test_generate_run_id_unique(self):
        ids = {generate_run_id() for _ in range(50)}
        assert len(ids) == 50

    def test_generate_run_id_format(self):
        rid = generate_run_id()
        assert rid.startswith("01")
        assert len(rid) == 18

    def test_utc_now_iso_format(self):
        s = utc_now_iso()
        assert s.endswith("Z")
        assert "T" in s


# ── _state ───────────────────────────────────────────────────────────────


class TestFileStateStore:
    def test_cursor_round_trip(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        c = Cursor(
            stream="orders",
            value={"high_water_mark": "2026-01-01T00:00:00Z"},
            updated_at=utc_now_iso(),
        )
        store.set_cursor("p1", "b1", c)
        got = store.get_cursor("p1", "b1", "orders")
        assert got is not None
        assert got.stream == "orders"
        assert got.value == c.value

    def test_watermark_round_trip(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        w = Watermark(
            stream="orders",
            kind="high_water_mark",
            value="2026-01-01T00:00:00Z",
            updated_at=utc_now_iso(),
        )
        store.set_watermark("p1", "b1", w)
        got = store.get_watermark("p1", "b1", "orders")
        assert got is not None and got.kind == "high_water_mark"

    def test_run_record_round_trip(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        rec = {"run_id": "r1", "state": "succeeded", "records": 100}
        store.write_run_record("p1", "b1", rec)
        got = store.read_run_record("p1", "b1", "r1")
        assert got == rec

    def test_list_runs_orders_newest_first(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        for run_id in ("r1", "r2", "r3"):
            store.write_run_record("p1", "b1", {"run_id": run_id, "state": "succeeded"})
        runs = store.list_runs("p1", "b1", limit=3)
        assert len(runs) == 3
        # Names sort in reverse alphabetically; r3 first.
        assert runs[0]["run_id"] == "r3"

    def test_lock_acquire_and_release(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        with store.acquire_lock("product", "p1", timeout_seconds=60) as lock:
            assert lock.scope == "product"
            assert lock.resource_id == "p1"
        # After context exit, the lock file is gone.
        lock_path = tmp_path / "locks" / "product__p1.lock"
        assert not lock_path.exists()

    def test_lock_held_raises_under_abort(self, tmp_path: Path):
        store = FileStateStore(tmp_path)
        with store.acquire_lock("product", "p1", timeout_seconds=60):
            with pytest.raises(LockHeldError):
                with store.acquire_lock("product", "p1", timeout_seconds=60, on_contended="abort"):
                    pass

    def test_atomic_write_no_partial_files_left(self, tmp_path: Path):
        """No `.tmp-*` files leaked after a successful write."""
        store = FileStateStore(tmp_path)
        store.write_run_record("p1", "b1", {"run_id": "r1"})
        for path in tmp_path.rglob("*.tmp-*"):
            pytest.fail(f"leftover tmp file: {path}")


# ── _retry ───────────────────────────────────────────────────────────────


class TestRetry:
    def test_retryable_classification(self):
        assert is_retryable(TimeoutError("connection timed out"))
        assert is_retryable(RuntimeError("503 Service Unavailable"))
        assert not is_retryable(PermissionError("403 Forbidden"))
        assert not is_retryable(ValueError("invalid argument"))

    def test_with_retry_succeeds_after_retry(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("connection timed out")
            return "ok"

        result = with_retry(
            flaky, RetryPolicy(count=3, jitter=False, initial_delay=0.0), sleep=lambda _: None
        )
        assert result == "ok"
        assert calls["n"] == 3

    def test_with_retry_propagates_non_retryable(self):
        def boom():
            raise ValueError("invalid argument")

        with pytest.raises(ValueError):
            with_retry(
                boom, RetryPolicy(count=3, jitter=False, initial_delay=0.0), sleep=lambda _: None
            )

    def test_with_retry_exhausts_retries(self):
        def boom():
            raise TimeoutError("connection timed out")

        with pytest.raises(TimeoutError):
            with_retry(
                boom, RetryPolicy(count=2, jitter=False, initial_delay=0.0), sleep=lambda _: None
            )


# ── _idempotency ─────────────────────────────────────────────────────────


class TestIdempotencyKey:
    def test_default_template_with_pk(self):
        k = format_key(DEFAULT_KEY_TEMPLATE, "r1", "orders", {"id": 42, "name": "x"})
        assert k == "r1:orders:42"

    def test_default_template_falls_back_to_hash(self):
        k1 = format_key(DEFAULT_KEY_TEMPLATE, "r1", "orders", {"name": "alice"})
        k2 = format_key(DEFAULT_KEY_TEMPLATE, "r1", "orders", {"name": "alice"})
        assert k1 == k2  # deterministic
        assert k1.startswith("r1:orders:")
        # Different record → different key
        k3 = format_key(DEFAULT_KEY_TEMPLATE, "r1", "orders", {"name": "bob"})
        assert k1 != k3

    def test_custom_template(self):
        k = format_key("{run_id}-{country}-{record_pk}", "r1", "orders", {"id": 7, "country": "US"})
        assert k == "r1-US-7"


# ── _fingerprint ─────────────────────────────────────────────────────────


class TestFingerprint:
    def test_column_order_invariance(self):
        cols_a = [{"name": "id", "type": "int"}, {"name": "name", "type": "varchar"}]
        cols_b = [{"name": "name", "type": "varchar"}, {"name": "id", "type": "int"}]
        assert fingerprint_from_columns(cols_a).digest == fingerprint_from_columns(cols_b).digest

    def test_added_column_changes_digest(self):
        cols_a = [{"name": "id", "type": "int"}]
        cols_b = [{"name": "id", "type": "int"}, {"name": "email", "type": "varchar"}]
        assert fingerprint_from_columns(cols_a).digest != fingerprint_from_columns(cols_b).digest


# ── _schema_evolution ────────────────────────────────────────────────────


class TestSchemaEvolutionMatrix:
    def test_strict_fails_on_added(self):
        baseline = [SchemaColumn("id", "int")]
        current = [SchemaColumn("id", "int"), SchemaColumn("email", "varchar")]
        plan = resolve(baseline, current, SchemaPolicy.STRICT)
        assert plan.must_fail
        assert plan.decisions[0].action is EvolutionAction.FAIL

    def test_evolve_safe_includes_added(self):
        baseline = [SchemaColumn("id", "int")]
        current = [SchemaColumn("id", "int"), SchemaColumn("email", "varchar")]
        plan = resolve(baseline, current, SchemaPolicy.EVOLVE_SAFE)
        assert not plan.must_fail
        assert plan.decisions[0].action is EvolutionAction.INCLUDE

    def test_evolve_safe_warns_on_removed(self):
        baseline = [SchemaColumn("id", "int"), SchemaColumn("name", "varchar")]
        current = [SchemaColumn("id", "int")]
        plan = resolve(baseline, current, SchemaPolicy.EVOLVE_SAFE)
        assert plan.decisions[0].action is EvolutionAction.WARN

    def test_evolve_all_drops_removed(self):
        baseline = [SchemaColumn("id", "int"), SchemaColumn("name", "varchar")]
        current = [SchemaColumn("id", "int")]
        plan = resolve(baseline, current, SchemaPolicy.EVOLVE_ALL)
        assert plan.decisions[0].action is EvolutionAction.DROP

    def test_evolve_safe_fails_on_type_narrow(self):
        baseline = [SchemaColumn("amount", "bigint")]
        current = [SchemaColumn("amount", "int")]
        plan = resolve(baseline, current, SchemaPolicy.EVOLVE_SAFE)
        assert plan.must_fail

    def test_evolve_all_casts_type_narrow(self):
        baseline = [SchemaColumn("amount", "bigint")]
        current = [SchemaColumn("amount", "int")]
        plan = resolve(baseline, current, SchemaPolicy.EVOLVE_ALL)
        assert plan.decisions[0].action is EvolutionAction.CAST

    def test_discover_and_freeze_first_run_includes_added(self):
        baseline = [SchemaColumn("id", "int")]
        current = [SchemaColumn("id", "int"), SchemaColumn("name", "varchar")]
        plan = resolve(baseline, current, SchemaPolicy.DISCOVER_AND_FREEZE, is_first_run=True)
        assert plan.decisions[0].action is EvolutionAction.INCLUDE

    def test_discover_and_freeze_after_first_run_fails(self):
        baseline = [SchemaColumn("id", "int")]
        current = [SchemaColumn("id", "int"), SchemaColumn("name", "varchar")]
        plan = resolve(baseline, current, SchemaPolicy.DISCOVER_AND_FREEZE, is_first_run=False)
        assert plan.must_fail

    def test_override_stricter_wins(self):
        baseline = [SchemaColumn("id", "int")]
        current = [SchemaColumn("id", "int"), SchemaColumn("email", "varchar")]
        plan = resolve(
            baseline, current, SchemaPolicy.EVOLVE_ALL, overrides={"onAddedColumn": "fail"}
        )
        assert plan.must_fail


# ── _dlq ─────────────────────────────────────────────────────────────────


class TestDLQ:
    def test_append_and_count(self, tmp_path: Path):
        cfg = DLQConfig(enabled=True, location=str(tmp_path / "dlq"), max_records_before_abort=100)
        writer = DLQWriter(cfg, "r1", tmp_path)
        for i in range(5):
            writer.append("orders", {"id": i}, "test_reason")
        assert writer.total() == 5
        # File written
        files = list((tmp_path / "dlq" / "r1").glob("*.ndjson"))
        assert len(files) == 1
        lines = files[0].read_text().strip().splitlines()
        assert len(lines) == 5
        first = json.loads(lines[0])
        assert first["reason"] == "test_reason"

    def test_overflow_raises(self, tmp_path: Path):
        cfg = DLQConfig(enabled=True, location=str(tmp_path / "dlq"), max_records_before_abort=2)
        writer = DLQWriter(cfg, "r1", tmp_path)
        writer.append("orders", {"id": 1}, "x")
        writer.append("orders", {"id": 2}, "x")
        with pytest.raises(DLQOverflowError):
            writer.append("orders", {"id": 3}, "x")


# ── _cost ────────────────────────────────────────────────────────────────


class TestCost:
    def test_parse_bytes_units(self):
        assert parse_bytes("100B") == 100
        assert parse_bytes("50GB") == 50 * 10**9
        assert parse_bytes("1.5MB") == 1_500_000
        assert parse_bytes(None) is None
        assert parse_bytes("garbage") is None

    def test_in_memory_tracker_records(self):
        t = InMemoryCostTracker()
        t.record_records(100)
        t.record_bytes(1_000_000, direction="read")
        t.record_compute_seconds(15.5)
        usage = t.usage()
        assert usage["rows"] == 100
        assert usage["bytes_read"] == 1_000_000
        assert usage["compute_seconds"] == 15

    def test_gate_or_raise_aborts_when_over_budget(self):
        t = InMemoryCostTracker()
        cap = BudgetCap(rows=10, on_exceed="abort")
        with pytest.raises(BudgetExceededError):
            gate_or_raise(t, cap, prior_usage={"rows": 11})

    def test_gate_or_raise_passes_when_within_budget(self):
        t = InMemoryCostTracker()
        cap = BudgetCap(rows=100, on_exceed="abort")
        gate_or_raise(t, cap, prior_usage={"rows": 50})  # no raise


# ── _anomaly ─────────────────────────────────────────────────────────────


class TestAnomaly:
    def test_ewma_warmup_returns_seed(self):
        s = ewma_update(None, 100.0)
        assert s.mean == 100.0

    def test_ewma_z_score_zero_for_stable_series(self):
        s: EwmaState | None = None
        for x in [100.0] * 10:
            s = ewma_update(s, x)
        assert s is not None
        assert ewma_z_score(s, 100.0) == 0.0

    def test_iqr_score_outlier_detection(self):
        history = [10.0, 11.0, 9.0, 12.0, 10.5, 9.5, 11.5]
        score = iqr_score(history, 50.0)
        assert score > 1.0


# ── _retention ───────────────────────────────────────────────────────────


class TestRetention:
    def test_parse_iso_duration_days(self):
        d = parse_iso_duration("P30D")
        assert d.days == 30

    def test_parse_iso_duration_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_iso_duration("30 days")

    def test_retention_config_defaults(self):
        cfg = RetentionConfig.from_dict(None)
        assert cfg.run_state.days == 30
        assert cfg.run_logs.days == 90
        assert cfg.lineage.days == 365


# ── hooks integration ────────────────────────────────────────────────────


class TestHookChain:
    def test_dlp_then_tokenize(self):
        from fluid_build.api.hooks import HookChain
        from fluid_build.build_runners.hooks.dlp_scan import DlpScanHook
        from fluid_build.build_runners.hooks.tokenize_pii import TokenizePiiHook

        chain = HookChain([DlpScanHook(), TokenizePiiHook()])
        records = [
            {"id": 1, "email": "alice@example.com", "name": "Alice"},
            {"id": 2, "email": "bob@example.com", "name": "Bob"},
        ]
        # Tokenize hook reads classifications from ctx; chain.run threads them.
        ctx: Dict[str, Any] = {"classifications": {}}
        # First pass: dlp_scan populates classifications. We feed those into ctx,
        # then tokenize_pii uses them.
        result = chain.run(records, ctx={"classifications": {}})
        # After scan, classifications include 'email'.
        assert "email" in result.classifications
        assert "email" in result.classifications and "email" in result.classifications["email"]


# ── finalize_run_result (silent-failure fix) ─────────────────────────────
#
# Pin: when an acquisition runner returns a ``RunResult`` with
# ``state == FAILED`` and a non-empty ``error`` string, the helper MUST
# write the error to stderr AND log it under ``fluid.acquire.<engine>``.
#
# Before the fix, runners returned ``return 0 if state in (SUCCEEDED,
# PARTIAL) else 1`` and the error message was captured in the run record
# but never surfaced to the user — they saw "❌ Failed: 1" with no
# explanation. The real-world test pass surfaced this against the
# debezium runner (``deployment.server_url`` missing); the fix lifted
# the error-printing into a shared helper so all six engines benefit.


class TestFinalizeRunResult:
    def _run_result(self, *, state: str, error: str = ""):
        from fluid_build.api.runner import RunResult, RunState

        return RunResult(
            run_id="test-run",
            state=RunState[state],
            started_at="2026-05-01T00:00:00Z",
            finished_at="2026-05-01T00:00:01Z",
            records_total=0,
            bytes_total=0,
            dlq_records=0,
            streams=[],
            error=error or None,
            facets={},
        )

    def test_succeeded_returns_zero_silently(self, capsys):
        from fluid_build.build_runners._acquisition_common import (
            finalize_run_result,
        )

        rc = finalize_run_result("duckdb", "ingest_x", self._run_result(state="SUCCEEDED"))
        assert rc == 0
        captured = capsys.readouterr()
        assert "failed" not in captured.err.lower()

    def test_partial_returns_zero_by_default(self, capsys):
        from fluid_build.build_runners._acquisition_common import (
            finalize_run_result,
        )

        rc = finalize_run_result("airbyte", "ingest_x", self._run_result(state="PARTIAL"))
        assert rc == 0
        captured = capsys.readouterr()
        assert "failed" not in captured.err.lower()

    def test_failed_returns_one_AND_prints_error_to_stderr(self, capsys):
        """The bug we fixed: a FAILED RunResult with an error message
        now surfaces that message; before the fix, ``❌ Failed: 1``
        was the only signal the user got."""
        from fluid_build.build_runners._acquisition_common import (
            finalize_run_result,
        )

        rc = finalize_run_result(
            "debezium",
            "cdc_orders",
            self._run_result(
                state="FAILED",
                error="debezium kafka-connect mode requires deployment.server_url",
            ),
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "debezium build 'cdc_orders' failed" in captured.err
        assert "deployment.server_url" in captured.err

    def test_failed_with_no_error_message_uses_placeholder(self, capsys):
        """A defensive check: even when the runner forgets to populate
        ``result.error``, the user gets a non-empty message."""
        from fluid_build.build_runners._acquisition_common import (
            finalize_run_result,
        )

        rc = finalize_run_result("dlt", "ingest_x", self._run_result(state="FAILED"))
        assert rc == 1
        captured = capsys.readouterr()
        assert "failed" in captured.err.lower()
        assert "no error message captured" in captured.err

    def test_succeeded_states_override_treats_partial_as_failure(self, capsys):
        """The duckdb runner uses ``succeeded_states=(SUCCEEDED,)`` so
        a PARTIAL run is treated as a failure (raises PartialFailureError
        before reaching this helper, but the override still works as a
        defensive boundary)."""
        from fluid_build.api.runner import RunState
        from fluid_build.build_runners._acquisition_common import (
            finalize_run_result,
        )

        rc = finalize_run_result(
            "duckdb",
            "ingest_x",
            self._run_result(state="PARTIAL", error="one stream failed"),
            succeeded_states=(RunState.SUCCEEDED,),
        )
        assert rc == 1
        captured = capsys.readouterr()
        assert "one stream failed" in captured.err

    def test_redacts_password_in_error_string_to_stderr(self, capsys):
        """Security: when the runner's exception echoes the libpq DSN
        (which the duckdb postgres / mysql extensions routinely do),
        the password MUST be redacted before it reaches the user's
        terminal. The user-facing path routes through
        ``cli.console.error`` (applies ``_redact_str``) and
        ``redact_secret_text`` runs first."""
        from fluid_build.build_runners._acquisition_common import (
            finalize_run_result,
        )

        rc = finalize_run_result(
            "duckdb",
            "ingest",
            self._run_result(
                state="FAILED",
                error="binder error: host=db.x.com user=alice password=hunter2 db=t",
            ),
        )
        assert rc == 1
        captured = capsys.readouterr()
        # The actual security property: the plaintext password value
        # must not leak. Which redaction marker fires first
        # (``***REDACTED***`` from ours, ``<redacted>`` from the
        # console layer) is an implementation detail.
        assert "hunter2" not in captured.err
        assert ("REDACTED" in captured.err) or ("<redacted>" in captured.err)
        # The non-secret context survives so the user can still diagnose.
        assert "binder error" in captured.err
        assert "duckdb build 'ingest' failed" in captured.err

    def test_strips_ansi_escapes_from_error_string(self, capsys):
        """Security: a contract-supplied error string can carry ANSI
        escape sequences that overwrite prior terminal output (status
        line spoofing). The escape sequences must be stripped before
        the stderr write."""
        from fluid_build.build_runners._acquisition_common import (
            finalize_run_result,
        )

        rc = finalize_run_result(
            "airbyte",
            "ingest",
            self._run_result(
                state="FAILED",
                error="connect failed\x1b[2J\x1b[H[FAKE OK]\rrun completed",
            ),
        )
        assert rc == 1
        captured = capsys.readouterr()
        # ANSI control sequences must be gone.
        assert "\x1b[" not in captured.err
        # \r (which would overwrite the previous line) must be stripped.
        assert "\r" not in captured.err
        # The plain text from the error survives.
        assert "connect failed" in captured.err
