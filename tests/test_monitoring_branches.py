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

"""Branch-coverage tests for fluid_build.forge.core.monitoring"""

import json
import time

import pytest

from fluid_build.forge.core.monitoring import (
    Alert,
    AlertSeverity,
    HealthCheck,
    LogEntry,
    Metric,
    MetricType,
    MonitoringSystem,
)

# ── Enum tests ──────────────────────────────────────────────────────


class TestMetricType:
    def test_counter(self):
        assert MetricType.COUNTER.value == "counter"

    def test_gauge(self):
        assert MetricType.GAUGE.value == "gauge"

    def test_histogram(self):
        assert MetricType.HISTOGRAM.value == "histogram"

    def test_timer(self):
        assert MetricType.TIMER.value == "timer"


class TestAlertSeverity:
    def test_info(self):
        assert AlertSeverity.INFO.value == "info"

    def test_warning(self):
        assert AlertSeverity.WARNING.value == "warning"

    def test_error(self):
        assert AlertSeverity.ERROR.value == "error"

    def test_critical(self):
        assert AlertSeverity.CRITICAL.value == "critical"


# ── Dataclass tests ─────────────────────────────────────────────────


class TestMetric:
    def test_create_with_defaults(self):
        m = Metric(name="cpu", value=42.0, metric_type=MetricType.GAUGE)
        assert m.name == "cpu"
        assert m.value == 42.0
        assert m.metric_type == MetricType.GAUGE
        assert isinstance(m.timestamp, float)
        assert m.tags == {}
        assert m.unit is None

    def test_create_with_all_fields(self):
        m = Metric(
            name="requests",
            value=100,
            metric_type=MetricType.COUNTER,
            timestamp=1000.0,
            tags={"env": "prod"},
            unit="req/s",
        )
        assert m.tags == {"env": "prod"}
        assert m.unit == "req/s"
        assert m.timestamp == 1000.0


class TestAlert:
    def test_create_with_defaults(self):
        a = Alert(name="high_cpu", severity=AlertSeverity.WARNING, message="CPU is high")
        assert a.name == "high_cpu"
        assert a.resolved is False
        assert a.tags == {}

    def test_create_resolved(self):
        a = Alert(name="x", severity=AlertSeverity.INFO, message="ok", resolved=True)
        assert a.resolved is True


class TestLogEntry:
    def test_create_with_defaults(self):
        le = LogEntry(level="INFO", message="hello")
        assert le.component == "unknown"
        assert le.trace_id is None
        assert le.exception is None
        assert le.tags == {}

    def test_create_with_all_fields(self):
        le = LogEntry(
            level="ERROR",
            message="bad",
            component="api",
            trace_id="abc",
            tags={"k": "v"},
            exception="traceback...",
        )
        assert le.component == "api"
        assert le.trace_id == "abc"
        assert le.exception == "traceback..."


class TestHealthCheck:
    def test_create_with_defaults(self):
        hc = HealthCheck(name="db", status="healthy", message="ok")
        assert hc.duration_ms == 0
        assert hc.tags == {}

    def test_create_unhealthy(self):
        hc = HealthCheck(name="db", status="unhealthy", message="timeout", duration_ms=500.0)
        assert hc.duration_ms == 500.0


# ── MonitoringSystem tests ──────────────────────────────────────────


@pytest.fixture
def mon():
    """Create a MonitoringSystem, then shut it down after the test."""
    ms = MonitoringSystem(project_name="test-project")
    yield ms
    ms._running = False


class TestMonitoringSystemInit:
    def test_project_name(self, mon):
        assert mon.project_name == "test-project"

    def test_collections_initially_no_alerts(self, mon):
        assert len(mon.alerts) == 0

    def test_collections_initially_no_health_checks(self, mon):
        assert len(mon.health_checks) == 0

    def test_aggregation_intervals(self, mon):
        assert 60 in mon.aggregation_intervals
        assert 300 in mon.aggregation_intervals
        assert 3600 in mon.aggregation_intervals

    def test_default_alert_rules(self, mon):
        assert len(mon.alert_rules) == 3  # high_error_rate, high_memory, high_response


