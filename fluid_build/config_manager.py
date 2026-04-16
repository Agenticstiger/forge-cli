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
Configuration Management for FLUID CLI

Provides hierarchical configuration loading from multiple sources:
1. Default configuration (built-in)
2. System-wide configuration (/etc/fluid/config.yaml or C:\\ProgramData\\fluid\\config.yaml)
3. User configuration (~/.fluidrc.yaml or ~/.config/fluid/config.yaml)
4. Project configuration (.fluidrc.yaml or fluid.config.yaml in current directory)
5. Environment variables (FLUID_*)
6. Command-line arguments (highest priority)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from .errors import ConfigurationError, FileSystemError

LOGGER = logging.getLogger("fluid.config")


# Default configuration
DEFAULT_CONFIG = {
    "logging": {"level": "INFO", "format": "text", "file": None, "console": True},  # text or json
    "cache": {"dir": "~/.fluid/cache", "schema_cache_days": 7, "auto_refresh": True},
    "network": {
        "timeout": 30,
        "connect_timeout": 10,
        "max_retries": 3,
        "rate_limit": 50,  # requests per minute
        "verify_ssl": True,
    },
    "validation": {"strict": False, "offline": False, "schema_version": None},  # auto-detect
    "apply": {
        "dry_run": False,
        "parallel_phases": True,
        "rollback_strategy": "immediate",
        "timeout_minutes": 60,
    },
    "providers": {
        "gcp": {"default_region": "us-central1", "default_location": "US"},
        "aws": {"default_region": "us-east-1"},
        "snowflake": {"default_warehouse": "COMPUTE_WH"},
    },
    "catalogs": {
        "fluid-command-center": {
            "endpoint": "http://localhost:8000",
            "auth": {"type": "api_key"},
            "enabled": True,
            "max_retries": 3,
            "timeout": 30.0,
            "circuit_breaker_threshold": 3,
            "circuit_breaker_timeout": 60,
        }
    },
    "output": {
        "format": "text",  # text, json, yaml
        "color": True,
        "verbose": False,
        "quiet": False,
    },
}


