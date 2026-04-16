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

"""Branch-coverage tests for fluid_build.cli.test"""

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.test import (
    COMMAND,
    _detect_provider_label,
    _output_json,
    _output_junit,
    _output_plain,
    register,
    run,
)


@pytest.fixture
def logger():
    return logging.getLogger("test_test")


# ── Module constants ────────────────────────────────────────────────


class TestModuleConstants:
    def test_command_name(self):
        assert COMMAND == "test"


# ── register ────────────────────────────────────────────────────────


class TestRegister:
    def test_register_adds_parser(self):
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        args = parser.parse_args(["test", "contract.fluid.yaml"])
        assert args.contract == "contract.fluid.yaml"


# ── _detect_provider_label ──────────────────────────────────────────


class TestDetectProviderLabel:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("gcp", "gcp (BigQuery)"),
            ("snowflake", "snowflake"),
            ("aws", "aws (Glue/Athena)"),
            ("local", "local (DuckDB)"),
        ],
    )
    def test_known_providers(self, name, expected):
        report = SimpleNamespace(provider_name=name)
        assert _detect_provider_label(report) == expected

    def test_unknown_provider(self):
        report = SimpleNamespace(provider_name="azure")
        assert _detect_provider_label(report) == "azure"

    def test_no_provider_name(self):
        report = SimpleNamespace()
        assert _detect_provider_label(report) == "auto-detected"

    def test_none_provider_name(self):
        report = SimpleNamespace(provider_name=None)
        assert _detect_provider_label(report) == "auto-detected"


# ── Mock report factory ─────────────────────────────────────────────


def _make_report(valid=True, errors=None, warnings=None, issues=None):
    """Create a mock report object matching ContractValidator output."""
    r = MagicMock()
    r.is_valid.return_value = valid
    r.contract_path = "contract.fluid.yaml"
    r.contract_id = "test-dp"
    r.contract_version = "1.0"
    r.validation_time = datetime(2024, 1, 1, 12, 0, 0)
    r.duration = 1.23
    r.exposes_validated = 3
    r.consumes_validated = 1
    r.checks_passed = 10
    r.checks_failed = 0 if valid else 2
    r.provider_name = "local"

    if issues is None:
        issues = []
    if errors is None:
        errors = [i for i in issues if getattr(i, "severity", "") == "error"]
    if warnings is None:
        warnings = [i for i in issues if getattr(i, "severity", "") == "warning"]

    r.get_errors.return_value = errors
    r.get_warnings.return_value = warnings
    r.issues = issues
    return r


def _make_issue(severity="error", category="schema", message="bad", suggestion=None):
    """Create a mock issue."""
    i = SimpleNamespace(
        severity=severity,
        category=category,
        message=message,
        path="spec.exposes[0]",
        expected="string",
        actual="int",
        suggestion=suggestion,
    )
    return i


# ── _output_json ────────────────────────────────────────────────────


class TestOutputJson:
    def test_stdout(self, capsys):
        report = _make_report(valid=True, issues=[])
        _output_json(report)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["is_valid"] is True
        assert data["contract_id"] == "test-dp"
        assert data["summary"]["errors"] == 0

    def test_to_file(self, tmp_path):
        report = _make_report(valid=False, issues=[_make_issue()])
        out_file = str(tmp_path / "report.json")
        _output_json(report, out_file)
        data = json.loads(Path(out_file).read_text())
        assert data["is_valid"] is False
        assert len(data["issues"]) == 1


# ── _output_plain ───────────────────────────────────────────────────


class TestOutputPlain:
    @patch("fluid_build.cli.test.cprint")
    def test_stdout_pass(self, mock_cprint):
        report = _make_report(valid=True, issues=[])
        _output_plain(report)
        call_text = mock_cprint.call_args[0][0]
        assert "PASS" in call_text

    @patch("fluid_build.cli.test.cprint")
    def test_stdout_fail(self, mock_cprint):
        report = _make_report(valid=False, issues=[_make_issue()])
        _output_plain(report)
        call_text = mock_cprint.call_args[0][0]
        assert "FAIL" in call_text

    def test_to_file(self, tmp_path):
        report = _make_report(valid=True, issues=[])
        out_file = str(tmp_path / "report.txt")
        _output_plain(report, out_file)
        content = Path(out_file).read_text()
        assert "PASS" in content

    @patch("fluid_build.cli.test.cprint")
    def test_with_suggestion(self, mock_cprint):
        issue = _make_issue(suggestion="fix it")
        report = _make_report(valid=False, issues=[issue])
        _output_plain(report)
        call_text = mock_cprint.call_args[0][0]
        assert "fix it" in call_text


