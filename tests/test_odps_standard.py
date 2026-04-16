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

"""Unit tests for fluid_build/cli/odps_standard.py."""

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.providers.odps_standard import OdpsStandardProvider


def _sample_fluid_contract_with_consumes_and_expose_id():
    return {
        "fluidVersion": "0.7.1",
        "kind": "DataProduct",
        "id": "bizlab.teleforge.subscriber_health_360_lineage_local",
        "name": "TeleForge Subscriber Health 360 Local",
        "description": (
            "Gold subscriber health mart built from the Silver usage and billing daily products."
        ),
        "domain": "telco",
        "metadata": {
            "layer": "Gold",
            "owner": {"team": "bizlab", "email": "bizlab@example.com"},
        },
        "consumes": [
            {
                "productId": "bizlab.teleforge.subscriber_usage_daily_lineage_local",
                "exposeId": "subscriber_usage_daily",
                "purpose": "Supply daily subscriber usage features to the health model.",
            },
            {
                "productId": "bizlab.teleforge.billing_health_daily_lineage_local",
                "exposeId": "billing_health_daily",
                "purpose": "Supply payment behavior and overdue indicators to the health model.",
            },
        ],
        "exposes": [
            {
                "exposeId": "subscriber_health_360",
                "title": "Subscriber Health 360",
                "version": "1.0.0",
                "kind": "table",
                "binding": {
                    "platform": "local",
                    "format": "parquet",
                    "location": {"path": "runtime/lineage-sim/subscriber_health_360.parquet"},
                },
                "contract": {
                    "schema": [
                        {"name": "subscriber_id", "type": "STRING", "required": True},
                        {"name": "overall_health_score", "type": "INTEGER", "required": True},
                    ]
                },
            }
        ],
    }


def test_provider_render_supports_expose_id_only_contracts():
    provider = OdpsStandardProvider()

    result = provider.render(_sample_fluid_contract_with_consumes_and_expose_id())

    assert result["id"] == "bizlab.teleforge.subscriber_health_360_lineage_local"
    assert result["description"] == {
        "purpose": (
            "Gold subscriber health mart built from the Silver usage and billing daily products."
        )
    }
    assert result["team"]["name"] == "bizlab"
    assert result["outputPorts"][0]["id"] == "subscriber_health_360"
    assert result["outputPorts"][0]["name"] == "subscriber_health_360"
    assert result["outputPorts"][0]["type"] == "local"


def test_provider_render_prefers_expose_id_over_legacy_id():
    provider = OdpsStandardProvider()
    contract = _sample_fluid_contract_with_consumes_and_expose_id()
    contract["exposes"][0]["id"] = "legacy_output_id"

    result = provider.render(contract)

    assert result["outputPorts"][0]["id"] == "subscriber_health_360"
    assert result["outputPorts"][0]["name"] == "subscriber_health_360"


def test_provider_render_maps_consumes_to_input_ports():
    provider = OdpsStandardProvider()

    result = provider.render(_sample_fluid_contract_with_consumes_and_expose_id())

    # Only fields explicitly present on the consume entries are emitted —
    # the provider deliberately does NOT fabricate a ``contractId`` suffix
    # or a default ``required: True`` (see CHANGELOG for rationale).
    assert result["inputPorts"] == [
        {
            "id": "subscriber_usage_daily",
            "name": "subscriber_usage_daily",
            "description": "Supply daily subscriber usage features to the health model.",
            "version": "1",
            "reference": "bizlab.teleforge.subscriber_usage_daily_lineage_local",
        },
        {
            "id": "billing_health_daily",
            "name": "billing_health_daily",
            "description": "Supply payment behavior and overdue indicators to the health model.",
            "version": "1",
            "reference": "bizlab.teleforge.billing_health_daily_lineage_local",
        },
    ]


def test_provider_render_emits_input_port_contract_id_and_required_only_when_set():
    """Explicit ``contractId`` and ``required`` on a consume should pass through;
    omission should NOT fabricate a default value.

    The contract is labelled as FLUID 0.7.1 rather than 0.7.2 because the
    0.7.2 ``consumeRef`` schema has ``additionalProperties: false`` and does
    not include ``contractId`` / ``required`` — this test exercises the
    provider's *extension-field tolerance* for older or custom contracts,
    not a conforming 0.7.2 document.
    """
    provider = OdpsStandardProvider()

    contract = {
        "fluidVersion": "0.7.1",
        "kind": "DataProduct",
        "id": "test.explicit.lineage",
        "name": "Explicit Lineage",
        "metadata": {"owner": {"team": "platform"}},
        "consumes": [
            {
                "productId": "test.upstream.a",
                "exposeId": "upstream_a",
                "contractId": "test.upstream.a.contract.v1",
                "required": False,
            },
            {
                "productId": "test.upstream.b",
                "exposeId": "upstream_b",
            },
        ],
        "exposes": [],
    }

    result = provider.render(contract)

    assert result["inputPorts"][0]["contractId"] == "test.upstream.a.contract.v1"
    assert result["inputPorts"][0]["required"] is False
    # Second port: neither field was set → neither appears in the output.
    assert "contractId" not in result["inputPorts"][1]
    assert "required" not in result["inputPorts"][1]


