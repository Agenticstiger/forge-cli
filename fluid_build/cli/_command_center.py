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
FLUID Command Center Client - Unified Detection and Integration

Provides centralized Command Center detection, configuration, and feature discovery
for use across CLI commands (market, marketplace, etc.)
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

try:
    import requests

    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


@dataclass
class CommandCenterFeatures:
    """Available Command Center features"""

    marketplace: bool = False
    catalog: bool = False
    governance: bool = False
    analytics: bool = False
    version: str = "unknown"


class CommandCenterClient:
    """
    Unified Command Center client with auto-detection.

    Detects FLUID Command Center availability and determines which
    features are accessible. Falls back gracefully if unavailable.
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        """
        Initialize Command Center client.

        Args:
            logger: Optional logger for diagnostic messages
        """
        self.logger = logger or logging.getLogger(__name__)
        self.url = self._detect_url()
        self.available = False
        self.features = CommandCenterFeatures()

        # Perform availability check
        if self.url:
            self._check_availability()

    def _detect_url(self) -> Optional[str]:
        """
        Detect Command Center URL from multiple sources.

        Priority order:
        1. Environment variable: FLUID_COMMAND_CENTER_URL
        2. Config file: ~/.fluid/config.yaml
        3. Default: http://localhost:8000

        Returns:
            Command Center URL or None if detection disabled
        """
        # Check if detection is disabled
        if os.getenv("FLUID_DISABLE_CC_DETECTION", "").lower() in ("true", "1", "yes"):
            self.logger.debug("Command Center detection disabled via environment")
            return None

        # Priority 1: Environment variable
        env_url = os.getenv("FLUID_COMMAND_CENTER_URL")
        if env_url:
            self.logger.debug(f"Command Center URL from environment: {env_url}")
            return env_url.rstrip("/")

        # Priority 2: Config file
        config_url = self._load_from_config()
        if config_url:
            self.logger.debug(f"Command Center URL from config: {config_url}")
            return config_url.rstrip("/")

        # Priority 3: Default (only if auto-detect enabled)
        default_url = "http://localhost:8000"
        self.logger.debug(f"Using default Command Center URL: {default_url}")
        return default_url

    def _load_from_config(self) -> Optional[str]:
        """
        Load Command Center URL from config file.

        Checks:
        - ~/.fluid/config.yaml
        - ~/.fluid/config.yml
        - ./fluid.yaml
        - ./fluid.yml

        Returns:
            URL from config or None
        """
        config_paths = [
            Path.home() / ".fluid" / "config.yaml",
            Path.home() / ".fluid" / "config.yml",
            Path("fluid.yaml"),
            Path("fluid.yml"),
        ]

        for config_path in config_paths:
            if not config_path.exists():
                continue

            try:
                with open(config_path) as f:
                    config = yaml.safe_load(f)

                if not config:
                    continue

                # Check command_center.url
                cc_config = config.get("command_center", {})
                if isinstance(cc_config, dict):
                    url = cc_config.get("url")
                    if url:
                        return url

            except Exception as e:
                self.logger.warning(f"Failed to load config from {config_path}: {e}")
                continue

        return None

    def _check_availability(self) -> None:
        """
        Check if Command Center is available and detect features.

        Performs a health check and queries available endpoints.
        Sets self.available and self.features.
        """
        if not REQUESTS_AVAILABLE:
            self.logger.warning("requests library not available, skipping Command Center detection")
            return

        try:
            # Quick health check with timeout
            health_url = f"{self.url}/health"
            response = requests.get(health_url, timeout=2)

            if response.status_code == 200:
                self.available = True
                self.logger.info(f"Command Center available at {self.url}")

                # Detect available features
                self._detect_features()
            else:
                self.logger.debug(f"Command Center health check failed: {response.status_code}")
                self.available = False

        except requests.exceptions.Timeout:
            self.logger.debug(f"Command Center health check timeout: {self.url}")
            self.available = False

        except requests.exceptions.ConnectionError:
            self.logger.debug(f"Command Center not reachable: {self.url}")
            self.available = False

        except Exception as e:
            self.logger.warning(f"Command Center availability check failed: {e}")
            self.available = False

    def _detect_features(self) -> None:
        """
        Detect which Command Center features are available.

        Checks multiple endpoints to determine feature availability.
        Updates self.features.
        """
        # Check marketplace endpoint
        self.features.marketplace = self._check_endpoint("/api/v1/blueprints-marketplace")

        # Check catalog endpoint
        self.features.catalog = self._check_endpoint("/api/v1/data-products")

        # Check governance endpoint
        self.features.governance = self._check_endpoint("/api/v1/policies")

        # Check analytics endpoint
        self.features.analytics = self._check_endpoint("/api/v1/analytics")

        # Try to get version
        self.features.version = self._get_version()

        self.logger.info(f"Command Center features: {self.features}")

    def _check_endpoint(self, endpoint: str) -> bool:
        """
        Check if a specific endpoint is available.

        Args:
            endpoint: API endpoint path (e.g., '/api/v1/blueprints-marketplace')

        Returns:
            True if endpoint is available, False otherwise
        """
        try:
            url = f"{self.url}{endpoint}"

            # Try HEAD request first (faster)
            response = requests.head(url, timeout=1)

            # 200 OK or 405 Method Not Allowed (HEAD not supported but endpoint exists)
            if response.status_code in (200, 405):
                return True

            # 404 = endpoint doesn't exist
            if response.status_code == 404:
                return False

            # For other status codes, try GET with limit
            response = requests.get(url, params={"limit": 1}, timeout=1)
            return response.status_code in (200, 401, 403)  # Exists (may need auth)

        except Exception as e:
            self.logger.debug(f"Endpoint check failed for {endpoint}: {e}")
            return False

    def _get_version(self) -> str:
        """
        Get Command Center version.

        Returns:
            Version string or "unknown"
        """
        try:
            response = requests.get(f"{self.url}/api/v1/version", timeout=1)
            if response.status_code == 200:
                data = response.json()
                return data.get("version", "unknown")
        except Exception:
            pass

        return "unknown"

    def get_marketplace_url(self) -> Optional[str]:
        """
        Get marketplace API URL if available.

        Returns:
            Full marketplace URL or None
        """
        if self.available and self.features.marketplace:
            return f"{self.url}/api/v1/blueprints-marketplace"
        return None

    def get_catalog_url(self) -> Optional[str]:
        """
        Get catalog API URL if available.

        Returns:
            Full catalog URL or None
        """
        if self.available and self.features.catalog:
            return f"{self.url}/api/v1/data-products"
        return None

    def get_status(self) -> Dict[str, Any]:
        """
        Get Command Center status summary.

        Returns:
            Status dictionary with availability and features
        """
        return {
            "url": self.url,
            "available": self.available,
            "features": {
                "marketplace": self.features.marketplace,
                "catalog": self.features.catalog,
                "governance": self.features.governance,
                "analytics": self.features.analytics,
                "version": self.features.version,
            },
        }


# Global instance (lazy-initialized)
_global_client: Optional[CommandCenterClient] = None


def get_command_center_client(logger: Optional[logging.Logger] = None) -> CommandCenterClient:
    """
    Get or create global Command Center client instance.

    Args:
        logger: Optional logger

    Returns:
        CommandCenterClient instance
    """
    global _global_client

    if _global_client is None:
        _global_client = CommandCenterClient(logger=logger)

    return _global_client


def reset_global_client() -> None:
    """Reset global client (useful for testing)."""
    global _global_client
    _global_client = None
