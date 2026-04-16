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

"""Tests for fluid_build.cli._command_center."""

from unittest.mock import MagicMock, mock_open, patch

import pytest

from fluid_build.cli._command_center import (
    CommandCenterClient,
    CommandCenterFeatures,
)

# ── CommandCenterFeatures ─────────────────────────────────────────────


class TestCommandCenterFeatures:
    def test_defaults(self):
        f = CommandCenterFeatures()
        assert f.marketplace is False
        assert f.catalog is False
        assert f.governance is False
        assert f.analytics is False
        assert f.version == "unknown"

    def test_custom_values(self):
        f = CommandCenterFeatures(marketplace=True, version="1.2.3")
        assert f.marketplace is True
        assert f.version == "1.2.3"


# ── CommandCenterClient ───────────────────────────────────────────────


class TestCommandCenterClient:
    @patch.object(CommandCenterClient, "_check_availability")
    @patch.object(CommandCenterClient, "_detect_url")
    def test_init_with_url(self, mock_detect, mock_check):
        mock_detect.return_value = "http://localhost:8000"
        client = CommandCenterClient()
        assert client.url == "http://localhost:8000"
        mock_check.assert_called_once()

    @patch.object(CommandCenterClient, "_check_availability")
    @patch.object(CommandCenterClient, "_detect_url")
    def test_init_without_url(self, mock_detect, mock_check):
        mock_detect.return_value = None
        client = CommandCenterClient()
        assert client.url is None
        assert client.available is False
        mock_check.assert_not_called()

    @patch.object(CommandCenterClient, "_check_availability")
    def test_detect_url_from_env(self, _mock_check):
        with patch.dict("os.environ", {"FLUID_COMMAND_CENTER_URL": "http://cc.example.com"}):
            client = CommandCenterClient()
            assert client.url == "http://cc.example.com"

    @patch.object(CommandCenterClient, "_check_availability")
    @patch("fluid_build.cli._command_center.Path")
    def test_detect_url_from_config_file(self, mock_path_cls, _mock_check):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("FLUID_COMMAND_CENTER_URL", None)

            mock_config_path = MagicMock()
            mock_config_path.exists.return_value = True
            mock_config_path.read_text.return_value = (
                "command_center:\n  url: http://from-config.example.com\n"
            )

            mock_home = MagicMock()
            mock_home.__truediv__ = MagicMock(
                side_effect=lambda x: (
                    mock_config_path
                    if "config" in str(x)
                    else MagicMock(exists=MagicMock(return_value=False))
                )
            )
            mock_path_cls.home.return_value = mock_home

            client = CommandCenterClient()
            # Should attempt to read config file

    @patch.object(CommandCenterClient, "_detect_url", return_value="http://localhost:8000")
    def test_check_availability_success(self, _mock_detect):
        mock_health_response = MagicMock()
        mock_health_response.status_code = 200

        mock_head_response = MagicMock()
        mock_head_response.status_code = 200

        mock_version_response = MagicMock()
        mock_version_response.status_code = 200
        mock_version_response.json.return_value = {"version": "2.0.0"}

        with (
            patch(
                "fluid_build.cli._command_center.requests.get",
                side_effect=[mock_health_response, mock_version_response],
            ),
            patch(
                "fluid_build.cli._command_center.requests.head",
                return_value=mock_head_response,
            ),
        ):
            client = CommandCenterClient()
            assert client.available is True

    @patch.object(CommandCenterClient, "_detect_url", return_value="http://localhost:8000")
    def test_check_availability_failure(self, _mock_detect):
        with patch(
            "fluid_build.cli._command_center.requests.get",
            side_effect=Exception("connection refused"),
        ):
            client = CommandCenterClient()
            assert client.available is False

    @patch.object(CommandCenterClient, "_detect_url", return_value="http://localhost:8000")
    def test_check_availability_requests_not_available(self, _mock_detect):
        with patch("fluid_build.cli._command_center.REQUESTS_AVAILABLE", False):
            client = CommandCenterClient()
            assert client.available is False
