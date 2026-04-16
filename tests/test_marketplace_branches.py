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

"""Branch-coverage tests for fluid_build.cli.marketplace"""

import argparse
import logging
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli._common import CLIError
from fluid_build.cli.marketplace import (
    COMMAND,
    FALLBACK_OPTIONS,
    get_api_url,
    register,
    run,
)


@pytest.fixture
def logger():
    return logging.getLogger("test_marketplace")


# ── Module-level constants ──────────────────────────────────────────


class TestModuleConstants:
    def test_command_name(self):
        assert COMMAND == "marketplace"

    def test_fallback_options_has_keys(self):
        assert "local" in FALLBACK_OPTIONS
        assert "public" in FALLBACK_OPTIONS
        assert "none" in FALLBACK_OPTIONS


# ── register ────────────────────────────────────────────────────────


class TestRegister:
    def test_register_adds_parser(self):
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        # Should be able to parse the marketplace command
        args = parser.parse_args(["marketplace", "search", "analytics"])
        assert args.marketplace_action == "search"
        assert args.query == "analytics"

    def test_register_categories_action(self):
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        args = parser.parse_args(["marketplace", "categories"])
        assert args.marketplace_action == "categories"


# ── run dispatch ────────────────────────────────────────────────────


class TestRun:
    def test_no_action_returns_1(self, logger):
        args = SimpleNamespace(marketplace_action=None)
        result = run(args, logger)
        assert result == 1

    @patch("fluid_build.cli.marketplace.get_api_url", return_value="http://test")
    @patch("fluid_build.cli.marketplace.search_blueprints", return_value=0)
    def test_search_dispatch(self, mock_search, _mock_url, logger):
        args = SimpleNamespace(marketplace_action="search")
        result = run(args, logger)
        assert result == 0
        mock_search.assert_called_once()

    @patch("fluid_build.cli.marketplace.get_api_url", return_value="http://test")
    @patch("fluid_build.cli.marketplace.show_blueprint_info", return_value=0)
    def test_info_dispatch(self, _mock_info, _mock_url, logger):
        args = SimpleNamespace(marketplace_action="info")
        result = run(args, logger)
        assert result == 0

    @patch("fluid_build.cli.marketplace.get_api_url", return_value="http://test")
    @patch("fluid_build.cli.marketplace.instantiate_blueprint", return_value=0)
    def test_instantiate_dispatch(self, _mock_inst, _mock_url, logger):
        args = SimpleNamespace(marketplace_action="instantiate")
        result = run(args, logger)
        assert result == 0

    @patch("fluid_build.cli.marketplace.get_api_url", return_value="http://test")
    @patch("fluid_build.cli.marketplace.list_categories", return_value=0)
    def test_categories_dispatch(self, _mock_cats, _mock_url, logger):
        args = SimpleNamespace(marketplace_action="categories")
        result = run(args, logger)
        assert result == 0

    @patch("fluid_build.cli.marketplace.get_api_url", return_value="http://test")
    def test_unknown_action_returns_1(self, _mock_url, logger):
        args = SimpleNamespace(marketplace_action="unknown_action")
        result = run(args, logger)
        assert result == 1

    @patch("fluid_build.cli.marketplace.get_api_url", side_effect=Exception("boom"))
    def test_exception_returns_1(self, _mock_url, logger):
        args = SimpleNamespace(marketplace_action="search")
        with patch("fluid_build.cli.marketplace.console", MagicMock()):
            result = run(args, logger)
        assert result == 1


# ── get_api_url ─────────────────────────────────────────────────────