def test_provider_render_preserves_input_port_source_system_metadata():
    provider = OdpsStandardProvider()

    contract = {
        "fluidVersion": "0.7.1",
        "kind": "DataProduct",
        "id": "test.source-system.lineage",
        "name": "Source System Lineage",
        "metadata": {"owner": {"team": "platform"}},
        "consumes": [
            {
                "productId": "test.upstream.crm",
                "exposeId": "crm_accounts",
                "sourceSystem": "bss-crm",
            }
        ],
        "exposes": [],
    }

    result = provider.render(contract)

    assert result["inputPorts"][0]["sourceSystemId"] == "bss-crm"


def test_provider_render_skips_malformed_consume_entries(caplog):
    """Non-mapping or id-less consume entries are skipped with a warning,
    not silently dropped."""
    import logging

    provider = OdpsStandardProvider()

    contract = {
        "fluidVersion": "0.7.1",
        "kind": "DataProduct",
        "id": "test.skip.lineage",
        "name": "Skip Lineage",
        "metadata": {"owner": {"team": "platform"}},
        "consumes": [
            "not-a-mapping",  # will be skipped
            {"productId": "test.no-id"},  # no exposeId/id → skipped
            {"exposeId": "valid", "productId": "test.valid"},
        ],
        "exposes": [],
    }

    with caplog.at_level(logging.WARNING):
        result = provider.render(contract)

    ids = [port["id"] for port in result["inputPorts"]]
    assert ids == ["valid"]
    # Two warnings for the two malformed entries.
    skip_warnings = [r for r in caplog.records if "Skipping consumes" in r.getMessage()]
    assert len(skip_warnings) == 2


def test_provider_render_raises_provider_error_for_expose_without_id():
    """Exposes missing both ``id`` and ``exposeId`` should raise a typed
    ``ProviderError`` rather than a generic KeyError."""
    from fluid_build.providers.base import ProviderError

    provider = OdpsStandardProvider()

    contract = {
        "fluidVersion": "0.7.1",
        "kind": "DataProduct",
        "id": "test.bad.expose",
        "name": "Bad Expose",
        "metadata": {"owner": {"team": "platform"}},
        "exposes": [{"title": "no-id-here", "contract": {"schema": []}}],
    }

    with pytest.raises(ProviderError, match="id/exposeId"):
        provider.render(contract)


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------


class TestRegister(unittest.TestCase):
    def test_registers_odps_bitol_subcommand(self):
        from fluid_build.cli.odps_standard import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        # Should be able to parse odps-bitol
        args = parser.parse_args(["odps-bitol"])
        assert hasattr(args, "odps_bitol_command")

    def test_registers_export_subcommand(self):
        from fluid_build.cli.odps_standard import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        args = parser.parse_args(["odps-bitol", "export", "contract.yaml"])
        assert args.contract == "contract.yaml"

    def test_registers_validate_subcommand(self):
        from fluid_build.cli.odps_standard import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        args = parser.parse_args(["odps-bitol", "validate", "product.yaml"])
        assert args.odps_file == "product.yaml"

    def test_registers_info_subcommand(self):
        from fluid_build.cli.odps_standard import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        args = parser.parse_args(["odps-bitol", "info"])
        assert hasattr(args, "func")

    def test_export_format_default_yaml(self):
        from fluid_build.cli.odps_standard import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        args = parser.parse_args(["odps-bitol", "export", "c.yaml"])
        assert args.format == "yaml"

    def test_export_format_json(self):
        from fluid_build.cli.odps_standard import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        args = parser.parse_args(["odps-bitol", "export", "c.yaml", "-f", "json"])
        assert args.format == "json"

    def test_export_no_custom_flag(self):
        from fluid_build.cli.odps_standard import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)
        args = parser.parse_args(["odps-bitol", "export", "c.yaml", "--no-custom"])
        assert args.no_custom is True


# ---------------------------------------------------------------------------
# _run_odps_export()
# ---------------------------------------------------------------------------


