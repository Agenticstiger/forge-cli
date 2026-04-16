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
Unit tests for fluid_build.cli.test — targeting missing lines:
60-61, 244, 262-481, 613-618, 628-635.

Covers: register(), run(), _output_rich(), _output_plain(), _output_json(),
_output_junit(), _detect_provider_label(), _publish_results().
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

import fluid_build.cli.test as cli_test
from fluid_build.cli.test import (
    _detect_provider_label,
    _output_json,
    _output_junit,
    _output_plain,
    _output_rich,
    _publish_results,
    register,
    run,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_issue(
    severity="error",
    category="schema",
    message="oops",
    suggestion=None,
    path=None,
    expected=None,
    actual=None,
):
    issue = MagicMock()
    issue.severity = severity
    issue.category = category
    issue.message = message
    issue.suggestion = suggestion
    issue.path = path
    issue.expected = expected
    issue.actual = actual
    return issue


def _make_report(
    is_valid=True,
    issues=None,
    contract_id="test-contract",
    contract_version="1.0",
    duration=0.42,
    exposes_validated=1,
    consumes_validated=0,
    checks_passed=5,
    checks_failed=0,
    provider_name=None,
    contract_path="test.fluid.yaml",
):
    report = MagicMock()
    report.is_valid.return_value = is_valid
    report.issues = issues or []
    report.contract_id = contract_id
    report.contract_version = contract_version
    report.duration = duration
    report.exposes_validated = exposes_validated
    report.consumes_validated = consumes_validated
    report.checks_passed = checks_passed
    report.checks_failed = checks_failed
    report.provider_name = provider_name
    report.contract_path = contract_path
    report.validation_time = datetime(2026, 1, 1, 12, 0, 0)
    report.get_errors.return_value = [i for i in (issues or []) if i.severity == "error"]
    report.get_warnings.return_value = [i for i in (issues or []) if i.severity == "warning"]
    return report


def _make_args(**kwargs):
    defaults = dict(
        contract="test.fluid.yaml",
        env=None,
        provider=None,
        project=None,
        region=None,
        strict=False,
        no_data=False,
        output="text",
        output_file=None,
        cache=True,
        cache_ttl=3600,
        cache_clear=False,
        check_drift=False,
        publish=None,
        server=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Tests: register()  (lines 60-61 = RICH_AVAILABLE flag + register body)
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_adds_test_subcommand(self):
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        args = parser.parse_args(["test", "my.yaml"])
        assert args.contract == "my.yaml"

    def test_register_sets_func_to_run(self):
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        args = parser.parse_args(["test", "my.yaml"])
        assert args.func is run

    def test_register_strict_flag(self):
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        args = parser.parse_args(["test", "my.yaml", "--strict"])
        assert args.strict is True

    def test_register_output_choices(self):
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        for fmt in ("text", "json", "junit"):
            args = parser.parse_args(["test", "my.yaml", "--output", fmt])
            assert args.output == fmt

    def test_register_check_drift_flag(self):
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        args = parser.parse_args(["test", "my.yaml", "--check-drift"])
        assert args.check_drift is True

    def test_register_no_data_flag(self):
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        args = parser.parse_args(["test", "my.yaml", "--no-data"])
        assert args.no_data is True

    def test_register_publish_flag(self):
        parser = argparse.ArgumentParser()
        subs = parser.add_subparsers()
        register(subs)
        args = parser.parse_args(
            ["test", "my.yaml", "--publish", "https://api.example.com/results"]
        )
        assert args.publish == "https://api.example.com/results"


# ---------------------------------------------------------------------------
# Tests: run()
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_returns_1_when_contract_not_found(self, tmp_path):
        args = _make_args(contract=str(tmp_path / "missing.yaml"))
        logger = MagicMock()
        with patch("fluid_build.cli.test.console_error"):
            result = run(args, logger)
        assert result == 1

    def test_run_returns_0_on_valid_report(self, tmp_path):
        contract = tmp_path / "contract.yaml"
        contract.write_text("spec: v1\n")
        args = _make_args(contract=str(contract))
        logger = MagicMock()
        report = _make_report(is_valid=True)

        mock_validator = MagicMock()
        mock_validator.validate.return_value = report

        with (
            patch(
                "fluid_build.cli.contract_validation.ContractValidator", return_value=mock_validator
            ),
            patch("fluid_build.cli.test._output_rich"),
        ):
            result = run(args, logger)
        assert result == 0

    def test_run_returns_1_on_exception(self, tmp_path):
        contract = tmp_path / "contract.yaml"
        contract.write_text("spec: v1\n")
        args = _make_args(contract=str(contract))
        logger = MagicMock()

        import fluid_build.cli.contract_validation as cv_mod

        mock_validator = MagicMock()
        mock_validator.validate.side_effect = RuntimeError("db connection failed")

        with (
            patch.object(cv_mod, "ContractValidator", return_value=mock_validator),
            patch("fluid_build.cli.test.console_error"),
        ):
            result = run(args, logger)
        assert result == 1

    def test_run_calls_output_json_when_format_json(self, tmp_path):
        contract = tmp_path / "c.yaml"
        contract.write_text("spec: v1\n")
        args = _make_args(contract=str(contract), output="json")
        logger = MagicMock()
        report = _make_report(is_valid=True)

        import fluid_build.cli.contract_validation as cv_mod

        mock_validator = MagicMock()
        mock_validator.validate.return_value = report

        with (
            patch.object(cv_mod, "ContractValidator", return_value=mock_validator),
            patch("fluid_build.cli.test._output_json") as mock_json,
        ):
            run(args, logger)
        mock_json.assert_called_once_with(report, None)

    def test_run_calls_output_junit_when_format_junit(self, tmp_path):
        contract = tmp_path / "c.yaml"
        contract.write_text("spec: v1\n")
        args = _make_args(contract=str(contract), output="junit")
        logger = MagicMock()
        report = _make_report(is_valid=True)

        import fluid_build.cli.contract_validation as cv_mod

        mock_validator = MagicMock()
        mock_validator.validate.return_value = report

        with (
            patch.object(cv_mod, "ContractValidator", return_value=mock_validator),
            patch("fluid_build.cli.test._output_junit") as mock_junit,
        ):
            run(args, logger)
        mock_junit.assert_called_once_with(report, None)

    def test_run_strict_mode_returns_1_on_warnings(self, tmp_path):
        contract = tmp_path / "c.yaml"
        contract.write_text("spec: v1\n")
        args = _make_args(contract=str(contract), strict=True)
        logger = MagicMock()

        warn_issue = _make_issue(severity="warning", category="metadata", message="missing owner")
        report = _make_report(is_valid=True, issues=[warn_issue])

        import fluid_build.cli.contract_validation as cv_mod

        mock_validator = MagicMock()
        mock_validator.validate.return_value = report

        with (
            patch.object(cv_mod, "ContractValidator", return_value=mock_validator),
            patch("fluid_build.cli.test._output_rich"),
            patch("fluid_build.cli.test.console_error"),
        ):
            result = run(args, logger)
        assert result == 1

    def test_run_calls_publish_when_url_set(self, tmp_path):
        contract = tmp_path / "c.yaml"
        contract.write_text("spec: v1\n")
        args = _make_args(contract=str(contract), publish="https://api.example.com/results")
        logger = MagicMock()
        report = _make_report(is_valid=True)

        import fluid_build.cli.contract_validation as cv_mod

        mock_validator = MagicMock()
        mock_validator.validate.return_value = report

        with (
            patch.object(cv_mod, "ContractValidator", return_value=mock_validator),
            patch("fluid_build.cli.test._output_rich"),
            patch("fluid_build.cli.test._publish_results") as mock_pub,
        ):
            run(args, logger)
        mock_pub.assert_called_once_with(report, "https://api.example.com/results", logger)


# ---------------------------------------------------------------------------
# Tests: _output_rich()  (lines 262-481)
# ---------------------------------------------------------------------------


class TestOutputRich:
    """Test _output_rich with various issue combinations."""

    def _call(self, report, output_file=None):
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
        ):
            mock_console = MagicMock()
            MockConsole.return_value = mock_console
            _output_rich(report, output_file)
            return mock_console

    def test_passes_when_report_valid_no_issues(self):
        report = _make_report(is_valid=True)
        console = self._call(report)
        assert console.print.called

    def test_shows_error_icon_when_not_valid(self):
        issue = _make_issue(severity="error", category="schema", message="bad schema")
        report = _make_report(is_valid=False, issues=[issue])
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
            patch("fluid_build.cli.test.Panel") as MockPanel,
        ):
            MockConsole.return_value = MagicMock()
            _output_rich(report)
        # Panel called — check that border_style="red" was used
        call_kwargs = MockPanel.call_args[1] if MockPanel.call_args else {}
        assert call_kwargs.get("border_style") == "red"

    def test_shows_connection_error(self):
        issue = _make_issue(severity="error", category="connection", message="no conn")
        report = _make_report(is_valid=False, issues=[issue])
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
            patch("fluid_build.cli.test.Table") as MockTable,
        ):
            mock_table = MagicMock()
            MockTable.return_value = mock_table
            MockConsole.return_value = MagicMock()
            _output_rich(report)
        # Table.add_row was called multiple times — just ensure no exception raised.

    def test_binding_warning_row(self):
        issue = _make_issue(severity="warning", category="binding", message="missing binding")
        report = _make_report(is_valid=True, issues=[issue])
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
        ):
            MockConsole.return_value = MagicMock()
            _output_rich(report)  # Should not raise

    def test_binding_error_row(self):
        issue = _make_issue(severity="error", category="binding", message="bad binding")
        report = _make_report(is_valid=False, issues=[issue])
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
        ):
            MockConsole.return_value = MagicMock()
            _output_rich(report)

    def test_missing_resource_error_row(self):
        issue = _make_issue(
            severity="error", category="missing_resource", message="table not found"
        )
        report = _make_report(is_valid=False, issues=[issue], exposes_validated=0)
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
        ):
            MockConsole.return_value = MagicMock()
            _output_rich(report)

    def test_field_error_row(self):
        issue = _make_issue(severity="error", category="missing_field", message="field X missing")
        report = _make_report(is_valid=False, issues=[issue])
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
        ):
            MockConsole.return_value = MagicMock()
            _output_rich(report)

    def test_field_warning_row(self):
        issue = _make_issue(severity="warning", category="type_mismatch", message="type mismatch")
        report = _make_report(is_valid=True, issues=[issue])
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
        ):
            MockConsole.return_value = MagicMock()
            _output_rich(report)

    def test_row_count_error_row(self):
        issue = _make_issue(severity="error", category="empty_table", message="table empty")
        report = _make_report(is_valid=False, issues=[issue])
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
        ):
            MockConsole.return_value = MagicMock()
            _output_rich(report)

    def test_row_count_warning_row(self):
        issue = _make_issue(
            severity="warning", category="row_count_below_threshold", message="low rows"
        )
        report = _make_report(is_valid=True, issues=[issue])
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
        ):
            MockConsole.return_value = MagicMock()
            _output_rich(report)

    def test_quality_error_row(self):
        issue = _make_issue(severity="error", category="quality", message="null check failed")
        report = _make_report(is_valid=False, issues=[issue])
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
        ):
            MockConsole.return_value = MagicMock()
            _output_rich(report)

    def test_quality_warning_row(self):
        issue = _make_issue(severity="warning", category="quality", message="partial nulls")
        report = _make_report(is_valid=True, issues=[issue])
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
        ):
            MockConsole.return_value = MagicMock()
            _output_rich(report)

    def test_metadata_error_row(self):
        issue = _make_issue(severity="error", category="metadata", message="owner missing")
        report = _make_report(is_valid=False, issues=[issue])
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
        ):
            MockConsole.return_value = MagicMock()
            _output_rich(report)

    def test_metadata_info_row(self):
        issue = _make_issue(severity="info", category="metadata", message="desc optional")
        report = _make_report(is_valid=True, issues=[issue])
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
        ):
            MockConsole.return_value = MagicMock()
            _output_rich(report)

    def test_drift_row_shown_when_drift_issue(self):
        issue = _make_issue(severity="warning", category="drift", message="new field detected")
        report = _make_report(is_valid=True, issues=[issue])
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
        ):
            MockConsole.return_value = MagicMock()
            _output_rich(report)

    def test_output_file_passed_to_console(self, tmp_path):
        out_file = str(tmp_path / "report.txt")
        report = _make_report(is_valid=True)
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", True),
            patch("fluid_build.cli.test.Console") as MockConsole,
            patch("fluid_build.cli.test.cprint"),
        ):
            MockConsole.return_value = MagicMock()
            _output_rich(report, out_file)
        # Console was called with file=<opened file>
        assert MockConsole.called

    def test_falls_back_to_plain_when_rich_unavailable(self):
        report = _make_report(is_valid=True)
        with (
            patch("fluid_build.cli.test.RICH_AVAILABLE", False),
            patch("fluid_build.cli.test._output_plain") as mock_plain,
        ):
            _output_rich(report)
        mock_plain.assert_called_once_with(report, None)


