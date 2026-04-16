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

"""Branch coverage tests for contract_validation.py."""

import argparse
import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---- Dataclasses ----


class TestValidationIssue:
    def test_create(self):
        from fluid_build.cli.contract_validation import ValidationIssue

        issue = ValidationIssue(
            severity="error", category="schema", message="Missing field", path="exposes[0].id"
        )
        assert issue.severity == "error"

    def test_str_minimal(self):
        from fluid_build.cli.contract_validation import ValidationIssue

        issue = ValidationIssue(severity="error", category="schema", message="bad", path="")
        s = str(issue)
        assert "ERROR" in s

    def test_str_full(self):
        from fluid_build.cli.contract_validation import ValidationIssue

        issue = ValidationIssue(
            severity="warning",
            category="binding",
            message="check",
            path="exposes[0]",
            expected="int",
            actual="str",
            suggestion="Fix type",
            documentation_url="http://docs.example.com",
        )
        s = str(issue)
        assert "Expected" in s
        assert "Actual" in s
        assert "Suggestion" in s
        assert "Docs" in s

    def test_str_with_path(self):
        from fluid_build.cli.contract_validation import ValidationIssue

        issue = ValidationIssue(
            severity="info", category="metadata", message="ok", path="root.field"
        )
        s = str(issue)
        assert "Path: root.field" in s


class TestValidationReport:
    def _make_report(self):
        from fluid_build.cli.contract_validation import ValidationReport

        return ValidationReport(
            contract_path="test.yaml",
            contract_id="test-dp",
            contract_version="1.0.0",
            validation_time=datetime.now(),
            duration=0.5,
        )

    def test_create(self):
        report = self._make_report()
        assert report.contract_id == "test-dp"

    def test_add_error(self):
        report = self._make_report()
        report.add_issue("error", "schema", "Missing field", "path")
        assert report.checks_failed == 1
        assert len(report.get_errors()) == 1

    def test_add_warning(self):
        report = self._make_report()
        report.add_issue("warning", "binding", "Optional missing", "path")
        assert report.checks_passed == 1
        assert len(report.get_warnings()) == 1

    def test_is_valid_no_errors(self):
        report = self._make_report()
        report.add_issue("warning", "schema", "info", "path")
        assert report.is_valid() is True

    def test_is_valid_with_errors(self):
        report = self._make_report()
        report.add_issue("error", "schema", "bad", "path")
        assert report.is_valid() is False

    def test_get_summary(self):
        report = self._make_report()
        report.add_issue("error", "schema", "err", "p")
        report.add_issue("warning", "metadata", "warn", "p")
        summary = report.get_summary()
        assert "INVALID" in summary
        assert "1 error" in summary
        assert "1 warning" in summary

    def test_get_summary_valid(self):
        report = self._make_report()
        report.add_issue("info", "metadata", "note", "p")
        summary = report.get_summary()
        assert "VALID" in summary

    def test_add_issue_with_suggestion(self):
        report = self._make_report()
        report.add_issue(
            "error",
            "binding",
            "msg",
            "p",
            expected="A",
            actual="B",
            suggestion="Fix it",
            documentation_url="http://example.com",
        )
        assert len(report.issues) == 1
        assert report.issues[0].suggestion == "Fix it"


# ---- ContractValidator ----


