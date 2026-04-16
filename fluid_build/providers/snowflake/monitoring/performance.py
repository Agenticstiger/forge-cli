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

# fluid_build/providers/snowflake/monitoring/performance.py
"""
Snowflake Performance Monitoring.

Tracks query execution time, resource usage, and Snowflake credit consumption.
Provides performance metrics, alerts, and optimization recommendations.

Features:
- Query execution time tracking
- Snowflake credit usage monitoring
- Warehouse utilization metrics
- Query optimization recommendations
- Performance regression detection
- Cost analysis and alerts

Usage:
    from fluid_build.providers.snowflake.monitoring import PerformanceMonitor

    monitor = PerformanceMonitor(connection)

    with monitor.track_query("load_data"):
        cursor.execute("COPY INTO table...")

    metrics = monitor.get_metrics()
    report = monitor.generate_report()
"""

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class QueryMetrics:
    """Metrics for a single query execution."""

    query_id: str
    query_text: str
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_seconds: Optional[float] = None
    rows_produced: Optional[int] = None
    bytes_scanned: Optional[int] = None
    credits_used: Optional[float] = None
    warehouse_name: Optional[str] = None
    warehouse_size: Optional[str] = None
    execution_status: str = "RUNNING"
    error_message: Optional[str] = None

    def __post_init__(self):
        """Calculate derived metrics."""
        if self.end_time and self.start_time:
            self.duration_seconds = (self.end_time - self.start_time).total_seconds()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "query_id": self.query_id,
            "query_text": self.query_text[:500],  # Truncate long queries
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "duration_seconds": self.duration_seconds,
            "rows_produced": self.rows_produced,
            "bytes_scanned": self.bytes_scanned,
            "credits_used": self.credits_used,
            "warehouse_name": self.warehouse_name,
            "warehouse_size": self.warehouse_size,
            "execution_status": self.execution_status,
            "error_message": self.error_message,
        }


@dataclass
class PerformanceReport:
    """Performance analysis report."""

    total_queries: int
    total_duration: float
    total_credits: float
    avg_query_time: float
    slowest_queries: List[QueryMetrics]
    most_expensive_queries: List[QueryMetrics]
    warehouse_utilization: Dict[str, Any]
    recommendations: List[str]
    time_period: Dict[str, str]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "total_queries": self.total_queries,
            "total_duration_seconds": self.total_duration,
            "total_credits_used": self.total_credits,
            "avg_query_time_seconds": self.avg_query_time,
            "slowest_queries": [q.to_dict() for q in self.slowest_queries],
            "most_expensive_queries": [q.to_dict() for q in self.most_expensive_queries],
            "warehouse_utilization": self.warehouse_utilization,
            "recommendations": self.recommendations,
            "time_period": self.time_period,
        }


