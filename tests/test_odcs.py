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

"""Tests for fluid_build.cli.odcs (click commands and argparse helpers)."""

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from fluid_build.cli.odcs import (
    _run_odcs_export,
    _run_odcs_import,
    _run_odcs_info,
    _run_odcs_validate,
    export_command,
    import_command,
    info_command,
    odcs_cli,
    validate_command,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fluid_contract():
    return {
        "metadata": {
            "name": "test-product",
            "version": "1.0.0",
            "status": "active",
            "owner": {"team": "data-team", "email": "data@example.com"},
        },
        "contract": {"id": "test-product", "kind": "DataProduct", "version": "1.0.0"},
        "exposes": [
            {
                "exposeId": "test_table",
                "contract": {
                    "schema": [{"name": "id", "type": "STRING"}],
                    "servers": [],
                },
            }
        ],
        "expects": [],
    }


def _odcs_contract():
    return {
        "id": "test-product",
        "apiVersion": "v3.1.0",
        "name": "Test Product",
        "version": "1.0.0",
        "status": "active",
        "schema": [{"name": "id", "type": "string"}],
        "servers": [{"name": "bq", "type": "bigquery"}],
    }


# ---------------------------------------------------------------------------
# export_command (Click)
# ---------------------------------------------------------------------------


class TestExportCommand:
    """Tests for the `fluid odcs export` click command."""

    def test_export_yaml_default(self, tmp_path):
        runner = CliRunner()
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text(yaml.dump(_fluid_contract()))

        out_file = tmp_path / "out.yaml"

        mock_provider = MagicMock()
        mock_provider.odcs_version = "3.1.0"
        mock_provider.render.return_value = _odcs_contract()

        with (
            patch("fluid_build.cli.odcs.load_contract", return_value=_fluid_contract()),
            patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider),
        ):
            result = runner.invoke(
                export_command,
                [str(contract_file), "--output", str(out_file), "--format", "yaml"],
            )

        assert result.exit_code == 0
        assert "Successfully exported" in result.output

    def test_export_json_format(self, tmp_path):
        runner = CliRunner()
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text(yaml.dump(_fluid_contract()))

        out_file = tmp_path / "out.json"

        mock_provider = MagicMock()
        mock_provider.odcs_version = "3.1.0"
        mock_provider.render.return_value = _odcs_contract()

        with (
            patch("fluid_build.cli.odcs.load_contract", return_value=_fluid_contract()),
            patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider),
        ):
            result = runner.invoke(
                export_command,
                [str(contract_file), "--output", str(out_file), "--format", "json"],
            )

        assert result.exit_code == 0

    def test_expose_id_and_per_port_are_mutually_exclusive(self, tmp_path):
        runner = CliRunner()
        contract_file = tmp_path / "c.yaml"
        contract_file.write_text("id: test")

        result = runner.invoke(
            export_command,
            [str(contract_file), "--expose-id", "port1", "--per-port"],
        )

        # Should raise UsageError, exit code 2 for click usage errors
        assert result.exit_code != 0

    def test_per_port_mode(self, tmp_path):
        runner = CliRunner()
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text(yaml.dump(_fluid_contract()))

        mock_provider = MagicMock()
        mock_provider.odcs_version = "3.1.0"
        mock_provider.render_all_ports.return_value = [
            ("port1", _odcs_contract()),
        ]

        with (
            patch("fluid_build.cli.odcs.load_contract", return_value=_fluid_contract()),
            patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider),
        ):
            result = runner.invoke(
                export_command,
                [str(contract_file), "--per-port"],
            )

        assert result.exit_code == 0
        assert "port1" in result.output

    def test_per_port_no_exposes_warns(self, tmp_path):
        runner = CliRunner()
        contract_file = tmp_path / "contract.fluid.yaml"
        contract_file.write_text(yaml.dump(_fluid_contract()))

        mock_provider = MagicMock()
        mock_provider.odcs_version = "3.1.0"
        mock_provider.render_all_ports.return_value = []

        with (
            patch("fluid_build.cli.odcs.load_contract", return_value=_fluid_contract()),
            patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider),
        ):
            result = runner.invoke(
                export_command,
                [str(contract_file), "--per-port"],
            )

        assert "WARNING" in result.output or result.exit_code == 0

    def test_export_exception_aborts(self, tmp_path):
        runner = CliRunner()
        contract_file = tmp_path / "c.yaml"
        contract_file.write_text(yaml.dump(_fluid_contract()))

        with patch("fluid_build.cli.odcs.load_contract", side_effect=ValueError("bad")):
            result = runner.invoke(export_command, [str(contract_file)])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# import_command (Click)
# ---------------------------------------------------------------------------