class TestContractValidator:
    def test_init_defaults(self):
        from fluid_build.cli.contract_validation import ContractValidator

        v = ContractValidator(contract_path=Path("test.yaml"))
        assert v.strict is False
        assert v.check_data is True

    def test_init_with_cache(self):
        from fluid_build.cli.contract_validation import ContractValidator

        v = ContractValidator(contract_path=Path("test.yaml"), use_cache=True, cache_ttl=600)
        assert v.cache is not None

    def test_init_no_cache(self):
        from fluid_build.cli.contract_validation import ContractValidator

        v = ContractValidator(contract_path=Path("test.yaml"), use_cache=False)
        assert v.cache is None

    def test_init_cache_clear(self):
        from fluid_build.cli.contract_validation import ContractValidator

        v = ContractValidator(contract_path=Path("test.yaml"), use_cache=True, cache_clear=True)
        assert v.cache is not None

    def test_init_history(self):
        from fluid_build.cli.contract_validation import ContractValidator

        v = ContractValidator(contract_path=Path("test.yaml"), track_history=True)
        assert v.history is not None

    def test_init_no_history(self):
        from fluid_build.cli.contract_validation import ContractValidator

        v = ContractValidator(contract_path=Path("test.yaml"), track_history=False)
        assert v.history is None

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_simple(self, mock_schema, mock_load):
        from fluid_build.cli.contract_validation import ContractValidator

        mock_load.return_value = {"id": "test", "version": "1.0", "exposes": []}
        mock_result = MagicMock()
        mock_result.is_valid = True
        mock_result.errors = []
        mock_result.warnings = []
        mock_schema.return_value.validate_contract.return_value = mock_result
        v = ContractValidator(contract_path=Path("test.yaml"), use_cache=False, track_history=False)
        report = v.validate()
        assert report is not None
        assert report.contract_id == "test"

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_no_provider(self, mock_schema, mock_load):
        from fluid_build.cli.contract_validation import ContractValidator

        mock_load.return_value = {
            "id": "test",
            "version": "1.0",
            "exposes": [{"id": "dp1", "type": "table", "binding": {}, "schema": []}],
        }
        mock_result = MagicMock()
        mock_result.is_valid = True
        mock_result.errors = []
        mock_result.warnings = []
        mock_schema.return_value.validate_contract.return_value = mock_result
        v = ContractValidator(contract_path=Path("test.yaml"), use_cache=False, track_history=False)
        report = v.validate()
        # Should warn about no provider
        warnings = [i for i in report.issues if i.severity == "warning"]
        assert len(warnings) >= 1

    @patch(
        "fluid_build.cli.contract_validation.load_contract_with_overlay",
        side_effect=FileNotFoundError("nope"),
    )
    def test_validate_contract_not_found(self, _mock_load):
        from fluid_build.cli.contract_validation import ContractValidator

        v = ContractValidator(
            contract_path=Path("missing.yaml"), use_cache=False, track_history=False
        )
        with pytest.raises(Exception):
            v.validate()


# ---- Provider Detection ----


class TestProviderDetection:
    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_detect_gcp_provider(self, mock_schema, mock_load):
        from fluid_build.cli.contract_validation import ContractValidator

        mock_load.return_value = {
            "id": "test",
            "version": "1.0",
            "exposes": [
                {
                    "id": "dp",
                    "type": "table",
                    "binding": {
                        "platform": "gcp",
                        "location": {
                            "properties": {"project": "myproj", "dataset": "ds", "table": "t"},
                            "format": "bigquery",
                        },
                    },
                    "schema": [],
                }
            ],
        }
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_schema.return_value.validate_contract.return_value = mock_result
        v = ContractValidator(
            contract_path=Path("test.yaml"), use_cache=False, track_history=False, check_data=False
        )
        report = v.validate()
        assert report.provider_name == "gcp"

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_detect_aws_provider(self, mock_schema, mock_load):
        from fluid_build.cli.contract_validation import ContractValidator

        mock_load.return_value = {
            "id": "test",
            "version": "1.0",
            "exposes": [
                {
                    "id": "dp",
                    "type": "table",
                    "binding": {"platform": "aws", "location": {}},
                    "schema": [],
                }
            ],
        }
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_schema.return_value.validate_contract.return_value = mock_result
        v = ContractValidator(
            contract_path=Path("test.yaml"), use_cache=False, track_history=False, check_data=False
        )
        report = v.validate()
        assert report.provider_name == "aws"

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_detect_unknown_provider(self, mock_schema, mock_load):
        from fluid_build.cli.contract_validation import ContractValidator

        mock_load.return_value = {
            "id": "test",
            "version": "1.0",
            "exposes": [
                {
                    "id": "dp",
                    "type": "table",
                    "binding": {"platform": "unknown_provider", "location": {}},
                    "schema": [],
                }
            ],
        }
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_schema.return_value.validate_contract.return_value = mock_result
        v = ContractValidator(
            contract_path=Path("test.yaml"), use_cache=False, track_history=False, check_data=False
        )
        report = v.validate()
        errors = [
            i for i in report.issues if i.severity == "error" and "Unknown provider" in i.message
        ]
        assert len(errors) >= 1

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_detect_databricks_provider(self, mock_schema, mock_load):
        from fluid_build.cli.contract_validation import ContractValidator

        mock_load.return_value = {
            "id": "test",
            "version": "1.0",
            "exposes": [
                {
                    "id": "dp",
                    "type": "table",
                    "binding": {"platform": "databricks", "location": {}},
                    "schema": [],
                }
            ],
        }
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_schema.return_value.validate_contract.return_value = mock_result
        v = ContractValidator(
            contract_path=Path("test.yaml"), use_cache=False, track_history=False, check_data=False
        )
        report = v.validate()
        # Should have "not yet implemented" warning
        warnings = [i for i in report.issues if "not yet implemented" in i.message]
        assert len(warnings) >= 1

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_detect_provider_from_builds(self, mock_schema, mock_load):
        from fluid_build.cli.contract_validation import ContractValidator

        mock_load.return_value = {
            "id": "test",
            "version": "1.0",
            "builds": [{"execution": {"runtime": {"platform": "local"}}}],
            "exposes": [{"id": "dp", "type": "table", "binding": {}, "schema": []}],
        }
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_schema.return_value.validate_contract.return_value = mock_result
        v = ContractValidator(
            contract_path=Path("test.yaml"), use_cache=False, track_history=False, check_data=False
        )
        report = v.validate()
        assert report.provider_name == "local"


