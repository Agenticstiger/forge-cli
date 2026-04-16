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
Command Center configuration.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


def _safe_home_dir() -> Path:
    """Resolve a usable home directory even in restricted environments."""
    try:
        return Path.home()
    except Exception:
        env_home = os.getenv("HOME") or os.getenv("USERPROFILE")
        if env_home:
            return Path(env_home)
        return Path.cwd()


@dataclass
class CommandCenterConfig:
    """
    Configuration for Command Center integration.

    Priority order (highest to lowest):
    1. Environment variables
    2. Config file (~/.fluid/config.yaml)
    3. CLI flags (if provided)
    """

    url: Optional[str] = None
    api_key: Optional[str] = None
    enabled: bool = True
    timeout: int = 5  # seconds
    retry_attempts: int = 3
    batch_size: int = 100  # logs/metrics per batch
    flush_interval: int = 5  # seconds

    @classmethod
    def from_environment(cls) -> "CommandCenterConfig":
        """
        Load configuration from environment and config file.

        Environment variables:
        - FLUID_COMMAND_CENTER_URL: Command Center URL
        - FLUID_COMMAND_CENTER_API_KEY: API key for authentication
        - FLUID_COMMAND_CENTER_ENABLED: Enable/disable integration (default: true)
        - FLUID_COMMAND_CENTER_TIMEOUT: Request timeout in seconds (default: 5)

        Config file: ~/.fluid/config.yaml
        ```yaml
        command_center:
          url: https://command-center.company.com
          api_key: fluid_abc123xyz789
          enabled: true
          timeout: 5
        ```

        Returns:
            CommandCenterConfig instance
        """
        # Start with defaults
        config = cls()

        # Load from config file
        config_file = _safe_home_dir() / ".fluid" / "config.yaml"
        if config_file.exists():
            try:
                with open(config_file) as f:
                    yaml_config = yaml.safe_load(f) or {}
                    cc_config = yaml_config.get("command_center", {})

                    if "url" in cc_config:
                        config.url = cc_config["url"]
                    if "api_key" in cc_config:
                        config.api_key = cc_config["api_key"]
                    if "enabled" in cc_config:
                        config.enabled = bool(cc_config["enabled"])
                    if "timeout" in cc_config:
                        config.timeout = int(cc_config["timeout"])
                    if "retry_attempts" in cc_config:
                        config.retry_attempts = int(cc_config["retry_attempts"])
                    if "batch_size" in cc_config:
                        config.batch_size = int(cc_config["batch_size"])
                    if "flush_interval" in cc_config:
                        config.flush_interval = int(cc_config["flush_interval"])
            except Exception:
                # Ignore config file errors (env vars can still work)
                pass

        # Override with environment variables (highest priority)
        if url := os.getenv("FLUID_COMMAND_CENTER_URL"):
            config.url = url
        if api_key := os.getenv("FLUID_COMMAND_CENTER_API_KEY"):
            config.api_key = api_key
        if enabled := os.getenv("FLUID_COMMAND_CENTER_ENABLED"):
            config.enabled = enabled.lower() in ("true", "1", "yes")
        if timeout := os.getenv("FLUID_COMMAND_CENTER_TIMEOUT"):
            try:
                config.timeout = int(timeout)
            except ValueError:
                pass

        return config

    def is_configured(self) -> bool:
        """
        Check if Command Center is properly configured.

        Returns:
            True if URL and API key are set, False otherwise
        """
        return bool(self.enabled and self.url and self.api_key)

    def __repr__(self) -> str:
        # Mask API key for security
        masked_key = None
        if self.api_key:
            visible_prefix = self.api_key[:4]
            masked_key = f"{visible_prefix}***REDACTED***"
        return (
            f"CommandCenterConfig(url={self.url}, api_key={masked_key}, "
            f"enabled={self.enabled}, timeout={self.timeout})"
        )