# ── _output_junit ───────────────────────────────────────────────────


class TestOutputJunit:
    def test_to_file(self, tmp_path):
        report = _make_report(valid=True, issues=[])
        out_file = str(tmp_path / "report.xml")
        _output_junit(report, out_file)
        content = Path(out_file).read_text()
        assert "testsuite" in content

    def test_to_stdout(self, capsys):
        report = _make_report(valid=True, issues=[])
        _output_junit(report)
        out = capsys.readouterr().out
        assert "testsuite" in out

    def test_with_errors(self, tmp_path):
        issue = _make_issue(severity="error", category="schema", message="type mismatch")
        report = _make_report(valid=False, issues=[issue])
        out_file = str(tmp_path / "report.xml")
        _output_junit(report, out_file)
        content = Path(out_file).read_text()
        assert "failure" in content

    def test_with_warnings(self, tmp_path):
        issue = _make_issue(severity="warning", category="quality", message="low coverage")
        report = _make_report(valid=True, issues=[issue], warnings=[issue])
        out_file = str(tmp_path / "report.xml")
        _output_junit(report, out_file)
        content = Path(out_file).read_text()
        assert "system-out" in content


# ── run ─────────────────────────────────────────────────────────────


class TestRun:
    def test_missing_contract_returns_1(self, logger):
        args = SimpleNamespace(contract="/nonexistent/contract.yaml")
        result = run(args, logger)
        assert result == 1

    @patch("fluid_build.cli.test._output_rich")
    def test_valid_contract_returns_0(self, _mock_output, logger, tmp_path):
        contract = tmp_path / "contract.fluid.yaml"
        contract.write_text("apiVersion: 0.5.7")
        report = _make_report(valid=True, issues=[])
        with patch("fluid_build.cli.contract_validation.ContractValidator") as mock_cls:
            mock_cls.return_value.validate.return_value = report
            args = SimpleNamespace(
                contract=str(contract),
                env=None,
                provider=None,
                project=None,
                region=None,
                strict=False,
                no_data=False,
                cache=True,
                cache_ttl=3600,
                cache_clear=False,
                check_drift=False,
                server=None,
                output="text",
                output_file=None,
                publish=None,
            )
            result = run(args, logger)
        assert result == 0

    @patch("fluid_build.cli.test._output_json")
    def test_json_output_format(self, mock_output, logger, tmp_path):
        contract = tmp_path / "contract.fluid.yaml"
        contract.write_text("apiVersion: 0.5.7")
        report = _make_report(valid=True, issues=[])
        with patch("fluid_build.cli.contract_validation.ContractValidator") as mock_cls:
            mock_cls.return_value.validate.return_value = report
            args = SimpleNamespace(
                contract=str(contract),
                env=None,
                provider=None,
                project=None,
                region=None,
                strict=False,
                no_data=False,
                cache=True,
                cache_ttl=3600,
                cache_clear=False,
                check_drift=False,
                server=None,
                output="json",
                output_file=None,
                publish=None,
            )
            run(args, logger)
        mock_output.assert_called_once()

    @patch("fluid_build.cli.test._output_junit")
    def test_junit_output_format(self, mock_output, logger, tmp_path):
        contract = tmp_path / "contract.fluid.yaml"
        contract.write_text("apiVersion: 0.5.7")
        report = _make_report(valid=True, issues=[])
        with patch("fluid_build.cli.contract_validation.ContractValidator") as mock_cls:
            mock_cls.return_value.validate.return_value = report
            args = SimpleNamespace(
                contract=str(contract),
                env=None,
                provider=None,
                project=None,
                region=None,
                strict=False,
                no_data=False,
                cache=True,
                cache_ttl=3600,
                cache_clear=False,
                check_drift=False,
                server=None,
                output="junit",
                output_file=None,
                publish=None,
            )
            run(args, logger)
        mock_output.assert_called_once()

    @patch("fluid_build.cli.test._output_rich")
    def test_invalid_returns_1(self, _mock_output, logger, tmp_path):
        contract = tmp_path / "contract.fluid.yaml"
        contract.write_text("apiVersion: 0.5.7")
        report = _make_report(valid=False, issues=[_make_issue()])
        with patch("fluid_build.cli.contract_validation.ContractValidator") as mock_cls:
            mock_cls.return_value.validate.return_value = report
            args = SimpleNamespace(
                contract=str(contract),
                env=None,
                provider=None,
                project=None,
                region=None,
                strict=False,
                no_data=False,
                cache=True,
                cache_ttl=3600,
                cache_clear=False,
                check_drift=False,
                server=None,
                output="text",
                output_file=None,
                publish=None,
            )
            result = run(args, logger)
        assert result == 1

    @patch("fluid_build.cli.test._output_rich")
    def test_strict_with_warnings_returns_1(self, _mock_output, logger, tmp_path):
        contract = tmp_path / "contract.fluid.yaml"
        contract.write_text("apiVersion: 0.5.7")
        warn = _make_issue(severity="warning")
        report = _make_report(valid=True, issues=[warn], warnings=[warn])
        with patch("fluid_build.cli.contract_validation.ContractValidator") as mock_cls:
            mock_cls.return_value.validate.return_value = report
            args = SimpleNamespace(
                contract=str(contract),
                env=None,
                provider=None,
                project=None,
                region=None,
                strict=True,
                no_data=False,
                cache=True,
                cache_ttl=3600,
                cache_clear=False,
                check_drift=False,
                server=None,
                output="text",
                output_file=None,
                publish=None,
            )
            result = run(args, logger)
        assert result == 1

    @patch("fluid_build.cli.test._output_rich")
    def test_validate_exception_returns_1(self, _mock_output, logger, tmp_path):
        contract = tmp_path / "contract.fluid.yaml"
        contract.write_text("apiVersion: 0.5.7")
        with patch("fluid_build.cli.contract_validation.ContractValidator") as mock_cls:
            mock_cls.return_value.validate.side_effect = RuntimeError("boom")
            args = SimpleNamespace(
                contract=str(contract),
                env=None,
                provider=None,
                project=None,
                region=None,
                strict=False,
                no_data=False,
                cache=True,
                cache_ttl=3600,
                cache_clear=False,
                check_drift=False,
                server=None,
                output="text",
                output_file=None,
                publish=None,
            )
            result = run(args, logger)
        assert result == 1


