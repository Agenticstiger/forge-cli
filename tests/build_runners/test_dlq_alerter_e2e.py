# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""End-to-end DLQ + alerting flow.

Verifies the full path: HookChain runs over a batch → quality gate
fails some records → DLQWriter persists them on disk → Alerter fires
into the configured channels (log + file + webhook). The test pins the
contract between these subsystems so a regression in any of the four
fails immediately.

No external services required — webhook is mocked via ``respx``,
file channel writes to ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest
import respx

from fluid_build.api.hooks import HookChain
from fluid_build.build_runners._alerter import (
    Alerter,
    AlertEvent,
    channels_from_config,
    file_channel,
    log_channel,
    webhook_channel,
)
from fluid_build.build_runners._dlq import (
    DLQConfig,
    DLQOverflowError,
    DLQWriter,
    process_batch_with_dlq,
)
from fluid_build.build_runners.hooks.quality_gate import QualityGateHook

# ── Unit: alerter channel wiring ──────────────────────────────────────────


class TestAlerterChannels:
    def test_default_alerter_has_log_channel(self):
        a = Alerter.default()
        assert len(a.channels) == 1

    def test_log_channel_doesnt_throw(self, caplog):
        import logging

        caplog.set_level(logging.INFO, logger="fluid.acquire.alerter")
        ch = log_channel("INFO")
        ch(_dummy_event())
        assert any("alert" in r.message for r in caplog.records)

    def test_file_channel_appends_ndjson(self, tmp_path: Path):
        path = tmp_path / "alerts.ndjson"
        ch = file_channel(path)
        ch(_dummy_event(category="quality_gate_failed", count=3))
        ch(_dummy_event(category="schema_violation", count=1))
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["category"] == "quality_gate_failed"
        assert first["count"] == 3

    @respx.mock
    def test_webhook_channel_posts_to_url(self, monkeypatch):
        # Pin the host on the SSRF allow-list so the constructor accepts
        # the synthetic test host. The real route is mocked by respx so
        # no DNS / TCP traffic actually leaves the test process.
        monkeypatch.setenv("FLUID_WEBHOOK_HOST_ALLOWLIST", "hooks.test")
        route = respx.post("https://hooks.test/services/X").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        ch = webhook_channel("https://hooks.test/services/X")
        ch(_dummy_event(category="quality_gate_failed"))
        assert route.called
        body = json.loads(route.calls[0].request.content)
        assert "text" in body
        assert "quality_gate_failed" in body["text"]
        assert body["event"]["category"] == "quality_gate_failed"

    def test_channels_from_config_builds_log_plus_extras(self, tmp_path: Path, monkeypatch):
        # Webhook URL is synthetic — pin its suffix on the allow-list so
        # the SSRF guard doesn't refuse it at construction time.
        monkeypatch.setenv("FLUID_WEBHOOK_HOST_ALLOWLIST", "example")
        cfg = {
            "channels": [
                {"kind": "file", "path": str(tmp_path / "out.ndjson")},
                {"kind": "webhook", "url": "https://example/w"},
                {"kind": "unknown", "url": "ignored"},
            ]
        }
        chans = channels_from_config(cfg)
        # log + file + webhook; unknown is dropped.
        assert len(chans) == 3

    def test_channel_failure_does_not_propagate(self):
        def boom(_event):
            raise RuntimeError("boom")

        a = Alerter(channels=[boom])
        # Must not raise — alerter swallows + logs.
        a.fire(_dummy_event())


def _dummy_event(**overrides: Any) -> AlertEvent:
    return AlertEvent(
        run_id=overrides.get("run_id", "01HXX"),
        product_id=overrides.get("product_id", "bronze.orders"),
        build_id=overrides.get("build_id", "ingest"),
        category=overrides.get("category", "test"),
        severity=overrides.get("severity", "warn"),
        message=overrides.get("message", "test"),
        count=overrides.get("count", 0),
    )


# ── End-to-end: HookChain + DLQWriter + Alerter ──────────────────────────