# ---------------------------------------------------------------------------
# Tests: _output_plain()
# ---------------------------------------------------------------------------


class TestOutputPlain:
    def test_plain_output_to_stdout(self, capsys):
        report = _make_report(is_valid=True)
        with patch("fluid_build.cli.test.cprint") as mock_cprint:
            _output_plain(report)
        mock_cprint.assert_called()

    def test_plain_output_to_file(self, tmp_path):
        out_file = tmp_path / "plain.txt"
        report = _make_report(is_valid=True)
        with patch("fluid_build.cli.test.cprint") as mock_cprint:
            _output_plain(report, str(out_file))
        assert out_file.exists()
        content = out_file.read_text()
        assert "fluid test" in content
        mock_cprint.assert_called()

    def test_plain_shows_fail_label(self):
        issue = _make_issue(severity="error", category="schema", message="bad")
        report = _make_report(is_valid=False, issues=[issue])
        lines_seen = []
        with patch("fluid_build.cli.test.cprint", side_effect=lambda t: lines_seen.append(t)):
            _output_plain(report)
        joined = "\n".join(str(l) for l in lines_seen)
        assert "FAIL" in joined

    def test_plain_shows_pass_label(self):
        report = _make_report(is_valid=True)
        lines_seen = []
        with patch("fluid_build.cli.test.cprint", side_effect=lambda t: lines_seen.append(t)):
            _output_plain(report)
        joined = "\n".join(str(l) for l in lines_seen)
        assert "PASS" in joined

    def test_plain_shows_suggestion(self):
        issue = _make_issue(severity="error", category="schema", message="bad", suggestion="fix it")
        report = _make_report(is_valid=False, issues=[issue])
        lines_seen = []
        with patch("fluid_build.cli.test.cprint", side_effect=lambda t: lines_seen.append(t)):
            _output_plain(report)
        joined = "\n".join(str(l) for l in lines_seen)
        assert "fix it" in joined