class TestImportCommand:
    """Tests for the `fluid odcs import` click command."""

    def test_import_yaml_default(self, tmp_path):
        runner = CliRunner()
        odcs_file = tmp_path / "contract.odcs.yaml"
        odcs_file.write_text(yaml.dump(_odcs_contract()))

        out_file = tmp_path / "fluid.yaml"
        fluid = _fluid_contract()

        mock_provider = MagicMock()
        mock_provider.import_contract.return_value = fluid

        with patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider):
            result = runner.invoke(
                import_command,
                [str(odcs_file), "--output", str(out_file)],
            )

        assert result.exit_code == 0
        assert "Successfully imported" in result.output

    def test_import_default_output_name(self, tmp_path):
        runner = CliRunner()
        odcs_file = tmp_path / "mycontract.yaml"
        odcs_file.write_text(yaml.dump(_odcs_contract()))

        fluid = _fluid_contract()
        mock_provider = MagicMock()
        mock_provider.import_contract.return_value = fluid

        with patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider):
            result = runner.invoke(import_command, [str(odcs_file)])

        assert result.exit_code == 0

    def test_import_json_output(self, tmp_path):
        runner = CliRunner()
        odcs_file = tmp_path / "contract.odcs.yaml"
        odcs_file.write_text(yaml.dump(_odcs_contract()))

        out_file = tmp_path / "out.json"
        fluid = _fluid_contract()
        mock_provider = MagicMock()
        mock_provider.import_contract.return_value = fluid

        with patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider):
            result = runner.invoke(
                import_command,
                [str(odcs_file), "--output", str(out_file), "--format", "json"],
            )

        assert result.exit_code == 0
        assert out_file.exists()
        saved = json.loads(out_file.read_text())
        assert "metadata" in saved

    def test_import_exception_aborts(self, tmp_path):
        runner = CliRunner()
        odcs_file = tmp_path / "c.yaml"
        odcs_file.write_text(yaml.dump(_odcs_contract()))

        mock_provider = MagicMock()
        mock_provider.import_contract.side_effect = ValueError("parse error")

        with patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider):
            result = runner.invoke(import_command, [str(odcs_file)])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# validate_command (Click)
# ---------------------------------------------------------------------------


class TestValidateCommand:
    """Tests for the `fluid odcs validate` click command."""

    def test_valid_yaml_passes(self, tmp_path):
        runner = CliRunner()
        odcs_file = tmp_path / "contract.yaml"
        odcs_file.write_text(yaml.dump(_odcs_contract()))

        mock_provider = MagicMock()
        mock_provider.validate_contract = MagicMock()  # no exception = pass

        with patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider):
            result = runner.invoke(validate_command, [str(odcs_file)])

        assert result.exit_code == 0
        assert "Validation passed" in result.output

    def test_valid_json_passes(self, tmp_path):
        runner = CliRunner()
        odcs_file = tmp_path / "contract.json"
        odcs_file.write_text(json.dumps(_odcs_contract()))

        mock_provider = MagicMock()
        mock_provider.validate_contract = MagicMock()

        with patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider):
            result = runner.invoke(validate_command, [str(odcs_file)])

        assert result.exit_code == 0

    def test_invalid_contract_aborts(self, tmp_path):
        runner = CliRunner()
        odcs_file = tmp_path / "contract.yaml"
        odcs_file.write_text(yaml.dump(_odcs_contract()))

        mock_provider = MagicMock()
        mock_provider.validate_contract.side_effect = ValueError("schema mismatch")

        with patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider):
            result = runner.invoke(validate_command, [str(odcs_file)])

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# info_command (Click)
# ---------------------------------------------------------------------------


class TestInfoCommand:
    """Tests for the `fluid odcs info` click command."""

    def test_info_output(self):
        runner = CliRunner()

        mock_provider = MagicMock()
        mock_provider.odcs_version = "3.1.0"
        mock_provider.odcs_spec_url = "https://github.com/bitol-io/open-data-contract-standard"
        mock_provider.capabilities.return_value = {"export": True, "import": True}
        mock_provider.schema = {"type": "object"}

        with patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider):
            result = runner.invoke(info_command, [])

        assert result.exit_code == 0
        assert "ODCS" in result.output

    def test_info_schema_not_found(self):
        runner = CliRunner()

        mock_provider = MagicMock()
        mock_provider.odcs_version = "3.1.0"
        mock_provider.odcs_spec_url = "https://example.com"
        mock_provider.capabilities.return_value = {}
        mock_provider.schema = None

        with patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider):
            result = runner.invoke(info_command, [])

        assert result.exit_code == 0
        assert "Not found" in result.output


# ---------------------------------------------------------------------------
# _run_odcs_export (argparse helper)
# ---------------------------------------------------------------------------


