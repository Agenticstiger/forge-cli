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
FLUID CLI Configuration Management and Validation

Comprehensive configuration validation, environment management, and
deployment documentation for production-ready CLI operations.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, TypeVar, Union

import yaml

from .core import FluidCLIError
from .security import read_file_secure, validate_input_file

T = TypeVar("T")


class EnvironmentType(Enum):
    """Environment types for configuration validation"""

    DEVELOPMENT = "dev"
    TESTING = "test"
    STAGING = "staging"
    PRODUCTION = "prod"


class ConfigSource(Enum):
    """Configuration source types"""

    DEFAULT = "default"
    ENVIRONMENT = "environment"
    CONFIG_FILE = "config_file"
    CLI_ARGS = "cli_args"
    OVERRIDE = "override"


@dataclass
class ConfigValue:
    """Configuration value with metadata"""

    value: Any
    source: ConfigSource
    required: bool = False
    sensitive: bool = False
    validated: bool = False
    description: str = ""

    def mask_if_sensitive(self) -> str:
        """Return masked value if sensitive"""
        if self.sensitive and self.value:
            return "***MASKED***"
        return str(self.value)


@dataclass
class ValidationRule:
    """Configuration validation rule"""

    name: str
    validator: callable
    error_message: str
    severity: str = "error"  # error, warning, info
    environments: List[EnvironmentType] = None

    def __post_init__(self):
        if self.environments is None:
            self.environments = list(EnvironmentType)