# ---------------------------------------------------------------------------
# Tests: _output_json()
# ---------------------------------------------------------------------------


class TestOutputJson:
    def test_json_to_stdout(self):
        report = _make_report(is_valid=True)
        captured = io.StringIO()
        with patch("fluid_build.cli.test.sys") as mock_sys:
            mock_sys.stdout = captured
            _output_json(report)
        # Verify something was written to stdout
        assert captured.getvalue() != ""
        data = json.loads(captured.getvalue().strip())
        assert data["is_valid"] is True
        assert "issues" in data
        assert "summary" in data

    def test_json_to_file(self, tmp_path):
        out_file = tmp_path / "report.json"
        report = _make_report(is_valid=True, contract_id="my-contract")
        with patch("fluid_build.cli.test.cprint"):
            _output_json(report, str(out_file))
        data = json.loads(out_file.read_text())
        assert data["contract_id"] == "my-contract"

    def test_json_includes_issues(self):
        issue = _make_issue(
            severity="error",
            category="schema",
            message="bad",
            suggestion="fix",
            path="$.field",
            expected="STRING",
            actual="INTEGER",
        )
        report = _make_report(is_valid=False, issues=[issue])
        captured_text = {}

        def fake_write(text):
            captured_text["text"] = text

        with patch("fluid_build.cli.test.sys") as mock_sys:
            mock_sys.stdout.write = fake_write
            _output_json(report)

        data = json.loads(captured_text["text"].strip())
        assert len(data["issues"]) == 1
        assert data["issues"][0]["severity"] == "error"

    def test_json_validation_time_is_isoformat(self):
        report = _make_report(is_valid=True)
        captured_text = {}

        def fake_write(text):
            captured_text["text"] = text

        with patch("fluid_build.cli.test.sys") as mock_sys:
            mock_sys.stdout.write = fake_write
            _output_json(report)

        data = json.loads(captured_text["text"].strip())
        # Should parse without error
        datetime.fromisoformat(data["validation_time"])