class PerformanceMonitor:
    """
    Monitor Snowflake query performance and resource usage.

    Features:
    - Track query execution time
    - Monitor Snowflake credit consumption
    - Analyze warehouse utilization
    - Detect performance regressions
    - Generate optimization recommendations
    """

    def __init__(self, connection, alert_threshold_seconds: float = 60.0):
        """
        Initialize performance monitor.

        Args:
            connection: Snowflake connection object
            alert_threshold_seconds: Alert if query exceeds this duration
        """
        self.connection = connection
        self.alert_threshold_seconds = alert_threshold_seconds
        self.metrics: List[QueryMetrics] = []
        self.query_history: Dict[str, List[float]] = {}  # query_name -> durations

    @contextmanager
    def track_query(self, query_name: str, query_text: str = ""):
        """
        Context manager to track query execution.

        Args:
            query_name: Human-readable query name
            query_text: SQL query text

        Usage:
            with monitor.track_query("load_data", "COPY INTO ..."):
                cursor.execute(query)
        """
        start_time = datetime.now()
        query_id = f"{query_name}_{start_time.strftime('%Y%m%d_%H%M%S')}"

        metric = QueryMetrics(
            query_id=query_id,
            query_text=query_text or query_name,
            start_time=start_time,
        )

        try:
            yield metric

            metric.end_time = datetime.now()
            metric.execution_status = "SUCCESS"

            # Fetch execution details from Snowflake
            self._enrich_metrics(metric)

        except Exception as e:
            metric.end_time = datetime.now()
            metric.execution_status = "FAILED"
            metric.error_message = str(e)
            logger.error(f"Query {query_name} failed: {e}")
            raise

        finally:
            self.metrics.append(metric)

            # Track query duration for regression detection
            if query_name not in self.query_history:
                self.query_history[query_name] = []
            if metric.duration_seconds:
                self.query_history[query_name].append(metric.duration_seconds)

            # Alert if query is slow
            if metric.duration_seconds and metric.duration_seconds > self.alert_threshold_seconds:
                logger.warning(
                    f"SLOW QUERY ALERT: {query_name} took {metric.duration_seconds:.2f}s "
                    f"(threshold: {self.alert_threshold_seconds}s)"
                )

            # Check for performance regression
            regression = self._detect_regression(query_name)
            if regression:
                logger.warning(
                    f"PERFORMANCE REGRESSION: {query_name} is {regression:.1f}% slower than average"
                )

    def _enrich_metrics(self, metric: QueryMetrics):
        """
        Enrich metrics with data from Snowflake QUERY_HISTORY.

        Args:
            metric: QueryMetrics to enrich
        """
        try:
            cursor = self.connection.cursor()

            # Query execution details from INFORMATION_SCHEMA
            query = """
                SELECT 
                    QUERY_ID,
                    ROWS_PRODUCED,
                    BYTES_SCANNED,
                    CREDITS_USED_CLOUD_SERVICES,
                    WAREHOUSE_NAME,
                    WAREHOUSE_SIZE,
                    EXECUTION_STATUS
                FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
                WHERE START_TIME >= DATEADD(minute, -5, CURRENT_TIMESTAMP())
                  AND QUERY_TEXT LIKE %s
                ORDER BY START_TIME DESC
                LIMIT 1
            """

            cursor.execute(query, (f"%{metric.query_text[:100]}%",))
            row = cursor.fetchone()

            if row:
                metric.rows_produced = row[1]
                metric.bytes_scanned = row[2]
                metric.credits_used = float(row[3]) if row[3] else 0.0
                metric.warehouse_name = row[4]
                metric.warehouse_size = row[5]

                logger.debug(
                    f"Query {metric.query_id}: {metric.rows_produced} rows, "
                    f"{metric.bytes_scanned} bytes, {metric.credits_used:.4f} credits"
                )

        except Exception as e:
            logger.debug(f"Failed to enrich metrics: {e}")

    def _detect_regression(self, query_name: str) -> Optional[float]:
        """
        Detect performance regression for a query.

        Args:
            query_name: Query name to check

        Returns:
            Percentage regression (e.g., 25.0 for 25% slower) or None
        """
        history = self.query_history.get(query_name, [])

        if len(history) < 3:
            return None  # Not enough history

        # Compare latest execution to average of previous executions
        latest = history[-1]
        previous_avg = sum(history[:-1]) / len(history[:-1])

        if previous_avg == 0:
            return None

        regression_pct = ((latest - previous_avg) / previous_avg) * 100

        # Only report if >20% slower
        if regression_pct > 20:
            return regression_pct

        return None

    def get_metrics(self, last_n: Optional[int] = None) -> List[QueryMetrics]:
        """
        Get query metrics.

        Args:
            last_n: Return only last N metrics (or all if None)

        Returns:
            List of QueryMetrics
        """
        if last_n:
            return self.metrics[-last_n:]
        return self.metrics

    def get_total_credits(self) -> float:
        """Get total Snowflake credits used."""
        return sum(m.credits_used or 0.0 for m in self.metrics)

    def get_total_duration(self) -> float:
        """Get total query execution time in seconds."""
        return sum(m.duration_seconds or 0.0 for m in self.metrics)

    def generate_report(self, top_n: int = 10) -> PerformanceReport:
        """
        Generate comprehensive performance report.

        Args:
            top_n: Number of top queries to include

        Returns:
            PerformanceReport with analysis and recommendations
        """
        if not self.metrics:
            raise ValueError("No metrics available")

        # Calculate aggregates
        total_queries = len(self.metrics)
        total_duration = self.get_total_duration()
        total_credits = self.get_total_credits()
        avg_query_time = total_duration / total_queries if total_queries > 0 else 0

        # Find slowest queries
        slowest = sorted(
            [m for m in self.metrics if m.duration_seconds],
            key=lambda m: m.duration_seconds,
            reverse=True,
        )[:top_n]

        # Find most expensive queries
        most_expensive = sorted(
            [m for m in self.metrics if m.credits_used], key=lambda m: m.credits_used, reverse=True
        )[:top_n]

        # Warehouse utilization
        warehouse_stats = self._analyze_warehouse_utilization()

        # Generate recommendations
        recommendations = self._generate_recommendations(
            avg_query_time, slowest, most_expensive, warehouse_stats
        )

        # Time period
        start = min(m.start_time for m in self.metrics)
        end = max(m.end_time for m in self.metrics if m.end_time)

        return PerformanceReport(
            total_queries=total_queries,
            total_duration=total_duration,
            total_credits=total_credits,
            avg_query_time=avg_query_time,
            slowest_queries=slowest,
            most_expensive_queries=most_expensive,
            warehouse_utilization=warehouse_stats,
            recommendations=recommendations,
            time_period={
                "start": start.isoformat(),
                "end": end.isoformat() if end else datetime.now().isoformat(),
            },
        )

    def _analyze_warehouse_utilization(self) -> Dict[str, Any]:
        """Analyze warehouse utilization patterns."""
        warehouse_usage = {}

        for metric in self.metrics:
            if not metric.warehouse_name:
                continue

            if metric.warehouse_name not in warehouse_usage:
                warehouse_usage[metric.warehouse_name] = {
                    "queries": 0,
                    "total_duration": 0.0,
                    "total_credits": 0.0,
                    "size": metric.warehouse_size,
                }

            warehouse_usage[metric.warehouse_name]["queries"] += 1
            warehouse_usage[metric.warehouse_name]["total_duration"] += metric.duration_seconds or 0
            warehouse_usage[metric.warehouse_name]["total_credits"] += metric.credits_used or 0

        return warehouse_usage

    def _generate_recommendations(
        self,
        avg_query_time: float,
        slowest: List[QueryMetrics],
        most_expensive: List[QueryMetrics],
        warehouse_stats: Dict[str, Any],
    ) -> List[str]:
        """Generate optimization recommendations."""
        recommendations = []

        # Check average query time
        if avg_query_time > 30:
            recommendations.append(
                f"⚠️  Average query time is {avg_query_time:.1f}s. "
                "Consider optimizing queries or increasing warehouse size."
            )

        # Check for very slow queries
        if slowest and slowest[0].duration_seconds > 300:
            recommendations.append(
                f"🐌 Slowest query took {slowest[0].duration_seconds:.1f}s. "
                "Review query plan and consider adding indexes or materialized views."
            )

        # Check for expensive queries
        if most_expensive and most_expensive[0].credits_used > 1.0:
            recommendations.append(
                f"💰 Most expensive query used {most_expensive[0].credits_used:.2f} credits. "
                "Consider result caching or query optimization."
            )

        # Check warehouse utilization
        for warehouse_name, stats in warehouse_stats.items():
            avg_duration = stats["total_duration"] / stats["queries"]

            if avg_duration < 5 and stats["size"] not in ["XSMALL", "SMALL"]:
                recommendations.append(
                    f"📉 Warehouse {warehouse_name} ({stats['size']}) may be oversized. "
                    f"Average query time is only {avg_duration:.1f}s. "
                    "Consider downsizing to save costs."
                )

            if avg_duration > 60 and stats["size"] in ["XSMALL", "SMALL"]:
                recommendations.append(
                    f"📈 Warehouse {warehouse_name} ({stats['size']}) may be undersized. "
                    f"Average query time is {avg_duration:.1f}s. "
                    "Consider upsizing for better performance."
                )

        if not recommendations:
            recommendations.append(
                "✅ No major performance issues detected. Keep up the good work!"
            )

        return recommendations

    def export_metrics(self, filepath: str):
        """
        Export metrics to JSON file.

        Args:
            filepath: Path to output file
        """
        data = {
            "metrics": [m.to_dict() for m in self.metrics],
            "summary": {
                "total_queries": len(self.metrics),
                "total_duration": self.get_total_duration(),
                "total_credits": self.get_total_credits(),
            },
        }

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

        logger.info(f"Exported {len(self.metrics)} metrics to {filepath}")

    def reset_metrics(self):
        """Clear all collected metrics."""
        self.metrics.clear()
        self.query_history.clear()
        logger.debug("Reset performance metrics")
