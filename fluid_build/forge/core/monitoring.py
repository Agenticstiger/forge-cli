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

"""
Monitoring and observability system for FLUID Forge

This module provides comprehensive monitoring, logging, and metrics collection
for generated projects and the FLUID Forge system itself:

1. Performance monitoring and metrics collection
2. Distributed tracing and logging
3. Health checks and alerting
4. System diagnostics and debugging
5. Real-time dashboards and reporting
"""

import json
import queue
import threading
import time
import traceback
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from fluid_build.cli.console import cprint


class MetricType(Enum):
    """Types of metrics"""

    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    TIMER = "timer"


class AlertSeverity(Enum):
    """Alert severity levels"""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class Metric:
    """A metric measurement"""

    name: str
    value: Union[int, float]
    metric_type: MetricType
    timestamp: float = field(default_factory=time.time)
    tags: Dict[str, str] = field(default_factory=dict)
    unit: Optional[str] = None


@dataclass
class Alert:
    """An alert event"""

    name: str
    severity: AlertSeverity
    message: str
    timestamp: float = field(default_factory=time.time)
    tags: Dict[str, str] = field(default_factory=dict)
    resolved: bool = False


@dataclass
class LogEntry:
    """A log entry"""

    level: str
    message: str
    timestamp: float = field(default_factory=time.time)
    component: str = "unknown"
    trace_id: Optional[str] = None
    tags: Dict[str, str] = field(default_factory=dict)
    exception: Optional[str] = None


@dataclass
class HealthCheck:
    """Health check result"""

    name: str
    status: str  # "healthy", "unhealthy", "unknown"
    message: str
    timestamp: float = field(default_factory=time.time)
    duration_ms: float = 0
    tags: Dict[str, str] = field(default_factory=dict)


