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

# fluid_build/providers/aws/util/metrics.py
"""
Observability and metrics collection for AWS provider.

Tracks execution metrics and can optionally push to CloudWatch for production monitoring.
"""

import time
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional


class MetricsCollector:
    """Collects execution metrics for monitoring and analysis."""

    def __init__(self, namespace: str = "FLUID/Build", enabled: bool = True):
        """
        Initialize metrics collector.

        Args:
            namespace: CloudWatch namespace (if pushing metrics)
            enabled: Whether to collect metrics
        """
        self.namespace = namespace
        self.enabled = enabled
        self.metrics: List[Dict[str, Any]] = []
        self.action_timings: Dict[str, List[float]] = defaultdict(list)
        self.action_counts: Dict[str, int] = defaultdict(int)
        self.error_counts: Dict[str, int] = defaultdict(int)
        self.start_time: Optional[float] = None

    def start_execution(self) -> None:
        """Mark start of execution."""
        if self.enabled:
            self.start_time = time.time()

    def record_action(
        self, op: str, duration_ms: float, status: str, changed: bool = False
    ) -> None:
        """
        Record action execution metrics.

        Args:
            op: Operation type (e.g., "glue.ensure_table")
            duration_ms: Execution time in milliseconds
            status: Action status ("ok", "changed", "error")
            changed: Whether action made changes
        """
        if not self.enabled:
            return

        self.action_timings[op].append(duration_ms)
        self.action_counts[op] += 1

        if status == "error":
            self.error_counts[op] += 1

        self.metrics.append(
            {
                "timestamp": datetime.utcnow().isoformat(),
                "op": op,
                "duration_ms": duration_ms,
                "status": status,
                "changed": changed,
            }
        )

    def get_summary(self) -> Dict[str, Any]:
        """
        Get execution summary with statistics.

        Returns:
            Summary dict with counts, timings, and error rates
        """
        if not self.enabled:
            return {"enabled": False}

        total_duration = 0
        if self.start_time:
            total_duration = (time.time() - self.start_time) * 1000

        # Calculate statistics per operation
        op_stats = {}
        for op, timings in self.action_timings.items():
            op_stats[op] = {
                "count": self.action_counts[op],
                "errors": self.error_counts.get(op, 0),
                "error_rate": self.error_counts.get(op, 0) / self.action_counts[op],
                "total_duration_ms": sum(timings),
                "avg_duration_ms": sum(timings) / len(timings),
                "min_duration_ms": min(timings),
                "max_duration_ms": max(timings),
            }

        return {
            "enabled": True,
            "total_duration_ms": total_duration,
            "total_actions": sum(self.action_counts.values()),
            "total_errors": sum(self.error_counts.values()),
            "overall_error_rate": (
                sum(self.error_counts.values()) / sum(self.action_counts.values())
                if sum(self.action_counts.values()) > 0
                else 0
            ),
            "operations": op_stats,
        }

    def push_to_cloudwatch(self, region: str, dimensions: Optional[Dict[str, str]] = None) -> bool:
        """
        Push metrics to CloudWatch.

        Args:
            region: AWS region
            dimensions: Additional dimensions (e.g., {"Environment": "production"})

        Returns:
            True if successful, False otherwise
        """
        if not self.enabled or not self.metrics:
            return False

        try:
            import boto3

            cloudwatch = boto3.client("cloudwatch", region_name=region)

            metric_data = []

            # Aggregate metrics by operation
            for op, timings in self.action_timings.items():
                base_dimensions = [{"Name": "Operation", "Value": op}]
                if dimensions:
                    base_dimensions.extend([{"Name": k, "Value": v} for k, v in dimensions.items()])

                # Count metric
                metric_data.append(
                    {
                        "MetricName": "ActionCount",
                        "Value": self.action_counts[op],
                        "Unit": "Count",
                        "Timestamp": datetime.utcnow(),
                        "Dimensions": base_dimensions,
                    }
                )

                # Duration metrics
                metric_data.append(
                    {
                        "MetricName": "ActionDuration",
                        "Values": timings,
                        "Unit": "Milliseconds",
                        "Timestamp": datetime.utcnow(),
                        "Dimensions": base_dimensions,
                        "StatisticValues": {
                            "SampleCount": len(timings),
                            "Sum": sum(timings),
                            "Minimum": min(timings),
                            "Maximum": max(timings),
                        },
                    }
                )

                # Error count
                if op in self.error_counts:
                    metric_data.append(
                        {
                            "MetricName": "ActionErrors",
                            "Value": self.error_counts[op],
                            "Unit": "Count",
                            "Timestamp": datetime.utcnow(),
                            "Dimensions": base_dimensions,
                        }
                    )

            # Push in batches (CloudWatch limit: 20 metrics per call)
            for i in range(0, len(metric_data), 20):
                batch = metric_data[i : i + 20]
                cloudwatch.put_metric_data(Namespace=self.namespace, MetricData=batch)

            return True

        except Exception as e:
            # Don't fail execution if metrics push fails
            import logging

            logging.getLogger(__name__).warning(f"Failed to push metrics to CloudWatch: {e}")
            return False

    def export_prometheus(self) -> str:
        """
        Export metrics in Prometheus format.

        Returns:
            Prometheus-formatted metrics
        """
        if not self.enabled:
            return ""

        lines = []

        # Action count
        lines.append("# HELP fluid_action_count Total number of actions executed")
        lines.append("# TYPE fluid_action_count counter")
        for op, count in self.action_counts.items():
            lines.append(f'fluid_action_count{{operation="{op}"}} {count}')

        # Action duration
        lines.append("# HELP fluid_action_duration_ms Action execution duration in milliseconds")
        lines.append("# TYPE fluid_action_duration_ms summary")
        for op, timings in self.action_timings.items():
            lines.append(f'fluid_action_duration_ms_sum{{operation="{op}"}} {sum(timings)}')
            lines.append(f'fluid_action_duration_ms_count{{operation="{op}"}} {len(timings)}')

        # Error count
        lines.append("# HELP fluid_action_errors Total number of failed actions")
        lines.append("# TYPE fluid_action_errors counter")
        for op, count in self.error_counts.items():
            lines.append(f'fluid_action_errors{{operation="{op}"}} {count}')

        return "\n".join(lines) + "\n"


# Global metrics collector
_metrics_collector: Optional[MetricsCollector] = None


def get_metrics_collector(namespace: str = "FLUID/Build", enabled: bool = True) -> MetricsCollector:
    """
    Get or create global metrics collector.

    Args:
        namespace: CloudWatch namespace
        enabled: Whether to enable metrics collection

    Returns:
        MetricsCollector instance
    """
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector(namespace, enabled)
    return _metrics_collector


def reset_metrics_collector() -> None:
    """Reset global metrics collector."""
    global _metrics_collector
    _metrics_collector = None