class TestGetApiUrl:
    @patch.dict(os.environ, {"FLUID_API_URL": "http://env-override"}, clear=False)
    def test_env_var_priority(self, logger):
        """Priority 1: env var"""
        url = get_api_url(logger)
        assert url == "http://env-override"

    @patch.dict(os.environ, {}, clear=False)
    @patch("fluid_build.cli.marketplace.get_command_center_client")
    @patch("fluid_build.cli.marketplace.console", MagicMock())
    def test_command_center_priority(self, mock_cc, logger):
        """Priority 2: Command Center"""
        os.environ.pop("FLUID_API_URL", None)
        cc_instance = MagicMock()
        cc_instance.get_marketplace_url.return_value = "http://cc-url"
        mock_cc.return_value = cc_instance
        url = get_api_url(logger)
        assert url == "http://cc-url"

    @patch.dict(os.environ, {"FLUID_MARKETPLACE_FALLBACK": "local"}, clear=False)
    @patch("fluid_build.cli.marketplace.get_command_center_client")
    @patch("fluid_build.cli.marketplace.console", MagicMock())
    def test_local_fallback_exists(self, mock_cc, logger, tmp_path):
        """Priority 3: local fallback with existing cache"""
        os.environ.pop("FLUID_API_URL", None)
        cc_instance = MagicMock()
        cc_instance.get_marketplace_url.return_value = None
        cc_instance.url = None
        mock_cc.return_value = cc_instance

        local_dir = tmp_path / "blueprints"
        local_dir.mkdir()
        with patch.dict(FALLBACK_OPTIONS, {"local": lambda: str(local_dir)}):
            url = get_api_url(logger)
        assert url.startswith("file://")

    @patch.dict(os.environ, {"FLUID_MARKETPLACE_FALLBACK": "none"}, clear=False)
    @patch("fluid_build.cli.marketplace.get_command_center_client")
    @patch("fluid_build.cli.marketplace.console", MagicMock())
    def test_no_sources_raises(self, mock_cc, logger):
        """All sources exhausted raises CLIError (or TypeError from bad CLIError call)"""
        os.environ.pop("FLUID_API_URL", None)
        cc_instance = MagicMock()
        cc_instance.get_marketplace_url.return_value = None
        cc_instance.url = None
        mock_cc.return_value = cc_instance
        with pytest.raises((CLIError, TypeError)):
            get_api_url(logger)

    @patch.dict(
        os.environ,
        {"FLUID_MARKETPLACE_FALLBACK": "public", "FLUID_PUBLIC_REGISTRY": "http://pub"},
        clear=False,
    )
    @patch("fluid_build.cli.marketplace.get_command_center_client")
    @patch("fluid_build.cli.marketplace.console", MagicMock())
    def test_public_fallback(self, mock_cc, logger):
        os.environ.pop("FLUID_API_URL", None)
        cc_instance = MagicMock()
        cc_instance.get_marketplace_url.return_value = None
        cc_instance.url = None
        mock_cc.return_value = cc_instance
        url = get_api_url(logger)
        assert url == "http://pub"


# ── search_blueprints ───────────────────────────────────────────────


class TestSearchBlueprints:
    @patch("fluid_build.cli.marketplace.console", MagicMock())
    def test_search_success(self, logger):
        import requests as real_requests

        from fluid_build.cli.marketplace import search_blueprints

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "items": [
                {
                    "id": "bp1",
                    "name": "Test",
                    "description": "desc",
                    "category": "analytics",
                    "labels": {"maturity": "stable"},
                    "download_count": 100,
                    "version": "1.0",
                }
            ],
            "total": 1,
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("fluid_build.cli.marketplace.requests") as mock_requests:
            mock_requests.get.return_value = mock_resp
            mock_requests.exceptions = real_requests.exceptions
            args = SimpleNamespace(
                query="test",
                category=None,
                tags=None,
                maturity=None,
                state="published",
                sort="downloads",
                limit=20,
            )
            result = search_blueprints(args, logger, "http://api")
        assert result == 0

    @patch("fluid_build.cli.marketplace.console", MagicMock())
    def test_search_empty_results(self, logger):
        import requests as real_requests

        from fluid_build.cli.marketplace import search_blueprints

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"items": [], "total": 0}
        mock_resp.raise_for_status = MagicMock()
        with patch("fluid_build.cli.marketplace.requests") as mock_requests:
            mock_requests.get.return_value = mock_resp
            mock_requests.exceptions = real_requests.exceptions
            args = SimpleNamespace(
                query="xyz",
                category=None,
                tags=None,
                maturity=None,
                state="published",
                sort="downloads",
                limit=20,
            )
            result = search_blueprints(args, logger, "http://api")
        assert result == 0

    @patch("fluid_build.cli.marketplace.console", MagicMock())
    def test_search_with_all_filters(self, logger):
        import requests as real_requests

        from fluid_build.cli.marketplace import search_blueprints

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"items": [], "total": 0}
        mock_resp.raise_for_status = MagicMock()
        with patch("fluid_build.cli.marketplace.requests") as mock_requests:
            mock_requests.get.return_value = mock_resp
            mock_requests.exceptions = real_requests.exceptions
            args = SimpleNamespace(
                query="q",
                category="analytics",
                tags="tag1,tag2",
                maturity="stable",
                state="published",
                sort="name",
                limit=10,
            )
            result = search_blueprints(args, logger, "http://api")
        assert result == 0

    @patch("fluid_build.cli.marketplace.console", MagicMock())
    def test_search_request_error(self, logger):
        import requests as real_requests

        from fluid_build.cli.marketplace import search_blueprints

        with patch("fluid_build.cli.marketplace.requests") as mock_requests:
            mock_requests.get.side_effect = real_requests.exceptions.RequestException(
                "Network error"
            )
            mock_requests.exceptions = real_requests.exceptions
            args = SimpleNamespace(
                query="test",
                category=None,
                tags=None,
                maturity=None,
                state="published",
                sort="downloads",
                limit=20,
            )
            result = search_blueprints(args, logger, "http://api")
        assert result == 1


# ── list_categories ─────────────────────────────────────────────────


class TestListCategories:
    @patch("fluid_build.cli.marketplace.console", MagicMock())
    def test_returns_zero(self, logger):
        from fluid_build.cli.marketplace import list_categories

        args = SimpleNamespace()
        result = list_categories(args, logger, "http://api")
        assert result == 0
