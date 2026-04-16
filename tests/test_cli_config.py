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

"""Tests for fluid_build.cli.config — ConfigurationManager, dataclasses, validation."""

import os
from unittest.mock import patch

import pytest

from fluid_build.cli.config import (
    ConfigSource,
    ConfigurationManager,
    ConfigValue,
    EnvironmentType,
    ValidationRule,
)

# ── Enums ──


class TestEnvironmentType:
    def test_values(self):
        assert EnvironmentType.DEVELOPMENT.value == "dev"
        assert EnvironmentType.TESTING.value == "test"
        assert EnvironmentType.STAGING.value == "staging"
        assert EnvironmentType.PRODUCTION.value == "prod"


class TestConfigSource:
    def test_values(self):
        assert ConfigSource.DEFAULT.value == "default"
        assert ConfigSource.ENVIRONMENT.value == "environment"
        assert ConfigSource.CLI_ARGS.value == "cli_args"


# ── ConfigValue ──


class TestConfigValue:
    def test_basic(self):
        cv = ConfigValue(value="hello", source=ConfigSource.DEFAULT)
        assert cv.value == "hello"
        assert cv.sensitive is False
        assert cv.validated is False

    def test_mask_sensitive(self):
        cv = ConfigValue(value="secret123", source=ConfigSource.DEFAULT, sensitive=True)
        assert cv.mask_if_sensitive() == "***MASKED***"

    def test_mask_non_sensitive(self):
        cv = ConfigValue(value="public", source=ConfigSource.DEFAULT, sensitive=False)
        assert cv.mask_if_sensitive() == "public"

    def test_mask_empty_sensitive(self):
        cv = ConfigValue(value="", source=ConfigSource.DEFAULT, sensitive=True)
        # Empty string is falsy, should not mask
        assert cv.mask_if_sensitive() == ""


# ── ValidationRule ──


class TestValidationRule:
    def test_basic(self):
        rule = ValidationRule(
            name="test_rule",
            validator=lambda cfg: True,
            error_message="test error",
        )
        assert rule.name == "test_rule"
        assert rule.severity == "error"
        assert len(rule.environments) == len(list(EnvironmentType))

    def test_custom_environments(self):
        rule = ValidationRule(
            name="prod_only",
            validator=lambda cfg: True,
            error_message="err",
            environments=[EnvironmentType.PRODUCTION],
        )
        assert rule.environments == [EnvironmentType.PRODUCTION]


# ── ConfigurationManager ──


class TestConfigurationManager:
    def test_default_environment(self):
        with patch.dict(os.environ, {}, clear=False):
            # Remove FLUID_ENV if set
            os.environ.pop("FLUID_ENV", None)
            cm = ConfigurationManager()
            assert cm.environment == EnvironmentType.DEVELOPMENT

    def test_explicit_environment(self):
        cm = ConfigurationManager(environment="prod")
        assert cm.environment == EnvironmentType.PRODUCTION

    def test_unknown_environment_raises(self):
        with pytest.raises(AttributeError):
            ConfigurationManager(environment="unknown")

    def test_defaults_loaded(self):
        cm = ConfigurationManager()
        assert "provider" in cm.config
        assert cm.config["provider"].value == "local"
        assert cm.config["log_level"].value == "INFO"
        assert cm.config["cache_enabled"].value is True

    def test_convert_value_bool(self):
        cm = ConfigurationManager()
        assert cm._convert_value("true", bool) is True
        assert cm._convert_value("yes", bool) is True
        assert cm._convert_value("1", bool) is True
        assert cm._convert_value("false", bool) is False
        assert cm._convert_value("no", bool) is False

    def test_convert_value_int(self):
        cm = ConfigurationManager()
        assert cm._convert_value("42", int) == 42

    def test_convert_value_int_invalid(self):
        cm = ConfigurationManager()
        with pytest.raises(ValueError):
            cm._convert_value("abc", int)

    def test_convert_value_float(self):
        cm = ConfigurationManager()
        assert cm._convert_value("3.14", float) == pytest.approx(3.14)

    def test_convert_value_string(self):
        cm = ConfigurationManager()
        assert cm._convert_value("hello", str) == "hello"

    def test_update_from_args(self):
        cm = ConfigurationManager()
        cm.update_from_args({"provider": "gcp", "debug": True})
        assert cm.config["provider"].value == "gcp"
        assert cm.config["provider"].source == ConfigSource.CLI_ARGS
        assert cm.config["debug"].value is True

    def test_update_from_args_ignores_none(self):
        cm = ConfigurationManager()
        cm.update_from_args({"provider": None})
        assert cm.config["provider"].value == "local"  # Unchanged

    def test_update_from_args_ignores_unknown(self):
        cm = ConfigurationManager()
        cm.update_from_args({"unknown_key": "value"})
        assert "unknown_key" not in cm.config

    def test_validate_defaults_pass(self):
        cm = ConfigurationManager(environment="dev")
        issues = cm.validate()
        assert len(issues["errors"]) == 0

    def test_load_from_environment(self):
        with patch.dict(os.environ, {"FLUID_PROVIDER": "gcp", "FLUID_DEBUG": "true"}):
            cm = ConfigurationManager()
            cm.load_from_environment()
            assert cm.config["provider"].value == "gcp"
            assert cm.config["provider"].source == ConfigSource.ENVIRONMENT
            assert cm.config["debug"].value is True

    def test_get_config_summary(self):
        """Test that config has expected number of items."""
        cm = ConfigurationManager()
        assert len(cm.config) >= 15  # At least 15 configuration items

    def test_validation_rules_registered(self):
        cm = ConfigurationManager()
        assert len(cm.validation_rules) >= 5