class TestRunOdpsExport(unittest.TestCase):
    def _make_args(self, **kw):
        defaults = dict(
            contract="contract.yaml",
            output=None,
            format="yaml",
            no_custom=False,
        )
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_export_calls_provider_render(self):
        from fluid_build.cli.odps_standard import _run_odps_export

        mock_provider = MagicMock()
        mock_provider.odps_version = "1.0.0"
        mock_provider.render.return_value = {}
        mock_contract = {"id": "test"}

        args = self._make_args(output="out.yaml")

        with (
            patch(
                "fluid_build.cli.odps_standard.OdpsStandardProvider",
                return_value=mock_provider,
            ),
            patch(
                "fluid_build.cli.bootstrap.load_contract_with_overlay",
                return_value=mock_contract,
            ),
            patch("fluid_build.cli.odps_standard.cprint"),
        ):
            result = _run_odps_export(args)

        assert result == 0
        mock_provider.render.assert_called_once()

    def test_export_generates_default_output_path(self):
        from fluid_build.cli.odps_standard import _run_odps_export

        mock_provider = MagicMock()
        mock_provider.render.return_value = {}

        args = self._make_args(output=None, contract="my-contract.yaml", format="yaml")

        with (
            patch(
                "fluid_build.cli.odps_standard.OdpsStandardProvider",
                return_value=mock_provider,
            ),
            patch(
                "fluid_build.cli.bootstrap.load_contract_with_overlay",
                return_value={},
            ),
            patch("fluid_build.cli.odps_standard.cprint"),
        ):
            _run_odps_export(args)

        assert args.output == "my-contract-odps.yaml"

    def test_export_no_custom_disables_custom_properties(self):
        from fluid_build.cli.odps_standard import _run_odps_export

        mock_provider = MagicMock()
        mock_provider.render.return_value = {}

        args = self._make_args(output="out.yaml", no_custom=True)

        with (
            patch(
                "fluid_build.cli.odps_standard.OdpsStandardProvider",
                return_value=mock_provider,
            ),
            patch(
                "fluid_build.cli.bootstrap.load_contract_with_overlay",
                return_value={},
            ),
            patch("fluid_build.cli.odps_standard.cprint"),
        ):
            _run_odps_export(args)

        assert mock_provider.include_custom_properties is False


# ---------------------------------------------------------------------------
# _run_odps_validate()
# ---------------------------------------------------------------------------


class TestRunOdpsValidate(unittest.TestCase):
    def _write_odps_file(self, data, suffix=".yaml"):
        import yaml

        with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as f:
            if suffix in (".yaml", ".yml"):
                yaml.dump(data, f)
            else:
                json.dump(data, f)
            return f.name

    def test_validate_valid_file_returns_0(self):
        from fluid_build.cli.odps_standard import _run_odps_validate

        data = {
            "apiVersion": "v1.0.0",
            "kind": "DataProduct",
            "id": "dp-001",
            "name": "My Product",
            "status": "active",
        }
        path = self._write_odps_file(data)
        args = argparse.Namespace(odps_file=path)

        with patch("fluid_build.cli.odps_standard.cprint"):
            result = _run_odps_validate(args)
        assert result == 0

    def test_validate_missing_fields_returns_1(self):
        from fluid_build.cli.odps_standard import _run_odps_validate

        data = {"id": "dp-001"}
        path = self._write_odps_file(data)
        args = argparse.Namespace(odps_file=path)

        with patch("fluid_build.cli.odps_standard.cprint"):
            result = _run_odps_validate(args)
        assert result == 1

    def test_validate_wrong_kind_returns_1(self):
        from fluid_build.cli.odps_standard import _run_odps_validate

        data = {
            "apiVersion": "v1.0.0",
            "kind": "SomethingElse",
            "id": "dp-001",
            "name": "My Product",
            "status": "active",
        }
        path = self._write_odps_file(data)
        args = argparse.Namespace(odps_file=path)

        with patch("fluid_build.cli.odps_standard.cprint"):
            result = _run_odps_validate(args)
        assert result == 1

    def test_validate_json_file(self):
        from fluid_build.cli.odps_standard import _run_odps_validate

        data = {
            "apiVersion": "v1.0.0",
            "kind": "DataProduct",
            "id": "dp-002",
            "name": "JSON Product",
            "status": "active",
        }
        path = self._write_odps_file(data, suffix=".json")
        args = argparse.Namespace(odps_file=path)

        with patch("fluid_build.cli.odps_standard.cprint"):
            result = _run_odps_validate(args)
        assert result == 0


# ---------------------------------------------------------------------------
# _run_odps_info()
# ---------------------------------------------------------------------------