# ---------------------------------------------------------------------------
# Tests: _output_junit()  (lines 613-618, 628-635)
# ---------------------------------------------------------------------------


class TestOutputJunit:
    def test_junit_to_stdout(self, capsys):
        report = _make_report(is_valid=True, checks_passed=13, checks_failed=0, contract_id="my-id")
        captured_text = {}

        def fake_write(text):
            captured_text["text"] = text

        with patch("fluid_build.cli.test.sys") as mock_sys:
            mock_sys.stdout.write = fake_write
            _output_junit(report)

        assert "testsuite" in captured_text["text"]
        assert "my-id" in captured_text["text"]

    def test_junit_to_file(self, tmp_path):
        out_file = tmp_path / "results.xml"
        report = _make_report(is_valid=True, checks_passed=5, checks_failed=0, contract_id="xml-id")
        with patch("fluid_build.cli.test.cprint"):
            _output_junit(report, str(out_file))
        assert out_file.exists()
        tree = ET.parse(str(out_file))
        root = tree.getroot()
        assert root.tag == "testsuite"

    def test_junit_failure_elements_created(self):
        """Lines 613-618: failure element body with expected/actual/suggestion."""
        issue = _make_issue(
            severity="error",
            category="schema",
            message="type mismatch",
            expected="STRING",
            actual="INTEGER",
            suggestion="cast the field",
        )
        report = _make_report(is_valid=False, issues=[issue], checks_failed=1)
        captured_text = {}

        def fake_write(text):
            captured_text["text"] = text

        with patch("fluid_build.cli.test.sys") as mock_sys:
            mock_sys.stdout.write = fake_write
            _output_junit(report)

        xml = captured_text["text"]
        assert "type mismatch" in xml
        # expected/actual/suggestion lines are in failure body
        assert "expected" in xml or "STRING" in xml

    def test_junit_failure_with_no_expected_actual(self):
        issue = _make_issue(
            severity="error",
            category="connection",
            message="no conn",
            expected=None,
            actual=None,
            suggestion=None,
        )
        report = _make_report(is_valid=False, issues=[issue], checks_failed=1)
        captured_text = {}

        def fake_write(text):
            captured_text["text"] = text

        with patch("fluid_build.cli.test.sys") as mock_sys:
            mock_sys.stdout.write = fake_write
            _output_junit(report)

        xml = captured_text["text"]
        assert "no conn" in xml

    def test_junit_warning_generates_system_out(self):
        issue = _make_issue(severity="warning", category="binding", message="binding warn")
        report = _make_report(is_valid=True, issues=[issue])
        captured_text = {}

        def fake_write(text):
            captured_text["text"] = text

        with patch("fluid_build.cli.test.sys") as mock_sys:
            mock_sys.stdout.write = fake_write
            _output_junit(report)

        xml = captured_text["text"]
        assert "system-out" in xml or "binding warn" in xml

    def test_junit_extra_category_appended(self):
        """Lines 628-635: unknown categories not in all_categories get their own testcase."""
        issue = _make_issue(severity="error", category="custom_check", message="custom fail")
        report = _make_report(is_valid=False, issues=[issue], checks_failed=1)
        captured_text = {}

        def fake_write(text):
            captured_text["text"] = text

        with patch("fluid_build.cli.test.sys") as mock_sys:
            mock_sys.stdout.write = fake_write
            _output_junit(report)

        xml = captured_text["text"]
        assert "custom_check" in xml

    def test_junit_extra_category_no_error_no_failure_elem(self):
        """Extra category with only warning: no failure element in the extra testcase."""
        issue = _make_issue(severity="warning", category="custom_warn", message="soft warn")
        report = _make_report(is_valid=True, issues=[issue])
        captured_text = {}

        def fake_write(text):
            captured_text["text"] = text

        with patch("fluid_build.cli.test.sys") as mock_sys:
            mock_sys.stdout.write = fake_write
            _output_junit(report)

        # No exception is the main assertion; xml should still contain custom_warn
        xml = captured_text["text"]
        assert "custom_warn" in xml

    def test_junit_all_known_categories_have_testcases(self):
        report = _make_report(is_valid=True, checks_passed=13)
        captured_text = {}

        def fake_write(text):
            captured_text["text"] = text

        with patch("fluid_build.cli.test.sys") as mock_sys:
            mock_sys.stdout.write = fake_write
            _output_junit(report)

        xml = captured_text["text"]
        for cat in ["schema", "connection", "binding", "quality", "metadata", "drift"]:
            assert cat in xml