@pytest.fixture
def quality_gate_chain() -> HookChain:
    return HookChain(hooks=[QualityGateHook()])


def _read_ndjson(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestEndToEndFlow:
    def test_failing_records_land_in_dlq_and_fire_alert(
        self, quality_gate_chain: HookChain, tmp_path: Path
    ):
        records = [
            {"id": 1, "email": "alice@x.com"},
            {"id": 2, "email": None},  # not_null violation
            {"id": 3, "email": "bob@x.com"},
            {"id": 4, "email": None},  # not_null violation
        ]
        gates = [
            {"rule": "not_null", "columns": ["email"], "severity": "error"},
        ]
        dlq_cfg = DLQConfig(
            enabled=True,
            sink_format="ndjson",
            location=str(tmp_path / "dlq"),
            max_records_before_abort=100,
            alert_on=["quality_gate_failed"],
        )
        writer = DLQWriter(dlq_cfg, run_id="01HRUN", default_root=tmp_path / ".fluid")

        alert_path = tmp_path / "alerts.ndjson"
        alerter = Alerter(channels=[log_channel(), file_channel(alert_path)])

        passed = process_batch_with_dlq(
            records=records,
            hook_chain=quality_gate_chain,
            dlq_writer=writer,
            alerter=alerter,
            stream="orders",
            run_id="01HRUN",
            product_id="bronze.orders",
            build_id="ingest",
            ctx={"quality_gates": gates, "alert_on": dlq_cfg.alert_on},
        )

        # Two records passed.
        assert [r["id"] for r in passed] == [1, 3]

        # Two records persisted in DLQ NDJSON.
        dlq_files = list((tmp_path / "dlq" / "01HRUN").rglob("*.ndjson"))
        assert len(dlq_files) == 1
        dlq_records = _read_ndjson(dlq_files[0])
        assert len(dlq_records) == 2
        assert all(d["reason"] == "quality_gate_failed" for d in dlq_records)
        assert {d["record"]["id"] for d in dlq_records} == {2, 4}

        # Alert fired exactly once with count=2.
        alerts = _read_ndjson(alert_path)
        assert len(alerts) == 1
        assert alerts[0]["category"] == "quality_gate_failed"
        assert alerts[0]["count"] == 2
        assert alerts[0]["severity"] == "warn"

    def test_alert_only_fires_for_matching_category(
        self, quality_gate_chain: HookChain, tmp_path: Path
    ):
        # alert_on does NOT include quality_gate_failed → no alert event.
        records = [{"id": 1, "email": None}]
        gates = [{"rule": "not_null", "columns": ["email"], "severity": "error"}]
        writer = DLQWriter(
            DLQConfig(enabled=True, location=str(tmp_path / "dlq"), alert_on=["other_thing"]),
            run_id="01HRUN",
            default_root=tmp_path / ".fluid",
        )
        alert_path = tmp_path / "alerts.ndjson"
        alerter = Alerter(channels=[file_channel(alert_path)])

        process_batch_with_dlq(
            records=records,
            hook_chain=quality_gate_chain,
            dlq_writer=writer,
            alerter=alerter,
            stream="orders",
            run_id="01HRUN",
            ctx={"quality_gates": gates, "alert_on": ["other_thing"]},
        )

        # Record routed to DLQ but no alert because category doesn't match.
        assert not alert_path.exists() or alert_path.read_text() == ""

    def test_dlq_overflow_aborts(self, quality_gate_chain: HookChain, tmp_path: Path):
        # 3 failing records, max_records_before_abort=2 → on the 3rd append the
        # writer raises DLQOverflowError, which process_batch_with_dlq propagates.
        records = [{"id": i, "email": None} for i in range(3)]
        gates = [{"rule": "not_null", "columns": ["email"], "severity": "error"}]
        writer = DLQWriter(
            DLQConfig(
                enabled=True,
                location=str(tmp_path / "dlq"),
                max_records_before_abort=2,
            ),
            run_id="01HRUN",
            default_root=tmp_path / ".fluid",
        )
        with pytest.raises(DLQOverflowError):
            process_batch_with_dlq(
                records=records,
                hook_chain=quality_gate_chain,
                dlq_writer=writer,
                alerter=None,
                stream="orders",
                run_id="01HRUN",
                ctx={"quality_gates": gates},
            )

    def test_passing_records_pass_through_when_no_dlq_writer(
        self, quality_gate_chain: HookChain, tmp_path: Path
    ):
        # When dlq_writer is None and records all pass, behavior is unchanged.
        records = [{"id": 1, "email": "alice@x.com"}]
        gates = [{"rule": "not_null", "columns": ["email"], "severity": "error"}]
        out = process_batch_with_dlq(
            records=records,
            hook_chain=quality_gate_chain,
            dlq_writer=None,
            alerter=None,
            stream="orders",
            run_id="01HRUN",
            ctx={"quality_gates": gates},
        )
        assert out == records

    def test_failing_records_dropped_when_no_dlq_writer(
        self, quality_gate_chain: HookChain, tmp_path: Path
    ):
        # Without a writer, failing records are still excluded from the
        # post-hook records list (the gate dropped them); they're just not
        # persisted anywhere. This matches HookResult semantics.
        records = [
            {"id": 1, "email": "alice@x.com"},
            {"id": 2, "email": None},
        ]
        gates = [{"rule": "not_null", "columns": ["email"], "severity": "error"}]
        out = process_batch_with_dlq(
            records=records,
            hook_chain=quality_gate_chain,
            dlq_writer=None,
            alerter=None,
            stream="orders",
            run_id="01HRUN",
            ctx={"quality_gates": gates},
        )
        assert [r["id"] for r in out] == [1]


class TestRegexAndRangeGates:
    def test_email_regex_routes_invalid_to_dlq(self, tmp_path: Path):
        chain = HookChain(hooks=[QualityGateHook()])
        records = [
            {"id": 1, "email": "alice@example.com"},
            {"id": 2, "email": "bogus"},  # regex fail
        ]
        gates = [
            {
                "rule": "regex",
                "column": "email",
                "pattern": r"^[^@]+@[^@]+\.[^@]+$",
                "severity": "error",
            }
        ]
        writer = DLQWriter(
            DLQConfig(enabled=True, location=str(tmp_path / "dlq")),
            run_id="01HRUN",
            default_root=tmp_path / ".fluid",
        )
        out = process_batch_with_dlq(
            records=records,
            hook_chain=chain,
            dlq_writer=writer,
            alerter=None,
            stream="users",
            run_id="01HRUN",
            ctx={"quality_gates": gates},
        )
        assert [r["id"] for r in out] == [1]
        dlq_files = list((tmp_path / "dlq" / "01HRUN").rglob("*.ndjson"))
        assert len(dlq_files) == 1
        rows = _read_ndjson(dlq_files[0])
        assert len(rows) == 1
        assert rows[0]["record"]["id"] == 2

    def test_warn_severity_does_not_route_to_dlq(self, tmp_path: Path):
        chain = HookChain(hooks=[QualityGateHook()])
        records = [{"id": 1, "amount": 999_999_999}]
        gates = [
            {"rule": "range", "column": "amount", "max": 1000, "severity": "warn"},
        ]
        writer = DLQWriter(
            DLQConfig(enabled=True, location=str(tmp_path / "dlq")),
            run_id="01HRUN",
            default_root=tmp_path / ".fluid",
        )
        out = process_batch_with_dlq(
            records=records,
            hook_chain=chain,
            dlq_writer=writer,
            alerter=None,
            stream="orders",
            run_id="01HRUN",
            ctx={"quality_gates": gates},
        )
        # Warn severity → record passes; nothing in DLQ.
        assert [r["id"] for r in out] == [1]
        # No DLQ file was even created — writer.count stayed 0.
        assert writer.total() == 0
