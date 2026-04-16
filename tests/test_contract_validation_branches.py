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

"""Branch-coverage tests for fluid_build.cli.contract_validation"""

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluid_build.cli.contract_validation import (
    ContractValidator,
    ValidationIssue,
    ValidationReport,
    output_json_report,
    output_plain_report,
    output_text_report,
    register,
    run,
)

# ===================== ValidationIssue =====================


class TestValidationIssue:
    def test_str_minimal(self):
        vi = ValidationIssue(severity="error", category="schema", message="bad", path="")
        s = str(vi)
        assert "[ERROR]" in s
        assert "bad" in s

    def test_str_with_path(self):
        vi = ValidationIssue(severity="warning", category="binding", message="x", path="a.b")
        assert "Path: a.b" in str(vi)

    def test_str_with_expected_actual(self):
        vi = ValidationIssue(
            severity="info",
            category="schema",
            message="m",
            path="p",
            expected="INT",
            actual="STRING",
        )
        s = str(vi)
        assert "Expected: INT" in s
        assert "Actual: STRING" in s

    def test_str_with_suggestion_and_docs(self):
        vi = ValidationIssue(
            severity="error",
            category="quality",
            message="m",
            path="p",
            suggestion="fix it",
            documentation_url="http://docs",
        )
        s = str(vi)
        assert "Suggestion: fix it" in s
        assert "Docs: http://docs" in s


# ===================== ValidationReport =====================


class TestValidationReport:
    def _make_report(self):
        return ValidationReport(
            contract_path="test.yaml",
            contract_id="test-id",
            contract_version="1.0.0",
            validation_time=datetime.now(),
            duration=1.5,
        )

    def test_add_issue_error(self):
        r = self._make_report()
        r.add_issue(
            "error",
            "schema",
            "bad field",
            "a.b",
            expected="X",
            actual="Y",
            suggestion="fix",
            documentation_url="http://x",
        )
        assert r.checks_failed == 1
        assert len(r.get_errors()) == 1
        assert not r.is_valid()

    def test_add_issue_warning(self):
        r = self._make_report()
        r.add_issue("warning", "binding", "missing", "x")
        assert r.checks_passed == 1
        assert len(r.get_warnings()) == 1
        assert r.is_valid()

    def test_get_summary_valid(self):
        r = self._make_report()
        r.exposes_validated = 2
        r.consumes_validated = 1
        s = r.get_summary()
        assert "VALID" in s
        assert "2" in s

    def test_get_summary_invalid(self):
        r = self._make_report()
        r.add_issue("error", "schema", "fail", "")
        s = r.get_summary()
        assert "INVALID" in s


# ===================== ContractValidator =====================


