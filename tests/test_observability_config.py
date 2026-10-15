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

"""Tests for fluid_build.observability.config — Command Center configuration."""

import os
from unittest.mock import patch

from fluid_build.observability.config import CommandCenterConfig


class TestCommandCenterConfig:
    def test_defaults(self):
        cfg = CommandCenterConfig()
        assert cfg.url is None
        assert cfg.api_key is None
        assert cfg.enabled is True
        assert cfg.timeout == 5
        assert cfg.retry_attempts == 3
        assert cfg.batch_size == 100
        assert cfg.flush_interval == 5

    def test_from_environment_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("pathlib.Path.exists", return_value=False):
                cfg = CommandCenterConfig.from_environment()
                assert cfg.url is None
                assert cfg.enabled is True

    def test_from_environment_env_vars(self):
        env = {
            "FLUID_COMMAND_CENTER_URL": "https://cc.example.com",
            "FLUID_COMMAND_CENTER_API_KEY": "test-key-123",
            "FLUID_COMMAND_CENTER_ENABLED": "false",
            "FLUID_COMMAND_CENTER_TIMEOUT": "10",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("pathlib.Path.exists", return_value=False):
                cfg = CommandCenterConfig.from_environment()
                assert cfg.url == "https://cc.example.com"
                assert cfg.api_key == "test-key-123"
                assert cfg.enabled is False
                assert cfg.timeout == 10

    def test_env_vars_override_config_file(self, tmp_path):
        config_yaml = """
command_center:
  url: https://from-file.example.com
  api_key: file-key
  timeout: 3
"""
        config_file = tmp_path / ".fluid" / "config.yaml"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(config_yaml)

        env = {"FLUID_COMMAND_CENTER_URL": "https://from-env.example.com"}
        with patch.dict(os.environ, env, clear=True):
            with patch("pathlib.Path.home", return_value=tmp_path):
                cfg = CommandCenterConfig.from_environment()
                # Env var wins
                assert cfg.url == "https://from-env.example.com"
                # Config file values loaded for non-overridden fields
                assert cfg.api_key == "file-key"
                assert cfg.timeout == 3

    def test_invalid_timeout_ignored(self):
        env = {"FLUID_COMMAND_CENTER_TIMEOUT": "not_a_number"}
        with patch.dict(os.environ, env, clear=True):
            with patch("pathlib.Path.exists", return_value=False):
                cfg = CommandCenterConfig.from_environment()
                assert cfg.timeout == 5  # default

    def test_config_file_error_gracefully_handled(self, tmp_path):
        config_file = tmp_path / ".fluid" / "config.yaml"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("{{invalid yaml")
        with patch.dict(os.environ, {}, clear=True):
            with patch("pathlib.Path.home", return_value=tmp_path):
                cfg = CommandCenterConfig.from_environment()
                assert cfg.url is None  # defaults still work

    def test_repr_masks_api_key_with_short_fixed_preview(self):
        cfg = CommandCenterConfig(
            url="https://cc.example.com",
            api_key="fluid_abcdefghijklmnopqrstuvwxyz",
        )

        rendered = repr(cfg)

        assert "fluid_abcdefghijklmnopqrstuvwxyz" not in rendered
        assert "api_key=flui***REDACTED***" in rendered


class TestNoHomeDirectory:
    """With no home directory there is no operator config: the working
    directory (the checkout) is never read in its place."""

    def _no_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.delenv("USERPROFILE", raising=False)
        for var in ("FLUID_COMMAND_CENTER_URL", "FLUID_COMMAND_CENTER_ENABLED"):
            monkeypatch.delenv(var, raising=False)

        def _raise():
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr("pathlib.Path.home", staticmethod(_raise))
        checkout = tmp_path / "checkout"
        (checkout / ".fluid").mkdir(parents=True)
        (checkout / ".fluid" / "config.yaml").write_text(
            "command_center:\n  url: https://cc-attacker.example.test\n  enabled: true\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(checkout)

    def test_the_checkout_is_not_read_as_the_operators_config(self, monkeypatch, tmp_path):
        self._no_home(monkeypatch, tmp_path)
        monkeypatch.setenv("FLUID_COMMAND_CENTER_API_KEY", "operator-key-not-a-secret")

        cfg = CommandCenterConfig.from_environment()

        assert cfg.url is None
        assert cfg.is_configured() is False

    def test_the_apply_report_has_nothing_to_send_to(self, monkeypatch, tmp_path):
        import logging

        from fluid_build.cli._apply_cc_report import _command_center_config

        self._no_home(monkeypatch, tmp_path)
        monkeypatch.setenv("FLUID_COMMAND_CENTER_API_KEY", "operator-key-not-a-secret")

        config, reason = _command_center_config(logging.getLogger("test"))

        assert config is None
        assert "cc-attacker" not in str(reason)

    def test_the_environment_still_configures_it(self, monkeypatch, tmp_path):
        self._no_home(monkeypatch, tmp_path)
        monkeypatch.setenv("FLUID_COMMAND_CENTER_URL", "https://cc.example.com")
        monkeypatch.setenv("FLUID_COMMAND_CENTER_API_KEY", "operator-key-not-a-secret")

        cfg = CommandCenterConfig.from_environment()

        assert cfg.url == "https://cc.example.com"
        assert cfg.is_configured() is True
