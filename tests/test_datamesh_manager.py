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
Unit tests for fluid_build.cli.datamesh_manager — 62% coverage.

Covers: add_parser(), _make_provider(), _cmd_publish(), _publish_exit_code(),
_failure_reason(), _print_publish_result(), _print_dry_run(),
_cmd_list(), _cmd_get(), _cmd_delete(), _cmd_teams().
"""

from __future__ import annotations

import argparse
import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

import pytest

from fluid_build.cli.datamesh_manager import (
    _cmd_delete,
    _cmd_get,
    _cmd_list,
    _cmd_publish,
    _cmd_teams,
    _failure_reason,
    _make_provider,
    _print_dry_run,
    _print_publish_result,
    _publish_exit_code,
    add_parser,
)
from fluid_build.providers.base import ProviderError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs):
    defaults = dict(
        contract="c.yaml",
        overlay=None,
        team_id=None,
        dry_run=False,
        with_contract=False,
        no_create_team=False,
        contract_format="odcs",
        data_product_spec=None,
        provider=None,
        validate_generated_contracts=False,
        validation_mode="warn",
        fail_on_contract_error=False,
        api_key=None,
        api_url=None,
        product_id="prod-123",
        yes=False,
        format="table",
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Tests: add_parser()
# ---------------------------------------------------------------------------


class TestAddParser:
    def _make_subparsers(self):
        parser = argparse.ArgumentParser()
        return parser, parser.add_subparsers()

    def test_parser_registered_with_alias(self):
        parent, subs = self._make_subparsers()
        add_parser(subs)
        # Both 'datamesh-manager' and 'dmm' should be parseable
        args = parent.parse_args(["datamesh-manager", "list"])
        assert args.dmm_command == "list"

    def test_dmm_alias(self):
        parent, subs = self._make_subparsers()
        add_parser(subs)
        args = parent.parse_args(["dmm", "list"])
        assert args.dmm_command == "list"

    def test_publish_subcommand_sets_func(self):
        parent, subs = self._make_subparsers()
        add_parser(subs)
        args = parent.parse_args(["datamesh-manager", "publish", "c.yaml"])
        assert args.func is _cmd_publish

    def test_list_subcommand_sets_func(self):
        parent, subs = self._make_subparsers()
        add_parser(subs)
        args = parent.parse_args(["datamesh-manager", "list"])
        assert args.func is _cmd_list

    def test_get_subcommand_sets_func(self):
        parent, subs = self._make_subparsers()
        add_parser(subs)
        args = parent.parse_args(["datamesh-manager", "get", "my-product"])
        assert args.func is _cmd_get
        assert args.product_id == "my-product"

    def test_delete_subcommand_sets_func(self):
        parent, subs = self._make_subparsers()
        add_parser(subs)
        args = parent.parse_args(["datamesh-manager", "delete", "my-product"])
        assert args.func is _cmd_delete

    def test_teams_subcommand_sets_func(self):
        parent, subs = self._make_subparsers()
        add_parser(subs)
        args = parent.parse_args(["datamesh-manager", "teams"])
        assert args.func is _cmd_teams

    def test_publish_dry_run_flag(self):
        parent, subs = self._make_subparsers()
        add_parser(subs)
        args = parent.parse_args(["datamesh-manager", "publish", "c.yaml", "--dry-run"])
        assert args.dry_run is True

    def test_publish_with_contract_flag(self):
        parent, subs = self._make_subparsers()
        add_parser(subs)
        args = parent.parse_args(["datamesh-manager", "publish", "c.yaml", "--with-contract"])
        assert args.with_contract is True

    def test_publish_contract_format_dcs(self):
        parent, subs = self._make_subparsers()
        add_parser(subs)
        args = parent.parse_args(
            ["datamesh-manager", "publish", "c.yaml", "--contract-format", "dcs"]
        )
        assert args.contract_format == "dcs"

    def test_publish_api_key_flag(self):
        parent, subs = self._make_subparsers()
        add_parser(subs)
        args = parent.parse_args(["datamesh-manager", "publish", "c.yaml", "--api-key", "my-key"])
        assert args.api_key == "my-key"

    def test_delete_yes_flag(self):
        parent, subs = self._make_subparsers()
        add_parser(subs)
        args = parent.parse_args(["datamesh-manager", "delete", "prod-1", "--yes"])
        assert args.yes is True

    def test_list_format_json(self):
        parent, subs = self._make_subparsers()
        add_parser(subs)
        args = parent.parse_args(["datamesh-manager", "list", "--format", "json"])
        assert args.format == "json"


# ---------------------------------------------------------------------------
# Tests: _make_provider()
# ---------------------------------------------------------------------------


class TestMakeProvider:
    def test_no_api_key_or_url(self):
        args = _make_args(api_key=None, api_url=None)
        with patch("fluid_build.cli.datamesh_manager.DataMeshManagerProvider") as MockProvider:
            MockProvider.return_value = MagicMock()
            _make_provider(args)
        MockProvider.assert_called_once_with()

    def test_with_api_key(self):
        args = _make_args(api_key="my-key", api_url=None)
        with patch("fluid_build.cli.datamesh_manager.DataMeshManagerProvider") as MockProvider:
            MockProvider.return_value = MagicMock()
            _make_provider(args)
        MockProvider.assert_called_once_with(api_key="my-key")

    def test_with_api_url(self):
        args = _make_args(api_key=None, api_url="https://my.api")
        with patch("fluid_build.cli.datamesh_manager.DataMeshManagerProvider") as MockProvider:
            MockProvider.return_value = MagicMock()
            _make_provider(args)
        MockProvider.assert_called_once_with(api_url="https://my.api")

    def test_with_both_api_key_and_url(self):
        args = _make_args(api_key="k", api_url="https://u")
        with patch("fluid_build.cli.datamesh_manager.DataMeshManagerProvider") as MockProvider:
            MockProvider.return_value = MagicMock()
            _make_provider(args)
        MockProvider.assert_called_once_with(api_key="k", api_url="https://u")


# ---------------------------------------------------------------------------
# Tests: _publish_exit_code()
# ---------------------------------------------------------------------------


class TestPublishExitCode:
    def test_returns_0_when_no_odcs_contracts(self):
        result = {"odcs_contracts": []}
        args = _make_args()
        assert _publish_exit_code(result, args) == 0

    def test_returns_0_when_odcs_contracts_not_list(self):
        result = {"odcs_contracts": "not-a-list"}
        args = _make_args()
        assert _publish_exit_code(result, args) == 0

    def test_strict_mode_returns_1_on_invalid_contract(self):
        result = {"odcs_contracts": [{"valid": False, "success": True}]}
        args = _make_args(validation_mode="strict")
        assert _publish_exit_code(result, args) == 1

    def test_strict_mode_returns_0_all_valid(self):
        result = {"odcs_contracts": [{"valid": True, "success": True}]}
        args = _make_args(validation_mode="strict")
        assert _publish_exit_code(result, args) == 0

    def test_fail_on_contract_error_returns_1_on_http_failure(self):
        result = {"odcs_contracts": [{"valid": True, "success": False}]}
        args = _make_args(fail_on_contract_error=True, validation_mode="warn")
        assert _publish_exit_code(result, args) == 1

    def test_fail_on_contract_error_returns_0_all_success(self):
        result = {"odcs_contracts": [{"valid": True, "success": True}]}
        args = _make_args(fail_on_contract_error=True, validation_mode="warn")
        assert _publish_exit_code(result, args) == 0

    def test_warn_mode_with_invalid_returns_0(self):
        result = {"odcs_contracts": [{"valid": False, "success": True}]}
        args = _make_args(validation_mode="warn", fail_on_contract_error=False)
        assert _publish_exit_code(result, args) == 0


# ---------------------------------------------------------------------------
# Tests: _failure_reason()
# ---------------------------------------------------------------------------


class TestFailureReason:
    def test_validation_failed(self):
        assert _failure_reason({"valid": False}) == "VALIDATION_FAILED"

    def test_http_failed(self):
        assert _failure_reason({"valid": True, "success": False}) == "HTTP_FAILED"

    def test_no_failure(self):
        assert _failure_reason({"valid": True, "success": True}) == ""

    def test_empty_dict(self):
        # Neither flag present → no failure
        assert _failure_reason({}) == ""


# ---------------------------------------------------------------------------
# Tests: _print_publish_result()
# ---------------------------------------------------------------------------


class TestPrintPublishResult:
    def _run_without_rich(self, result):
        printed = []
        with (
            patch("fluid_build.cli.datamesh_manager.RICH_AVAILABLE", False),
            patch(
                "fluid_build.cli.datamesh_manager.success", side_effect=lambda t: printed.append(t)
            ),
            patch(
                "fluid_build.cli.datamesh_manager.cprint",
                side_effect=lambda *a: printed.append(a[0] if a else ""),
            ),
        ):
            _print_publish_result(result)
        return printed

    def test_product_id_printed(self):
        result = {"product_id": "my-product", "url": "https://view.me"}
        lines = self._run_without_rich(result)
        joined = "\n".join(str(l) for l in lines)
        assert "my-product" in joined

    def test_url_printed(self):
        result = {"product_id": "p1", "url": "https://view.me"}
        lines = self._run_without_rich(result)
        joined = "\n".join(str(l) for l in lines)
        assert "https://view.me" in joined

    def test_data_contract_section_printed(self):
        result = {
            "product_id": "p1",
            "url": "",
            "data_contract": {"contract_id": "dc-1", "url": "https://dc.view"},
        }
        lines = self._run_without_rich(result)
        joined = "\n".join(str(l) for l in lines)
        assert "dc-1" in joined

    def test_odcs_contracts_printed(self):
        result = {
            "product_id": "p1",
            "url": "",
            "odcs_contracts": [
                {"contract_id": "odcs-1", "success": True, "url": "https://odcs.view"},
            ],
        }
        lines = self._run_without_rich(result)
        joined = "\n".join(str(l) for l in lines)
        assert "odcs-1" in joined

    def test_odcs_failure_reason_printed(self):
        result = {
            "product_id": "p1",
            "url": "",
            "odcs_contracts": [
                {"contract_id": "odcs-fail", "success": False, "valid": True, "error": "500 err"},
            ],
        }
        lines = self._run_without_rich(result)
        joined = "\n".join(str(l) for l in lines)
        assert "HTTP_FAILED" in joined or "500 err" in joined

    def test_odcs_validation_error_printed(self):
        result = {
            "product_id": "p1",
            "url": "",
            "odcs_contracts": [
                {
                    "contract_id": "odcs-v",
                    "success": True,
                    "valid": False,
                    "validation_error": "schema mismatch",
                },
            ],
        }
        lines = self._run_without_rich(result)
        joined = "\n".join(str(l) for l in lines)
        assert "schema mismatch" in joined

    def test_rich_path_no_exception(self):
        result = {"product_id": "p1", "url": "https://x", "odcs_contracts": []}
        with (
            patch("fluid_build.cli.datamesh_manager.RICH_AVAILABLE", True),
            patch("fluid_build.cli.datamesh_manager.Console") as MockConsole,
            patch("fluid_build.cli.datamesh_manager.Panel"),
        ):
            MockConsole.return_value = MagicMock()
            _print_publish_result(result)  # Should not raise

    def test_rich_path_with_odcs_success(self):
        result = {
            "product_id": "p2",
            "url": "",
            "odcs_contracts": [{"contract_id": "oc1", "success": True, "url": ""}],
        }
        with (
            patch("fluid_build.cli.datamesh_manager.RICH_AVAILABLE", True),
            patch("fluid_build.cli.datamesh_manager.Console") as MockConsole,
            patch("fluid_build.cli.datamesh_manager.Panel"),
        ):
            MockConsole.return_value = MagicMock()
            _print_publish_result(result)

    def test_rich_path_with_odcs_failure_reason(self):
        result = {
            "product_id": "p3",
            "url": "",
            "odcs_contracts": [
                {
                    "contract_id": "oc2",
                    "success": False,
                    "valid": False,
                    "error": "err",
                    "validation_error": "ve",
                }
            ],
        }
        with (
            patch("fluid_build.cli.datamesh_manager.RICH_AVAILABLE", True),
            patch("fluid_build.cli.datamesh_manager.Console") as MockConsole,
            patch("fluid_build.cli.datamesh_manager.Panel"),
        ):
            MockConsole.return_value = MagicMock()
            _print_publish_result(result)


# ---------------------------------------------------------------------------
# Tests: _print_dry_run()
# ---------------------------------------------------------------------------


class TestPrintDryRun:
    def test_dry_run_plain(self):
        result = {
            "method": "PUT",
            "url": "https://api.x/p",
            "payload": {"info": {"name": "test"}},
            "odcs_contracts": [],
        }
        printed = []
        with (
            patch("fluid_build.cli.datamesh_manager.RICH_AVAILABLE", False),
            patch(
                "fluid_build.cli.datamesh_manager.cprint",
                side_effect=lambda *a: printed.append(a[0] if a else ""),
            ),
        ):
            _print_dry_run(result)
        joined = "\n".join(str(l) for l in printed)
        assert "PUT" in joined
        assert "https://api.x/p" in joined

    def test_dry_run_plain_with_odcs_contracts(self):
        result = {
            "method": "PUT",
            "url": "https://api.x/p",
            "payload": {},
            "odcs_contracts": [
                {"method": "PUT", "url": "https://api.x/c", "payload": {"id": "c1"}},
            ],
        }
        printed = []
        with (
            patch("fluid_build.cli.datamesh_manager.RICH_AVAILABLE", False),
            patch(
                "fluid_build.cli.datamesh_manager.cprint",
                side_effect=lambda *a: printed.append(a[0] if a else ""),
            ),
        ):
            _print_dry_run(result)
        joined = "\n".join(str(l) for l in printed)
        assert "ODCS" in joined

    def test_dry_run_rich_no_exception(self):
        result = {
            "method": "PUT",
            "url": "https://api.x/p",
            "payload": {},
            "odcs_contracts": [],
        }
        with (
            patch("fluid_build.cli.datamesh_manager.RICH_AVAILABLE", True),
            patch("fluid_build.cli.datamesh_manager.Console") as MockConsole,
            patch("fluid_build.cli.datamesh_manager.Panel"),
        ):
            MockConsole.return_value = MagicMock()
            _print_dry_run(result)

    def test_dry_run_rich_with_odcs(self):
        result = {
            "method": "PUT",
            "url": "https://x",
            "payload": {},
            "odcs_contracts": [{"method": "PUT", "url": "https://y", "payload": {}}],
        }
        with (
            patch("fluid_build.cli.datamesh_manager.RICH_AVAILABLE", True),
            patch("fluid_build.cli.datamesh_manager.Console") as MockConsole,
            patch("fluid_build.cli.datamesh_manager.Panel"),
        ):
            MockConsole.return_value = MagicMock()
            _print_dry_run(result)


# ---------------------------------------------------------------------------
# Tests: _cmd_publish()
# ---------------------------------------------------------------------------


class TestCmdPublish:
    def test_publish_returns_0_on_success(self):
        args = _make_args(dry_run=False)
        mock_provider = MagicMock()
        mock_provider.apply.return_value = {
            "product_id": "p1",
            "url": "https://x",
            "odcs_contracts": [],
        }

        with (
            patch("fluid_build.cli.datamesh_manager.load_contract_with_overlay", return_value={}),
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch("fluid_build.cli.datamesh_manager._print_publish_result"),
        ):
            result = _cmd_publish(args)
        assert result == 0

    def test_publish_dry_run_returns_0(self):
        args = _make_args(dry_run=True)
        mock_provider = MagicMock()
        mock_provider.apply.return_value = {
            "method": "PUT",
            "url": "https://x",
            "payload": {},
            "odcs_contracts": [],
        }

        with (
            patch("fluid_build.cli.datamesh_manager.load_contract_with_overlay", return_value={}),
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch("fluid_build.cli.datamesh_manager._print_dry_run") as mock_dry,
        ):
            result = _cmd_publish(args)
        mock_dry.assert_called_once()
        assert result == 0

    def test_publish_returns_1_on_provider_error(self):
        args = _make_args(dry_run=False)
        with (
            patch(
                "fluid_build.cli.datamesh_manager.load_contract_with_overlay",
                side_effect=ProviderError("bad creds"),
            ),
            patch("fluid_build.cli.datamesh_manager.console_error"),
        ):
            result = _cmd_publish(args)
        assert result == 1

    def test_publish_returns_1_on_generic_exception(self):
        args = _make_args(dry_run=False)
        with (
            patch(
                "fluid_build.cli.datamesh_manager.load_contract_with_overlay",
                side_effect=RuntimeError("unexpected"),
            ),
            patch("fluid_build.cli.datamesh_manager.console_error"),
        ):
            result = _cmd_publish(args)
        assert result == 1


# ---------------------------------------------------------------------------
# Tests: _cmd_list()
# ---------------------------------------------------------------------------


class TestCmdList:
    def test_list_json_format(self):
        args = _make_args(format="json")
        products = [{"info": {"id": "p1", "name": "Product 1"}, "teamId": "t1"}]
        mock_provider = MagicMock()
        mock_provider.list_products.return_value = products

        printed = []
        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch(
                "fluid_build.cli.datamesh_manager.cprint", side_effect=lambda t: printed.append(t)
            ),
        ):
            result = _cmd_list(args)

        assert result == 0
        assert any("p1" in str(l) for l in printed)

    def test_list_table_format_rich(self):
        args = _make_args(format="table")
        products = [{"info": {"id": "p1", "name": "Product 1", "status": "active"}, "teamId": "t1"}]
        mock_provider = MagicMock()
        mock_provider.list_products.return_value = products

        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch("fluid_build.cli.datamesh_manager.RICH_AVAILABLE", True),
            patch("fluid_build.cli.datamesh_manager.Console") as MockConsole,
            patch("fluid_build.cli.datamesh_manager.Table"),
        ):
            MockConsole.return_value = MagicMock()
            result = _cmd_list(args)
        assert result == 0

    def test_list_table_format_plain(self):
        args = _make_args(format="table")
        products = [{"info": {"id": "p1", "name": "Product 1"}, "teamId": "t1"}]
        mock_provider = MagicMock()
        mock_provider.list_products.return_value = products

        printed = []
        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch("fluid_build.cli.datamesh_manager.RICH_AVAILABLE", False),
            patch(
                "fluid_build.cli.datamesh_manager.cprint", side_effect=lambda t: printed.append(t)
            ),
        ):
            result = _cmd_list(args)

        assert result == 0
        joined = "\n".join(str(l) for l in printed)
        assert "p1" in joined

    def test_list_returns_1_on_provider_error(self):
        args = _make_args(format="table")
        mock_provider = MagicMock()
        mock_provider.list_products.side_effect = ProviderError("auth fail")

        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch("fluid_build.cli.datamesh_manager.console_error"),
        ):
            result = _cmd_list(args)
        assert result == 1


# ---------------------------------------------------------------------------
# Tests: _cmd_get()
# ---------------------------------------------------------------------------


class TestCmdGet:
    def test_get_prints_json(self):
        args = _make_args(product_id="prod-abc")
        product = {"id": "prod-abc", "name": "My Product"}
        mock_provider = MagicMock()
        mock_provider.verify.return_value = product

        printed = []
        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch(
                "fluid_build.cli.datamesh_manager.cprint", side_effect=lambda t: printed.append(t)
            ),
        ):
            result = _cmd_get(args)

        assert result == 0
        joined = "\n".join(str(l) for l in printed)
        assert "prod-abc" in joined

    def test_get_returns_1_on_provider_error(self):
        args = _make_args(product_id="bad-id")
        mock_provider = MagicMock()
        mock_provider.verify.side_effect = ProviderError("not found")

        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch("fluid_build.cli.datamesh_manager.console_error"),
        ):
            result = _cmd_get(args)
        assert result == 1


# ---------------------------------------------------------------------------
# Tests: _cmd_delete()
# ---------------------------------------------------------------------------


class TestCmdDelete:
    def test_delete_with_yes_flag_no_confirmation(self):
        args = _make_args(product_id="prod-del", yes=True)
        mock_provider = MagicMock()
        mock_provider.delete.return_value = True

        printed = []
        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch(
                "fluid_build.cli.datamesh_manager.cprint", side_effect=lambda t: printed.append(t)
            ),
        ):
            result = _cmd_delete(args)

        assert result == 0
        joined = "\n".join(str(l) for l in printed)
        assert "prod-del" in joined

    def test_delete_with_user_confirmation_y(self):
        args = _make_args(product_id="prod-del2", yes=False)
        mock_provider = MagicMock()
        mock_provider.delete.return_value = True

        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch("fluid_build.cli.datamesh_manager.cprint"),
            patch("builtins.input", return_value="y"),
        ):
            result = _cmd_delete(args)
        assert result == 0

    def test_delete_cancelled_on_n_input(self):
        args = _make_args(product_id="prod-del3", yes=False)
        mock_provider = MagicMock()

        printed = []
        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch(
                "fluid_build.cli.datamesh_manager.cprint", side_effect=lambda t: printed.append(t)
            ),
            patch("builtins.input", return_value="n"),
        ):
            result = _cmd_delete(args)

        assert result == 0
        joined = "\n".join(str(l) for l in printed)
        assert "Cancelled" in joined

    def test_delete_returns_1_when_delete_fails(self):
        args = _make_args(product_id="prod-fail", yes=True)
        mock_provider = MagicMock()
        mock_provider.delete.return_value = False

        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch("fluid_build.cli.datamesh_manager.console_error"),
        ):
            result = _cmd_delete(args)
        assert result == 1

    def test_delete_returns_1_on_provider_error(self):
        args = _make_args(product_id="prod-err", yes=True)
        mock_provider = MagicMock()
        mock_provider.delete.side_effect = ProviderError("cannot delete")

        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch("fluid_build.cli.datamesh_manager.console_error"),
        ):
            result = _cmd_delete(args)
        assert result == 1


# ---------------------------------------------------------------------------
# Tests: _cmd_teams()
# ---------------------------------------------------------------------------


class TestCmdTeams:
    def test_teams_json_format(self):
        args = _make_args(format="json")
        teams = [{"id": "t1", "name": "Team One"}]
        mock_provider = MagicMock()
        mock_provider.list_teams.return_value = teams

        printed = []
        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch(
                "fluid_build.cli.datamesh_manager.cprint", side_effect=lambda t: printed.append(t)
            ),
        ):
            result = _cmd_teams(args)

        assert result == 0
        joined = "\n".join(str(l) for l in printed)
        assert "t1" in joined

    def test_teams_table_rich(self):
        args = _make_args(format="table")
        teams = [{"id": "t2", "name": "Team Two"}]
        mock_provider = MagicMock()
        mock_provider.list_teams.return_value = teams

        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch("fluid_build.cli.datamesh_manager.RICH_AVAILABLE", True),
            patch("fluid_build.cli.datamesh_manager.Console") as MockConsole,
            patch("fluid_build.cli.datamesh_manager.Table"),
        ):
            MockConsole.return_value = MagicMock()
            result = _cmd_teams(args)
        assert result == 0

    def test_teams_table_plain(self):
        args = _make_args(format="table")
        teams = [{"id": "t3", "name": "Team Three"}]
        mock_provider = MagicMock()
        mock_provider.list_teams.return_value = teams

        printed = []
        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch("fluid_build.cli.datamesh_manager.RICH_AVAILABLE", False),
            patch(
                "fluid_build.cli.datamesh_manager.cprint", side_effect=lambda t: printed.append(t)
            ),
        ):
            result = _cmd_teams(args)

        assert result == 0
        joined = "\n".join(str(l) for l in printed)
        assert "t3" in joined

    def test_teams_returns_1_on_provider_error(self):
        args = _make_args(format="table")
        mock_provider = MagicMock()
        mock_provider.list_teams.side_effect = ProviderError("auth fail")

        with (
            patch("fluid_build.cli.datamesh_manager._make_provider", return_value=mock_provider),
            patch("fluid_build.cli.datamesh_manager.console_error"),
        ):
            result = _cmd_teams(args)
        assert result == 1