class TestContractValidator:
    def _make_contract(self, **overrides):
        base = {
            "id": "test-product",
            "version": "1.0.0",
            "metadata": {"owner": "team", "layer": "Gold", "domain": "analytics", "tags": ["a"]},
            "exposes": [
                {
                    "id": "exp1",
                    "type": "table",
                    "binding": {
                        "platform": "gcp",
                        "location": {
                            "format": "bigquery",
                            "properties": {"project": "proj", "dataset": "ds", "table": "tbl"},
                        },
                    },
                    "schema": [
                        {"name": "id", "type": "INTEGER"},
                        {"name": "name", "type": "VARCHAR"},
                    ],
                }
            ],
            "quality": {"sla": {"freshness": "1h"}, "tests": [{"name": "row_count"}]},
        }
        base.update(overrides)
        return base

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_schema_valid(self, MockSM, mock_load):
        contract = self._make_contract()
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock()
        mock_result.is_valid = True
        mock_result.errors = []
        mock_result.warnings = []
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        assert report.contract_id == "test-product"

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_schema_with_errors(self, MockSM, mock_load):
        contract = self._make_contract()
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock()
        mock_result.is_valid = False
        mock_result.errors = ["missing field X"]
        mock_result.warnings = ["deprecated field Y"]
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        errors = report.get_errors()
        assert any("missing field X" in e.message for e in errors)

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_schema_exception(self, MockSM, mock_load):
        contract = self._make_contract()
        mock_load.return_value = contract
        MockSM.side_effect = Exception("schema boom")

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        assert any("Schema validation failed" in e.message for e in report.get_errors())

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    def test_validate_file_not_found(self, mock_load):
        mock_load.side_effect = FileNotFoundError("nope")
        v = ContractValidator(Path("missing.yaml"), use_cache=False, check_data=False)
        with pytest.raises(Exception):
            v.validate()

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    def test_validate_load_error(self, mock_load):
        mock_load.side_effect = ValueError("parse error")
        v = ContractValidator(Path("bad.yaml"), use_cache=False, check_data=False)
        with pytest.raises(Exception):
            v.validate()

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_detect_provider_from_exposes(self, MockSM, mock_load):
        contract = self._make_contract()
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        # provider detected as gcp but BIGQUERY_AVAILABLE is False in test
        # so there should be a warning/error about missing package
        assert report.provider_name == "gcp"

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_detect_provider_from_builds(self, MockSM, mock_load):
        contract = self._make_contract(
            exposes=[{"id": "e1", "type": "table", "binding": {}, "schema": []}],
            builds=[{"execution": {"runtime": {"platform": "snowflake"}}}],
        )
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        assert report.provider_name == "snowflake"

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_no_provider_detected(self, MockSM, mock_load):
        contract = self._make_contract(
            exposes=[{"id": "e1", "type": "table", "binding": {}, "schema": []}],
        )
        contract.pop("builds", None)
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        assert any("No provider" in w.message for w in report.get_warnings())

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_unknown_provider(self, MockSM, mock_load):
        contract = self._make_contract()
        contract["exposes"][0]["binding"]["platform"] = "unknown_provider"
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        assert any("Unknown provider" in e.message for e in report.get_errors())

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_databricks_provider_not_implemented(self, MockSM, mock_load):
        contract = self._make_contract()
        contract["exposes"][0]["binding"]["platform"] = "databricks"
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        assert any("not yet implemented" in w.message for w in report.get_warnings())

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_exposes_empty(self, MockSM, mock_load):
        contract = self._make_contract(exposes=[])
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        assert any("No data products" in w.message for w in report.get_warnings())

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_expose_missing_fields(self, MockSM, mock_load):
        contract = self._make_contract(exposes=[{"notid": "x"}])
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        errors = report.get_errors()
        assert any("Missing required field" in e.message for e in errors)

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_binding_missing_platform(self, MockSM, mock_load):
        contract = self._make_contract(
            exposes=[
                {
                    "id": "e1",
                    "type": "table",
                    "binding": {"location": {"format": "x"}},
                    "schema": [],
                }
            ]
        )
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        assert any("Missing 'platform'" in e.message for e in report.get_errors())

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_binding_missing_location(self, MockSM, mock_load):
        contract = self._make_contract(
            exposes=[{"id": "e1", "type": "table", "binding": {"platform": "gcp"}, "schema": []}]
        )
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        errors = report.get_errors()
        assert any("Missing 'location'" in e.message for e in errors)

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_schema_nonstandard_type(self, MockSM, mock_load):
        contract = self._make_contract(
            exposes=[
                {
                    "id": "e1",
                    "type": "table",
                    "binding": {
                        "platform": "gcp",
                        "location": {
                            "format": "bq",
                            "properties": {"project": "p", "dataset": "d", "table": "t"},
                        },
                    },
                    "schema": [{"name": "x", "type": "WEIRD_TYPE"}],
                }
            ]
        )
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        assert any("Non-standard column type" in w.message for w in report.get_warnings())

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_schema_missing_name(self, MockSM, mock_load):
        contract = self._make_contract(
            exposes=[
                {
                    "id": "e1",
                    "type": "table",
                    "binding": {
                        "platform": "gcp",
                        "location": {
                            "format": "bq",
                            "properties": {"project": "p", "dataset": "d", "table": "t"},
                        },
                    },
                    "schema": [{"type": "STRING"}, {"name": "y"}],
                }
            ]
        )
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        errors = report.get_errors()
        assert any("Missing 'name'" in e.message for e in errors)
        assert any("Missing 'type'" in e.message for e in errors)

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_consumes(self, MockSM, mock_load):
        contract = self._make_contract(
            consumes=[
                {"id": "c1", "ref": "some-ref"},
                {"id": "c2"},  # missing ref
            ]
        )
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        assert report.consumes_validated == 2
        assert any("Missing 'ref'" in e.message for e in report.get_errors())

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_quality_no_quality(self, MockSM, mock_load):
        contract = self._make_contract()
        contract.pop("quality", None)
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        assert any("No quality" in i.message for i in report.issues if i.severity == "info")

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_quality_bad_freshness(self, MockSM, mock_load):
        contract = self._make_contract(quality={"sla": {"freshness": 42}})
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        assert any("freshness" in w.message.lower() for w in report.get_warnings())

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_quality_test_missing_name(self, MockSM, mock_load):
        contract = self._make_contract(quality={"tests": [{"rule": "x"}]})
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        assert any("missing 'name'" in w.message.lower() for w in report.get_warnings())

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_metadata_empty(self, MockSM, mock_load):
        contract = self._make_contract()
        contract["metadata"] = {}
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        # empty metadata triggers warning
        assert any("No metadata" in w.message for w in report.get_warnings())

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_metadata_bad_layer(self, MockSM, mock_load):
        contract = self._make_contract()
        contract["metadata"]["layer"] = "BadLayer"
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        assert any("BadLayer" in w.message for w in report.get_warnings())

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_validate_metadata_missing_recommended(self, MockSM, mock_load):
        contract = self._make_contract()
        contract["metadata"] = {"owner": "team"}  # missing layer, domain, tags
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        v = ContractValidator(Path("test.yaml"), use_cache=False, check_data=False)
        report = v.validate()
        infos = [i for i in report.issues if i.severity == "info"]
        assert any("layer" in i.message for i in infos)

    def test_cache_clear(self):
        with patch("fluid_build.cli.contract_validation.ValidationCache") as MockCache:
            mock_cache = MockCache.return_value
            ContractValidator(Path("test.yaml"), use_cache=True, cache_clear=True)
            mock_cache.clear.assert_called_once()

    @patch("fluid_build.cli.contract_validation.load_contract_with_overlay")
    @patch("fluid_build.cli.contract_validation.FluidSchemaManager")
    def test_build_snowflake_config(self, MockSM, mock_load):
        contract = {
            "id": "test",
            "version": "1.0.0",
            "exposes": [
                {
                    "id": "e1",
                    "type": "table",
                    "binding": {
                        "platform": "snowflake",
                        "location": {"account": "acc", "database": "db", "schema": "sch"},
                    },
                    "schema": [],
                }
            ],
            "metadata": {},
        }
        mock_load.return_value = contract
        mock_sm = MockSM.return_value
        mock_result = MagicMock(is_valid=True, errors=[], warnings=[])
        mock_sm.validate_contract.return_value = mock_result

        with (
            patch("fluid_build.cli.contract_validation.SNOWFLAKE_VALIDATION_AVAILABLE", True),
            patch("fluid_build.cli.contract_validation.SnowflakeValidationProvider") as MockSFP,
        ):
            mock_sfp = MockSFP.return_value
            mock_sfp.validate_connection.return_value = True
            v = ContractValidator(
                Path("test.yaml"), use_cache=False, check_data=False, server="override-acc"
            )
            v.validate()
            # The _build_snowflake_config should have been called
            call_args = MockSFP.call_args[0][0]
            assert call_args["account"] == "override-acc"


