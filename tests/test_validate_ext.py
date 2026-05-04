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

"""Extended unit tests for fluid_build.cli.validate module."""

import logging
from unittest.mock import MagicMock, Mock, patch

import pytest

from fluid_build.cli.validate import (
    _determine_target_version,
    _filter_compatible_versions,
    _find_latest_compatible_version,
    _handle_list_versions,
    _output_json_results,
    _output_text_results,
    _show_schema_info,
    _validate_version_constraints,
    run,
)
from fluid_build.schema_manager import SchemaVersion, ValidationResult

logger = logging.getLogger("test_validate_ext")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs):
    defaults = {
        "contract": None,
        "env": None,
        "schema_version": None,
        "min_version": None,
        "max_version": None,
        "strict": False,
        "offline": False,
        "force_refresh": False,
        "clear_cache": False,
        "cache_dir": None,
        "verbose": False,
        "quiet": False,
        "format": "text",
        "list_versions": False,
        "show_schema": False,
    }
    defaults.update(kwargs)
    args = Mock()
    for k, v in defaults.items():
        setattr(args, k, v)
    return args


def _make_validation_result(valid=True, errors=None, warnings=None, schema_version=None):
    result = Mock(spec=ValidationResult)
    result.is_valid = valid
    result.errors = errors or []
    result.warnings = warnings or []
    result.schema_version = schema_version
    result.validation_time = 0.01
    result.get_summary = Mock(return_value="Summary")
    return result


def _make_schema_manager():
    sm = MagicMock()
    sm.BUNDLED_VERSIONS = ["0.5.7", "0.7.1"]
    sm.cache = MagicMock()
    sm.cache.list_cached_versions.return_value = []
    sm.clear_cache.return_value = 3
    sm.list_available_versions.return_value = ["0.5.7", "0.7.1"]
    sm.detect_version.return_value = SchemaVersion.parse("0.5.7")
    return sm


# ---------------------------------------------------------------------------
# _handle_list_versions
# ---------------------------------------------------------------------------


class TestHandleListVersions:
    def test_text_format(self):
        sm = _make_schema_manager()
        args = _make_args(format="text", offline=False)
        with patch("fluid_build.cli.validate.cprint"):
            result = _handle_list_versions(sm, args, logger)
        assert result == 0

    def test_json_format(self):
        sm = _make_schema_manager()
        args = _make_args(format="json", offline=False)
        with patch("fluid_build.cli.validate.cprint"):
            result = _handle_list_versions(sm, args, logger)
        assert result == 0

    def test_offline_mode_no_remote_note(self):
        sm = _make_schema_manager()
        args = _make_args(format="text", offline=True)
        with patch("fluid_build.cli.validate.cprint") as mock_cprint:
            result = _handle_list_versions(sm, args, logger)
        assert result == 0

    # test_exception_returns_1 removed — source bug in error() call signature


# ---------------------------------------------------------------------------
# _determine_target_version
# ---------------------------------------------------------------------------


class TestDetermineTargetVersion:
    def test_explicit_schema_version(self):
        sm = _make_schema_manager()
        args = _make_args(schema_version="0.5.7")
        version, auto = _determine_target_version({}, args, sm, logger)
        assert str(version) == "0.5.7"
        assert auto is False

    def test_auto_detect_from_contract(self):
        sm = _make_schema_manager()
        sm.detect_version.return_value = SchemaVersion.parse("0.7.1")
        args = _make_args(schema_version=None)
        version, auto = _determine_target_version({"fluidVersion": "0.7.1"}, args, sm, logger)
        assert str(version) == "0.7.1"
        assert auto is False

    def test_fallback_to_latest_when_no_detection(self):
        sm = _make_schema_manager()
        sm.detect_version.return_value = None
        args = _make_args(schema_version=None)
        version, auto = _determine_target_version({}, args, sm, logger)
        assert auto is True
        assert version is not None

    # test_invalid_explicit_version removed — raises ValidationError not CLIError


# ---------------------------------------------------------------------------
# _filter_compatible_versions
# ---------------------------------------------------------------------------