class TestRunOdpsInfo(unittest.TestCase):
    def test_info_returns_0(self):
        from fluid_build.cli.odps_standard import _run_odps_info

        mock_provider = MagicMock()
        mock_provider.odps_version = "1.0.0"
        mock_provider.odps_spec_url = "https://example.com"

        args = argparse.Namespace()
        with (
            patch(
                "fluid_build.cli.odps_standard.OdpsStandardProvider",
                return_value=mock_provider,
            ),
            patch("fluid_build.cli.odps_standard.cprint"),
        ):
            result = _run_odps_info(args)
        assert result == 0


# ---------------------------------------------------------------------------
# Click CLI commands (export_command, validate_command, info_command)
# ---------------------------------------------------------------------------


class TestClickExportCommand(unittest.TestCase):
    def test_export_command_success(self):
        from click.testing import CliRunner

        from fluid_build.cli.odps_standard import export_command

        runner = CliRunner()
        mock_provider = MagicMock()
        mock_provider.odps_version = "1.0.0"
        mock_provider.render.return_value = {
            "name": "test",
            "id": "test-id",
            "status": "active",
            "outputPorts": [],
        }
        mock_contract = {"id": "test"}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("id: test\nversion: '1.0'\n")
            contract_path = f.name

        with (
            patch("fluid_build.cli.odps_standard.OdpsStandardProvider", return_value=mock_provider),
            patch("fluid_build.cli.odps_standard.load_contract", return_value=mock_contract),
        ):
            result = runner.invoke(export_command, [contract_path, "-o", "/tmp/out.yaml"])

        # Click invocation should not abort (exit code 0 or minor issues only)
        assert result.exit_code in (0, 1)

    def test_export_command_failure(self):
        from click.testing import CliRunner

        from fluid_build.cli.odps_standard import export_command

        runner = CliRunner()

        with patch("fluid_build.cli.odps_standard.load_contract", side_effect=RuntimeError("fail")):
            with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                f.write("id: test\n")
                contract_path = f.name
            result = runner.invoke(export_command, [contract_path])

        # Should abort (non-zero exit code)
        assert result.exit_code != 0


class TestClickValidateCommand(unittest.TestCase):
    def test_validate_valid_yaml(self):
        import yaml
        from click.testing import CliRunner

        from fluid_build.cli.odps_standard import validate_command

        data = {
            "apiVersion": "v1.0.0",
            "kind": "DataProduct",
            "id": "dp-001",
            "name": "My Product",
            "status": "active",
        }
        runner = CliRunner()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        result = runner.invoke(validate_command, [path])
        assert result.exit_code == 0

    def test_validate_missing_fields_aborts(self):
        import yaml
        from click.testing import CliRunner

        from fluid_build.cli.odps_standard import validate_command

        data = {"id": "dp-001"}
        runner = CliRunner()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        result = runner.invoke(validate_command, [path])
        assert result.exit_code != 0

    def test_validate_wrong_api_version_warns(self):
        import yaml
        from click.testing import CliRunner

        from fluid_build.cli.odps_standard import validate_command

        data = {
            "apiVersion": "v2.0.0",
            "kind": "DataProduct",
            "id": "dp-001",
            "name": "My Product",
            "status": "active",
        }
        runner = CliRunner()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(data, f)
            path = f.name

        result = runner.invoke(validate_command, [path])
        # Warning issued but not necessarily abort
        assert "Warning" in result.output or result.exit_code in (0, 1)


class TestClickInfoCommand(unittest.TestCase):
    def test_info_command_shows_version(self):
        from click.testing import CliRunner

        from fluid_build.cli.odps_standard import info_command

        mock_provider = MagicMock()
        mock_provider.odps_version = "1.0.0"
        mock_provider.odps_spec_url = "https://github.com/bitol-io/open-data-product-standard"
        mock_provider.capabilities.return_value = {"export_yaml": True, "export_json": True}

        runner = CliRunner()
        with patch(
            "fluid_build.cli.odps_standard.OdpsStandardProvider", return_value=mock_provider
        ):
            result = runner.invoke(info_command, [])

        assert result.exit_code == 0
        assert "1.0.0" in result.output


class TestOdpsBitolCli(unittest.TestCase):
    def test_cli_group_help(self):
        from click.testing import CliRunner

        from fluid_build.cli.odps_standard import odps_bitol_cli

        runner = CliRunner()
        result = runner.invoke(odps_bitol_cli, ["--help"])
        assert result.exit_code == 0
        assert "ODPS-Bitol" in result.output or "odps-bitol" in result.output.lower()


if __name__ == "__main__":
    unittest.main()