# ===================== run() and register() =====================


class TestRunFunction:
    @patch("fluid_build.cli.contract_validation.ContractValidator")
    def test_run_contract_not_found(self, _MockCV):
        args = argparse.Namespace(
            contract="nonexistent.yaml",
            output_format="text",
            output_file=None,
            strict=False,
            env=None,
            provider=None,
            project=None,
            region=None,
            no_data=False,
            cache=True,
            cache_ttl=3600,
            cache_clear=False,
            track_history=True,
            check_drift=False,
        )
        with patch.object(Path, "exists", return_value=False):
            result = run(args, logging.getLogger())
        assert result == 1

    @patch("fluid_build.cli.contract_validation.ContractValidator")
    def test_run_cache_stats(self, _MockCV):
        args = argparse.Namespace(
            contract="test.yaml",
            output_format="text",
            output_file=None,
            strict=False,
            cache_stats=True,
            cache_ttl=3600,
        )
        with (
            patch.object(Path, "exists", return_value=True),
            patch("fluid_build.cli.contract_validation.ValidationCache") as MockCache,
        ):
            mock_cache = MockCache.return_value
            mock_cache.get_cache_stats.return_value = {
                "total_entries": 5,
                "fresh_entries": 3,
                "stale_entries": 2,
                "total_size_bytes": 1024,
                "cache_dir": "/tmp",
                "ttl_seconds": 3600,
            }
            result = run(args, logging.getLogger())
        assert result == 0

    @patch("fluid_build.cli.contract_validation.output_json_report")
    @patch("fluid_build.cli.contract_validation.ContractValidator")
    def test_run_json_output(self, MockCV, mock_json_out):
        mock_report = MagicMock()
        mock_report.is_valid.return_value = True
        mock_report.get_warnings.return_value = []
        MockCV.return_value.validate.return_value = mock_report

        args = argparse.Namespace(
            contract="test.yaml",
            output_format="json",
            output_file=None,
            strict=False,
            env=None,
            provider=None,
            project=None,
            region=None,
            no_data=False,
            cache=True,
            cache_ttl=3600,
            cache_clear=False,
            track_history=True,
            check_drift=False,
        )
        with patch.object(Path, "exists", return_value=True):
            result = run(args, logging.getLogger())
        assert result == 0
        mock_json_out.assert_called_once()

    @patch("fluid_build.cli.contract_validation.output_text_report")
    @patch("fluid_build.cli.contract_validation.ContractValidator")
    def test_run_strict_with_warnings(self, MockCV, _mock_text_out):
        mock_report = MagicMock()
        mock_report.is_valid.return_value = True
        mock_report.get_warnings.return_value = [MagicMock()]
        MockCV.return_value.validate.return_value = mock_report

        args = argparse.Namespace(
            contract="test.yaml",
            output_format="text",
            output_file=None,
            strict=True,
            env=None,
            provider=None,
            project=None,
            region=None,
            no_data=False,
            cache=True,
            cache_ttl=3600,
            cache_clear=False,
            track_history=True,
            check_drift=False,
        )
        with patch.object(Path, "exists", return_value=True):
            result = run(args, logging.getLogger())
        assert result == 1

    @patch("fluid_build.cli.contract_validation.ContractValidator")
    def test_run_validation_exception(self, MockCV):
        MockCV.return_value.validate.side_effect = RuntimeError("boom")
        args = argparse.Namespace(
            contract="test.yaml",
            output_format="text",
            output_file=None,
            strict=False,
            env=None,
            provider=None,
            project=None,
            region=None,
            no_data=False,
            cache=True,
            cache_ttl=3600,
            cache_clear=False,
            track_history=True,
            check_drift=False,
        )
        with patch.object(Path, "exists", return_value=True):
            result = run(args, logging.getLogger())
        assert result == 1

    def test_register(self):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        register(sub)


