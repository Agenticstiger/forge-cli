# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for fluid_build.cli._command_center."""

from unittest.mock import MagicMock, mock_open, patch

import httpx
import pytest
import respx

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
        with respx.mock(assert_all_called=False) as router:
            router.get("http://localhost:8000/health").respond(200)
            router.head(url__regex=r"http://localhost:8000/api/.*").respond(200)
            router.get("http://localhost:8000/api/v1/version").respond(
                200, json={"version": "2.0.0"}
            )
            client = CommandCenterClient()
            assert client.available is True

    @patch.object(CommandCenterClient, "_detect_url", return_value="http://localhost:8000")
    def test_check_availability_failure(self, _mock_detect):
        with respx.mock() as router:
            router.get("http://localhost:8000/health").mock(
                side_effect=httpx.ConnectError("connection refused")
            )
            client = CommandCenterClient()
            assert client.available is False

    @patch.object(CommandCenterClient, "_detect_url", return_value="http://localhost:8000")
    def test_check_availability_health_not_ok(self, _mock_detect):
        with respx.mock() as router:
            router.get("http://localhost:8000/health").respond(503)
            client = CommandCenterClient()
            assert client.available is False