class TestRecordMetric:
    def test_record_metric_puts_to_queue(self, mon):
        mon.record_metric("test.metric", 42, MetricType.GAUGE)
        assert not mon.metric_queue.empty()

    def test_increment_counter(self, mon):
        mon.increment_counter("req_count", 5, tags={"env": "dev"})
        metric = mon.metric_queue.get(timeout=1)
        assert metric.name == "req_count"
        assert metric.value == 5
        assert metric.metric_type == MetricType.COUNTER

    def test_set_gauge(self, mon):
        mon.set_gauge("cpu", 0.75)
        metric = mon.metric_queue.get(timeout=1)
        assert metric.value == 0.75
        assert metric.metric_type == MetricType.GAUGE

    def test_record_timer(self, mon):
        mon.record_timer("api.latency", 123.4, tags={"path": "/health"})
        metric = mon.metric_queue.get(timeout=1)
        assert metric.metric_type == MetricType.TIMER
        assert metric.unit == "ms"


class TestTimerContextManager:
    def test_timer_records_duration(self, mon):
        with mon.timer("op.duration", tags={"op": "test"}):
            time.sleep(0.01)
        metric = mon.metric_queue.get(timeout=1)
        assert metric.name == "op.duration"
        assert metric.value >= 10  # at least ~10 ms


class TestLogging:
    def test_log_puts_to_queue(self, mon):
        mon.log("INFO", "hello", component="c1")
        entry = mon.log_queue.get(timeout=1)
        assert entry.level == "INFO"
        assert entry.component == "c1"

    def test_log_level_uppercased(self, mon):
        mon.log("warning", "w", component="c1")
        entry = mon.log_queue.get(timeout=1)
        assert entry.level == "WARNING"

    def test_log_info(self, mon):
        mon.log_info("info msg", component="c1")
        entry = mon.log_queue.get(timeout=1)
        assert entry.level == "INFO"

    def test_log_warning(self, mon):
        mon.log_warning("warn msg")
        entry = mon.log_queue.get(timeout=1)
        assert entry.level == "WARNING"

    def test_log_error(self, mon):
        mon.log_error("err msg", exception=ValueError("x"))
        entry = mon.log_queue.get(timeout=1)
        assert entry.level == "ERROR"

    def test_log_critical(self, mon):
        mon.log_critical("crit msg")
        entry = mon.log_queue.get(timeout=1)
        assert entry.level == "CRITICAL"


class TestAlerts:
    def test_create_alert(self, mon):
        mon.create_alert("test_alert", AlertSeverity.WARNING, "check this")
        alert = mon.alert_queue.get(timeout=1)
        assert alert.name == "test_alert"
        assert alert.severity == AlertSeverity.WARNING

    def test_get_alerts_empty(self, mon):
        assert mon.get_alerts() == []

    def test_get_alerts_filter_severity(self, mon):
        mon.alerts.append(Alert(name="a", severity=AlertSeverity.WARNING, message="w"))
        mon.alerts.append(Alert(name="b", severity=AlertSeverity.ERROR, message="e"))
        result = mon.get_alerts(severity=AlertSeverity.ERROR)
        assert len(result) == 1
        assert result[0].name == "b"

    def test_get_alerts_filter_since(self, mon):
        old = Alert(name="old", severity=AlertSeverity.INFO, message="old", timestamp=100.0)
        new = Alert(name="new", severity=AlertSeverity.INFO, message="new", timestamp=time.time())
        mon.alerts.extend([old, new])
        result = mon.get_alerts(since=time.time() - 10)
        assert len(result) == 1
        assert result[0].name == "new"


class TestGetLogs:
    def test_get_logs_filter_nonexistent_component(self, mon):
        result = mon.get_logs(component="nonexistent_comp_xyz")
        assert result == []

    def test_get_logs_filter_level(self, mon):
        mon.logs.clear()
        mon.logs.append(LogEntry(level="INFO", message="i"))
        mon.logs.append(LogEntry(level="ERROR", message="e"))
        result = mon.get_logs(level="ERROR")
        assert len(result) == 1
        assert result[0].message == "e"

    def test_get_logs_filter_component(self, mon):
        mon.logs.clear()
        mon.logs.append(LogEntry(level="INFO", message="a", component="api"))
        mon.logs.append(LogEntry(level="INFO", message="b", component="db"))
        result = mon.get_logs(component="api")
        assert len(result) == 1
        assert result[0].message == "a"

    def test_get_logs_filter_since(self, mon):
        mon.logs.clear()
        old = LogEntry(level="INFO", message="old", timestamp=100.0)
        new = LogEntry(level="INFO", message="new", timestamp=time.time())
        mon.logs.extend([old, new])
        result = mon.get_logs(since=time.time() - 10)
        assert len(result) == 1

    def test_get_logs_limit(self, mon):
        mon.logs.clear()
        for i in range(20):
            mon.logs.append(LogEntry(level="INFO", message=f"msg{i}"))
        result = mon.get_logs(limit=5)
        assert len(result) == 5


