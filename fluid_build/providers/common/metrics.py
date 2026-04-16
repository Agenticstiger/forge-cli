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

"""Metrics collection for monitoring and observability"""

import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass
class MetricsCollector:
    """Collect and manage metrics for monitoring

    Tracks:
    - Publish requests and success/failure rates
    - Operation latency
    - Validation errors
    - Circuit breaker stats
    """

    publish_requests: Counter = field(default_factory=Counter)
    publish_success: Counter = field(default_factory=Counter)
    publish_failures: Counter = field(default_factory=Counter)
    publish_latency: dict = field(default_factory=lambda: defaultdict(list))
    validation_errors: Counter = field(default_factory=Counter)
    circuit_breaker_stats: dict = field(default_factory=dict)

    def record_publish_request(self, catalog_type: str):
        """Record a publish request"""
        self.publish_requests[catalog_type] += 1

    def record_publish_success(self, catalog_type: str, latency: float):
        """Record successful publish operation"""
        self.publish_success[catalog_type] += 1
        self.publish_latency[catalog_type].append(latency)

    def record_publish_failure(self, catalog_type: str, error_type: str):
        """Record failed publish operation"""
        self.publish_failures[catalog_type] += 1
        self.validation_errors[f"{catalog_type}:{error_type}"] += 1

    def record_validation_error(self, error_type: str):
        """Record validation error"""
        self.validation_errors[error_type] += 1

    def update_circuit_breaker_stats(
        self, catalog_type: str, state: str, failure_count: int, success_count: int
    ):
        """Update circuit breaker statistics"""
        self.circuit_breaker_stats[catalog_type] = {
            "state": state,
            "failure_count": failure_count,
            "success_count": success_count,
            "timestamp": time.time(),
        }

    def get_summary(self) -> Dict[str, Any]:
        """Get comprehensive metrics summary"""
        total_requests = sum(self.publish_requests.values())
        total_success = sum(self.publish_success.values())
        total_failures = sum(self.publish_failures.values())
        success_rate = (total_success / total_requests * 100) if total_requests > 0 else 0.0

        # Calculate average latencies
        avg_latencies = {}
        for catalog_type, latencies in self.publish_latency.items():
            if latencies:
                avg_latencies[catalog_type] = sum(latencies) / len(latencies)

        return {
            "total_requests": total_requests,
            "total_success": total_success,
            "total_failures": total_failures,
            "success_rate": round(success_rate, 2),
            "avg_latencies": avg_latencies,
            "validation_errors": dict(self.validation_errors),
            "circuit_breaker_stats": self.circuit_breaker_stats,
            "requests_by_catalog": dict(self.publish_requests),
            "success_by_catalog": dict(self.publish_success),
            "failures_by_catalog": dict(self.publish_failures),
        }

    def get_health_score(self) -> float:
        """Calculate overall health score (0.0 to 1.0)"""
        total_requests = sum(self.publish_requests.values())
        if total_requests == 0:
            return 1.0

        total_success = sum(self.publish_success.values())
        return total_success / total_requests

    def reset(self):
        """Reset all metrics"""
        self.publish_requests.clear()
        self.publish_success.clear()
        self.publish_failures.clear()
        self.publish_latency.clear()
        self.validation_errors.clear()
        self.circuit_breaker_stats.clear()


# Global metrics collector instance
metrics_collector = MetricsCollector()