class ConfigurationManager:
    """Production-ready configuration management with validation"""

    def __init__(self, environment: Optional[str] = None):
        self.environment = self._determine_environment(environment)
        self.config: Dict[str, ConfigValue] = {}
        self.validation_rules: List[ValidationRule] = []
        self.logger = logging.getLogger(__name__)

        # Load default configuration
        self._load_defaults()
        self._register_default_validation_rules()

    def _determine_environment(self, env: Optional[str]) -> EnvironmentType:
        """Determine the current environment"""
        env_str = env or os.getenv("FLUID_ENV", "dev")

        try:
            return EnvironmentType(env_str.lower())
        except ValueError:
            # Default to development for unknown environments
            self.logger.warning(f"Unknown environment '{env_str}', defaulting to development")
            return EnvironmentType.DEVELOPMENT

    def _load_defaults(self) -> None:
        """Load default configuration values"""
        defaults = {
            # Infrastructure
            "provider": ConfigValue(
                "local", ConfigSource.DEFAULT, description="Infrastructure provider"
            ),
            "region": ConfigValue("us-central1", ConfigSource.DEFAULT, description="Cloud region"),
            "project": ConfigValue(None, ConfigSource.DEFAULT, description="Cloud project ID"),
            # Logging
            "log_level": ConfigValue("INFO", ConfigSource.DEFAULT, description="Logging level"),
            "log_file": ConfigValue(None, ConfigSource.DEFAULT, description="Log file path"),
            "log_format": ConfigValue(
                "text", ConfigSource.DEFAULT, description="Log format (text/json)"
            ),
            # Security
            "safe_mode": ConfigValue(
                False, ConfigSource.DEFAULT, description="Enhanced security mode"
            ),
            "max_file_size_mb": ConfigValue(
                100, ConfigSource.DEFAULT, description="Maximum file size in MB"
            ),
            "timeout_seconds": ConfigValue(
                300, ConfigSource.DEFAULT, description="Default timeout in seconds"
            ),
            # Performance
            "cache_enabled": ConfigValue(True, ConfigSource.DEFAULT, description="Enable caching"),
            "cache_ttl_seconds": ConfigValue(
                3600, ConfigSource.DEFAULT, description="Cache TTL in seconds"
            ),
            "parallel_operations": ConfigValue(
                4, ConfigSource.DEFAULT, description="Max parallel operations"
            ),
            # Features
            "rich_output": ConfigValue(
                True, ConfigSource.DEFAULT, description="Enable rich terminal output"
            ),
            "auto_confirm": ConfigValue(
                False, ConfigSource.DEFAULT, description="Auto-confirm operations"
            ),
            "dry_run": ConfigValue(False, ConfigSource.DEFAULT, description="Dry run mode"),
            # Development
            "debug": ConfigValue(False, ConfigSource.DEFAULT, description="Debug mode"),
            "profiling": ConfigValue(
                False, ConfigSource.DEFAULT, description="Performance profiling"
            ),
            "trace": ConfigValue(False, ConfigSource.DEFAULT, description="Execution tracing"),
        }

        self.config.update(defaults)

    def _register_default_validation_rules(self) -> None:
        """Register default validation rules"""
        self.validation_rules = [
            # Provider validation
            ValidationRule(
                "provider_valid",
                lambda cfg: cfg.get("provider", {}).value
                in ["local", "gcp", "aws", "azure", "snowflake"],
                "Provider must be one of: local, gcp, aws, azure, snowflake",
            ),
            # Cloud provider project requirement
            ValidationRule(
                "cloud_provider_project",
                lambda cfg: not (
                    cfg.get("provider", {}).value in ["gcp", "aws", "azure"]
                    and not cfg.get("project", {}).value
                ),
                "Cloud providers (gcp, aws, azure) require a project to be specified",
                environments=[EnvironmentType.STAGING, EnvironmentType.PRODUCTION],
            ),
            # Production security requirements
            ValidationRule(
                "production_safe_mode",
                lambda cfg: (
                    cfg.get("safe_mode", {}).value
                    if cfg.get("environment") == EnvironmentType.PRODUCTION
                    else True
                ),
                "Safe mode should be enabled in production",
                severity="warning",
                environments=[EnvironmentType.PRODUCTION],
            ),
            # Log level validation
            ValidationRule(
                "log_level_valid",
                lambda cfg: cfg.get("log_level", {}).value.upper()
                in ["DEBUG", "INFO", "WARNING", "ERROR"],
                "Log level must be one of: DEBUG, INFO, WARNING, ERROR",
            ),
            # File size limits
            ValidationRule(
                "file_size_reasonable",
                lambda cfg: 1 <= cfg.get("max_file_size_mb", {}).value <= 1000,
                "Maximum file size must be between 1MB and 1000MB",
            ),
            # Timeout validation
            ValidationRule(
                "timeout_reasonable",
                lambda cfg: 10 <= cfg.get("timeout_seconds", {}).value <= 3600,
                "Timeout must be between 10 seconds and 1 hour",
            ),
            # Parallel operations limit
            ValidationRule(
                "parallel_operations_limit",
                lambda cfg: 1 <= cfg.get("parallel_operations", {}).value <= 20,
                "Parallel operations must be between 1 and 20",
            ),
            # Production debugging warning
            ValidationRule(
                "production_debug_warning",
                lambda cfg: not (
                    cfg.get("debug", {}).value
                    and cfg.get("environment") == EnvironmentType.PRODUCTION
                ),
                "Debug mode should not be enabled in production",
                severity="warning",
                environments=[EnvironmentType.PRODUCTION],
            ),
        ]

    def load_from_environment(self) -> None:
        """Load configuration from environment variables"""
        env_mappings = {
            "FLUID_PROVIDER": "provider",
            "FLUID_REGION": "region",
            "FLUID_PROJECT": "project",
            "FLUID_LOG_LEVEL": "log_level",
            "FLUID_LOG_FILE": "log_file",
            "FLUID_LOG_FORMAT": "log_format",
            "FLUID_SAFE_MODE": "safe_mode",
            "FLUID_MAX_FILE_SIZE_MB": "max_file_size_mb",
            "FLUID_TIMEOUT_SECONDS": "timeout_seconds",
            "FLUID_CACHE_ENABLED": "cache_enabled",
            "FLUID_CACHE_TTL_SECONDS": "cache_ttl_seconds",
            "FLUID_PARALLEL_OPERATIONS": "parallel_operations",
            "FLUID_RICH_OUTPUT": "rich_output",
            "FLUID_AUTO_CONFIRM": "auto_confirm",
            "FLUID_DRY_RUN": "dry_run",
            "FLUID_DEBUG": "debug",
            "FLUID_PROFILING": "profiling",
            "FLUID_TRACE": "trace",
        }

        for env_var, config_key in env_mappings.items():
            env_value = os.getenv(env_var)
            if env_value is not None:
                # Type conversion based on current config
                current_config = self.config.get(config_key)
                if current_config:
                    converted_value = self._convert_value(env_value, type(current_config.value))
                    self.config[config_key] = ConfigValue(
                        converted_value,
                        ConfigSource.ENVIRONMENT,
                        current_config.required,
                        current_config.sensitive,
                        description=current_config.description,
                    )

    def load_from_file(self, config_path: Union[str, Path]) -> None:
        """Load configuration from a file"""
        try:
            config_file = validate_input_file(config_path, "configuration file")
            content = read_file_secure(config_file, "configuration file")

            # Parse based on file extension
            if config_file.suffix.lower() in [".yaml", ".yml"]:
                file_config = yaml.safe_load(content)
            elif config_file.suffix.lower() == ".json":
                file_config = json.loads(content)
            else:
                raise FluidCLIError(
                    1,
                    "invalid_config_format",
                    f"Unsupported configuration file format: {config_file.suffix}",
                    suggestions=[
                        "Use .yaml, .yml, or .json configuration files",
                        "Check the file extension matches the content format",
                    ],
                )

            # Update configuration
            for key, value in file_config.items():
                if key in self.config:
                    current_config = self.config[key]
                    self.config[key] = ConfigValue(
                        value,
                        ConfigSource.CONFIG_FILE,
                        current_config.required,
                        current_config.sensitive,
                        description=current_config.description,
                    )
                else:
                    self.logger.warning(f"Unknown configuration key in file: {key}")

        except Exception as e:
            raise FluidCLIError(
                1,
                "config_file_load_failed",
                f"Failed to load configuration file: {config_path}",
                context={"error": str(e)},
                suggestions=[
                    "Check if the configuration file exists and is readable",
                    "Verify the file format (YAML or JSON)",
                    "Check for syntax errors in the configuration file",
                ],
            )

    def update_from_args(self, args_dict: Dict[str, Any]) -> None:
        """Update configuration from CLI arguments"""
        for key, value in args_dict.items():
            if key in self.config and value is not None:
                current_config = self.config[key]
                self.config[key] = ConfigValue(
                    value,
                    ConfigSource.CLI_ARGS,
                    current_config.required,
                    current_config.sensitive,
                    description=current_config.description,
                )

    def _convert_value(self, value: str, target_type: Type) -> Any:
        """Convert string value to target type"""
        if target_type == bool:
            return value.lower() in ("true", "1", "yes", "on", "enabled")
        elif target_type == int:
            try:
                return int(value)
            except ValueError:
                raise ValueError(f"Cannot convert '{value}' to integer")
        elif target_type == float:
            try:
                return float(value)
            except ValueError:
                raise ValueError(f"Cannot convert '{value}' to float")
        else:
            return value

    def validate(self) -> Dict[str, List[str]]:
        """Validate current configuration and return issues"""
        issues = {"errors": [], "warnings": [], "info": []}

        # Add environment to config for validation
        temp_config = self.config.copy()
        temp_config["environment"] = ConfigValue(self.environment, ConfigSource.DEFAULT)

        for rule in self.validation_rules:
            # Check if rule applies to current environment
            if self.environment not in rule.environments:
                continue

            try:
                if not rule.validator(temp_config):
                    issues[f"{rule.severity}s"].append(f"{rule.name}: {rule.error_message}")
            except Exception as e:
                issues["errors"].append(f"{rule.name}: Validation error - {e}")

        # Mark configuration as validated
        for config_value in self.config.values():
            config_value.validated = True

        return issues

    def get(self, key: str, default: Any = None) -> Any:
        """Get configuration value"""
        config_value = self.config.get(key)
        return config_value.value if config_value else default

    def set(self, key: str, value: Any, source: ConfigSource = ConfigSource.OVERRIDE) -> None:
        """Set configuration value"""
        current_config = self.config.get(key)
        if current_config:
            self.config[key] = ConfigValue(
                value,
                source,
                current_config.required,
                current_config.sensitive,
                description=current_config.description,
            )
        else:
            self.config[key] = ConfigValue(value, source)

    def get_summary(self, include_sensitive: bool = False) -> Dict[str, Any]:
        """Get configuration summary"""
        summary = {
            "environment": self.environment.value,
            "configuration": {},
            "sources": {},
            "validation_status": (
                "validated" if all(cv.validated for cv in self.config.values()) else "not_validated"
            ),
        }

        for key, config_value in self.config.items():
            if include_sensitive or not config_value.sensitive:
                summary["configuration"][key] = config_value.value
            else:
                summary["configuration"][key] = config_value.mask_if_sensitive()

            summary["sources"][key] = config_value.source.value

        return summary

    def generate_example_config(self, format: str = "yaml") -> str:
        """Generate example configuration file"""
        config_dict = {}

        for key, config_value in self.config.items():
            if not config_value.sensitive:
                config_dict[key] = {
                    "value": config_value.value,
                    "description": config_value.description,
                    "required": config_value.required,
                }

        if format.lower() == "yaml":
            return yaml.dump(config_dict, default_flow_style=False, indent=2)
        elif format.lower() == "json":
            return json.dumps(config_dict, indent=2)
        else:
            raise ValueError(f"Unsupported format: {format}")


