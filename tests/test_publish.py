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

"""Tests for fluid_build.cli.publish."""

import argparse
import asyncio
import logging
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fluid_build.providers.catalogs.base import PublishResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(
    success=True,
    catalog_id="fluid-command-center",
    asset_id="test-asset",
    catalog_url="https://example.com/assets/test-asset",
    error=None,
    details=None,
):
    return PublishResult(
        success=success,
        catalog_id=catalog_id,
        asset_id=asset_id,
        catalog_url=catalog_url,
        error=error,
        details=details or {},
        timestamp=datetime(2024, 1, 1),
    )


def _make_args(**kwargs):
    defaults = {
        "contract_files": ["contract.fluid.yaml"],
        "catalog": "fluid-command-center",
        "list_catalogs": False,
        "dry_run": False,
        "verify_only": False,
        "force": False,
        "format": "text",
        "verbose": False,
        "quiet": False,
        "skip_health_check": False,
        "show_metrics": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# publish_contract
# ---------------------------------------------------------------------------


class TestPublishContract:
    """Tests for publish_contract async function."""

    def test_load_contract_failure_returns_error_result(self, tmp_path):
        contract_path = tmp_path / "bad.fluid.yaml"
        contract_path.write_text("invalid: yaml: [")

        from fluid_build.cli import publish as pub_mod

        with (
            patch.object(pub_mod, "load_contract", side_effect=ValueError("bad yaml")),
            patch.object(pub_mod, "FluidConfig"),
        ):
            result = _run(
                pub_mod.publish_contract(
                    contract_path=contract_path,
                    catalog_name="fluid-command-center",
                    config=MagicMock(),
                )
            )

        assert result.success is False
        assert "Failed to load contract" in result.error

    def test_catalog_not_configured_returns_error(self, tmp_path):
        from fluid_build.cli import publish as pub_mod

        contract_path = tmp_path / "c.fluid.yaml"
        contract_path.write_text("id: test")

        config = MagicMock()
        config.get_catalog_config.return_value = None

        with patch.object(pub_mod, "load_contract", return_value={"id": "test"}):
            result = _run(
                pub_mod.publish_contract(
                    contract_path=contract_path,
                    catalog_name="nonexistent",
                    config=config,
                )
            )

        assert result.success is False
        assert "not configured" in result.error

    def test_disabled_catalog_returns_error(self, tmp_path):
        from fluid_build.cli import publish as pub_mod

        contract_path = tmp_path / "c.fluid.yaml"
        contract_path.write_text("id: test")

        config = MagicMock()
        config.get_catalog_config.return_value = {"enabled": False}

        with patch.object(pub_mod, "load_contract", return_value={"id": "test"}):
            result = _run(
                pub_mod.publish_contract(
                    contract_path=contract_path,
                    catalog_name="my-catalog",
                    config=config,
                )
            )

        assert result.success is False
        assert "disabled" in result.error

    def test_provider_creation_failure_returns_error(self, tmp_path):
        from fluid_build.cli import publish as pub_mod

        contract_path = tmp_path / "c.fluid.yaml"
        contract_path.write_text("id: test")

        config = MagicMock()
        config.get_catalog_config.return_value = {"enabled": True}

        with (
            patch.object(pub_mod, "load_contract", return_value={"id": "test"}),
            patch.object(pub_mod, "get_catalog_provider", side_effect=RuntimeError("no driver")),
        ):
            result = _run(
                pub_mod.publish_contract(
                    contract_path=contract_path,
                    catalog_name="my-catalog",
                    config=config,
                )
            )

        assert result.success is False
        assert "Failed to create catalog provider" in result.error

    def test_verify_only_asset_exists(self, tmp_path):
        from fluid_build.cli import publish as pub_mod

        contract_path = tmp_path / "c.fluid.yaml"
        contract_path.write_text("id: test")

        asset = MagicMock()
        asset.id = "asset-123"
        provider = MagicMock()
        provider.map_contract_to_asset.return_value = asset
        provider.verify = AsyncMock(return_value=True)

        config = MagicMock()
        config.get_catalog_config.return_value = {"enabled": True}

        with (
            patch.object(pub_mod, "load_contract", return_value={"id": "test"}),
            patch.object(pub_mod, "get_catalog_provider", return_value=provider),
        ):
            result = _run(
                pub_mod.publish_contract(
                    contract_path=contract_path,
                    catalog_name="my-catalog",
                    config=config,
                    verify_only=True,
                )
            )

        assert result.success is True
        assert result.details.get("verified") is True

    def test_verify_only_asset_missing(self, tmp_path):
        from fluid_build.cli import publish as pub_mod

        contract_path = tmp_path / "c.fluid.yaml"
        contract_path.write_text("id: test")

        asset = MagicMock()
        asset.id = "asset-123"
        provider = MagicMock()
        provider.map_contract_to_asset.return_value = asset
        provider.verify = AsyncMock(return_value=False)

        config = MagicMock()
        config.get_catalog_config.return_value = {"enabled": True}

        with (
            patch.object(pub_mod, "load_contract", return_value={"id": "test"}),
            patch.object(pub_mod, "get_catalog_provider", return_value=provider),
        ):
            result = _run(
                pub_mod.publish_contract(
                    contract_path=contract_path,
                    catalog_name="my-catalog",
                    config=config,
                    verify_only=True,
                )
            )

        assert result.success is False
        assert "not found" in result.error

    def test_dry_run_valid_asset(self, tmp_path):
        from fluid_build.cli import publish as pub_mod

        contract_path = tmp_path / "c.fluid.yaml"
        contract_path.write_text("id: test")

        asset = MagicMock()
        asset.id = "asset-123"
        provider = MagicMock()
        provider.map_contract_to_asset.return_value = asset
        provider.validate_asset.return_value = (True, None)

        config = MagicMock()
        config.get_catalog_config.return_value = {"enabled": True}

        with (
            patch.object(pub_mod, "load_contract", return_value={"id": "test"}),
            patch.object(pub_mod, "get_catalog_provider", return_value=provider),
        ):
            result = _run(
                pub_mod.publish_contract(
                    contract_path=contract_path,
                    catalog_name="my-catalog",
                    config=config,
                    dry_run=True,
                )
            )

        assert result.success is True
        assert result.details.get("dry_run") is True

    def test_health_check_failure_returns_error(self, tmp_path):
        from fluid_build.cli import publish as pub_mod

        contract_path = tmp_path / "c.fluid.yaml"
        contract_path.write_text("id: test")

        asset = MagicMock()
        asset.id = "asset-123"
        provider = MagicMock()
        provider.map_contract_to_asset.return_value = asset
        provider.health_check = AsyncMock(return_value=False)

        config = MagicMock()
        config.get_catalog_config.return_value = {"enabled": True}

        with (
            patch.object(pub_mod, "load_contract", return_value={"id": "test"}),
            patch.object(pub_mod, "get_catalog_provider", return_value=provider),
        ):
            result = _run(
                pub_mod.publish_contract(
                    contract_path=contract_path,
                    catalog_name="my-catalog",
                    config=config,
                )
            )

        assert result.success is False
        assert "health check failed" in result.error

    def test_successful_publish(self, tmp_path):
        from fluid_build.cli import publish as pub_mod

        contract_path = tmp_path / "c.fluid.yaml"
        contract_path.write_text("id: test")

        asset = MagicMock()
        asset.id = "asset-123"
        expected = _make_result(success=True, asset_id="asset-123")
        provider = MagicMock()
        provider.map_contract_to_asset.return_value = asset
        provider.health_check = AsyncMock(return_value=True)
        provider.publish = AsyncMock(return_value=expected)

        config = MagicMock()
        config.get_catalog_config.return_value = {"enabled": True}

        with (
            patch.object(pub_mod, "load_contract", return_value={"id": "test"}),
            patch.object(pub_mod, "get_catalog_provider", return_value=provider),
        ):
            result = _run(
                pub_mod.publish_contract(
                    contract_path=contract_path,
                    catalog_name="my-catalog",
                    config=config,
                    skip_health_check=False,
                )
            )

        assert result.success is True

    def test_map_contract_failure_returns_error(self, tmp_path):
        from fluid_build.cli import publish as pub_mod

        contract_path = tmp_path / "c.fluid.yaml"
        contract_path.write_text("id: test")

        provider = MagicMock()
        provider.map_contract_to_asset.side_effect = KeyError("name")

        config = MagicMock()
        config.get_catalog_config.return_value = {"enabled": True}

        with (
            patch.object(pub_mod, "load_contract", return_value={"id": "test"}),
            patch.object(pub_mod, "get_catalog_provider", return_value=provider),
        ):
            result = _run(
                pub_mod.publish_contract(
                    contract_path=contract_path,
                    catalog_name="my-catalog",
                    config=config,
                )
            )

        assert result.success is False
        assert "Failed to map contract to asset" in result.error


# ---------------------------------------------------------------------------
# format_results
# ---------------------------------------------------------------------------


class TestFormatResults:
    """Tests for format_results."""

    def test_json_format(self):
        import json

        from fluid_build.cli.publish import format_results

        results = [_make_result()]
        output = format_results(results, format="json")
        parsed = json.loads(output)
        assert len(parsed) == 1
        assert parsed[0]["success"] is True
        assert parsed[0]["asset_id"] == "test-asset"

    def test_yaml_format(self):
        import yaml

        from fluid_build.cli.publish import format_results

        results = [_make_result()]
        output = format_results(results, format="yaml")
        parsed = yaml.safe_load(output)
        assert isinstance(parsed, list)
        assert parsed[0]["catalog_id"] == "fluid-command-center"

    def test_text_format_plain(self):
        from fluid_build.cli.publish import format_results

        results = [_make_result(), _make_result(success=False, error="oops")]
        output = format_results(results, format="text", console=None)
        assert "test-asset" in output
        assert "oops" in output

    def test_text_format_with_console(self):
        from fluid_build.cli.publish import format_results

        mock_console = MagicMock()
        results = [_make_result()]
        output = format_results(results, format="text", console=mock_console)
        mock_console.print.assert_called_once()
        assert output == ""

    def test_failed_result_shows_error_in_text(self):
        from fluid_build.cli.publish import format_results

        results = [_make_result(success=False, error="catalog unreachable", catalog_url=None)]
        output = format_results(results, format="text", console=None)
        assert "catalog unreachable" in output

    def test_multiple_results_json(self):
        import json

        from fluid_build.cli.publish import format_results

        results = [_make_result(asset_id="a1"), _make_result(asset_id="a2", success=False)]
        output = format_results(results, format="json")
        parsed = json.loads(output)
        assert len(parsed) == 2


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


class TestRun:
    """Tests for the synchronous run entry point."""

    def test_keyboard_interrupt_returns_130(self):
        from fluid_build.cli.publish import run

        args = _make_args()
        logger = logging.getLogger("test")

        with patch("fluid_build.cli.publish.asyncio.run", side_effect=KeyboardInterrupt()):
            code = run(args, logger)

        assert code == 130

    def test_exception_returns_1(self):
        from fluid_build.cli.publish import run

        args = _make_args()
        logger = logging.getLogger("test")

        with patch("fluid_build.cli.publish.asyncio.run", side_effect=RuntimeError("boom")):
            code = run(args, logger)

        assert code == 1

    def test_success_propagates_0(self):
        from fluid_build.cli.publish import run

        args = _make_args()
        logger = logging.getLogger("test")

        with patch("fluid_build.cli.publish.asyncio.run", return_value=0):
            code = run(args, logger)

        assert code == 0

    def test_verbose_traceback_on_exception(self):
        """Lines 511-513: verbose=True triggers traceback.print_exc on exception."""
        from fluid_build.cli.publish import run

        args = _make_args(verbose=True)
        logger = logging.getLogger("test")

        with (
            patch("fluid_build.cli.publish.asyncio.run", side_effect=RuntimeError("err")),
            patch("traceback.print_exc") as mock_tb,
        ):
            code = run(args, logger)

        assert code == 1
        mock_tb.assert_called_once()


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


class TestRegister:
    """Tests for the register() function."""

    def test_register_adds_publish_command(self):
        """register() adds a publish sub-command with required positional arg."""
        import argparse

        from fluid_build.cli.publish import register

        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        ns = parser.parse_args(["publish", "contract.fluid.yaml"])
        assert ns.contract_files == ["contract.fluid.yaml"]

    def test_register_dry_run_flag(self):
        """Lines 52-53 area: --dry-run flag parsed correctly."""
        import argparse

        from fluid_build.cli.publish import register

        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        ns = parser.parse_args(["publish", "c.yaml", "--dry-run"])
        assert ns.dry_run is True

    def test_register_catalog_flag(self):
        """--catalog flag uses provided value."""
        import argparse

        from fluid_build.cli.publish import register

        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        ns = parser.parse_args(["publish", "c.yaml", "--catalog", "my-cat"])
        assert ns.catalog == "my-cat"

    def test_register_list_catalogs_flag(self):
        """--list-catalogs flag sets list_catalogs=True."""
        import argparse

        from fluid_build.cli.publish import register

        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        ns = parser.parse_args(["publish", "c.yaml", "--list-catalogs"])
        assert ns.list_catalogs is True

    def test_register_format_choices(self):
        """--format accepts json and yaml values."""
        import argparse

        from fluid_build.cli.publish import register

        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        for fmt in ("json", "yaml", "text"):
            ns = parser.parse_args(["publish", "c.yaml", "--format", fmt])
            assert ns.format == fmt


# ---------------------------------------------------------------------------
# run_async – list-catalogs branch
# ---------------------------------------------------------------------------


class TestRunAsyncListCatalogs:
    """Lines 403-427: run_async() handles --list-catalogs."""

    def test_list_catalogs_plain_text_returns_0(self):
        """Lines 419-427: no-rich path for list-catalogs returns 0."""
        from fluid_build.cli import publish as pub_mod

        args = _make_args(list_catalogs=True)
        logger = logging.getLogger("test")

        catalog_cfg = {
            "fluid-command-center": {
                "endpoint": "https://catalog.example.com",
                "enabled": True,
                "auth": {"type": "api_key"},
            }
        }

        with (
            patch.object(pub_mod, "RICH_AVAILABLE", False),
            patch.object(pub_mod, "FluidConfig") as mock_cfg_cls,
            patch.object(pub_mod, "cprint") as mock_cprint,
        ):
            mock_cfg = MagicMock()
            mock_cfg.get_catalog_config.return_value = catalog_cfg
            mock_cfg_cls.return_value = mock_cfg

            loop = asyncio.new_event_loop()
            try:
                code = loop.run_until_complete(pub_mod.run_async(args, logger))
            finally:
                loop.close()

        assert code == 0
        mock_cprint.assert_called()

    def test_list_catalogs_rich_path_returns_0(self):
        """Lines 407-418: rich console path for list-catalogs."""
        from fluid_build.cli import publish as pub_mod

        args = _make_args(list_catalogs=True)
        logger = logging.getLogger("test")

        catalog_cfg = {
            "my-cat": {
                "endpoint": "https://my.cat",
                "enabled": False,
                "auth": {"type": "none"},
            }
        }

        mock_console = MagicMock()

        with (
            patch.object(pub_mod, "RICH_AVAILABLE", True),
            patch.object(pub_mod, "FluidConfig") as mock_cfg_cls,
            patch.object(pub_mod, "Console", return_value=mock_console),
        ):
            mock_cfg = MagicMock()
            mock_cfg.get_catalog_config.return_value = catalog_cfg
            mock_cfg_cls.return_value = mock_cfg

            loop = asyncio.new_event_loop()
            try:
                code = loop.run_until_complete(pub_mod.run_async(args, logger))
            finally:
                loop.close()

        assert code == 0
        mock_console.print.assert_called()


# ---------------------------------------------------------------------------
# run_async – publish flow (lines 294-498)
# ---------------------------------------------------------------------------


class TestRunAsyncPublishFlow:
    """Lines 429-498: glob expansion, path validation, publish loop, metrics, exit codes."""

    def test_invalid_path_returns_1(self, tmp_path):
        """Lines 444-448: non-existent contract files cause exit code 1."""
        from fluid_build.cli import publish as pub_mod

        args = _make_args(contract_files=["nonexistent_file.yaml"])
        logger = logging.getLogger("test")

        with (
            patch.object(pub_mod, "RICH_AVAILABLE", False),
            patch.object(pub_mod, "FluidConfig") as mock_cfg_cls,
        ):
            mock_cfg_cls.return_value = MagicMock()

            loop = asyncio.new_event_loop()
            try:
                code = loop.run_until_complete(pub_mod.run_async(args, logger))
            finally:
                loop.close()

        assert code == 1

    def test_all_success_returns_0(self, tmp_path):
        """Lines 492-498: all contracts succeed → exit 0."""
        from fluid_build.cli import publish as pub_mod

        contract_file = tmp_path / "c.fluid.yaml"
        contract_file.write_text("id: test")

        args = _make_args(contract_files=[str(contract_file)])
        logger = logging.getLogger("test")

        success_result = _make_result(success=True)

        with (
            patch.object(pub_mod, "RICH_AVAILABLE", False),
            patch.object(pub_mod, "FluidConfig") as mock_cfg_cls,
            patch.object(pub_mod, "publish_contract", return_value=success_result),
            patch.object(pub_mod, "cprint"),
        ):
            mock_cfg_cls.return_value = MagicMock()

            loop = asyncio.new_event_loop()
            try:
                code = loop.run_until_complete(pub_mod.run_async(args, logger))
            finally:
                loop.close()

        assert code == 0

    def test_all_failure_returns_1(self, tmp_path):
        """Lines 492-494: all contracts fail → exit 1."""
        from fluid_build.cli import publish as pub_mod

        contract_file = tmp_path / "c.fluid.yaml"
        contract_file.write_text("id: test")

        args = _make_args(contract_files=[str(contract_file)])
        logger = logging.getLogger("test")

        fail_result = _make_result(success=False, error="nope", catalog_url=None)

        with (
            patch.object(pub_mod, "RICH_AVAILABLE", False),
            patch.object(pub_mod, "FluidConfig") as mock_cfg_cls,
            patch.object(pub_mod, "publish_contract", return_value=fail_result),
            patch.object(pub_mod, "cprint"),
        ):
            mock_cfg_cls.return_value = MagicMock()

            loop = asyncio.new_event_loop()
            try:
                code = loop.run_until_complete(pub_mod.run_async(args, logger))
            finally:
                loop.close()

        assert code == 1

    def test_partial_success_returns_2(self, tmp_path):
        """Lines 495-496: some success, some failure → exit 2."""
        from fluid_build.cli import publish as pub_mod

        f1 = tmp_path / "c1.fluid.yaml"
        f2 = tmp_path / "c2.fluid.yaml"
        f1.write_text("id: a")
        f2.write_text("id: b")

        args = _make_args(contract_files=[str(f1), str(f2)])
        logger = logging.getLogger("test")

        results = [
            _make_result(success=True, asset_id="a"),
            _make_result(success=False, asset_id="b", error="err", catalog_url=None),
        ]
        result_iter = iter(results)

        async def fake_publish(**_kwargs):
            return next(result_iter)

        with (
            patch.object(pub_mod, "RICH_AVAILABLE", False),
            patch.object(pub_mod, "FluidConfig") as mock_cfg_cls,
            patch.object(pub_mod, "publish_contract", side_effect=fake_publish),
            patch.object(pub_mod, "cprint"),
        ):
            mock_cfg_cls.return_value = MagicMock()

            loop = asyncio.new_event_loop()
            try:
                code = loop.run_until_complete(pub_mod.run_async(args, logger))
            finally:
                loop.close()

        assert code == 2

    def test_show_metrics_plain_text(self, tmp_path):
        """Lines 472-490: show_metrics=True with no rich prints metrics."""
        from fluid_build.cli import publish as pub_mod

        contract_file = tmp_path / "c.fluid.yaml"
        contract_file.write_text("id: test")

        args = _make_args(contract_files=[str(contract_file)], show_metrics=True)
        logger = logging.getLogger("test")

        success_result = _make_result(success=True)

        mock_metrics = {
            "total_requests": 1,
            "success_rate": 100.0,
            "total_failures": 0,
        }
        mock_mc = MagicMock()
        mock_mc.get_summary.return_value = mock_metrics
        mock_mc.get_health_score.return_value = 1.0

        with (
            patch.object(pub_mod, "RICH_AVAILABLE", False),
            patch.object(pub_mod, "FluidConfig") as mock_cfg_cls,
            patch.object(pub_mod, "publish_contract", return_value=success_result),
            patch.object(pub_mod, "metrics_collector", mock_mc),
            patch.object(pub_mod, "cprint") as mock_cprint,
        ):
            mock_cfg_cls.return_value = MagicMock()

            loop = asyncio.new_event_loop()
            try:
                code = loop.run_until_complete(pub_mod.run_async(args, logger))
            finally:
                loop.close()

        assert code == 0
        # Verify metrics were printed
        calls = " ".join(str(c) for c in mock_cprint.call_args_list)
        assert "Metrics" in calls or "Total" in calls or "Success" in calls

    def test_publish_contract_verbose_logging(self, tmp_path):
        """Lines 212-213, 268-269: verbose=True triggers extra logger.info calls."""
        from fluid_build.cli import publish as pub_mod

        contract_path = tmp_path / "c.fluid.yaml"
        contract_path.write_text("id: test")

        asset = MagicMock()
        asset.id = "asset-xyz"
        asset.name = "Test Asset"
        provider = MagicMock()
        provider.map_contract_to_asset.return_value = asset
        provider.health_check = AsyncMock(return_value=True)
        provider.publish = AsyncMock(return_value=_make_result(success=True, asset_id="asset-xyz"))

        config = MagicMock()
        config.get_catalog_config.return_value = {"enabled": True}

        with (
            patch.object(pub_mod, "load_contract", return_value={"id": "test"}),
            patch.object(pub_mod, "get_catalog_provider", return_value=provider),
        ):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    pub_mod.publish_contract(
                        contract_path=contract_path,
                        catalog_name="my-catalog",
                        config=config,
                        verbose=True,
                    )
                )
            finally:
                loop.close()

        assert result.success is True

    def test_publish_skips_health_check_when_flag_set(self, tmp_path):
        """Lines 293-305: health check is skipped when skip_health_check=True."""
        from fluid_build.cli import publish as pub_mod

        contract_path = tmp_path / "c.fluid.yaml"
        contract_path.write_text("id: test")

        asset = MagicMock()
        asset.id = "asset-abc"
        provider = MagicMock()
        provider.map_contract_to_asset.return_value = asset
        # health_check should NOT be called
        provider.health_check = AsyncMock(return_value=False)
        provider.publish = AsyncMock(return_value=_make_result(success=True, asset_id="asset-abc"))

        config = MagicMock()
        config.get_catalog_config.return_value = {"enabled": True}

        with (
            patch.object(pub_mod, "load_contract", return_value={"id": "test"}),
            patch.object(pub_mod, "get_catalog_provider", return_value=provider),
        ):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    pub_mod.publish_contract(
                        contract_path=contract_path,
                        catalog_name="my-catalog",
                        config=config,
                        skip_health_check=True,
                    )
                )
            finally:
                loop.close()

        provider.health_check.assert_not_called()
        assert result.success is True

    def test_dry_run_invalid_asset_returns_error(self, tmp_path):
        """Lines 283-291: dry_run with invalid asset returns error result."""
        from fluid_build.cli import publish as pub_mod

        contract_path = tmp_path / "c.fluid.yaml"
        contract_path.write_text("id: test")

        asset = MagicMock()
        asset.id = "asset-bad"
        provider = MagicMock()
        provider.map_contract_to_asset.return_value = asset
        provider.validate_asset.return_value = (False, "missing required field")

        config = MagicMock()
        config.get_catalog_config.return_value = {"enabled": True}

        with (
            patch.object(pub_mod, "load_contract", return_value={"id": "test"}),
            patch.object(pub_mod, "get_catalog_provider", return_value=provider),
        ):
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(
                    pub_mod.publish_contract(
                        contract_path=contract_path,
                        catalog_name="my-catalog",
                        config=config,
                        dry_run=True,
                    )
                )
            finally:
                loop.close()

        assert result.success is False
        assert result.error == "missing required field"