# ===================== Output functions =====================


class TestOutputFunctions:
    def _make_report(self, valid=True):
        r = ValidationReport(
            contract_path="test.yaml",
            contract_id="test-id",
            contract_version="1.0.0",
            validation_time=datetime.now(),
            duration=1.5,
            exposes_validated=1,
            consumes_validated=0,
            checks_passed=3,
            checks_failed=0,
        )
        if not valid:
            r.add_issue("error", "schema", "bad field", "a.b")
        return r

    @patch("fluid_build.cli.contract_validation.RICH_AVAILABLE", False)
    def test_output_text_report_plain(self):
        r = self._make_report()
        output_text_report(r)

    @patch("fluid_build.cli.contract_validation.RICH_AVAILABLE", False)
    def test_output_plain_report_with_issues(self):
        r = self._make_report(valid=False)
        output_plain_report(r)

    @patch("fluid_build.cli.contract_validation.RICH_AVAILABLE", False)
    def test_output_plain_report_to_file(self, tmp_path=None):
        import os
        import tempfile

        r = self._make_report()
        fd, path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            output_plain_report(r, path)
            with open(path) as f:
                assert "VALID" in f.read()
        finally:
            os.unlink(path)

    def test_output_json_report(self):
        r = self._make_report()
        output_json_report(r)

    def test_output_json_report_to_file(self):
        import os
        import tempfile

        r = self._make_report(valid=False)
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            output_json_report(r, path)
            with open(path) as f:
                data = json.loads(f.read())
            assert data["contract_id"] == "test-id"
            assert data["is_valid"] is False
        finally:
            os.unlink(path)

    @patch("fluid_build.cli.contract_validation.RICH_AVAILABLE", True)
    def test_output_rich_report(self):
        from fluid_build.cli.contract_validation import output_rich_report

        r = self._make_report(valid=False)
        # Just ensure it doesn't crash; Rich writes to console
        output_rich_report(r)

    @patch("fluid_build.cli.contract_validation.RICH_AVAILABLE", True)
    def test_output_rich_report_valid(self):
        from fluid_build.cli.contract_validation import output_rich_report

        r = self._make_report(valid=True)
        output_rich_report(r)