def create_production_documentation() -> str:
    """Create comprehensive production deployment documentation"""

    doc = """
# FLUID CLI Production Deployment Guide

## Overview
This guide covers the production deployment of the FLUID CLI with comprehensive
security, performance, and operational considerations.

## Environment Setup

### Required Environment Variables
```bash
# Infrastructure (Required)
export FLUID_PROVIDER="gcp"              # or aws, azure, snowflake
export FLUID_PROJECT="your-project-id"   # Cloud project identifier
export FLUID_REGION="us-central1"        # Cloud region

# Security (Recommended)
export FLUID_SAFE_MODE="true"            # Enable enhanced security
export FLUID_MAX_FILE_SIZE_MB="100"      # File size limit
export FLUID_TIMEOUT_SECONDS="300"       # Operation timeout

# Logging (Recommended)
export FLUID_LOG_LEVEL="INFO"            # or WARNING for production
export FLUID_LOG_FILE="/var/log/fluid.log"  # Centralized logging
export FLUID_LOG_FORMAT="json"           # Structured logging

# Performance (Optional)
export FLUID_CACHE_ENABLED="true"        # Enable caching
export FLUID_CACHE_TTL_SECONDS="3600"    # Cache duration
export FLUID_PARALLEL_OPERATIONS="4"     # Concurrent operations
```

### Configuration File Example
Create `/etc/fluid/config.yaml`:
```yaml
# Production FLUID CLI Configuration

# Infrastructure
provider: "gcp"
project: "your-production-project"
region: "us-central1"

# Security
safe_mode: true
max_file_size_mb: 100
timeout_seconds: 300

# Logging
log_level: "INFO"
log_file: "/var/log/fluid.log"
log_format: "json"

# Performance
cache_enabled: true
cache_ttl_seconds: 3600
parallel_operations: 4

# Features
rich_output: false  # Disable for scripts
auto_confirm: false  # Never auto-confirm in production
dry_run: false
debug: false  # Never enable in production
```

## Security Considerations

### File System Security
- Run with minimal required permissions
- Use dedicated service account for cloud operations
- Restrict file access to project directories only
- Enable safe mode for enhanced validation

### Network Security
- Configure firewall rules for cloud API access
- Use VPC endpoints where available
- Enable audit logging for all cloud operations
- Rotate credentials regularly

### Input Validation
- All file paths are validated for traversal attacks
- File size limits are enforced
- Command injection protection is enabled
- Environment variable sanitization is active

## Performance Tuning

### Memory Management
- Monitor memory usage with --stats flag
- Set appropriate file size limits
- Enable caching for repeated operations
- Use parallel operations judiciously

### Network Optimization
- Configure appropriate timeouts
- Use regional endpoints when possible
- Enable request caching
- Monitor API rate limits

## Monitoring and Observability

### Health Checks
Run periodic health checks:
```bash
fluid --health-check
```

### Performance Monitoring
Monitor performance metrics:
```bash
fluid --stats
```

### Logging Configuration
- Use structured JSON logging for production
- Centralize logs to monitoring system
- Set up log rotation and retention
- Monitor for security events

## Operational Procedures

### Deployment
1. Validate configuration: `fluid validate config.yaml`
2. Run health checks: `fluid --health-check`
3. Test with dry run: `fluid plan --dry-run`
4. Deploy with monitoring: `fluid apply --log-file deployment.log`

### Monitoring
- Monitor CPU and memory usage
- Track operation success/failure rates
- Monitor API rate limit consumption
- Alert on security violations

### Troubleshooting
- Enable debug logging temporarily
- Use --trace for detailed execution flow
- Check health status of dependencies
- Review audit logs for security events

## Example Production Commands

### Validation
```bash
# Validate contract with full security checks
fluid validate contract.fluid.yaml --safe-mode

# Validate with specific environment
fluid validate contract.fluid.yaml --env prod
```

### Planning
```bash
# Generate plan with monitoring
fluid plan contract.fluid.yaml --env prod --log-file plan.log

# Plan with timeout and retries
fluid plan contract.fluid.yaml --timeout 600 --safe-mode
```

### Deployment
```bash
# Apply with comprehensive logging
fluid apply plan.json --log-file apply.log --log-level INFO

# Apply with health checks
fluid apply plan.json --health-check --stats
```

### Monitoring
```bash
# Generate visualization for documentation
fluid viz-graph contract.fluid.yaml --format html --out docs/graph.html

# Run diagnostics
fluid doctor --out-dir diagnostics/$(date +%Y%m%d_%H%M%S)
```

## Security Checklist

- [ ] Safe mode enabled
- [ ] File size limits configured
- [ ] Timeout limits set
- [ ] Structured logging enabled
- [ ] Debug mode disabled
- [ ] Service account permissions minimal
- [ ] Network access restricted
- [ ] Audit logging enabled
- [ ] Credential rotation scheduled
- [ ] Monitoring alerts configured

## Performance Checklist

- [ ] Caching enabled
- [ ] Appropriate timeout values
- [ ] Memory limits configured
- [ ] Parallel operations tuned
- [ ] Regional endpoints configured
- [ ] API rate limits monitored
- [ ] Performance metrics collected
- [ ] Resource usage monitored

## Operational Checklist

- [ ] Health checks automated
- [ ] Performance monitoring enabled
- [ ] Log rotation configured
- [ ] Backup procedures defined
- [ ] Incident response plan ready
- [ ] Documentation up to date
- [ ] Team training completed
- [ ] Emergency contacts defined

For additional support, contact the FLUID team or refer to the enterprise documentation.
"""

    return doc.strip()


# Global configuration manager instance
_config_manager: Optional[ConfigurationManager] = None


def get_config_manager(environment: Optional[str] = None) -> ConfigurationManager:
    """Get or create the global configuration manager"""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigurationManager(environment)
    return _config_manager


def validate_production_config() -> Dict[str, List[str]]:
    """Validate production configuration"""
    config_mgr = get_config_manager()
    config_mgr.load_from_environment()
    return config_mgr.validate()


# Export public interface
__all__ = [
    "EnvironmentType",
    "ConfigSource",
    "ConfigValue",
    "ValidationRule",
    "ConfigurationManager",
    "get_config_manager",
    "validate_production_config",
    "create_production_documentation",
]