# ---- Validate exposes fields ----


class TestValidateExposes:
    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_missing_expose_fields(self, mock_schema, mock_load):
        from fluid_build.cli.contract_validation import ContractValidator

        mock_load.return_value = {
            "id": "test",
            "version": "1.0",
            "exposes": [{"id": "dp1"}],  # missing type, binding, schema
        }
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_schema.return_value.validate_contract.return_value = mock_result
        v = ContractValidator(
            contract_path=Path("test.yaml"), use_cache=False, track_history=False, check_data=False
        )
        report = v.validate()
        missing_errors = [i for i in report.issues if "Missing required field" in i.message]
        assert len(missing_errors) >= 2  # type, binding, schema

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_no_exposes(self, mock_schema, mock_load):
        from fluid_build.cli.contract_validation import ContractValidator

        mock_load.return_value = {"id": "test", "version": "1.0"}
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_schema.return_value.validate_contract.return_value = mock_result
        v = ContractValidator(
            contract_path=Path("test.yaml"), use_cache=False, track_history=False, check_data=False
        )
        report = v.validate()
        warnings = [i for i in report.issues if "No data products exposed" in i.message]
        assert len(warnings) >= 1


# ---- Validate binding ----


class TestValidateBinding:
    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_missing_platform(self, mock_schema, mock_load):
        from fluid_build.cli.contract_validation import ContractValidator

        mock_load.return_value = {
            "id": "test",
            "version": "1.0",
            "exposes": [{"id": "dp", "type": "table", "binding": {"location": {}}, "schema": []}],
        }
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_schema.return_value.validate_contract.return_value = mock_result
        v = ContractValidator(
            contract_path=Path("test.yaml"), use_cache=False, track_history=False, check_data=False
        )
        report = v.validate()
        errors = [i for i in report.issues if "platform" in i.message.lower()]
        assert len(errors) >= 1

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_missing_location(self, mock_schema, mock_load):
        from fluid_build.cli.contract_validation import ContractValidator

        mock_load.return_value = {
            "id": "test",
            "version": "1.0",
            "exposes": [
                {"id": "dp", "type": "table", "binding": {"platform": "gcp"}, "schema": []}
            ],
        }
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_schema.return_value.validate_contract.return_value = mock_result
        v = ContractValidator(
            contract_path=Path("test.yaml"), use_cache=False, track_history=False, check_data=False
        )
        report = v.validate()
        errors = [i for i in report.issues if "location" in i.message.lower()]
        assert len(errors) >= 1

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_gcp_missing_props(self, mock_schema, mock_load):
        from fluid_build.cli.contract_validation import ContractValidator

        mock_load.return_value = {
            "id": "test",
            "version": "1.0",
            "exposes": [
                {
                    "id": "dp",
                    "type": "table",
                    "binding": {
                        "platform": "gcp",
                        "location": {"format": "bigquery", "properties": {}},
                    },
                    "schema": [],
                }
            ],
        }
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_schema.return_value.validate_contract.return_value = mock_result
        v = ContractValidator(
            contract_path=Path("test.yaml"), use_cache=False, track_history=False, check_data=False
        )
        report = v.validate()
        errors = [i for i in report.issues if "Missing required property" in i.message]
        assert len(errors) >= 1  # project, dataset, table


# ---- Register and run ----