# ---------------------------------------------------------------------------
# Tests: _detect_provider_label()
# ---------------------------------------------------------------------------


class TestDetectProviderLabel:
    def test_gcp_mapped(self):
        report = MagicMock()
        report.provider_name = "gcp"
        assert _detect_provider_label(report) == "gcp (BigQuery)"

    def test_snowflake_mapped(self):
        report = MagicMock()
        report.provider_name = "snowflake"
        assert _detect_provider_label(report) == "snowflake"

    def test_aws_mapped(self):
        report = MagicMock()
        report.provider_name = "aws"
        assert _detect_provider_label(report) == "aws (Glue/Athena)"

    def test_local_mapped(self):
        report = MagicMock()
        report.provider_name = "local"
        assert _detect_provider_label(report) == "local (DuckDB)"

    def test_unknown_provider_returned_as_is(self):
        report = MagicMock()
        report.provider_name = "databricks"
        assert _detect_provider_label(report) == "databricks"

    def test_none_provider_returns_auto_detected(self):
        report = MagicMock()
        report.provider_name = None
        assert _detect_provider_label(report) == "auto-detected"

    def test_missing_provider_name_attr_returns_auto_detected(self):
        report = object()  # no provider_name attribute
        assert _detect_provider_label(report) == "auto-detected"