class FluidConfig:
    """
    Hierarchical configuration manager for FLUID CLI.

    Configuration is loaded from multiple sources in order:
    1. Defaults (lowest priority)
    2. System config
    3. User config
    4. Project config
    5. Environment variables
    6. CLI arguments (highest priority)
    """

    def __init__(self):
        self._config: Dict[str, Any] = {}
        self._load_defaults()
        self._load_system_config()
        self._load_user_config()
        self._load_project_config()
        self._load_env_vars()

    def _load_defaults(self) -> None:
        """Load default configuration."""
        self._config = DEFAULT_CONFIG.copy()
        LOGGER.debug("Loaded default configuration")

    def _load_system_config(self) -> None:
        """Load system-wide configuration."""
        if os.name == "nt":
            # Windows: C:\ProgramData\fluid\config.yaml
            system_config = (
                Path(os.environ.get("ProgramData", "C:\\ProgramData")) / "fluid" / "config.yaml"
            )
        else:
            # Unix: /etc/fluid/config.yaml
            system_config = Path("/etc/fluid/config.yaml")

        self._merge_config_file(system_config, "system")

    def _load_user_config(self) -> None:
        """Load user configuration."""
        home = Path.home()

        # Try multiple locations
        user_configs = [
            home / ".fluidrc.yaml",
            home / ".fluidrc",
            home / ".config" / "fluid" / "config.yaml",
        ]

        for config_path in user_configs:
            if config_path.exists():
                self._merge_config_file(config_path, "user")
                break

    def _load_project_config(self) -> None:
        """Load project-specific configuration from current directory."""
        cwd = Path.cwd()

        # Try multiple filenames
        project_configs = [
            cwd / ".fluidrc.yaml",
            cwd / ".fluidrc",
            cwd / "fluid.config.yaml",
            cwd / ".fluid" / "config.yaml",
        ]

        for config_path in project_configs:
            if config_path.exists():
                self._merge_config_file(config_path, "project")
                break

    def _load_env_vars(self) -> None:
        """Load configuration from environment variables."""
        env_mappings = {
            # Logging
            "FLUID_LOG_LEVEL": ("logging", "level"),
            "FLUID_LOG_FORMAT": ("logging", "format"),
            "FLUID_LOG_FILE": ("logging", "file"),
            # Cache
            "FLUID_CACHE_DIR": ("cache", "dir"),
            # Network
            "FLUID_TIMEOUT": ("network", "timeout"),
            "FLUID_MAX_RETRIES": ("network", "max_retries"),
            "FLUID_RATE_LIMIT": ("network", "rate_limit"),
            # Providers
            "GCP_PROJECT_ID": ("providers", "gcp", "project_id"),
            "GCP_REGION": ("providers", "gcp", "default_region"),
            "AWS_REGION": ("providers", "aws", "default_region"),
            "SNOWFLAKE_ACCOUNT": ("providers", "snowflake", "account"),
            # Output
            "FLUID_OUTPUT_FORMAT": ("output", "format"),
            "FLUID_VERBOSE": ("output", "verbose"),
            "NO_COLOR": ("output", "color"),  # Inverse
        }

        for env_var, config_path in env_mappings.items():
            value = os.environ.get(env_var)
            if value is not None:
                # Special handling for NO_COLOR
                if env_var == "NO_COLOR":
                    value = False if value else True
                # Convert string booleans
                elif value.lower() in ("true", "yes", "1"):
                    value = True
                elif value.lower() in ("false", "no", "0"):
                    value = False
                # Convert numeric strings
                elif value.isdigit():
                    value = int(value)

                self._set_nested(self._config, config_path, value)
                LOGGER.debug(f"Loaded config from env var {env_var}: {config_path} = {value}")

    def _merge_config_file(self, path: Path, source: str) -> None:
        """
        Merge configuration from a YAML file.

        Args:
            path: Path to config file
            source: Source description for logging
        """
        if not path.exists():
            return

        try:
            with open(path) as f:
                file_config = yaml.safe_load(f) or {}

            self._deep_merge(self._config, file_config)
            LOGGER.debug(f"Loaded {source} configuration from {path}")

        except yaml.YAMLError as e:
            raise ConfigurationError(
                f"Failed to parse {source} configuration file",
                context={"path": str(path), "error": str(e)},
                suggestions=[
                    "Check YAML syntax",
                    f"Validate file with: yamllint {path}",
                    "Remove or fix the configuration file",
                ],
                original_error=e,
            )
        except Exception as e:
            raise FileSystemError(
                f"Failed to read {source} configuration file",
                context={"path": str(path), "error": str(e)},
                suggestions=["Check file permissions", "Verify file is readable"],
                original_error=e,
            )

    def _deep_merge(self, base: Dict, override: Dict) -> None:
        """
        Deep merge override dict into base dict.

        Args:
            base: Base dictionary to merge into
            override: Dictionary to merge from
        """
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value

    def _set_nested(self, d: Dict, path: tuple, value: Any) -> None:
        """
        Set a nested dictionary value using a path tuple.

        Args:
            d: Dictionary to modify
            path: Tuple of keys representing the path
            value: Value to set
        """
        for key in path[:-1]:
            if key not in d:
                d[key] = {}
            d = d[key]
        d[path[-1]] = value

    def get(self, path: str, default: Any = None) -> Any:
        """
        Get a configuration value using dot notation.

        Args:
            path: Configuration path (e.g., 'logging.level')
            default: Default value if path doesn't exist

        Returns:
            Configuration value or default

        Examples:
            >>> config.get('logging.level')
            'INFO'
            >>> config.get('providers.gcp.default_region')
            'us-central1'
        """
        keys = path.split(".")
        value = self._config

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return default

        return value

    def set(self, path: str, value: Any) -> None:
        """
        Set a configuration value using dot notation.

        Args:
            path: Configuration path (e.g., 'logging.level')
            value: Value to set

        Examples:
            >>> config.set('logging.level', 'DEBUG')
            >>> config.set('providers.gcp.project_id', 'my-project')
        """
        keys = path.split(".")
        self._set_nested(self._config, tuple(keys), value)

    def get_section(self, section: str) -> Dict[str, Any]:
        """
        Get an entire configuration section.

        Args:
            section: Section name (e.g., 'logging', 'providers.gcp')

        Returns:
            Dictionary of configuration values
        """
        value = self.get(section, {})
        return value if isinstance(value, dict) else {}

    def get_catalog_config(self, catalog_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Get catalog configuration.

        Args:
            catalog_name: Specific catalog name (e.g., 'fluid-command-center')
                         If None, returns all catalog configurations

        Returns:
            Catalog configuration dictionary
        """
        catalogs = self.get_section("catalogs")

        if catalog_name:
            catalog_config = catalogs.get(catalog_name, {})

            # Apply environment variable overrides
            if catalog_name == "fluid-command-center":
                endpoint = os.environ.get("FLUID_CC_ENDPOINT")
                if endpoint:
                    catalog_config["endpoint"] = endpoint

                api_key = os.environ.get("FLUID_API_KEY")
                if api_key:
                    if "auth" not in catalog_config:
                        catalog_config["auth"] = {}
                    catalog_config["auth"]["api_key"] = api_key

            return catalog_config

        return catalogs

    def to_dict(self) -> Dict[str, Any]:
        """
        Get the complete configuration as a dictionary.

        Returns:
            Complete configuration dictionary
        """
        return self._config.copy()

    def update_from_args(self, **kwargs) -> None:
        """
        Update configuration from command-line arguments.

        This should be called after parsing CLI arguments to give them
        highest priority.

        Args:
            **kwargs: Configuration key-value pairs

        Examples:
            >>> config.update_from_args(verbose=True, log_level='DEBUG')
        """
        mapping = {
            "verbose": "output.verbose",
            "quiet": "output.quiet",
            "log_level": "logging.level",
            "log_file": "logging.file",
            "cache_dir": "cache.dir",
            "timeout": "network.timeout",
            "max_retries": "network.max_retries",
            "dry_run": "apply.dry_run",
            "offline": "validation.offline",
            "strict": "validation.strict",
            "format": "output.format",
            "color": "output.color",
        }

        for arg_name, value in kwargs.items():
            if value is None:
                continue

            config_path = mapping.get(arg_name, arg_name)
            self.set(config_path, value)
            LOGGER.debug(f"Updated config from CLI arg {arg_name}: {config_path} = {value}")

    def save_user_config(self, path: Optional[Path] = None) -> None:
        """
        Save current configuration to user config file.

        Args:
            path: Optional custom path (default: ~/.fluidrc.yaml)

        Raises:
            FileSystemError: If unable to write config file
        """
        if path is None:
            path = Path.home() / ".fluidrc.yaml"

        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            with open(path, "w") as f:
                yaml.dump(self._config, f, default_flow_style=False, sort_keys=False)

            LOGGER.info(f"Saved user configuration to {path}")

        except Exception as e:
            raise FileSystemError(
                "Failed to save configuration file",
                context={"path": str(path), "error": str(e)},
                suggestions=[
                    "Check directory permissions",
                    f"Manually create directory: mkdir -p {path.parent}",
                ],
                original_error=e,
            )


# Global configuration instance
_global_config: Optional[FluidConfig] = None


def get_config() -> FluidConfig:
    """
    Get the global configuration instance.

    Returns:
        Global FluidConfig instance
    """
    global _global_config
    if _global_config is None:
        _global_config = FluidConfig()
    return _global_config


def reset_config() -> None:
    """Reset the global configuration (useful for testing)."""
    global _global_config
    _global_config = None


def create_sample_config(path: Path) -> None:
    """
    Create a sample configuration file with comments.

    Args:
        path: Path to create config file
    """
    sample_config = """# FLUID CLI Configuration File
# 
# This file configures default behavior for the FLUID CLI.
# Settings can be overridden by environment variables or command-line arguments.
#
# Configuration hierarchy (lowest to highest priority):
# 1. This file
# 2. Environment variables (FLUID_*)
# 3. Command-line arguments

# Logging configuration
logging:
  level: INFO  # DEBUG, INFO, WARNING, ERROR
  format: text  # text or json
  file: null  # Path to log file (null = no file logging)
  console: true  # Log to console

# Cache configuration
cache:
  dir: ~/.fluid/cache
  schema_cache_days: 7  # How long to cache schemas
  auto_refresh: true  # Automatically refresh stale caches

# Network configuration
network:
  timeout: 30  # Request timeout in seconds
  connect_timeout: 10  # Connection timeout in seconds
  max_retries: 3  # Maximum retry attempts
  rate_limit: 50  # Max requests per minute
  verify_ssl: true  # Verify SSL certificates

# Validation configuration
validation:
  strict: false  # Treat warnings as errors
  offline: false  # Only use cached schemas
  schema_version: null  # Force specific schema version (null = auto-detect)

# Apply command configuration
apply:
  dry_run: false  # Default dry-run mode
  parallel_phases: true  # Execute phases in parallel when possible
  rollback_strategy: immediate  # immediate, phase_complete, full_rollback
  timeout_minutes: 60  # Global execution timeout

# Provider-specific configuration
providers:
  gcp:
    default_region: us-central1
    default_location: US
    # project_id: my-gcp-project  # Uncomment to set default project
  
  aws:
    default_region: us-east-1
    # account_id: YOUR_AWS_ACCOUNT_ID  # Uncomment to set default account
  
  snowflake:
    default_warehouse: COMPUTE_WH
    # account: myaccount  # Uncomment to set default account

# Catalog configuration for publishing data products
catalogs:
  fluid-command-center:
    endpoint: http://localhost:8000
    auth:
      type: api_key  # api_key, bearer, or basic
      # api_key will be read from FLUID_API_KEY env var
    enabled: true
    max_retries: 3
    timeout: 30.0
    circuit_breaker_threshold: 3
    circuit_breaker_timeout: 60

  # Example: Collibra catalog (future support)
  # collibra:
  #   endpoint: https://collibra.company.com
  #   auth:
  #     type: bearer
  #   enabled: false

# Output configuration
output:
  format: text  # text, json, yaml
  color: true  # Colorized output
  verbose: false  # Verbose output
  quiet: false  # Minimal output
"""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(sample_config)
        LOGGER.info(f"Created sample configuration at {path}")
    except Exception as e:
        raise FileSystemError(
            "Failed to create sample configuration",
            context={"path": str(path), "error": str(e)},
            suggestions=[
                "Check directory permissions",
                f"Manually create directory: mkdir -p {path.parent}",
            ],
            original_error=e,
        )