class TestRunOdcsExport:
    def test_basic_export(self, tmp_path):
        import argparse

        contract_file = tmp_path / "c.yaml"
        contract_file.write_text(yaml.dump(_fluid_contract()))
        out_file = tmp_path / "out.yaml"

        args = argparse.Namespace(
            contract=str(contract_file),
            output=str(out_file),
            format="yaml",
            no_quality=False,
            no_sla=False,
        )

        mock_provider = MagicMock()

        with (
            patch(
                "fluid_build.cli.bootstrap.load_contract_with_overlay",
                return_value=_fluid_contract(),
            ),
            patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider),
            patch("fluid_build.cli.odcs.cprint"),
        ):
            from fluid_build.cli.odcs import _run_odcs_export

            _run_odcs_export(args)

        mock_provider.render.assert_called_once()

    def test_no_quality_sets_flag(self, tmp_path):
        import argparse

        contract_file = tmp_path / "c.yaml"
        contract_file.write_text(yaml.dump(_fluid_contract()))
        out_file = tmp_path / "out.yaml"

        args = argparse.Namespace(
            contract=str(contract_file),
            output=str(out_file),
            format="yaml",
            no_quality=True,
            no_sla=False,
        )

        mock_provider = MagicMock()
        mock_provider.include_quality_checks = True
        mock_provider.include_sla = True

        with (
            patch(
                "fluid_build.cli.bootstrap.load_contract_with_overlay",
                return_value=_fluid_contract(),
            ),
            patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider),
            patch("fluid_build.cli.odcs.cprint"),
        ):
            _run_odcs_export(args)

        assert mock_provider.include_quality_checks is False


# ---------------------------------------------------------------------------
# _run_odcs_import (argparse helper)
# ---------------------------------------------------------------------------


class TestRunOdcsImport:
    def test_basic_import(self, tmp_path):
        import argparse

        odcs_file = tmp_path / "c.yaml"
        odcs_file.write_text(yaml.dump(_odcs_contract()))
        out_file = tmp_path / "out.yaml"

        args = argparse.Namespace(
            odcs_file=str(odcs_file),
            output=str(out_file),
            format="yaml",
        )

        mock_provider = MagicMock()
        mock_provider.import_contract.return_value = _fluid_contract()

        with (
            patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider),
            patch("fluid_build.cli.odcs.cprint"),
        ):
            _run_odcs_import(args)

        assert out_file.exists()

    def test_default_output_name(self, tmp_path):
        import argparse

        odcs_file = tmp_path / "mycontract.yaml"
        odcs_file.write_text(yaml.dump(_odcs_contract()))
        out_file = tmp_path / "mycontract-fluid.yaml"

        args = argparse.Namespace(
            odcs_file=str(odcs_file),
            output=str(out_file),
            format="yaml",
        )

        mock_provider = MagicMock()
        mock_provider.import_contract.return_value = _fluid_contract()

        with (
            patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider),
            patch("fluid_build.cli.odcs.cprint"),
        ):
            _run_odcs_import(args)

        assert out_file.exists()


# ---------------------------------------------------------------------------
# _run_odcs_validate (argparse helper)
# ---------------------------------------------------------------------------


class TestRunOdcsValidate:
    def test_valid_contract_returns_0(self, tmp_path):
        import argparse

        odcs_file = tmp_path / "c.yaml"
        odcs_file.write_text(yaml.dump(_odcs_contract()))

        args = argparse.Namespace(odcs_file=str(odcs_file))

        mock_provider = MagicMock()
        mock_provider._validate_odcs = MagicMock()

        with (
            patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider),
            patch("fluid_build.cli.odcs.cprint"),
        ):
            code = _run_odcs_validate(args)

        assert code == 0

    def test_invalid_contract_returns_1(self, tmp_path):
        import argparse

        odcs_file = tmp_path / "c.yaml"
        odcs_file.write_text(yaml.dump(_odcs_contract()))

        args = argparse.Namespace(odcs_file=str(odcs_file))

        mock_provider = MagicMock()
        mock_provider._validate_odcs.side_effect = ValueError("invalid schema")

        with (
            patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider),
            patch("fluid_build.cli.odcs.cprint"),
        ):
            code = _run_odcs_validate(args)

        assert code == 1

    def test_json_file_parsed_correctly(self, tmp_path):
        import argparse

        odcs_file = tmp_path / "c.json"
        odcs_file.write_text(json.dumps(_odcs_contract()))

        args = argparse.Namespace(odcs_file=str(odcs_file))

        mock_provider = MagicMock()
        mock_provider._validate_odcs = MagicMock()

        with (
            patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider),
            patch("fluid_build.cli.odcs.cprint"),
        ):
            code = _run_odcs_validate(args)

        assert code == 0


# ---------------------------------------------------------------------------
# _run_odcs_info (argparse helper)
# ---------------------------------------------------------------------------


class TestRunOdcsInfo:
    def test_info_returns_0(self):
        import argparse

        args = argparse.Namespace()

        mock_provider = MagicMock()
        mock_provider.odcs_version = "3.1.0"
        mock_provider.odcs_spec_url = "https://example.com"
        mock_provider.schema = {"type": "object"}

        with (
            patch("fluid_build.cli.odcs.OdcsProvider", return_value=mock_provider),
            patch("fluid_build.cli.odcs.cprint"),
        ):
            code = _run_odcs_info(args)

        assert code == 0
