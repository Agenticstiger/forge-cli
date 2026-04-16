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

"""Tests for fluid_build.cli.marketplace."""

import argparse
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import fluid_build.cli.marketplace as marketplace_module
from fluid_build.cli._common import CLIError
from fluid_build.cli.marketplace import (
    instantiate_blueprint,
    interactive_parameter_wizard,
    show_blueprint_info,
)

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


class TestShowBlueprintInfo:
    def test_success_returns_zero(self):
        args = _args(blueprint_id="test-bp", version=None, show_template=False)
        mock_response = MagicMock()
        mock_response.json.return_value = _make_blueprint_json()

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            patch("fluid_build.cli.marketplace.requests") as mock_requests,
        ):
            mock_requests.get.return_value = mock_response
            mock_requests.exceptions.RequestException = Exception
            result = show_blueprint_info(args, _logger(), "http://localhost:8000/api/v1/blueprints")

        assert result == 0

    def test_request_exception_returns_one(self):
        args = _args(blueprint_id="test-bp", version=None, show_template=False)

        class FakeRequestError(Exception):
            pass

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            patch("fluid_build.cli.marketplace.requests") as mock_requests,
        ):
            mock_requests.get.side_effect = FakeRequestError("connection refused")
            mock_requests.exceptions.RequestException = FakeRequestError
            result = show_blueprint_info(args, _logger(), "http://localhost:8000/api/v1/blueprints")

        assert result == 1

    def test_version_passed_in_params(self):
        args = _args(blueprint_id="test-bp", version="2.0.0", show_template=False)
        mock_response = MagicMock()
        mock_response.json.return_value = _make_blueprint_json()
        captured_params = {}

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            patch("fluid_build.cli.marketplace.requests") as mock_requests,
        ):
            mock_requests.get.side_effect = lambda url, params=None: (
                captured_params.update(params or {}),
                mock_response,
            )[-1]
            mock_requests.exceptions.RequestException = Exception
            show_blueprint_info(args, _logger(), "http://localhost:8000/api/v1/blueprints")

        assert captured_params.get("version") == "2.0.0"

    def test_success_rate_greater_than_one_shown_as_percentage(self):
        args = _args(blueprint_id="test-bp", version=None, show_template=False)
        bp_data = _make_blueprint_json(success_rate=97.5)
        mock_response = MagicMock()
        mock_response.json.return_value = bp_data
        printed_calls = []

        mock_console = MagicMock()

        with (
            patch.object(marketplace_module, "console", mock_console),
            patch("fluid_build.cli.marketplace.requests") as mock_requests,
        ):
            mock_requests.get.return_value = mock_response
            mock_requests.exceptions.RequestException = Exception
            result = show_blueprint_info(args, _logger(), "http://localhost:8000/api/v1/blueprints")

        assert result == 0

    def test_show_template_prints_syntax(self):
        args = _args(blueprint_id="test-bp", version=None, show_template=True)
        bp_data = _make_blueprint_json()
        bp_data["contract_template"] = "{{ project_id }}"
        mock_response = MagicMock()
        mock_response.json.return_value = bp_data

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            patch("fluid_build.cli.marketplace.requests") as mock_requests,
            patch("fluid_build.cli.marketplace.Syntax", MagicMock()),
        ):
            mock_requests.get.return_value = mock_response
            mock_requests.exceptions.RequestException = Exception
            result = show_blueprint_info(args, _logger(), "http://localhost:8000/api/v1/blueprints")

        assert result == 0


# ── instantiate_blueprint ─────────────────────────────────────────────


class TestInstantiateBlueprint:
    def _mock_requests(self, mock_requests, bp_data, instantiate_result=None):
        mock_requests.exceptions.RequestException = Exception
        bp_response = MagicMock()
        bp_response.json.return_value = bp_data

        inst_response = MagicMock()
        inst_response.json.return_value = instantiate_result or {
            "contract": {"builds": []},
            "cost_estimate": "$0.10",
        }

        def fake_get(url, **kwargs):
            return bp_response

        def fake_post(url, **kwargs):
            return inst_response

        mock_requests.get.side_effect = fake_get
        mock_requests.post.side_effect = fake_post

    def test_no_params_no_interactive_returns_one(self):
        args = _args(blueprint_id="test-bp", params=None, interactive=False)
        bp_data = _make_blueprint_json()

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            patch("fluid_build.cli.marketplace.requests") as mock_requests,
        ):
            self._mock_requests(mock_requests, bp_data)
            result = instantiate_blueprint(
                args, _logger(), "http://localhost:8000/api/v1/blueprints"
            )

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
            patch("fluid_build.cli.marketplace.requests") as mock_requests,
            patch("fluid_build.cli.marketplace.Syntax", MagicMock()),
        ):
            self._mock_requests(mock_requests, bp_data)
            result = instantiate_blueprint(
                args, _logger(), "http://localhost:8000/api/v1/blueprints"
            )

        assert result == 0

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
            patch("fluid_build.cli.marketplace.requests") as mock_requests,
            patch("fluid_build.cli.marketplace.Syntax", MagicMock()),
        ):
            self._mock_requests(mock_requests, bp_data)
            result = instantiate_blueprint(
                args, _logger(), "http://localhost:8000/api/v1/blueprints"
            )

        assert result == 0

    def test_fetch_failure_returns_one(self):
        args = _args(blueprint_id="test-bp", params='{"x": 1}', interactive=False)

        class FakeError(Exception):
            pass

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            patch("fluid_build.cli.marketplace.requests") as mock_requests,
        ):
            mock_requests.get.side_effect = FakeError("conn error")
            mock_requests.exceptions.RequestException = FakeError
            result = instantiate_blueprint(
                args, _logger(), "http://localhost:8000/api/v1/blueprints"
            )

        assert result == 1

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
            patch("fluid_build.cli.marketplace.requests") as mock_requests,
            patch("fluid_build.cli.marketplace.Syntax", MagicMock()),
        ):
            self._mock_requests(mock_requests, bp_data)
            result = instantiate_blueprint(
                args, _logger(), "http://localhost:8000/api/v1/blueprints"
            )

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

        class FakeReqError(Exception):
            response = None

        with (
            patch.object(marketplace_module, "console", MagicMock()),
            patch("fluid_build.cli.marketplace.requests") as mock_requests,
        ):
            mock_requests.exceptions.RequestException = FakeReqError
            bp_response = MagicMock()
            bp_response.json.return_value = bp_data
            mock_requests.get.return_value = bp_response
            mock_requests.post.side_effect = FakeReqError("instantiate failed")
            result = instantiate_blueprint(
                args, _logger(), "http://localhost:8000/api/v1/blueprints"
            )

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
