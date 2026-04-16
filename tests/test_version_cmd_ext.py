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

"""Extended unit tests for fluid_build.cli.version_cmd module."""

import argparse
import logging
from unittest.mock import Mock, patch

from fluid_build.cli.version_cmd import (
    _detect_features,
    _detect_providers,
    _display_version_info,
    _gather_version_info,
    register,
    run,
)

logger = logging.getLogger("test_version_cmd_ext")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs):
    defaults = {"short": False, "format": "text", "verbose": False}
    defaults.update(kwargs)
    args = Mock()
    for k, v in defaults.items():
        setattr(args, k, v)
    return args


# ---------------------------------------------------------------------------
# _detect_features
# ---------------------------------------------------------------------------


class TestDetectFeatures:
    def test_core_validation_always_true(self):
        features = _detect_features()
        assert features["core_validation"] is True
        assert features["legacy_057"] is True

    def test_returns_dict(self):
        features = _detect_features()
        assert isinstance(features, dict)

    def test_optional_features_are_bool(self):
        features = _detect_features()
        for key, value in features.items():
            assert isinstance(value, bool), f"{key} should be bool, got {type(value)}"

    def test_provider_actions_feature_present(self):
        features = _detect_features()
        assert "provider_actions" in features

    def test_sovereignty_feature_present(self):
        features = _detect_features()
        assert "sovereignty" in features


# ---------------------------------------------------------------------------
# _detect_providers
# ---------------------------------------------------------------------------


class TestDetectProviders:
    def test_returns_dict(self):
        providers = _detect_providers()
        assert isinstance(providers, dict)

    def test_local_always_available(self):
        providers = _detect_providers()
        assert providers.get("local") == "available"

    def test_keys_have_string_values(self):
        providers = _detect_providers()
        for k, v in providers.items():
            assert isinstance(v, str)


# ---------------------------------------------------------------------------
# _gather_version_info
# ---------------------------------------------------------------------------


class TestGatherVersionInfo:
    def test_basic_structure(self):
        args = _make_args()
        info = _gather_version_info(args)
        assert "cli" in info
        assert "spec_versions" in info
        assert "features" in info
        assert "providers" in info

    def test_cli_version_present(self):
        args = _make_args()
        info = _gather_version_info(args)
        assert "version" in info["cli"]
        assert info["cli"]["version"] is not None

    def test_verbose_adds_python_info(self):
        args = _make_args(verbose=True)
        info = _gather_version_info(args)
        assert "python" in info
        assert "system" in info
        assert "version" in info["python"]

    def test_non_verbose_no_python_info(self):
        args = _make_args(verbose=False)
        info = _gather_version_info(args)
        assert "python" not in info
        assert "system" not in info

    def test_spec_versions_has_required_keys(self):
        args = _make_args()
        info = _gather_version_info(args)
        spec = info["spec_versions"]
        assert "supported" in spec
        assert "default" in spec
        assert "latest" in spec


# ---------------------------------------------------------------------------
# _display_version_info
# ---------------------------------------------------------------------------


class TestDisplayVersionInfo:
    def _make_version_info(self, verbose=False):
        base = {
            "cli": {"version": "1.0.0", "api_version": "v1", "build": "production"},
            "spec_versions": {
                "supported": ["0.5.7", "0.7.1"],
                "default": "0.7.1",
                "latest": "0.7.1",
            },
            "features": {
                "core_validation": True,
                "legacy_057": True,
                "provider_actions": False,
                "0.7.1_support": True,
            },
            "providers": {"local": "available", "gcp": "not installed"},
        }
        if verbose:
            base["python"] = {
                "version": "3.9.0",
                "executable": "/usr/bin/python3",
                "platform": "linux",
            }
            base["system"] = {
                "platform": "Linux",
                "system": "Linux",
                "machine": "x86_64",
            }
        return base

    def test_display_text_no_errors(self):
        info = self._make_version_info()
        with patch("fluid_build.cli.version_cmd.cprint"):
            with patch("fluid_build.cli.version_cmd.RICH_AVAILABLE", False):
                _display_version_info(info, verbose=False)

    def test_display_text_verbose(self):
        info = self._make_version_info(verbose=True)
        with patch("fluid_build.cli.version_cmd.cprint"):
            with patch("fluid_build.cli.version_cmd.RICH_AVAILABLE", False):
                _display_version_info(info, verbose=True)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_version_command(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["version"])
        assert hasattr(args, "func")

    def test_register_version_verbose(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["version", "--verbose"])
        assert args.verbose is True

    def test_register_version_short(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["version", "--short"])
        assert args.short is True

    def test_register_version_json_format(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["version", "--format", "json"])
        assert args.format == "json"


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


class TestRunFunction:
    def test_run_short_returns_0(self):
        args = _make_args(short=True)
        with patch("fluid_build.cli.version_cmd.cprint"):
            result = run(args, logger)
        assert result == 0

    def test_run_text_format_returns_0(self):
        args = _make_args(short=False, format="text")
        with patch("fluid_build.cli.version_cmd.cprint"):
            with patch("fluid_build.cli.version_cmd._display_version_info"):
                result = run(args, logger)
        assert result == 0

    def test_run_json_format_returns_0(self):
        args = _make_args(short=False, format="json")
        with patch("fluid_build.cli.version_cmd.cprint"):
            result = run(args, logger)
        assert result == 0

    def test_run_verbose_returns_0(self):
        args = _make_args(short=False, format="text", verbose=True)
        with patch("fluid_build.cli.version_cmd.cprint"):
            with patch("fluid_build.cli.version_cmd._display_version_info"):
                result = run(args, logger)
        assert result == 0