# ── _publish_results ────────────────────────────────────────────────


class TestPublishResults:
    @patch.dict(os.environ, {"DMM_API_KEY": ""}, clear=False)
    @patch("fluid_build.cli.test.warning")
    def test_missing_api_key_skips(self, mock_warn, logger):
        from fluid_build.cli.test import _publish_results

        _publish_results(MagicMock(), "http://dmm", logger)
        mock_warn.assert_called_once()

    @patch.dict(os.environ, {"DMM_API_KEY": "secret"}, clear=False)
    @patch("fluid_build.cli.test.success")
    def test_publish_success(self, mock_success, logger):
        from fluid_build.cli.test import _publish_results

        with patch(
            "fluid_build.providers.datamesh_manager.datamesh_manager.DataMeshManagerProvider"
        ) as mock_prov:
            mock_prov.return_value.publish_test_results.return_value = {"status_code": 200}
            _publish_results(MagicMock(), "http://dmm", logger)
        mock_success.assert_called_once()

    @patch.dict(os.environ, {"DMM_API_KEY": "secret"}, clear=False)
    @patch("fluid_build.cli.test.console_error")
    def test_publish_failure(self, mock_err, logger):
        from fluid_build.cli.test import _publish_results

        with patch(
            "fluid_build.providers.datamesh_manager.datamesh_manager.DataMeshManagerProvider"
        ) as mock_prov:
            mock_prov.return_value.publish_test_results.side_effect = RuntimeError("fail")
            _publish_results(MagicMock(), "http://dmm", logger)
        mock_err.assert_called_once()