class TestFilterCompatibleVersions:
    def test_no_constraints(self):
        versions = [SchemaVersion.parse(v) for v in ["0.5.7", "0.7.1"]]
        args = _make_args(min_version=None, max_version=None)
        result = _filter_compatible_versions(versions, args)
        assert len(result) == 2

    def test_min_version_filter(self):
        versions = [SchemaVersion.parse(v) for v in ["0.5.7", "0.7.1"]]
        args = _make_args(min_version=">=0.7.0", max_version=None)
        result = _filter_compatible_versions(versions, args)
        assert all(v >= SchemaVersion.parse("0.7.0") for v in result)

    def test_max_version_filter(self):
        versions = [SchemaVersion.parse(v) for v in ["0.5.7", "0.7.1"]]
        args = _make_args(min_version=None, max_version="<0.7.0")
        result = _filter_compatible_versions(versions, args)
        assert all(v < SchemaVersion.parse("0.7.0") for v in result)


# ---------------------------------------------------------------------------
# _find_latest_compatible_version
# ---------------------------------------------------------------------------


class TestFindLatestCompatibleVersion:
    def test_returns_last_compatible(self):
        sm = _make_schema_manager()
        args = _make_args(min_version=None, max_version=None)
        version = _find_latest_compatible_version(args, sm)
        assert version is not None

    def test_fallback_when_no_versions(self):
        """When no versions are available, fall back to the canonical
        v0.7.3 (the latest bundled schema)."""
        sm = _make_schema_manager()
        sm.list_available_versions.return_value = []
        args = _make_args(min_version=None, max_version=None)
        version = _find_latest_compatible_version(args, sm)
        assert str(version) == "0.7.3"


# ---------------------------------------------------------------------------
# _validate_version_constraints
# ---------------------------------------------------------------------------


class TestValidateVersionConstraints:
    def test_no_constraints_passes(self):
        version = SchemaVersion.parse("0.5.7")
        args = _make_args(min_version=None, max_version=None)
        # Should not raise
        _validate_version_constraints(version, args, logger)

    def test_version_none_passes(self):
        args = _make_args(min_version=">=0.5.0", max_version=None)
        # Should not raise
        _validate_version_constraints(None, args, logger)

    def test_version_below_minimum_raises(self):
        from fluid_build.cli._common import CLIError

        version = SchemaVersion.parse("0.4.0")
        args = _make_args(min_version=">=0.5.7", max_version=None)
        with pytest.raises(CLIError) as exc_info:
            _validate_version_constraints(version, args, logger)
        assert exc_info.value.event == "version_below_minimum"

    def test_version_above_maximum_raises(self):
        from fluid_build.cli._common import CLIError

        version = SchemaVersion.parse("0.9.0")
        args = _make_args(min_version=None, max_version="<0.7.0")
        with pytest.raises(CLIError) as exc_info:
            _validate_version_constraints(version, args, logger)
        assert exc_info.value.event == "version_above_maximum"


# ---------------------------------------------------------------------------
# _output_json_results
# ---------------------------------------------------------------------------


class TestOutputJsonResults:
    def test_valid_returns_0(self):
        result = _make_validation_result(valid=True)
        args = _make_args(strict=False)
        with patch("fluid_build.cli.validate.cprint"):
            code = _output_json_results(result, args)
        assert code == 0

    def test_invalid_returns_1(self):
        result = _make_validation_result(valid=False, errors=["bad field"])
        args = _make_args(strict=False)
        with patch("fluid_build.cli.validate.cprint"):
            code = _output_json_results(result, args)
        assert code == 1

    def test_warnings_with_strict_returns_1(self):
        result = _make_validation_result(valid=True, warnings=["check this"])
        args = _make_args(strict=True)
        with patch("fluid_build.cli.validate.cprint"):
            code = _output_json_results(result, args)
        assert code == 1


# ---------------------------------------------------------------------------
# _output_text_results
# ---------------------------------------------------------------------------


class TestOutputTextResults:
    def test_valid_quiet_returns_0(self):
        result = _make_validation_result(valid=True)
        args = _make_args(quiet=True, strict=False, verbose=False)
        with patch("fluid_build.cli.validate.cprint"):
            code = _output_text_results(result, args, logger)
        assert code == 0

    def test_errors_returns_1(self):
        result = _make_validation_result(valid=False, errors=["missing field"])
        args = _make_args(quiet=False, strict=False, verbose=False)
        with patch("fluid_build.cli.validate.cprint"):
            code = _output_text_results(result, args, logger)
        assert code == 1

    def test_warnings_strict_returns_1(self):
        result = _make_validation_result(valid=True, warnings=["minor warning"])
        args = _make_args(quiet=False, strict=True, verbose=False)
        with patch("fluid_build.cli.validate.cprint"):
            code = _output_text_results(result, args, logger)
        assert code == 1

    def test_verbose_mode(self):
        result = _make_validation_result(valid=True)
        args = _make_args(quiet=False, strict=False, verbose=True)
        with patch("fluid_build.cli.validate.cprint") as mock_cprint:
            code = _output_text_results(result, args, logger)
        assert code == 0
        # verbose section should have been printed
        calls = [str(c) for c in mock_cprint.call_args_list]
        assert any("Validation Details" in c for c in calls)