class MonitoringSystem:
    """Central monitoring and observability system"""

    def __init__(self, project_name: str = "fluid-forge"):
        self.project_name = project_name
        self.metrics: Dict[str, List[Metric]] = defaultdict(list)
        self.alerts: List[Alert] = []
        self.logs: deque = deque(maxlen=10000)  # Keep last 10k log entries
        self.health_checks: Dict[str, HealthCheck] = {}
        self.traces: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        # Thread-safe queues for async processing
        self.metric_queue = queue.Queue()
        self.log_queue = queue.Queue()
        self.alert_queue = queue.Queue()

        # Background workers
        self._running = True
        self._start_background_workers()

        # Alert rules
        self.alert_rules: List[Callable[[List[Metric]], Optional[Alert]]] = []

        # Metric aggregation intervals (in seconds)
        self.aggregation_intervals = [60, 300, 3600]  # 1m, 5m, 1h
        self.aggregated_metrics: Dict[int, Dict[str, List[Metric]]] = {
            interval: defaultdict(list) for interval in self.aggregation_intervals
        }

        # Setup default alert rules
        self._setup_default_alert_rules()

    def _start_background_workers(self):
        """Start background worker threads"""

        workers = [
            ("metric_processor", self._process_metrics),
            ("log_processor", self._process_logs),
            ("alert_processor", self._process_alerts),
            ("aggregator", self._aggregate_metrics),
        ]

        for worker_name, worker_func in workers:
            thread = threading.Thread(target=worker_func, name=worker_name, daemon=True)
            thread.start()

    def _process_metrics(self):
        """Background thread to process metrics"""

        while self._running:
            try:
                metric = self.metric_queue.get(timeout=1.0)
                self.metrics[metric.name].append(metric)

                # Keep only last 1000 metrics per name
                if len(self.metrics[metric.name]) > 1000:
                    self.metrics[metric.name] = self.metrics[metric.name][-1000:]

                # Check alert rules
                self._check_alert_rules(metric)

            except queue.Empty:
                continue
            except Exception as e:
                self._log_error(f"Error processing metric: {e}")

    def _process_logs(self):
        """Background thread to process logs"""

        while self._running:
            try:
                log_entry = self.log_queue.get(timeout=1.0)
                self.logs.append(log_entry)

                # Check for error patterns that should trigger alerts
                if log_entry.level in ["ERROR", "CRITICAL"]:
                    alert = Alert(
                        name="log_error",
                        severity=(
                            AlertSeverity.ERROR
                            if log_entry.level == "ERROR"
                            else AlertSeverity.CRITICAL
                        ),
                        message=f"Log error in {log_entry.component}: {log_entry.message}",
                        tags=log_entry.tags,
                    )
                    self.alert_queue.put(alert)

            except queue.Empty:
                continue
            except Exception as e:
                self._log_error(f"Error processing log: {e}")

    def _process_alerts(self):
        """Background thread to process alerts"""

        while self._running:
            try:
                alert = self.alert_queue.get(timeout=1.0)
                self.alerts.append(alert)

                # Keep only last 1000 alerts
                if len(self.alerts) > 1000:
                    self.alerts = self.alerts[-1000:]

                # Trigger alert handlers
                self._handle_alert(alert)

            except queue.Empty:
                continue
            except Exception as e:
                self._log_error(f"Error processing alert: {e}")

    def _aggregate_metrics(self):
        """Background thread to aggregate metrics"""

        while self._running:
            try:
                current_time = time.time()

                for interval in self.aggregation_intervals:
                    # Aggregate metrics for this interval
                    window_start = current_time - interval

                    for metric_name, metric_list in self.metrics.items():
                        # Get metrics in this time window
                        window_metrics = [m for m in metric_list if m.timestamp >= window_start]

                        if window_metrics:
                            # Calculate aggregations
                            aggregated = self._calculate_aggregations(window_metrics)
                            if aggregated:
                                self.aggregated_metrics[interval][metric_name] = aggregated

                # Sleep for 30 seconds before next aggregation
                time.sleep(30)

            except Exception as e:
                self._log_error(f"Error aggregating metrics: {e}")

    def _calculate_aggregations(self, metrics: List[Metric]) -> List[Metric]:
        """Calculate aggregated metrics (avg, min, max, sum, count)"""

        if not metrics:
            return []

        values = [m.value for m in metrics]
        base_metric = metrics[0]
        timestamp = time.time()

        aggregations = []

        # Count
        aggregations.append(
            Metric(
                name=f"{base_metric.name}.count",
                value=len(values),
                metric_type=MetricType.GAUGE,
                timestamp=timestamp,
                tags=base_metric.tags,
            )
        )

        # Sum
        aggregations.append(
            Metric(
                name=f"{base_metric.name}.sum",
                value=sum(values),
                metric_type=MetricType.GAUGE,
                timestamp=timestamp,
                tags=base_metric.tags,
            )
        )

        # Average
        aggregations.append(
            Metric(
                name=f"{base_metric.name}.avg",
                value=sum(values) / len(values),
                metric_type=MetricType.GAUGE,
                timestamp=timestamp,
                tags=base_metric.tags,
            )
        )

        # Min/Max
        aggregations.append(
            Metric(
                name=f"{base_metric.name}.min",
                value=min(values),
                metric_type=MetricType.GAUGE,
                timestamp=timestamp,
                tags=base_metric.tags,
            )
        )

        aggregations.append(
            Metric(
                name=f"{base_metric.name}.max",
                value=max(values),
                metric_type=MetricType.GAUGE,
                timestamp=timestamp,
                tags=base_metric.tags,
            )
        )

        return aggregations

    def _setup_default_alert_rules(self):
        """Setup default alert rules"""

        # High error rate
        def high_error_rate(metrics: List[Metric]) -> Optional[Alert]:
            error_metrics = [m for m in metrics if "error" in m.name.lower()]
            if len(error_metrics) > 10:  # More than 10 errors in recent metrics
                return Alert(
                    name="high_error_rate",
                    severity=AlertSeverity.WARNING,
                    message=f"High error rate detected: {len(error_metrics)} errors",
                )
            return None

        # Memory usage
        def high_memory_usage(metrics: List[Metric]) -> Optional[Alert]:
            memory_metrics = [m for m in metrics if "memory" in m.name.lower()]
            if memory_metrics:
                latest = max(memory_metrics, key=lambda x: x.timestamp)
                if latest.value > 0.9:  # 90% memory usage
                    return Alert(
                        name="high_memory_usage",
                        severity=AlertSeverity.ERROR,
                        message=f"High memory usage: {latest.value:.1%}",
                    )
            return None

        # Response time
        def high_response_time(metrics: List[Metric]) -> Optional[Alert]:
            response_metrics = [m for m in metrics if "response_time" in m.name.lower()]
            if response_metrics:
                recent_metrics = [m for m in response_metrics if time.time() - m.timestamp < 300]
                if recent_metrics:
                    avg_response = sum(m.value for m in recent_metrics) / len(recent_metrics)
                    if avg_response > 5000:  # 5 seconds
                        return Alert(
                            name="high_response_time",
                            severity=AlertSeverity.WARNING,
                            message=f"High response time: {avg_response:.0f}ms",
                        )
            return None

        self.alert_rules.extend([high_error_rate, high_memory_usage, high_response_time])

    def _check_alert_rules(self, metric: Metric):
        """Check if new metric triggers any alerts"""

        # Get recent metrics for context
        recent_time = time.time() - 300  # Last 5 minutes
        recent_metrics = []

        for metric_list in self.metrics.values():
            recent_metrics.extend([m for m in metric_list if m.timestamp >= recent_time])

        # Check each alert rule
        for rule in self.alert_rules:
            try:
                alert = rule(recent_metrics)
                if alert:
                    self.alert_queue.put(alert)
            except Exception as e:
                self._log_error(f"Error checking alert rule: {e}")

    def _handle_alert(self, alert: Alert):
        """Handle triggered alert"""

        # Log the alert
        self._log_info(f"ALERT [{alert.severity.value.upper()}] {alert.name}: {alert.message}")

        # Could add integrations here:
        # - Send to Slack/Teams
        # - Send email
        # - Create ticket
        # - Send to monitoring system (Prometheus, Datadog, etc.)

    def _log_error(self, message: str):
        """Internal error logging"""
        log_entry = LogEntry(level="ERROR", message=message, component="monitoring_system")
        self.logs.append(log_entry)

    def _log_info(self, message: str):
        """Internal info logging"""
        log_entry = LogEntry(level="INFO", message=message, component="monitoring_system")
        self.logs.append(log_entry)

    # Public API methods

    def record_metric(
        self,
        name: str,
        value: Union[int, float],
        metric_type: MetricType = MetricType.GAUGE,
        tags: Optional[Dict[str, str]] = None,
        unit: Optional[str] = None,
    ):
        """Record a metric"""

        metric = Metric(name=name, value=value, metric_type=metric_type, tags=tags or {}, unit=unit)

        self.metric_queue.put(metric)

    def increment_counter(self, name: str, value: int = 1, tags: Optional[Dict[str, str]] = None):
        """Increment a counter metric"""
        self.record_metric(name, value, MetricType.COUNTER, tags)

    def set_gauge(self, name: str, value: Union[int, float], tags: Optional[Dict[str, str]] = None):
        """Set a gauge metric"""
        self.record_metric(name, value, MetricType.GAUGE, tags)

    def record_timer(self, name: str, duration_ms: float, tags: Optional[Dict[str, str]] = None):
        """Record a timer metric"""
        self.record_metric(name, duration_ms, MetricType.TIMER, tags, "ms")

    @contextmanager
    def timer(self, name: str, tags: Optional[Dict[str, str]] = None):
        """Context manager for timing operations"""
        start_time = time.time()
        try:
            yield
        finally:
            duration_ms = (time.time() - start_time) * 1000
            self.record_timer(name, duration_ms, tags)

    def log(
        self,
        level: str,
        message: str,
        component: str = "unknown",
        trace_id: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
        exception: Optional[Exception] = None,
    ):
        """Log a message"""

        log_entry = LogEntry(
            level=level.upper(),
            message=message,
            component=component,
            trace_id=trace_id,
            tags=tags or {},
            exception=traceback.format_exc() if exception else None,
        )

        self.log_queue.put(log_entry)

    def log_info(self, message: str, component: str = "unknown", **kwargs):
        """Log info message"""
        self.log("INFO", message, component, **kwargs)

    def log_warning(self, message: str, component: str = "unknown", **kwargs):
        """Log warning message"""
        self.log("WARNING", message, component, **kwargs)

    def log_error(
        self,
        message: str,
        component: str = "unknown",
        exception: Optional[Exception] = None,
        **kwargs,
    ):
        """Log error message"""
        self.log("ERROR", message, component, exception=exception, **kwargs)

    def log_critical(
        self,
        message: str,
        component: str = "unknown",
        exception: Optional[Exception] = None,
        **kwargs,
    ):
        """Log critical message"""
        self.log("CRITICAL", message, component, exception=exception, **kwargs)

    def create_alert(
        self,
        name: str,
        severity: AlertSeverity,
        message: str,
        tags: Optional[Dict[str, str]] = None,
    ):
        """Create a custom alert"""

        alert = Alert(name=name, severity=severity, message=message, tags=tags or {})

        self.alert_queue.put(alert)

    def add_health_check(self, name: str, check_func: Callable[[], Tuple[str, str]]):
        """Add a health check function"""

        def run_check():
            start_time = time.time()
            try:
                status, message = check_func()
                duration_ms = (time.time() - start_time) * 1000

                self.health_checks[name] = HealthCheck(
                    name=name, status=status, message=message, duration_ms=duration_ms
                )

                # Record health check metric
                self.record_metric(
                    f"health_check.{name}.duration", duration_ms, tags={"status": status}
                )

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000

                self.health_checks[name] = HealthCheck(
                    name=name,
                    status="unhealthy",
                    message=f"Health check failed: {str(e)}",
                    duration_ms=duration_ms,
                )

                self.log_error(f"Health check {name} failed", "health_check", exception=e)

        # Run check immediately
        run_check()

        # Schedule periodic checks (could be improved with proper scheduler)
        def periodic_check():
            while self._running:
                time.sleep(60)  # Run every minute
                run_check()

        thread = threading.Thread(target=periodic_check, name=f"health_check_{name}", daemon=True)
        thread.start()

    def get_metrics(
        self, name_pattern: Optional[str] = None, since: Optional[float] = None
    ) -> List[Metric]:
        """Get metrics matching pattern and time range"""

        all_metrics = []

        for metric_name, metric_list in self.metrics.items():
            if name_pattern and name_pattern not in metric_name:
                continue

            for metric in metric_list:
                if since and metric.timestamp < since:
                    continue
                all_metrics.append(metric)

        return sorted(all_metrics, key=lambda x: x.timestamp)

    def get_aggregated_metrics(
        self, interval: int, name_pattern: Optional[str] = None
    ) -> List[Metric]:
        """Get aggregated metrics for a specific interval"""

        if interval not in self.aggregated_metrics:
            return []

        all_metrics = []

        for metric_name, metric_list in self.aggregated_metrics[interval].items():
            if name_pattern and name_pattern not in metric_name:
                continue
            all_metrics.extend(metric_list)

        return sorted(all_metrics, key=lambda x: x.timestamp)

    def get_alerts(
        self, severity: Optional[AlertSeverity] = None, since: Optional[float] = None
    ) -> List[Alert]:
        """Get alerts matching criteria"""

        filtered_alerts = []

        for alert in self.alerts:
            if severity and alert.severity != severity:
                continue
            if since and alert.timestamp < since:
                continue
            filtered_alerts.append(alert)

        return sorted(filtered_alerts, key=lambda x: x.timestamp, reverse=True)

    def get_logs(
        self,
        level: Optional[str] = None,
        component: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 100,
    ) -> List[LogEntry]:
        """Get logs matching criteria"""

        filtered_logs = []

        for log_entry in self.logs:
            if level and log_entry.level != level.upper():
                continue
            if component and log_entry.component != component:
                continue
            if since and log_entry.timestamp < since:
                continue
            filtered_logs.append(log_entry)

        # Sort by timestamp (newest first) and limit
        filtered_logs.sort(key=lambda x: x.timestamp, reverse=True)
        return filtered_logs[:limit]

    def get_health_status(self) -> Dict[str, HealthCheck]:
        """Get current health status of all checks"""
        return dict(self.health_checks)

    def export_metrics(self, format: str = "json") -> str:
        """Export metrics in specified format"""

        if format.lower() == "json":
            return self._export_json()
        elif format.lower() == "prometheus":
            return self._export_prometheus()
        else:
            raise ValueError(f"Unsupported export format: {format}")

    def _export_json(self) -> str:
        """Export metrics as JSON"""

        data = {
            "timestamp": time.time(),
            "project": self.project_name,
            "metrics": [],
            "alerts": [],
            "health_checks": [],
        }

        # Export recent metrics (last hour)
        since = time.time() - 3600
        for metric in self.get_metrics(since=since):
            data["metrics"].append(
                {
                    "name": metric.name,
                    "value": metric.value,
                    "type": metric.metric_type.value,
                    "timestamp": metric.timestamp,
                    "tags": metric.tags,
                    "unit": metric.unit,
                }
            )

        # Export recent alerts
        for alert in self.get_alerts(since=since):
            data["alerts"].append(
                {
                    "name": alert.name,
                    "severity": alert.severity.value,
                    "message": alert.message,
                    "timestamp": alert.timestamp,
                    "tags": alert.tags,
                    "resolved": alert.resolved,
                }
            )

        # Export health checks
        for health_check in self.health_checks.values():
            data["health_checks"].append(
                {
                    "name": health_check.name,
                    "status": health_check.status,
                    "message": health_check.message,
                    "timestamp": health_check.timestamp,
                    "duration_ms": health_check.duration_ms,
                    "tags": health_check.tags,
                }
            )

        return json.dumps(data, indent=2)

    def _export_prometheus(self) -> str:
        """Export metrics in Prometheus format"""

        lines = []

        # Group metrics by name
        metric_groups = defaultdict(list)
        since = time.time() - 3600  # Last hour

        for metric in self.get_metrics(since=since):
            metric_groups[metric.name].append(metric)

        for metric_name, metrics in metric_groups.items():
            if not metrics:
                continue

            # Use latest value for each unique tag combination
            latest_by_tags = {}
            for metric in metrics:
                tag_key = json.dumps(metric.tags, sort_keys=True)
                if (
                    tag_key not in latest_by_tags
                    or metric.timestamp > latest_by_tags[tag_key].timestamp
                ):
                    latest_by_tags[tag_key] = metric

            # Generate Prometheus format
            for metric in latest_by_tags.values():
                # Sanitize metric name for Prometheus
                prom_name = metric_name.replace(".", "_").replace("-", "_")

                if metric.tags:
                    tag_str = ",".join([f'{k}="{v}"' for k, v in metric.tags.items()])
                    lines.append(
                        f"{prom_name}{{{tag_str}}} {metric.value} {int(metric.timestamp * 1000)}"
                    )
                else:
                    lines.append(f"{prom_name} {metric.value} {int(metric.timestamp * 1000)}")

        return "\n".join(lines)

    def generate_dashboard(self) -> str:
        """Generate a simple HTML dashboard"""

        # Get recent data
        since = time.time() - 3600  # Last hour
        metrics = self.get_metrics(since=since)
        alerts = self.get_alerts(since=since)
        logs = self.get_logs(since=since, limit=50)
        health_checks = self.get_health_status()

        html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>FLUID Forge Monitoring Dashboard</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        .header {{ background: #f0f0f0; padding: 20px; border-radius: 5px; }}
        .section {{ margin: 20px 0; padding: 15px; border: 1px solid #ddd; border-radius: 5px; }}
        .metric {{ margin: 10px 0; }}
        .alert {{ padding: 10px; margin: 5px 0; border-radius: 3px; }}
        .alert.error {{ background: #ffebee; border-left: 4px solid #f44336; }}
        .alert.warning {{ background: #fff3e0; border-left: 4px solid #ff9800; }}
        .alert.info {{ background: #e3f2fd; border-left: 4px solid #2196f3; }}
        .health.healthy {{ color: green; }}
        .health.unhealthy {{ color: red; }}
        .log {{ font-family: monospace; font-size: 12px; margin: 2px 0; }}
        .log.error {{ color: red; }}
        .log.warning {{ color: orange; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #f2f2f2; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>FLUID Forge Monitoring Dashboard</h1>
        <p>Project: {self.project_name} | Last Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}</p>
    </div>
    
    <div class="section">
        <h2>Health Status</h2>
        <table>
            <tr><th>Check</th><th>Status</th><th>Message</th><th>Duration</th></tr>
        """

        for check in health_checks.values():
            status_class = "healthy" if check.status == "healthy" else "unhealthy"
            html += f"""
            <tr>
                <td>{check.name}</td>
                <td class="health {status_class}">{check.status}</td>
                <td>{check.message}</td>
                <td>{check.duration_ms:.1f}ms</td>
            </tr>
            """

        html += """
        </table>
    </div>
    
    <div class="section">
        <h2>Recent Alerts</h2>
        """

        if alerts:
            for alert in alerts[:10]:  # Show last 10 alerts
                html += f"""
                <div class="alert {alert.severity.value}">
                    <strong>{alert.name}</strong> [{alert.severity.value.upper()}]<br>
                    {alert.message}<br>
                    <small>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(alert.timestamp))}</small>
                </div>
                """
        else:
            html += "<p>No recent alerts</p>"

        html += """
    </div>
    
    <div class="section">
        <h2>Key Metrics (Last Hour)</h2>
        <table>
            <tr><th>Metric</th><th>Value</th><th>Type</th><th>Tags</th></tr>
        """

        # Show latest value for each metric
        latest_metrics = {}
        for metric in metrics:
            if (
                metric.name not in latest_metrics
                or metric.timestamp > latest_metrics[metric.name].timestamp
            ):
                latest_metrics[metric.name] = metric

        for metric in list(latest_metrics.values())[:20]:  # Show top 20 metrics
            tags_str = (
                ", ".join([f"{k}={v}" for k, v in metric.tags.items()]) if metric.tags else ""
            )
            unit_str = f" {metric.unit}" if metric.unit else ""
            html += f"""
            <tr>
                <td>{metric.name}</td>
                <td>{metric.value}{unit_str}</td>
                <td>{metric.metric_type.value}</td>
                <td>{tags_str}</td>
            </tr>
            """

        html += """
        </table>
    </div>
    
    <div class="section">
        <h2>Recent Logs</h2>
        <div style="background: #f8f8f8; padding: 10px; border-radius: 3px; max-height: 400px; overflow-y: auto;">
        """

        for log_entry in logs:
            level_class = log_entry.level.lower()
            timestamp_str = time.strftime("%H:%M:%S", time.localtime(log_entry.timestamp))
            html += f"""
            <div class="log {level_class}">
                [{timestamp_str}] [{log_entry.level}] [{log_entry.component}] {log_entry.message}
            </div>
            """

        html += """
        </div>
    </div>
    
    <script>
        // Auto-refresh every 30 seconds
        setTimeout(function(){ location.reload(); }, 30000);
    </script>
</body>
</html>
        """

        return html

    def shutdown(self):
        """Shutdown monitoring system"""
        self._running = False


# Global monitoring instance
_global_monitor: Optional[MonitoringSystem] = None


def get_monitor(project_name: str = "fluid-forge") -> MonitoringSystem:
    """Get global monitoring instance"""
    global _global_monitor

    if _global_monitor is None:
        _global_monitor = MonitoringSystem(project_name)

    return _global_monitor


# Convenience functions for global monitoring
def record_metric(name: str, value: Union[int, float], **kwargs):
    """Record a metric using global monitor"""
    get_monitor().record_metric(name, value, **kwargs)


def log_info(message: str, component: str = "unknown", **kwargs):
    """Log info using global monitor"""
    get_monitor().log_info(message, component, **kwargs)


def log_error(message: str, component: str = "unknown", **kwargs):
    """Log error using global monitor"""
    get_monitor().log_error(message, component, **kwargs)


def timer(name: str, tags: Optional[Dict[str, str]] = None):
    """Timer context manager using global monitor"""
    return get_monitor().timer(name, tags)


if __name__ == "__main__":
    # Demo/test the monitoring system
    monitor = MonitoringSystem("demo-project")

    # Add some sample data
    monitor.record_metric("cpu_usage", 75.5, MetricType.GAUGE, {"host": "web-01"})
    monitor.record_metric("memory_usage", 0.82, MetricType.GAUGE, {"host": "web-01"})
    monitor.record_metric("request_count", 1, MetricType.COUNTER, {"endpoint": "/api/users"})
    monitor.record_metric(
        "response_time", 250.0, MetricType.TIMER, {"endpoint": "/api/users"}, "ms"
    )

    monitor.log_info("Service started", "web-server")
    monitor.log_warning("High memory usage detected", "monitor")

    monitor.create_alert("test_alert", AlertSeverity.WARNING, "This is a test alert")

    # Add health check
    def sample_health_check():
        return "healthy", "All systems operational"

    monitor.add_health_check("web_server", sample_health_check)

    # Wait a bit for background processing
    time.sleep(2)

    # Generate dashboard
    dashboard = monitor.generate_dashboard()
    cprint("Dashboard generated:")
    cprint(f"Length: {len(dashboard)} characters")

    # Export metrics
    json_export = monitor.export_metrics("json")
    cprint(f"\nJSON export length: {len(json_export)} characters")

    prom_export = monitor.export_metrics("prometheus")
    cprint(f"Prometheus export length: {len(prom_export)} characters")

    monitor.shutdown()
