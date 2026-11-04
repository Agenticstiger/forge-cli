# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Tests for fluid_build.cli.marketplace."""

import argparse
import contextlib
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

import fluid_build.cli.marketplace as marketplace_module
from fluid_build.cli._common import CLIError
from fluid_build.cli.marketplace import (
    instantiate_blueprint,
    interactive_parameter_wizard,
    show_blueprint_info,
)
from fluid_build.util import http_client

# ── helpers ────────────────────────────────────────────────────────────


def _logger():
    return logging.getLogger("test.marketplace")


def _args(**kwargs):
    defaults = {
        "marketplace_action": None,
        "blueprint_id": "test-bp",
        "version": None,
        "show_template": False,
        "params": None,
        "interactive": False,
        "output": None,
        "validate": True,
        "submit": False,
        "query": None,
        "category": None,
        "tags": None,
        "maturity": None,
        "state": "published",
        "sort": "downloads",
        "limit": 20,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _make_blueprint_json(**kwargs):
    defaults = {
        "id": "test-bp",
        "name": "Test Blueprint",
        "description": "A test blueprint",
        "version": "1.0.0",
        "category": "analytics",
        "state": "published",
        "author": {"name": "Author", "organization": "Org"},
        "labels": {"maturity": "stable", "license": "Apache-2.0"},
        "download_count": 100,
        "usage_count": 50,
        "success_rate": 0.95,
        "tags": ["etl", "analytics"],
        "spec": {
            "parameters": [
                {
                    "name": "project_id",
                    "type": "string",
                    "required": True,
                    "description": "GCP project ID",
                    "default": None,
                }
            ]
        },
    }
    defaults.update(kwargs)
    return defaults


# ── show_blueprint_info ───────────────────────────────────────────────


API = "http://localhost:8000/api/v1/blueprints"


@contextlib.contextmanager
def _spy_client():
    """Record the keyword arguments each marketplace request opens its client with."""
    with patch("fluid_build.util.http_client.new_client", wraps=http_client.new_client) as spy:
        yield spy


class TestShowBlueprintInfo:
    def test_success_returns_zero(self):
        args = _args(blueprint_id="test-bp", version=None, show_template=False)

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            respx.mock(assert_all_called=False) as router,
            _spy_client() as spy,
        ):
            route = router.get(f"{API}/test-bp").mock(
                return_value=httpx.Response(200, json=_make_blueprint_json())
            )
            result = show_blueprint_info(args, _logger(), API)

        assert result == 0
        assert route.call_count == 1
        assert (
            spy.call_args.kwargs["timeout"] == marketplace_module.MARKETPLACE_HTTP_TIMEOUT_SECONDS
        )

    def test_request_exception_returns_one(self):
        args = _args(blueprint_id="test-bp", version=None, show_template=False)

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            respx.mock(assert_all_called=False) as router,
        ):
            router.get(f"{API}/test-bp").mock(side_effect=httpx.HTTPError("connection refused"))
            result = show_blueprint_info(args, _logger(), API)

        assert result == 1

    def test_version_passed_in_params(self):
        args = _args(blueprint_id="test-bp", version="2.0.0", show_template=False)

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            respx.mock(assert_all_called=False) as router,
        ):
            route = router.get(f"{API}/test-bp").mock(
                return_value=httpx.Response(200, json=_make_blueprint_json())
            )
            show_blueprint_info(args, _logger(), API)

        assert route.calls.last.request.url.params.get("version") == "2.0.0"

    def test_connection_error_returns_concise_message(self):
        args = _args(blueprint_id="test-bp", version=None, show_template=False)
        mock_console = MagicMock()

        with (
            patch.object(marketplace_module, "console", mock_console),
            respx.mock(assert_all_called=False) as router,
        ):
            route = router.get(f"{API}/test-bp").mock(
                side_effect=httpx.ConnectError("connection refused")
            )
            result = show_blueprint_info(args, _logger(), API)

        assert result == 1
        assert route.call_count == marketplace_module.MARKETPLACE_HTTP_RETRIES + 1
        assert "Could not connect to the marketplace API" in str(mock_console.print.call_args_list)

    def test_success_rate_greater_than_one_shown_as_percentage(self):
        args = _args(blueprint_id="test-bp", version=None, show_template=False)
        bp_data = _make_blueprint_json(success_rate=97.5)

        mock_console = MagicMock()

        with (
            patch.object(marketplace_module, "console", mock_console),
            respx.mock(assert_all_called=False) as router,
            _spy_client() as spy,
        ):
            router.get(f"{API}/test-bp").mock(return_value=httpx.Response(200, json=bp_data))
            result = show_blueprint_info(args, _logger(), API)

        assert result == 0
        assert (
            spy.call_args.kwargs["timeout"] == marketplace_module.MARKETPLACE_HTTP_TIMEOUT_SECONDS
        )

    def test_show_template_prints_syntax(self):
        args = _args(blueprint_id="test-bp", version=None, show_template=True)
        bp_data = _make_blueprint_json()
        bp_data["contract_template"] = "{{ project_id }}"

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            respx.mock(assert_all_called=False) as router,
            _spy_client() as spy,
            patch("fluid_build.cli.marketplace.Syntax", MagicMock()),
        ):
            router.get(f"{API}/test-bp").mock(return_value=httpx.Response(200, json=bp_data))
            result = show_blueprint_info(args, _logger(), API)

        assert result == 0
        assert (
            spy.call_args.kwargs["timeout"] == marketplace_module.MARKETPLACE_HTTP_TIMEOUT_SECONDS
        )


# ── instantiate_blueprint ─────────────────────────────────────────────


