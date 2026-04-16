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

"""Tests for opds.py: export, validate, info commands and helpers."""

import json
import logging
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.opds import (
    DEFAULT_VERSION,
    ODPS_VERSIONS,
    cmd_opds_export,
    cmd_opds_info,
    cmd_opds_validate,
    get_version_info,
    register,
)

LOG = logging.getLogger("test_opds_ext")


# ---------------------------------------------------------------------------
# get_version_info
# ---------------------------------------------------------------------------


class TestGetVersionInfo:
    def test_valid_version(self):
        info = get_version_info("4.1")
        assert "spec_url" in info
        assert "schema_url" in info
        assert info["status"] == "stable"

    def test_invalid_version(self):
        with pytest.raises(ValueError, match="Unsupported ODPS version"):
            get_version_info("99.99")

    def test_default_version(self):
        assert DEFAULT_VERSION == "4.1"

    def test_versions_dict(self):
        assert "4.1" in ODPS_VERSIONS
        assert ODPS_VERSIONS["4.1"]["default"] is True


# ---------------------------------------------------------------------------
# cmd_opds_export
# ---------------------------------------------------------------------------


class TestCmdOpdsExport:
    @patch("fluid_build.cli.opds.cprint")
    @patch("fluid_build.cli.bootstrap.build_provider")
    @patch("fluid_build.cli.bootstrap.load_contract_with_overlay")
    def test_export_stdout_pretty(self, mock_load, mock_build, mock_cprint):
        mock_load.return_value = {"id": "test", "name": "Test"}
        mock_provider = MagicMock()
        mock_provider.render.return_value = {"dataProductId": "test"}
        mock_build.return_value = mock_provider

        args = MagicMock()
        args.contract = "test.yaml"
        args.version = "4.1"
        args.out = "-"
        args.env = None
        args.pretty = True

        result = cmd_opds_export(args, LOG)
        assert result == 0
        mock_cprint.assert_called()

    @patch("fluid_build.cli.opds.cprint")
    @patch("fluid_build.cli.bootstrap.build_provider")
    @patch("fluid_build.cli.bootstrap.load_contract_with_overlay")
    def test_export_stdout_compact(self, mock_load, mock_build, _mock_cprint):
        mock_load.return_value = {"id": "test"}
        mock_provider = MagicMock()
        mock_provider.render.return_value = {"data": "value"}
        mock_build.return_value = mock_provider

        args = MagicMock()
        args.contract = "test.yaml"
        args.version = "4.1"
        args.out = "-"
        args.env = None
        args.pretty = False

        result = cmd_opds_export(args, LOG)
        assert result == 0

    @patch("fluid_build.cli.opds.cprint")
    @patch("fluid_build.cli.bootstrap.build_provider")
    @patch("fluid_build.cli.bootstrap.load_contract_with_overlay")
    def test_export_to_file(self, mock_load, mock_build, _mock_cprint):
        mock_load.return_value = {"id": "test"}
        mock_provider = MagicMock()
        mock_provider.render.return_value = {"data": "value"}
        mock_build.return_value = mock_provider

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            f.write(b'{"test": 1}')
            tmp_path = f.name

        try:
            args = MagicMock()
            args.contract = "test.yaml"
            args.version = "4.1"
            args.out = tmp_path
            args.env = None
            args.pretty = True

            result = cmd_opds_export(args, LOG)
            assert result == 0
        finally:
            os.unlink(tmp_path)

    @patch("fluid_build.cli.opds.console_error")
    @patch(
        "fluid_build.cli.bootstrap.load_contract_with_overlay", side_effect=Exception("bad file")
    )
    def test_export_contract_load_error(self, _mock_load, _mock_err):
        args = MagicMock()
        args.contract = "bad.yaml"
        args.version = "4.1"
        args.env = None

        result = cmd_opds_export(args, LOG)
        assert result == 1

    @patch("fluid_build.cli.opds.console_error")
    @patch("fluid_build.cli.bootstrap.build_provider", side_effect=Exception("no provider"))
    @patch("fluid_build.cli.bootstrap.load_contract_with_overlay", return_value={})
    def test_export_provider_error(self, _mock_load, _mock_build, _mock_err):
        args = MagicMock()
        args.contract = "test.yaml"
        args.version = "4.1"
        args.env = None

        result = cmd_opds_export(args, LOG)
        assert result == 1

    @patch("fluid_build.cli.opds.console_error")
    @patch("fluid_build.cli.bootstrap.build_provider")
    @patch("fluid_build.cli.bootstrap.load_contract_with_overlay", return_value={})
    def test_export_render_error(self, _mock_load, mock_build, _mock_err):
        mock_provider = MagicMock()
        mock_provider.render.side_effect = Exception("render failed")
        mock_build.return_value = mock_provider

        args = MagicMock()
        args.contract = "test.yaml"
        args.version = "4.1"
        args.out = "-"
        args.env = None
        args.pretty = True

        result = cmd_opds_export(args, LOG)
        assert result == 1