class TestContractValidationCLI:
    def test_register(self):
        from fluid_build.cli.contract_validation import register

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)

    @patch("fluid_build.cli.contract_validation.ContractValidator")
    def test_run_valid(self, mock_validator_cls, tmp_path):
        from fluid_build.cli.contract_validation import ValidationReport, run

        contract_file = tmp_path / "test.yaml"
        contract_file.write_text("name: test")
        mock_report = ValidationReport(
            contract_path=str(contract_file),
            contract_id="test",
            contract_version="1.0",
            validation_time=datetime.now(),
            duration=0.1,
        )
        mock_validator_cls.return_value.validate.return_value = mock_report
        args = argparse.Namespace(
            contract=str(contract_file),
            env=None,
            provider=None,
            project=None,
            region=None,
            strict=False,
            no_data=False,
            output_file=None,
            output_format="text",
            verbose=False,
            server=None,
            cache=True,
            cache_ttl=3600,
            cache_clear=False,
            track_history=True,
            check_drift=False,
        )
        result = run(args, logging.getLogger("test"))
        assert result == 0

    @patch("fluid_build.cli.contract_validation.ContractValidator")
    def test_run_invalid(self, mock_validator_cls, tmp_path):
        from fluid_build.cli.contract_validation import ValidationReport, run

        contract_file = tmp_path / "test.yaml"
        contract_file.write_text("name: test")
        mock_report = ValidationReport(
            contract_path=str(contract_file),
            contract_id="test",
            contract_version="1.0",
            validation_time=datetime.now(),
            duration=0.1,
        )
        mock_report.add_issue("error", "schema", "bad field", "exposes[0]")
        mock_validator_cls.return_value.validate.return_value = mock_report
        args = argparse.Namespace(
            contract=str(contract_file),
            env=None,
            provider=None,
            project=None,
            region=None,
            strict=False,
            no_data=False,
            output_file=None,
            output_format="text",
            verbose=False,
            server=None,
            cache=True,
            cache_ttl=3600,
            cache_clear=False,
            track_history=True,
            check_drift=False,
        )
        result = run(args, logging.getLogger("test"))
        assert result == 1

    @patch("fluid_build.cli.contract_validation.ContractValidator")
    def test_run_json_output(self, mock_validator_cls, tmp_path):
        from fluid_build.cli.contract_validation import ValidationReport, run

        contract_file = tmp_path / "test.yaml"
        contract_file.write_text("name: test")
        mock_report = ValidationReport(
            contract_path=str(contract_file),
            contract_id="test",
            contract_version="1.0",
            validation_time=datetime.now(),
            duration=0.1,
        )
        mock_validator_cls.return_value.validate.return_value = mock_report
        output_file = tmp_path / "report.json"
        args = argparse.Namespace(
            contract=str(contract_file),
            env=None,
            provider=None,
            project=None,
            region=None,
            strict=False,
            no_data=False,
            output_file=str(output_file),
            output_format="json",
            verbose=False,
            server=None,
            cache=True,
            cache_ttl=3600,
            cache_clear=False,
            track_history=True,
            check_drift=False,
        )
        result = run(args, logging.getLogger("test"))
        assert result == 0

    def test_run_missing_file(self):
        from fluid_build.cli.contract_validation import run

        args = argparse.Namespace(
            contract="/nonexistent/contract.yaml", env=None, output_format="text", output_file=None
        )
        result = run(args, logging.getLogger("test"))
        assert result == 1

    @patch("fluid_build.cli.contract_validation.ContractValidator")
    def test_run_strict_with_warnings(self, mock_validator_cls, tmp_path):
        from fluid_build.cli.contract_validation import ValidationReport, run

        contract_file = tmp_path / "test.yaml"
        contract_file.write_text("name: test")
        mock_report = ValidationReport(
            contract_path=str(contract_file),
            contract_id="test",
            contract_version="1.0",
            validation_time=datetime.now(),
            duration=0.1,
        )
        mock_report.add_issue("warning", "schema", "minor issue", "field")
        mock_validator_cls.return_value.validate.return_value = mock_report
        args = argparse.Namespace(
            contract=str(contract_file),
            env=None,
            provider=None,
            project=None,
            region=None,
            strict=True,
            no_data=False,
            output_file=None,
            output_format="text",
            verbose=False,
            server=None,
            cache=True,
            cache_ttl=3600,
            cache_clear=False,
            track_history=True,
            check_drift=False,
        )
        result = run(args, logging.getLogger("test"))
        assert result == 1  # Warnings fail in strict mode