class TestGetMetrics:
    def test_get_metrics_empty(self, mon):
        assert mon.get_metrics() == []

    def test_get_metrics_with_name_pattern(self, mon):
        mon.metrics["cpu.load"].append(
            Metric(name="cpu.load", value=0.5, metric_type=MetricType.GAUGE)
        )
        mon.metrics["mem.used"].append(
            Metric(name="mem.used", value=100, metric_type=MetricType.GAUGE)
        )
        result = mon.get_metrics(name_pattern="cpu")
        assert len(result) == 1
        assert result[0].name == "cpu.load"

    def test_get_metrics_with_since(self, mon):
        old = Metric(name="m", value=1, metric_type=MetricType.GAUGE, timestamp=100.0)
        new = Metric(name="m", value=2, metric_type=MetricType.GAUGE, timestamp=time.time())
        mon.metrics["m"].extend([old, new])
        result = mon.get_metrics(since=time.time() - 10)
        assert len(result) == 1
        assert result[0].value == 2


class TestGetAggregatedMetrics:
    def test_invalid_interval(self, mon):
        assert mon.get_aggregated_metrics(999) == []

    def test_valid_interval_empty(self, mon):
        assert mon.get_aggregated_metrics(60) == []

    def test_with_name_pattern(self, mon):
        m1 = Metric(name="cpu.count", value=5, metric_type=MetricType.GAUGE)
        m2 = Metric(name="mem.count", value=10, metric_type=MetricType.GAUGE)
        mon.aggregated_metrics[60]["cpu"].append(m1)
        mon.aggregated_metrics[60]["mem"].append(m2)
        result = mon.get_aggregated_metrics(60, name_pattern="cpu")
        assert len(result) == 1


class TestCalculateAggregations:
    def test_empty_list(self, mon):
        assert mon._calculate_aggregations([]) == []

    def test_single_metric(self, mon):
        m = Metric(name="x", value=10, metric_type=MetricType.GAUGE, tags={"a": "b"})
        result = mon._calculate_aggregations([m])
        assert len(result) == 5  # count, sum, avg, min, max
        names = [r.name for r in result]
        assert "x.count" in names
        assert "x.sum" in names
        assert "x.avg" in names
        assert "x.min" in names
        assert "x.max" in names

    def test_multiple_metrics(self, mon):
        metrics = [
            Metric(name="y", value=10, metric_type=MetricType.GAUGE),
            Metric(name="y", value=20, metric_type=MetricType.GAUGE),
            Metric(name="y", value=30, metric_type=MetricType.GAUGE),
        ]
        result = mon._calculate_aggregations(metrics)
        by_name = {r.name: r.value for r in result}
        assert by_name["y.count"] == 3
        assert by_name["y.sum"] == 60
        assert by_name["y.avg"] == 20.0
        assert by_name["y.min"] == 10
        assert by_name["y.max"] == 30


class TestExportMetrics:
    def test_export_json(self, mon):
        mon.metrics["test"].append(
            Metric(name="test", value=42, metric_type=MetricType.GAUGE, tags={})
        )
        result = mon.export_metrics("json")
        data = json.loads(result)
        assert data["project"] == "test-project"
        assert "metrics" in data
        assert "alerts" in data
        assert "health_checks" in data

    def test_export_prometheus_no_tags(self, mon):
        mon.metrics["cpu_load"].append(
            Metric(name="cpu_load", value=0.75, metric_type=MetricType.GAUGE, tags={})
        )
        result = mon.export_metrics("prometheus")
        assert "cpu_load" in result
        assert "0.75" in result

    def test_export_prometheus_with_tags(self, mon):
        mon.metrics["req_count"].append(
            Metric(
                name="req_count",
                value=100,
                metric_type=MetricType.COUNTER,
                tags={"env": "prod"},
            )
        )
        result = mon.export_metrics("prometheus")
        assert 'env="prod"' in result

    def test_export_unsupported_format(self, mon):
        with pytest.raises(ValueError, match="Unsupported export format"):
            mon.export_metrics("xml")