class TestInstantiateBlueprint:
    def _mock_routes(self, router, bp_data, instantiate_result=None):
        get_route = router.get(f"{API}/test-bp").mock(
            return_value=httpx.Response(200, json=bp_data)
        )
        post_route = router.post(f"{API}/test-bp/instantiate").mock(
            return_value=httpx.Response(
                200,
                json=instantiate_result or {"contract": {"builds": []}, "cost_estimate": "$0.10"},
            )
        )
        return get_route, post_route

    def test_no_params_no_interactive_returns_one(self):
        args = _args(blueprint_id="test-bp", params=None, interactive=False)
        bp_data = _make_blueprint_json()

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            respx.mock(assert_all_called=False) as router,
        ):
            self._mock_routes(router, bp_data)
            result = instantiate_blueprint(args, _logger(), API)

        assert result == 1

    def test_params_as_json_string_returns_zero(self):
        args = _args(
            blueprint_id="test-bp",
            params='{"project_id": "my-proj"}',
            interactive=False,
            output=None,
            submit=False,
        )
        bp_data = _make_blueprint_json()

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            respx.mock(assert_all_called=False) as router,
            _spy_client() as spy,
            patch("fluid_build.cli.marketplace.Syntax", MagicMock()),
        ):
            get_route, post_route = self._mock_routes(router, bp_data)
            result = instantiate_blueprint(args, _logger(), API)

        assert result == 0
        assert get_route.call_count == 1
        assert post_route.call_count == 1
        timeouts = [call.kwargs["timeout"] for call in spy.call_args_list]
        assert timeouts == [marketplace_module.MARKETPLACE_HTTP_TIMEOUT_SECONDS] * 2

    def test_params_from_file(self, tmp_path):
        params_file = tmp_path / "params.json"
        params_file.write_text(json.dumps({"project_id": "my-proj"}))
        args = _args(
            blueprint_id="test-bp",
            params=str(params_file),
            interactive=False,
            output=None,
            submit=False,
        )
        bp_data = _make_blueprint_json()

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            respx.mock(assert_all_called=False) as router,
            patch("fluid_build.cli.marketplace.Syntax", MagicMock()),
        ):
            self._mock_routes(router, bp_data)
            result = instantiate_blueprint(args, _logger(), API)

        assert result == 0

    def test_fetch_failure_returns_one(self):
        args = _args(blueprint_id="test-bp", params='{"x": 1}', interactive=False)

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            respx.mock(assert_all_called=False) as router,
        ):
            router.get(f"{API}/test-bp").mock(side_effect=httpx.HTTPError("conn error"))
            result = instantiate_blueprint(args, _logger(), API)

        assert result == 1

    def test_fetch_timeout_retries_and_returns_one(self):
        args = _args(blueprint_id="test-bp", params='{"x": 1}', interactive=False)
        mock_console = MagicMock()

        with (
            patch.object(marketplace_module, "console", mock_console),
            respx.mock(assert_all_called=False) as router,
        ):
            route = router.get(f"{API}/test-bp").mock(
                side_effect=httpx.ReadTimeout("read timed out")
            )
            result = instantiate_blueprint(args, _logger(), API)

        assert result == 1
        assert route.call_count == marketplace_module.MARKETPLACE_HTTP_RETRIES + 1
        assert "timed out after" in str(mock_console.print.call_args_list)

    def test_output_file_saved(self, tmp_path):
        output_file = tmp_path / "contract.json"
        args = _args(
            blueprint_id="test-bp",
            params='{"project_id": "p"}',
            interactive=False,
            output=str(output_file),
            submit=False,
        )
        bp_data = _make_blueprint_json()

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            respx.mock(assert_all_called=False) as router,
            patch("fluid_build.cli.marketplace.Syntax", MagicMock()),
        ):
            self._mock_routes(router, bp_data)
            result = instantiate_blueprint(args, _logger(), API)

        assert result == 0
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert "builds" in data

    def test_instantiate_request_failure_returns_one(self):
        args = _args(
            blueprint_id="test-bp",
            params='{"project_id": "p"}',
            interactive=False,
            output=None,
            submit=False,
        )
        bp_data = _make_blueprint_json()

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            respx.mock(assert_all_called=False) as router,
        ):
            router.get(f"{API}/test-bp").mock(return_value=httpx.Response(200, json=bp_data))
            router.post(f"{API}/test-bp/instantiate").mock(
                side_effect=httpx.HTTPError("instantiate failed")
            )
            result = instantiate_blueprint(args, _logger(), API)

        assert result == 1


# ── interactive_parameter_wizard ──────────────────────────────────────


class TestInteractiveParameterWizard:
    def test_boolean_parameter(self):
        params = [
            {
                "name": "enable_logging",
                "type": "boolean",
                "required": False,
                "description": "Enable logging",
                "default": True,
                "example": None,
                "enum": [],
            }
        ]

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            patch("fluid_build.cli.marketplace.Confirm") as mock_confirm,
        ):
            mock_confirm.ask.return_value = True
            result = interactive_parameter_wizard(params)

        assert result["enable_logging"] is True

    def test_enum_parameter(self):
        params = [
            {
                "name": "region",
                "type": "string",
                "required": True,
                "description": "Cloud region",
                "default": "us-east1",
                "example": None,
                "enum": ["us-east1", "eu-west1"],
            }
        ]

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            patch("rich.prompt.Prompt") as mock_prompt,
        ):
            mock_prompt.ask.return_value = "eu-west1"
            result = interactive_parameter_wizard(params)

        assert result["region"] == "eu-west1"