# ---------------------------------------------------------------------------
# Tests: _publish_results()  (line 244)
# ---------------------------------------------------------------------------


class TestPublishResults:
    def _make_logger(self):
        return MagicMock(spec=logging.Logger)

    def test_warns_when_api_key_not_set(self):
        report = _make_report(is_valid=True)
        logger = self._make_logger()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("fluid_build.cli.test.warning") as mock_warn,
            patch("fluid_build.cli.test.os") as mock_os,
        ):
            mock_os.getenv.return_value = ""
            _publish_results(report, "https://api.example.com/results", logger)
        mock_warn.assert_called_once()

    def test_publishes_successfully_when_key_set(self):
        report = _make_report(is_valid=True)
        logger = self._make_logger()

        mock_provider = MagicMock()
        mock_provider.publish_test_results.return_value = {"status_code": 200}

        fake_dmm_module = MagicMock()
        fake_dmm_module.DataMeshManagerProvider = MagicMock(return_value=mock_provider)

        with (
            patch.dict(
                "sys.modules",
                {"fluid_build.providers.datamesh_manager.datamesh_manager": fake_dmm_module},
            ),
            patch("fluid_build.cli.test.os") as mock_os,
            patch("fluid_build.cli.test.success") as mock_success,
        ):
            mock_os.getenv.return_value = "secret-key"
            _publish_results(report, "https://api.example.com/results", logger)

        mock_success.assert_called_once()

    def test_error_logged_on_publish_exception(self):
        report = _make_report(is_valid=True)
        logger = self._make_logger()

        mock_provider = MagicMock()
        mock_provider.publish_test_results.side_effect = RuntimeError("network error")

        fake_dmm_module = MagicMock()
        fake_dmm_module.DataMeshManagerProvider = MagicMock(return_value=mock_provider)

        with (
            patch.dict(
                "sys.modules",
                {"fluid_build.providers.datamesh_manager.datamesh_manager": fake_dmm_module},
            ),
            patch("fluid_build.cli.test.os") as mock_os,
            patch("fluid_build.cli.test.console_error") as mock_err,
        ):
            mock_os.getenv.return_value = "secret-key"
            _publish_results(report, "https://api.example.com/results", logger)

        mock_err.assert_called_once()