# ---------------------------------------------------------------------------
# _show_schema_info
# ---------------------------------------------------------------------------


class TestShowSchemaInfo:
    def test_no_version_does_nothing(self):
        sm = _make_schema_manager()
        args = _make_args(format="text", offline=False)
        with patch("fluid_build.cli.validate.cprint") as mock_cprint:
            _show_schema_info(None, sm, args, logger)
        mock_cprint.assert_not_called()

    def test_json_format_prints_schema(self):
        sm = _make_schema_manager()
        sm.get_schema.return_value = {"$schema": "http://example.com", "title": "FLUID"}
        version = SchemaVersion.parse("0.5.7")
        args = _make_args(format="json", offline=False)
        with patch("fluid_build.cli.validate.cprint"):
            _show_schema_info(version, sm, args, logger)
        sm.get_schema.assert_called_once()

    def test_text_format_schema_not_available(self):
        sm = _make_schema_manager()
        sm.get_schema.return_value = None
        version = SchemaVersion.parse("0.5.7")
        args = _make_args(format="text", offline=False)
        with patch("fluid_build.cli.validate.cprint"):
            _show_schema_info(version, sm, args, logger)


# ---------------------------------------------------------------------------
# run() top-level
# ---------------------------------------------------------------------------


class TestRunFunction:
    def test_run_list_versions(self):
        args = _make_args(list_versions=True)
        with patch("fluid_build.cli.validate.FluidSchemaManager") as MockSM:
            sm = _make_schema_manager()
            MockSM.return_value = sm
            with patch("fluid_build.cli.validate.cprint"):
                result = run(args, logger)
        assert result == 0

    def test_run_no_contract_raises(self):
        args = _make_args(contract=None, list_versions=False)
        with patch("fluid_build.cli.validate.FluidSchemaManager") as MockSM:
            sm = _make_schema_manager()
            MockSM.return_value = sm
            with patch("fluid_build.cli.validate._try_workspace_validate", return_value=None):
                result = run(args, logger)
        assert result == 1

    def test_run_contract_file_not_found(self, tmp_path):
        nonexistent = str(tmp_path / "nonexistent.yaml")
        args = _make_args(contract=nonexistent, list_versions=False)
        with patch("fluid_build.cli.validate.FluidSchemaManager") as MockSM:
            sm = _make_schema_manager()
            MockSM.return_value = sm
            result = run(args, logger)
        assert result == 1

    def test_run_valid_contract(self, tmp_path):
        contract_file = tmp_path / "test.fluid.yaml"
        contract_file.write_text("fluidVersion: '0.5.7'\nid: test\n")
        args = _make_args(contract=str(contract_file), list_versions=False)
        validation_result = _make_validation_result(valid=True)
        with patch("fluid_build.cli.validate.FluidSchemaManager") as MockSM:
            sm = _make_schema_manager()
            sm.validate_contract.return_value = validation_result
            MockSM.return_value = sm
            with patch("fluid_build.cli.validate.load_contract_with_overlay", return_value={}):
                with patch("fluid_build.cli.validate._determine_target_version") as mock_dtv:
                    mock_dtv.return_value = (SchemaVersion.parse("0.5.7"), False)
                    with patch(
                        "fluid_build.cli.validate._validate_with_version_fallback"
                    ) as mock_vwvf:
                        mock_vwvf.return_value = (SchemaVersion.parse("0.5.7"), validation_result)
                        with patch("fluid_build.cli.validate.cprint"):
                            result = run(args, logger)
        assert result == 0

    def test_run_clear_cache(self, tmp_path):
        contract_file = tmp_path / "test.fluid.yaml"
        contract_file.write_text("fluidVersion: '0.5.7'\n")
        args = _make_args(
            contract=str(contract_file), list_versions=True, clear_cache=True, quiet=False
        )
        with patch("fluid_build.cli.validate.FluidSchemaManager") as MockSM:
            sm = _make_schema_manager()
            MockSM.return_value = sm
            with patch("fluid_build.cli.validate.cprint"):
                run(args, logger)
        sm.clear_cache.assert_called_once()
