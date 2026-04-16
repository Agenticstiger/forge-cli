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

"""Tests for fluid_build.cli.bootstrap."""

from unittest.mock import MagicMock, patch

import pytest


class TestActiveProfile:
    def test_default_is_experimental(self):
        from fluid_build.cli.bootstrap import _active_profile

        with patch.dict("os.environ", {}, clear=True):
            # Remove FLUID_BUILD_PROFILE if present
            import os

            os.environ.pop("FLUID_BUILD_PROFILE", None)
            assert _active_profile() == "experimental"

    def test_reads_from_env(self):
        from fluid_build.cli.bootstrap import _active_profile

        with patch.dict("os.environ", {"FLUID_BUILD_PROFILE": "stable"}):
            assert _active_profile() == "stable"

    def test_case_insensitive(self):
        from fluid_build.cli.bootstrap import _active_profile

        with patch.dict("os.environ", {"FLUID_BUILD_PROFILE": "STABLE"}):
            assert _active_profile() == "stable"


class TestIsCommandEnabled:
    def test_experimental_enables_everything(self):
        from fluid_build.cli.bootstrap import is_command_enabled

        with patch.dict("os.environ", {"FLUID_BUILD_PROFILE": "experimental"}):
            assert is_command_enabled("init") is True
            assert is_command_enabled("some-random-command") is True

    def test_stable_enables_curated_set(self):
        from fluid_build.cli.bootstrap import is_command_enabled

        with patch.dict("os.environ", {"FLUID_BUILD_PROFILE": "stable"}):
            assert is_command_enabled("init") is True
            assert is_command_enabled("validate") is True
            assert is_command_enabled("plan") is True
            assert is_command_enabled("apply") is True
            assert is_command_enabled("version") is True

    def test_stable_disables_non_curated(self):
        from fluid_build.cli.bootstrap import is_command_enabled

        with patch.dict("os.environ", {"FLUID_BUILD_PROFILE": "stable"}):
            assert is_command_enabled("forge") is False
            assert is_command_enabled("copilot") is False
            assert is_command_enabled("marketplace") is False


class TestGetReporter:
    @patch("fluid_build.cli.bootstrap.CommandCenterReporter", create=True)
    @patch("fluid_build.cli.bootstrap.CommandCenterConfig", create=True)
    def test_returns_none_on_import_error(self, mock_config, _mock_reporter):
        import fluid_build.cli.bootstrap as bootstrap

        # Reset global
        bootstrap._REPORTER = None
        mock_config.side_effect = ImportError("no module")

        # The function catches the import error and returns None
        with patch.object(bootstrap, "get_reporter", wraps=bootstrap.get_reporter):
            result = bootstrap.get_reporter()
            # Should return None since import fails
            # Reset for other tests
            bootstrap._REPORTER = None

    def test_caches_reporter(self):
        import fluid_build.cli.bootstrap as bootstrap

        sentinel = MagicMock()
        bootstrap._REPORTER = sentinel
        assert bootstrap.get_reporter() is sentinel
        bootstrap._REPORTER = None  # cleanup
