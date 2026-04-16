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
Validation result caching system.

Provides efficient caching of validation results to reduce API calls and improve
performance, especially in CI/CD environments.
"""

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fluid_build.providers.validation_provider import ResourceSchema, ValidationResult


class ValidationCache:
    """
    Cache for validation results and resource schemas.

    Stores validation results to disk with configurable TTL to avoid
    repeated expensive API calls to cloud providers.
    """

    def __init__(self, cache_dir: Optional[Path] = None, ttl: int = 3600):
        """
        Initialize validation cache.

        Args:
            cache_dir: Directory to store cache files (default: ~/.fluid/cache)
            ttl: Time-to-live in seconds for cache entries (default: 1 hour)
        """
        if cache_dir is None:
            cache_dir = Path.home() / ".fluid" / "cache"

        self.cache_dir = Path(cache_dir)
        self.ttl = ttl
        self._ensure_cache_dir()

    def _ensure_cache_dir(self):
        """Create cache directory if it doesn't exist"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_key(self, resource_fqn: str, provider: str) -> str:
        """
        Generate cache key for a resource.

        Args:
            resource_fqn: Fully qualified resource name
            provider: Provider name (gcp, snowflake, etc.)

        Returns:
            Cache key (hash of resource and provider)
        """
        key_string = f"{provider}:{resource_fqn}"
        return hashlib.sha256(key_string.encode()).hexdigest()

    def _get_cache_path(self, cache_key: str) -> Path:
        """Get file path for cache entry"""
        return self.cache_dir / f"{cache_key}.json"

    def get_schema(self, resource_fqn: str, provider: str) -> Optional[ResourceSchema]:
        """
        Retrieve cached resource schema.

        Args:
            resource_fqn: Fully qualified resource name
            provider: Provider name

        Returns:
            ResourceSchema if cached and fresh, None otherwise
        """
        cache_key = self._get_cache_key(resource_fqn, provider)
        cache_path = self._get_cache_path(cache_key)

        if not cache_path.exists():
            return None

        # Check if cache is fresh
        cache_age = time.time() - cache_path.stat().st_mtime
        if cache_age > self.ttl:
            # Cache expired, remove it
            cache_path.unlink()
            return None

        # Load and parse cache
        try:
            data = json.loads(cache_path.read_text())

            # Reconstruct ResourceSchema
            from fluid_build.providers.validation_provider import FieldSchema, ResourceType

            fields = [
                FieldSchema(
                    name=f["name"],
                    type=f["type"],
                    mode=f.get("mode", "NULLABLE"),
                    description=f.get("description"),
                )
                for f in data.get("fields", [])
            ]

            return ResourceSchema(
                resource_type=ResourceType(data["resource_type"]),
                fully_qualified_name=data["fully_qualified_name"],
                fields=fields,
                row_count=data.get("row_count"),
                size_bytes=data.get("size_bytes"),
                last_modified=data.get("last_modified"),
                metadata=data.get("metadata", {}),
            )
        except Exception:
            # If cache is corrupted, remove it
            cache_path.unlink()
            return None

    def set_schema(self, resource_fqn: str, provider: str, schema: ResourceSchema):
        """
        Store resource schema in cache.

        Args:
            resource_fqn: Fully qualified resource name
            provider: Provider name
            schema: ResourceSchema to cache
        """
        cache_key = self._get_cache_key(resource_fqn, provider)
        cache_path = self._get_cache_path(cache_key)

        # Convert schema to dict
        data = {
            "resource_type": schema.resource_type.value,
            "fully_qualified_name": schema.fully_qualified_name,
            "fields": [
                {"name": f.name, "type": f.type, "mode": f.mode, "description": f.description}
                for f in schema.fields
            ],
            "row_count": schema.row_count,
            "size_bytes": schema.size_bytes,
            "last_modified": schema.last_modified,
            "metadata": schema.metadata,
        }

        # Write to cache
        cache_path.write_text(json.dumps(data, indent=2))

    def invalidate(self, resource_fqn: str, provider: str):
        """
        Invalidate cache entry for a resource.

        Args:
            resource_fqn: Fully qualified resource name
            provider: Provider name
        """
        cache_key = self._get_cache_key(resource_fqn, provider)
        cache_path = self._get_cache_path(cache_key)

        if cache_path.exists():
            cache_path.unlink()

    def clear(self):
        """Clear all cache entries"""
        for cache_file in self.cache_dir.glob("*.json"):
            cache_file.unlink()

    def get_cache_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache stats
        """
        # Ensure cache directory exists
        self._ensure_cache_dir()

        cache_files = list(self.cache_dir.glob("*.json"))
        total_files = len(cache_files)

        fresh_count = 0
        stale_count = 0
        total_size = 0

        current_time = time.time()

        for cache_file in cache_files:
            cache_age = current_time - cache_file.stat().st_mtime
            total_size += cache_file.stat().st_size

            if cache_age <= self.ttl:
                fresh_count += 1
            else:
                stale_count += 1

        return {
            "total_entries": total_files,
            "fresh_entries": fresh_count,
            "stale_entries": stale_count,
            "total_size_bytes": total_size,
            "cache_dir": str(self.cache_dir),
            "ttl_seconds": self.ttl,
        }


class ValidationResultHistory:
    """
    Track validation results over time for drift detection.

    Stores validation results in append-only log format for historical analysis.
    """

    def __init__(self, history_dir: Optional[Path] = None):
        """
        Initialize validation history tracker.

        Args:
            history_dir: Directory to store history files (default: ./runtime)
        """
        if history_dir is None:
            history_dir = Path("runtime")

        self.history_dir = Path(history_dir)
        self.history_file = self.history_dir / "validation-history.jsonl"
        self._ensure_history_dir()

    def _ensure_history_dir(self):
        """Create history directory if it doesn't exist"""
        self.history_dir.mkdir(parents=True, exist_ok=True)

    def record_validation(self, contract_path: str, result: ValidationResult, provider: str):
        """
        Record a validation result.

        Args:
            contract_path: Path to contract file
            result: ValidationResult to record
            provider: Provider name
        """
        from datetime import datetime

        record = {
            "timestamp": datetime.now().isoformat(),
            "contract": contract_path,
            "provider": provider,
            "resource": result.resource_name,
            "success": result.success,
            "error_count": len([i for i in result.issues if i.severity == "error"]),
            "warning_count": len([i for i in result.issues if i.severity == "warning"]),
            "issues": [
                {
                    "severity": issue.severity,
                    "category": issue.category,
                    "message": issue.message,
                    "path": issue.path,
                }
                for issue in result.issues
            ],
        }

        # Append to history file
        try:
            with self.history_file.open("a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            # Log but don't fail validation if history recording fails
            import logging

            logging.getLogger("fluid.validation.history").warning(
                f"Failed to record validation history: {e}"
            )

    def get_recent_validations(self, limit: int = 10) -> list:
        """
        Get recent validation results.

        Args:
            limit: Maximum number of results to return

        Returns:
            List of recent validation records
        """
        if not self.history_file.exists():
            return []

        # Read last N lines
        with self.history_file.open("r") as f:
            lines = f.readlines()

        recent_lines = lines[-limit:] if len(lines) > limit else lines
        return [json.loads(line) for line in recent_lines]

    def detect_drift(
        self, contract_path: str, resource_name: str, lookback: int = 5
    ) -> Dict[str, Any]:
        """
        Detect drift in validation results over time.

        Args:
            contract_path: Path to contract file
            resource_name: Resource to check
            lookback: Number of historical validations to analyze

        Returns:
            Drift analysis results
        """
        if not self.history_file.exists():
            return {"drift_detected": False, "message": "No historical data"}

        # Find relevant validations
        relevant_validations = []
        with self.history_file.open("r") as f:
            for line in f:
                record = json.loads(line)
                if record["contract"] == contract_path and record["resource"] == resource_name:
                    relevant_validations.append(record)

        if len(relevant_validations) < 2:
            return {"drift_detected": False, "message": "Insufficient historical data"}

        # Analyze recent validations
        recent = relevant_validations[-lookback:]

        # Check for degradation
        error_counts = [v["error_count"] for v in recent]
        _warning_counts = [v["warning_count"] for v in recent]  # noqa: F841

        # Detect increasing error trend
        if len(error_counts) >= 2:
            if error_counts[-1] > error_counts[0]:
                return {
                    "drift_detected": True,
                    "type": "degradation",
                    "message": f"Error count increased from {error_counts[0]} to {error_counts[-1]}",
                    "previous_errors": error_counts[0],
                    "current_errors": error_counts[-1],
                }

        # Check for new issues
        latest = recent[-1]
        if len(recent) > 1:
            previous = recent[-2]
            latest_categories = {i["category"] for i in latest["issues"]}
            previous_categories = {i["category"] for i in previous["issues"]}

            new_categories = latest_categories - previous_categories
            if new_categories:
                return {
                    "drift_detected": True,
                    "type": "new_issues",
                    "message": f'New issue categories detected: {", ".join(new_categories)}',
                    "new_categories": list(new_categories),
                }

        return {"drift_detected": False, "message": "No significant drift detected"}