class TestHealthChecks:
    def test_get_health_status_empty(self, mon):
        assert mon.get_health_status() == {}

    def test_add_health_check_healthy(self, mon):
        mon.add_health_check("db", lambda: ("healthy", "ok"))
        # Give time for the check to run
        time.sleep(0.1)
        status = mon.get_health_status()
        assert "db" in status
        assert status["db"].status == "healthy"

    def test_add_health_check_unhealthy_exception(self, mon):
        def bad():
            raise RuntimeError("fail")

        mon.add_health_check("bad", bad)
        time.sleep(0.1)
        status = mon.get_health_status()
        assert "bad" in status
        assert status["bad"].status == "unhealthy"


class TestDefaultAlertRules:
    def test_high_error_rate_triggers(self, mon):
        """More than 10 error metrics should trigger alert"""
        for i in range(15):
            mon.metrics["error.count"].append(
                Metric(name="error.count", value=1, metric_type=MetricType.COUNTER)
            )
        # Trigger rule check manually
        dummy = Metric(name="x", value=1, metric_type=MetricType.GAUGE)
        mon._check_alert_rules(dummy)
        # Should have put alert in queue
        found = False
        while not mon.alert_queue.empty():
            a = mon.alert_queue.get_nowait()
            if a.name == "high_error_rate":
                found = True
                break
        assert found

    def test_high_memory_triggers(self, mon):
        """Memory metric > 0.9 should trigger"""
        mon.metrics["memory.usage"].append(
            Metric(name="memory.usage", value=0.95, metric_type=MetricType.GAUGE)
        )
        dummy = Metric(name="x", value=1, metric_type=MetricType.GAUGE)
        mon._check_alert_rules(dummy)
        found = False
        while not mon.alert_queue.empty():
            a = mon.alert_queue.get_nowait()
            if a.name == "high_memory_usage":
                found = True
                break
        assert found

    def test_high_response_time_triggers(self, mon):
        """Response time > 5000ms should trigger"""
        mon.metrics["response_time"].append(
            Metric(name="response_time", value=6000, metric_type=MetricType.TIMER)
        )
        dummy = Metric(name="x", value=1, metric_type=MetricType.GAUGE)
        mon._check_alert_rules(dummy)
        found = False
        while not mon.alert_queue.empty():
            a = mon.alert_queue.get_nowait()
            if a.name == "high_response_time":
                found = True
                break
        assert found

    def test_no_alert_when_below_threshold(self, mon):
        """No alerts when metrics are below thresholds"""
        mon.metrics["cpu"].append(Metric(name="cpu", value=0.5, metric_type=MetricType.GAUGE))
        dummy = Metric(name="x", value=1, metric_type=MetricType.GAUGE)
        mon._check_alert_rules(dummy)
        # Queue should only have the alert rules that don't trigger
        # Drain queue - none should match our 3 default rules
        alerts = []
        while not mon.alert_queue.empty():
            alerts.append(mon.alert_queue.get_nowait())
        triggered_names = [a.name for a in alerts]
        assert "high_error_rate" not in triggered_names
        assert "high_memory_usage" not in triggered_names
        assert "high_response_time" not in triggered_names


class TestGenerateDashboard:
    def test_dashboard_contains_project(self, mon):
        html = mon.generate_dashboard()
        assert "test-project" in html

    def test_dashboard_contains_structure(self, mon):
        html = mon.generate_dashboard()
        assert "Health Status" in html
        assert "Recent Alerts" in html
        assert "Key Metrics" in html
        assert "Recent Logs" in html

    def test_dashboard_with_data(self, mon):
        mon.alerts.append(
            Alert(name="test_alert", severity=AlertSeverity.WARNING, message="warn msg")
        )
        mon.logs.append(LogEntry(level="ERROR", message="err msg", component="api"))
        html = mon.generate_dashboard()
        assert "test_alert" in html
        assert "No recent alerts" not in html

    def test_dashboard_no_alerts(self, mon):
        html = mon.generate_dashboard()
        assert "No recent alerts" in html


class TestInternalLogging:
    def test_log_error_internal(self, mon):
        mon._log_error("something bad")
        found = any(e.level == "ERROR" and "something bad" in e.message for e in mon.logs)
        assert found

    def test_log_info_internal(self, mon):
        mon._log_info("all good")
        found = any(e.level == "INFO" and "all good" in e.message for e in mon.logs)
        assert found
