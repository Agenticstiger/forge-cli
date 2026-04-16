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

"""Tests for fluid_build.forge.core.monitoring"""

from unittest.mock import patch

from fluid_build.forge.core.monitoring import (
    Alert,
    AlertSeverity,
    HealthCheck,
    LogEntry,
    Metric,
    MetricType,
    MonitoringSystem,
)

# ── Enum tests ──


class TestEnums:
    def test_metric_type_values(self):
        assert MetricType.COUNTER.value == "counter"
        assert MetricType.GAUGE.value == "gauge"
        assert MetricType.HISTOGRAM.value == "histogram"
        assert MetricType.TIMER.value == "timer"

    def test_alert_severity_values(self):
        assert AlertSeverity.INFO.value == "info"
        assert AlertSeverity.CRITICAL.value == "critical"


# ── Dataclass tests ──


class TestMetric:
    def test_create_minimal(self):
        m = Metric(name="cpu", value=42.0, metric_type=MetricType.GAUGE)
        assert m.name == "cpu"
        assert m.value == 42.0
        assert isinstance(m.timestamp, float)
        assert m.tags == {}
        assert m.unit is None

    def test_create_with_tags(self):
        m = Metric(
            name="req", value=1, metric_type=MetricType.COUNTER, tags={"host": "a"}, unit="count"
        )
        assert m.tags == {"host": "a"}
        assert m.unit == "count"


class TestAlert:
    def test_defaults(self):
        a = Alert(name="test", severity=AlertSeverity.WARNING, message="hi")
        assert a.resolved is False
        assert a.tags == {}

    def test_custom_fields(self):
        a = Alert(
            name="x", severity=AlertSeverity.CRITICAL, message="m", resolved=True, tags={"k": "v"}
        )
        assert a.resolved is True
        assert a.tags["k"] == "v"


class TestLogEntry:
    def test_defaults(self):
        le = LogEntry(level="INFO", message="hello")
        assert le.component == "unknown"
        assert le.trace_id is None
        assert le.exception is None


class TestHealthCheck:
    def test_defaults(self):
        hc = HealthCheck(name="db", status="healthy", message="ok")
        assert hc.duration_ms == 0
        assert hc.tags == {}


# ── MonitoringSystem core logic ──


class TestCalculateAggregations:
    """Test _calculate_aggregations — pure computation."""

    def _make_system(self):
        with patch.object(MonitoringSystem, "_start_background_workers"):
            return MonitoringSystem("test")

    def test_empty_metrics(self):
        sys = self._make_system()
        assert sys._calculate_aggregations([]) == []

    def test_single_metric(self):
        sys = self._make_system()
        m = Metric(name="x", value=10, metric_type=MetricType.GAUGE)
        aggs = sys._calculate_aggregations([m])
        names = {a.name for a in aggs}
        assert "x.count" in names
        assert "x.sum" in names
        assert "x.avg" in names
        assert "x.min" in names
        assert "x.max" in names
        by_name = {a.name: a.value for a in aggs}
        assert by_name["x.count"] == 1
        assert by_name["x.sum"] == 10
        assert by_name["x.avg"] == 10
        assert by_name["x.min"] == 10
        assert by_name["x.max"] == 10

    def test_multiple_metrics(self):
        sys = self._make_system()
        metrics = [
            Metric(name="y", value=2, metric_type=MetricType.COUNTER),
            Metric(name="y", value=8, metric_type=MetricType.COUNTER),
            Metric(name="y", value=5, metric_type=MetricType.COUNTER),
        ]
        aggs = sys._calculate_aggregations(metrics)
        by_name = {a.name: a.value for a in aggs}
        assert by_name["y.count"] == 3
        assert by_name["y.sum"] == 15
        assert by_name["y.avg"] == 5.0
        assert by_name["y.min"] == 2
        assert by_name["y.max"] == 8

    def test_aggregation_metric_type(self):
        sys = self._make_system()
        m = Metric(name="z", value=1, metric_type=MetricType.TIMER)
        aggs = sys._calculate_aggregations([m])
        for a in aggs:
            assert a.metric_type == MetricType.GAUGE


class TestDefaultAlertRules:
    def _make_system(self):
        with patch.object(MonitoringSystem, "_start_background_workers"):
            return MonitoringSystem("test")

    def test_alert_rules_created(self):
        sys = self._make_system()
        assert len(sys.alert_rules) >= 1

    def test_high_error_rate_triggers(self):
        sys = self._make_system()
        # First rule is high_error_rate
        rule = sys.alert_rules[0]
        # Create > 10 error metrics
        error_metrics = [
            Metric(name="error_count", value=1, metric_type=MetricType.COUNTER) for _ in range(15)
        ]
        result = rule(error_metrics)
        assert result is not None
        assert result.name == "high_error_rate"

    def test_no_alert_for_few_errors(self):
        sys = self._make_system()
        rule = sys.alert_rules[0]
        metrics = [
            Metric(name="error_count", value=1, metric_type=MetricType.COUNTER) for _ in range(5)
        ]
        result = rule(metrics)
        assert result is None