# ---------------------------------------------------------------------------
# cmd_opds_validate
# ---------------------------------------------------------------------------


class TestCmdOpdsValidate:
    def test_validate_file_not_found(self):
        args = MagicMock()
        args.file = "/nonexistent/file.json"
        args.version = "4.1"
        args.full_schema = True

        with patch("fluid_build.cli.opds.console_error"):
            result = cmd_opds_validate(args, LOG)
        assert result == 1

    def test_validate_valid_basic(self):
        data = {
            "dataProductId": "test-product",
            "dataProductName": "Test Product",
            "dataProductDescription": "A test product",
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            tmp_path = f.name

        try:
            args = MagicMock()
            args.file = tmp_path
            args.version = "4.1"
            args.full_schema = True

            mock_result = {
                "valid": True,
                "validation_type": "basic",
                "warnings": [],
            }
            with (
                patch("fluid_build.cli.opds.cprint"),
                patch(
                    "fluid_build.providers.odps.validator.validate_opds_structure",
                    return_value=mock_result,
                ),
            ):
                result = cmd_opds_validate(args, LOG)
            assert result == 0
        finally:
            os.unlink(tmp_path)

    def test_validate_missing_fields(self):
        data = {"dataProductId": "test"}
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            tmp_path = f.name

        try:
            args = MagicMock()
            args.file = tmp_path
            args.version = "4.1"
            args.full_schema = True

            mock_result = {
                "valid": False,
                "validation_type": "basic",
                "errors": ["Missing required fields: dataProductName, dataProductDescription"],
            }
            with (
                patch("fluid_build.cli.opds.console_error"),
                patch(
                    "fluid_build.providers.odps.validator.validate_opds_structure",
                    return_value=mock_result,
                ),
            ):
                result = cmd_opds_validate(args, LOG)
            assert result == 1
        finally:
            os.unlink(tmp_path)

    def test_validate_wrapped_format(self):
        data = {
            "artifacts": {
                "dataProductId": "test",
                "dataProductName": "Test",
                "dataProductDescription": "desc",
            }
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            tmp_path = f.name

        try:
            args = MagicMock()
            args.file = tmp_path
            args.version = "4.1"
            args.full_schema = True

            mock_result = {
                "valid": True,
                "validation_type": "basic",
                "warnings": [],
            }
            with (
                patch("fluid_build.cli.opds.cprint"),
                patch(
                    "fluid_build.providers.odps.validator.validate_opds_structure",
                    return_value=mock_result,
                ),
            ):
                result = cmd_opds_validate(args, LOG)
            assert result == 0
        finally:
            os.unlink(tmp_path)

    def test_validate_bad_json(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("not valid json{{{")
            tmp_path = f.name

        try:
            args = MagicMock()
            args.file = tmp_path
            args.version = "4.1"
            args.full_schema = True

            with patch("fluid_build.cli.opds.console_error"):
                result = cmd_opds_validate(args, LOG)
            assert result == 1
        finally:
            os.unlink(tmp_path)

    def test_validate_with_full_schema_validator(self):
        data = {
            "dataProductId": "test",
            "dataProductName": "Test",
            "dataProductDescription": "desc",
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            tmp_path = f.name

        try:
            args = MagicMock()
            args.file = tmp_path
            args.version = "4.1"
            args.full_schema = True

            mock_result = {
                "valid": True,
                "validation_type": "full_schema",
                "warnings": ["test warning"],
            }

            with (
                patch("fluid_build.cli.opds.cprint"),
                patch(
                    "fluid_build.providers.odps.validator.validate_opds_structure",
                    return_value=mock_result,
                ),
            ):
                result = cmd_opds_validate(args, LOG)
            assert result == 0
        finally:
            os.unlink(tmp_path)

    def test_validate_full_schema_fails(self):
        data = {"dataProductId": "test"}
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            tmp_path = f.name

        try:
            args = MagicMock()
            args.file = tmp_path
            args.version = "4.1"
            args.full_schema = True

            mock_result = {
                "valid": False,
                "validation_type": "full_schema",
                "errors": ["missing field X"],
            }

            with (
                patch("fluid_build.cli.opds.console_error"),
                patch(
                    "fluid_build.providers.odps.validator.validate_opds_structure",
                    return_value=mock_result,
                ),
            ):
                result = cmd_opds_validate(args, LOG)
            assert result == 1
        finally:
            os.unlink(tmp_path)

    def test_validate_with_version_field(self):
        data = {
            "dataProductId": "test",
            "dataProductName": "Test",
            "dataProductDescription": "desc",
            "version": "1.0",
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            tmp_path = f.name

        try:
            args = MagicMock()
            args.file = tmp_path
            args.version = "4.1"
            args.full_schema = True

            mock_result = {
                "valid": True,
                "validation_type": "basic",
                "warnings": [],
            }
            with (
                patch("fluid_build.cli.opds.cprint"),
                patch(
                    "fluid_build.providers.odps.validator.validate_opds_structure",
                    return_value=mock_result,
                ),
            ):
                result = cmd_opds_validate(args, LOG)
            assert result == 0
        finally:
            os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# cmd_opds_info
# ---------------------------------------------------------------------------


class TestCmdOpdsInfo:
    @patch("fluid_build.cli.opds.cprint")
    def test_info_all_versions(self, _mock_cprint):
        args = MagicMock()
        args.version = None
        args.json = False
        del args.version
        args.version = None

        result = cmd_opds_info(args, LOG)
        assert result == 0

    @patch("fluid_build.cli.opds.cprint")
    def test_info_specific_version(self, _mock_cprint):
        args = MagicMock()
        args.version = "4.1"
        args.json = False

        result = cmd_opds_info(args, LOG)
        assert result == 0

    @patch("fluid_build.cli.opds.cprint")
    def test_info_json_output(self, _mock_cprint):
        args = MagicMock()
        args.version = "4.1"
        args.json = True

        result = cmd_opds_info(args, LOG)
        assert result == 0

    @patch("fluid_build.cli.opds.cprint")
    def test_info_all_json(self, _mock_cprint):
        args = MagicMock()
        args.version = None
        args.json = True

        result = cmd_opds_info(args, LOG)
        assert result == 0

    @patch("fluid_build.cli.opds.console_error")
    def test_info_invalid_version(self, _mock_err):
        args = MagicMock()
        args.version = "99.99"
        args.json = False

        result = cmd_opds_info(args, LOG)
        assert result == 1


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_adds_subparser(self):
        import argparse

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        # Should not raise
        ns = parser.parse_args(["odps", "info"])
        assert hasattr(ns, "func")
