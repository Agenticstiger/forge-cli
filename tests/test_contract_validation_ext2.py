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

"""Extended tests for ContractValidator in cli/contract_validation.py.

Part 2: covers _detect_and_validate_provider, _validate_bigquery_resource,
_validate_generic_resource, _build_snowflake_config, output_* functions,
run() edge cases, _validate_single_expose, _validate_exposes,
_validate_binding (deeper), _validate_schema_definition (deeper).
"""

import argparse
import logging
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from fluid_build.cli.contract_validation import ContractValidator, ValidationReport


def _make_validator(contract_dict=None):
    """Create a ContractValidator with minimal mocking to avoid file I/O."""
    with patch.object(ContractValidator, "__init__", lambda self, *a, **kw: None):
        v = ContractValidator.__new__(ContractValidator)
    v.contract_path = Path("/fake/contract.yaml")
    v.contract = contract_dict or {}
    v.env = "dev"
    v.provider_name = None
    v.project = None
    v.region = None
    v.server = None
    v.check_data = False
    v.use_cache = False
    v.cache = None
    v.track_history = False
    v.check_drift = False
    v.history = None
    v.validation_provider = None
    v.logger = logging.getLogger("test")
    v.report = ValidationReport(
        contract_path="/fake/contract.yaml",
        contract_id="test",
        contract_version="1.0.0",
        validation_time=__import__("datetime").datetime.now(),
        duration=0.0,
    )
    return v


class TestValidateSingleExpose:
    def test_all_required_fields_present(self):
        v = _make_validator()
        expose = {
            "id": "out",
            "type": "table",
            "binding": {"platform": "local", "location": {"format": "csv", "properties": {}}},
            "schema": [{"name": "x", "type": "STRING"}],
        }
        v._validate_single_expose(expose, 0)
        errors = [i for i in v.report.issues if i.severity == "error"]
        assert len(errors) == 0

    def test_missing_required_fields(self):
        v = _make_validator()
        v._validate_single_expose({}, 0)
        errors = [i for i in v.report.issues if i.severity == "error"]
        # Should flag missing id, type, binding, schema
        assert len(errors) >= 4

    def test_missing_id_only(self):
        v = _make_validator()
        expose = {"type": "table", "binding": {"platform": "local"}, "schema": []}
        v._validate_single_expose(expose, 0)
        error_msgs = [i.message for i in v.report.issues if i.severity == "error"]
        assert any("id" in m for m in error_msgs)

    def test_binding_validation_called(self):
        v = _make_validator()
        expose = {
            "id": "out",
            "type": "table",
            "binding": {"platform": "local", "location": {"format": "parquet", "properties": {}}},
            "schema": [],
        }
        v._validate_single_expose(expose, 0)
        # Binding is valid so no binding errors
        binding_errors = [
            i for i in v.report.issues if i.category == "binding" and i.severity == "error"
        ]
        assert len(binding_errors) == 0


class TestValidateExposes:
    def test_no_exposes(self):
        v = _make_validator({"exposes": []})
        v._validate_exposes()
        warnings = [i for i in v.report.issues if i.severity == "warning"]
        assert any("No data products" in w.message for w in warnings)

    def test_multiple_exposes(self):
        v = _make_validator(
            {
                "exposes": [
                    {"id": "a", "type": "table", "binding": {"platform": "local"}, "schema": []},
                    {"id": "b", "type": "view", "binding": {"platform": "local"}, "schema": []},
                ]
            }
        )
        v._validate_exposes()
        assert v.report.exposes_validated == 2


class TestValidateBindingDeeper:
    def test_missing_platform(self):
        v = _make_validator()
        v._validate_binding({}, "exposes[0].binding", "test")
        errors = [i for i in v.report.issues if i.severity == "error"]
        assert any("platform" in e.message for e in errors)

    def test_missing_location(self):
        v = _make_validator()
        v._validate_binding({"platform": "local"}, "exposes[0].binding", "test")
        errors = [i for i in v.report.issues if i.severity == "error"]
        assert any("location" in e.message for e in errors)

    def test_location_missing_format(self):
        v = _make_validator()
        v._validate_binding(
            {"platform": "local", "location": {"properties": {}}}, "exposes[0].binding", "test"
        )
        warnings = [i for i in v.report.issues if i.severity == "warning"]
        assert any("format" in w.message for w in warnings)

    def test_location_missing_properties(self):
        v = _make_validator()
        v._validate_binding(
            {"platform": "local", "location": {"format": "parquet"}}, "exposes[0].binding", "test"
        )
        warnings = [i for i in v.report.issues if i.severity == "warning"]
        assert any("properties" in w.message for w in warnings)

    def test_gcp_missing_required_props(self):
        v = _make_validator()
        v.provider_name = "gcp"
        v._validate_binding(
            {"platform": "gcp", "location": {"format": "parquet", "properties": {"project": "p"}}},
            "path",
            "test",
        )
        errors = [i for i in v.report.issues if i.severity == "error"]
        # Missing dataset and table
        assert any("dataset" in e.message for e in errors)
        assert any("table" in e.message for e in errors)

    def test_gcp_all_props_present(self):
        v = _make_validator()
        v.provider_name = "gcp"
        v._validate_binding(
            {
                "platform": "gcp",
                "location": {
                    "format": "parquet",
                    "properties": {"project": "p", "dataset": "d", "table": "t"},
                },
            },
            "path",
            "test",
        )
        errors = [i for i in v.report.issues if i.severity == "error"]
        assert len(errors) == 0

    def test_valid_binding(self):
        v = _make_validator()
        v._validate_binding(
            {
                "platform": "local",
                "location": {"format": "parquet", "properties": {"path": "/data"}},
            },
            "path",
            "test",
        )
        assert len(v.report.issues) == 0


class TestValidateSchemaDefinitionDeeper:
    def test_empty_schema(self):
        v = _make_validator()
        v._validate_schema_definition([], "path", "test")
        warnings = [i for i in v.report.issues if i.severity == "warning"]
        assert any("Empty schema" in w.message for w in warnings)

    def test_valid_columns(self):
        v = _make_validator()
        schema = [
            {"name": "id", "type": "INTEGER"},
            {"name": "value", "type": "FLOAT"},
            {"name": "label", "type": "STRING"},
        ]
        v._validate_schema_definition(schema, "path", "test")
        assert len(v.report.issues) == 0

    def test_missing_name(self):
        v = _make_validator()
        v._validate_schema_definition([{"type": "STRING"}], "path", "test")
        errors = [i for i in v.report.issues if i.severity == "error"]
        assert any("name" in e.message for e in errors)

    def test_missing_type(self):
        v = _make_validator()
        v._validate_schema_definition([{"name": "col"}], "path", "test")
        errors = [i for i in v.report.issues if i.severity == "error"]
        assert any("type" in e.message for e in errors)

    def test_nonstandard_type(self):
        v = _make_validator()
        v._validate_schema_definition([{"name": "col", "type": "SUPERTYPE"}], "path", "test")
        warnings = [i for i in v.report.issues if i.severity == "warning"]
        assert any("Non-standard" in w.message for w in warnings)

    def test_all_standard_types(self):
        """Verify all standard types pass without warnings."""
        v = _make_validator()
        standard = [
            "VARCHAR",
            "STRING",
            "TEXT",
            "INT",
            "INTEGER",
            "BIGINT",
            "FLOAT",
            "DOUBLE",
            "DECIMAL",
            "BOOL",
            "BOOLEAN",
            "DATE",
            "DATETIME",
            "TIMESTAMP",
            "JSON",
            "BYTES",
        ]
        schema = [{"name": f"c{i}", "type": t} for i, t in enumerate(standard)]
        v._validate_schema_definition(schema, "path", "test")
        warnings = [i for i in v.report.issues if i.severity == "warning"]
        assert len(warnings) == 0


# ---------------------------------------------------------------------------
# _detect_and_validate_provider() branches
# ---------------------------------------------------------------------------


def _make_validator_new(provider_name="gcp"):
    from fluid_build.cli.contract_validation import ContractValidator, ValidationReport

    v = ContractValidator.__new__(ContractValidator)
    v.provider_name = provider_name
    v.project = "test-project"
    v.region = "us-central1"
    v.server = None
    v.strict = False
    v.check_data = True
    v.use_cache = False
    v.cache = None
    v.track_history = False
    v.history = None
    v.check_drift = False
    v.validation_provider = None
    v.contract = {}
    v.env = None
    v.contract_path = Path("/tmp/c.yaml")
    v.report = ValidationReport(
        contract_path="/tmp/c.yaml",
        contract_id="test-product",
        contract_version="1.0.0",
        validation_time=datetime.now(),
        duration=0.0,
    )
    v.logger = MagicMock()
    return v


def _make_run_args(**kw):
    defaults = dict(
        contract="/tmp/missing.yaml",
        env=None,
        provider=None,
        project=None,
        region=None,
        strict=False,
        no_data=False,
        output_format="text",
        output_file=None,
        cache=False,
        cache_ttl=3600,
        cache_clear=False,
        cache_stats=False,
        track_history=False,
        check_drift=False,
        server=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


class TestDetectAndValidateProviderExt(unittest.TestCase):
    def test_no_exposes_no_builds_adds_warning(self):
        v = _make_validator_new(provider_name=None)
        v.contract = {}
        v._detect_and_validate_provider()
        warnings = v.report.get_warnings()
        assert any("No provider" in w.message or "platform" in w.message for w in warnings)

    def test_unknown_provider_adds_error(self):
        v = _make_validator_new(provider_name="unknown_cloud")
        v.contract = {"exposes": [{"binding": {"platform": "unknown_cloud"}}]}
        v._detect_and_validate_provider()
        errors = v.report.get_errors()
        assert any("Unknown provider" in e.message for e in errors)

    def test_databricks_adds_warning(self):
        v = _make_validator_new(provider_name="databricks")
        v.contract = {"exposes": [{"binding": {"platform": "databricks"}}]}
        v._detect_and_validate_provider()
        warnings = v.report.get_warnings()
        assert any("databricks" in w.message.lower() for w in warnings)

    def test_azure_adds_warning(self):
        v = _make_validator_new(provider_name="azure")
        v.contract = {"exposes": [{"binding": {"platform": "azure"}}]}
        v._detect_and_validate_provider()
        warnings = v.report.get_warnings()
        assert any("azure" in w.message.lower() for w in warnings)

    def test_gcp_unavailable_adds_error(self):
        v = _make_validator_new(provider_name="gcp")
        v.contract = {"exposes": [{"binding": {"platform": "gcp"}}]}
        with patch("fluid_build.cli.contract_validation.BIGQUERY_AVAILABLE", False):
            v._detect_and_validate_provider()
        errors = v.report.get_errors()
        assert any("bigquery" in e.message.lower() or "gcp" in e.message.lower() for e in errors)

    def test_snowflake_unavailable_adds_error(self):
        v = _make_validator_new(provider_name="snowflake")
        v.contract = {"exposes": [{"binding": {"platform": "snowflake"}}]}
        with patch("fluid_build.cli.contract_validation.SNOWFLAKE_VALIDATION_AVAILABLE", False):
            v._detect_and_validate_provider()
        errors = v.report.get_errors()
        assert any("snowflake" in e.message.lower() for e in errors)

    def test_aws_unavailable_adds_error(self):
        v = _make_validator_new(provider_name="aws")
        v.contract = {"exposes": [{"binding": {"platform": "aws"}}]}
        with patch("fluid_build.cli.contract_validation.AWS_VALIDATION_AVAILABLE", False):
            v._detect_and_validate_provider()
        errors = v.report.get_errors()
        assert any("aws" in e.message.lower() or "boto3" in e.message.lower() for e in errors)

    def test_local_unavailable_adds_error(self):
        v = _make_validator_new(provider_name="local")
        v.contract = {"exposes": [{"binding": {"platform": "local"}}]}
        with patch("fluid_build.cli.contract_validation.LOCAL_VALIDATION_AVAILABLE", False):
            v._detect_and_validate_provider()
        errors = v.report.get_errors()
        assert any("local" in e.message.lower() or "duckdb" in e.message.lower() for e in errors)

    def test_provider_detected_from_builds(self):
        v = _make_validator_new(provider_name=None)
        v.contract = {"builds": [{"execution": {"runtime": {"platform": "snowflake"}}}]}
        with patch("fluid_build.cli.contract_validation.SNOWFLAKE_VALIDATION_AVAILABLE", False):
            v._detect_and_validate_provider()
        assert v.provider_name == "snowflake"

    def test_connection_validation_failure_adds_error(self):
        v = _make_validator_new(provider_name="local")
        v.contract = {"exposes": [{"binding": {"platform": "local"}}]}

        mock_provider = MagicMock()
        mock_provider.validate_connection.return_value = False

        with patch("fluid_build.cli.contract_validation.LOCAL_VALIDATION_AVAILABLE", True):
            with patch(
                "fluid_build.cli.contract_validation.LocalValidationProvider",
                return_value=mock_provider,
            ):
                v._detect_and_validate_provider()

        errors = v.report.get_errors()
        assert any("connection" in e.category or "connect" in e.message.lower() for e in errors)

    def test_connection_exception_adds_error(self):
        v = _make_validator_new(provider_name="local")
        v.contract = {"exposes": [{"binding": {"platform": "local"}}]}

        mock_provider = MagicMock()
        mock_provider.validate_connection.side_effect = RuntimeError("network error")

        with patch("fluid_build.cli.contract_validation.LOCAL_VALIDATION_AVAILABLE", True):
            with patch(
                "fluid_build.cli.contract_validation.LocalValidationProvider",
                return_value=mock_provider,
            ):
                v._detect_and_validate_provider()

        errors = v.report.get_errors()
        assert any("connection" in e.category for e in errors)


class TestValidateBigQueryResourceExt(unittest.TestCase):
    def test_no_provider_adds_warning(self):
        v = _make_validator_new(provider_name="gcp")
        v.validation_provider = None
        v._validate_bigquery_resource({"id": "tbl1"}, "path", {})
        warnings = v.report.get_warnings()
        assert any("No validation provider" in w.message for w in warnings)

    def test_incomplete_props_adds_error(self):
        v = _make_validator_new(provider_name="gcp")
        v.validation_provider = MagicMock()
        v._validate_bigquery_resource({"id": "tbl1"}, "path", {"project": "proj"})
        errors = v.report.get_errors()
        assert any("Incomplete BigQuery" in e.message for e in errors)

    def test_full_props_calls_validate_resource(self):
        v = _make_validator_new(provider_name="gcp")
        mock_provider = MagicMock()
        mock_result = MagicMock()
        mock_result.issues = []
        mock_result.success = True
        mock_provider.get_resource_schema.return_value = {}
        mock_provider.validate_resource.return_value = mock_result
        v.validation_provider = mock_provider

        v._validate_bigquery_resource(
            {"id": "tbl1"}, "path", {"project": "proj", "dataset": "ds", "table": "tbl"}
        )
        mock_provider.validate_resource.assert_called_once()

    def test_exception_adds_error(self):
        v = _make_validator_new(provider_name="gcp")
        mock_provider = MagicMock()
        mock_provider.get_resource_schema.side_effect = RuntimeError("BQ error")
        v.validation_provider = mock_provider

        v._validate_bigquery_resource(
            {"id": "tbl1"}, "path", {"project": "proj", "dataset": "ds", "table": "tbl"}
        )
        errors = v.report.get_errors()
        assert any("Failed to validate resource" in e.message for e in errors)

    def test_drift_detection_on_degradation(self):
        v = _make_validator_new(provider_name="gcp")
        v.check_drift = True

        mock_provider = MagicMock()
        mock_result = MagicMock()
        mock_result.issues = []
        mock_result.success = True
        mock_provider.get_resource_schema.return_value = {}
        mock_provider.validate_resource.return_value = mock_result
        v.validation_provider = mock_provider

        mock_history = MagicMock()
        mock_history.detect_drift.return_value = {
            "drift_detected": True,
            "type": "degradation",
            "message": "Errors increased",
            "previous_errors": 0,
            "current_errors": 3,
        }
        v.history = mock_history

        v._validate_bigquery_resource(
            {"id": "tbl1"}, "path", {"project": "proj", "dataset": "ds", "table": "tbl"}
        )
        drift_warnings = [w for w in v.report.get_warnings() if "drift" in w.message.lower()]
        assert len(drift_warnings) > 0


class TestValidateGenericResourceExt(unittest.TestCase):
    def test_no_provider_adds_warning(self):
        v = _make_validator_new(provider_name="snowflake")
        v.validation_provider = None
        v._validate_generic_resource({"id": "t1"}, "path")
        warnings = v.report.get_warnings()
        assert any("No validation provider" in w.message for w in warnings)

    def test_with_provider_calls_validate_resource(self):
        v = _make_validator_new(provider_name="snowflake")
        mock_provider = MagicMock()
        mock_result = MagicMock()
        mock_result.issues = []
        mock_result.success = True
        mock_provider.get_resource_schema.return_value = {}
        mock_provider.validate_resource.return_value = mock_result
        v.validation_provider = mock_provider

        v._validate_generic_resource({"id": "t1"}, "path")
        mock_provider.validate_resource.assert_called_once()

    def test_exception_adds_error(self):
        v = _make_validator_new(provider_name="aws")
        mock_provider = MagicMock()
        mock_provider.get_resource_schema.side_effect = RuntimeError("AWS error")
        v.validation_provider = mock_provider

        v._validate_generic_resource({"id": "t1"}, "path")
        errors = v.report.get_errors()
        assert any("Failed to validate" in e.message for e in errors)

    def test_new_issues_drift_detected(self):
        v = _make_validator_new(provider_name="aws")
        v.check_drift = True

        mock_provider = MagicMock()
        mock_result = MagicMock()
        mock_result.issues = []
        mock_result.success = True
        mock_provider.get_resource_schema.return_value = {}
        mock_provider.validate_resource.return_value = mock_result
        v.validation_provider = mock_provider

        mock_history = MagicMock()
        mock_history.detect_drift.return_value = {
            "drift_detected": True,
            "type": "new_issues",
            "message": "New categories",
            "new_categories": ["type_mismatch"],
        }
        v.history = mock_history

        v._validate_generic_resource({"id": "t1"}, "path")
        drift_warnings = [w for w in v.report.get_warnings() if w.category == "drift"]
        assert len(drift_warnings) > 0

    def test_cache_used_when_enabled(self):
        v = _make_validator_new(provider_name="snowflake")
        v.use_cache = True
        mock_cache = MagicMock()
        mock_cache.get_schema.return_value = {"col1": "string"}
        v.cache = mock_cache

        mock_provider = MagicMock()
        mock_result = MagicMock()
        mock_result.issues = []
        mock_result.success = True
        mock_provider.validate_resource.return_value = mock_result
        v.validation_provider = mock_provider

        v._validate_generic_resource({"id": "t1"}, "path")
        mock_provider.get_resource_schema.assert_not_called()


class TestBuildSnowflakeConfigExt(unittest.TestCase):
    def test_env_vars_used(self):
        v = _make_validator_new(provider_name="snowflake")
        v.contract = {}
        v.server = None
        with patch.dict(
            "os.environ",
            {"SNOWFLAKE_ACCOUNT": "my-acct", "SNOWFLAKE_USER": "user1", "SNOWFLAKE_PASSWORD": "pw"},
            clear=True,
        ):
            config = v._build_snowflake_config()
        assert config["account"] == "my-acct"
        assert config["user"] == "user1"

    def test_server_overrides_account(self):
        v = _make_validator_new(provider_name="snowflake")
        v.contract = {}
        v.server = "override-account"
        with patch.dict("os.environ", {}, clear=False):
            config = v._build_snowflake_config()
        assert config["account"] == "override-account"

    def test_contract_binding_extracted(self):
        v = _make_validator_new(provider_name="snowflake")
        v.contract = {
            "exposes": [
                {
                    "binding": {
                        "platform": "snowflake",
                        "location": {"account": "c-acct", "database": "mydb", "schema": "mysch"},
                    }
                }
            ]
        }
        v.server = None
        with patch.dict(
            "os.environ",
            {"SNOWFLAKE_ACCOUNT": "", "SNOWFLAKE_USER": "", "SNOWFLAKE_PASSWORD": ""},
            clear=False,
        ):
            config = v._build_snowflake_config()
        assert config["database"] == "mydb"
        assert config["schema"] == "mysch"


class TestOutputFunctionsExt(unittest.TestCase):
    def _make_full_report(self, valid=True):
        from fluid_build.cli.contract_validation import ValidationReport

        r = ValidationReport(
            contract_path="/tmp/c.yaml",
            contract_id="dp-test",
            contract_version="1.0.0",
            validation_time=datetime.now(),
            duration=0.1,
        )
        r.exposes_validated = 1
        r.checks_passed = 3
        if not valid:
            r.add_issue("error", "schema", "bad schema", "")
        return r

    def test_output_text_report_no_exception(self):
        from fluid_build.cli.contract_validation import output_text_report

        report = self._make_full_report(valid=True)
        try:
            output_text_report(report, None)
        except Exception as e:
            self.fail(f"output_text_report raised unexpectedly: {e}")

    def test_output_json_report_to_stdout(self):
        import json

        from fluid_build.cli.contract_validation import output_json_report

        report = self._make_full_report(valid=True)
        captured = []

        def capture_cprint(text=""):
            captured.append(str(text))

        with patch("fluid_build.cli.contract_validation.cprint", side_effect=capture_cprint):
            output_json_report(report, None)
        out_text = "".join(captured)
        data = json.loads(out_text)
        assert data["contract_id"] == "dp-test"

    def test_output_json_report_to_file(self):
        import json

        from fluid_build.cli.contract_validation import output_json_report

        report = self._make_full_report(valid=True)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            out_path = f.name

        with patch("fluid_build.cli.contract_validation.cprint"):
            output_json_report(report, out_path)

        with open(out_path) as fh:
            data = json.load(fh)
        assert data["contract_id"] == "dp-test"

    def test_output_plain_report_no_exception(self):
        from fluid_build.cli.contract_validation import output_plain_report

        report = self._make_full_report(valid=True)
        with patch("fluid_build.cli.contract_validation.cprint"):
            try:
                output_plain_report(report, None)
            except Exception as e:
                self.fail(f"output_plain_report raised: {e}")


class TestRunExt(unittest.TestCase):
    def test_run_cache_stats_returns_0(self):
        from fluid_build.cli.contract_validation import run

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            f.write(b"id: test\nversion: 1.0.0\n")
            contract_path = f.name

        args = _make_run_args(contract=contract_path, cache_stats=True, cache_ttl=3600)
        mock_stats = {
            "total_entries": 2,
            "fresh_entries": 1,
            "stale_entries": 1,
            "total_size_bytes": 512,
            "cache_dir": "/tmp",
            "ttl_seconds": 3600,
        }
        with (
            patch("fluid_build.cli.contract_validation.ValidationCache") as MockCache,
            patch("fluid_build.cli.contract_validation.cprint"),
        ):
            MockCache.return_value.get_cache_stats.return_value = mock_stats
            result = run(args, MagicMock())
        assert result == 0

    def test_run_strict_warnings_returns_1(self):
        from fluid_build.cli.contract_validation import ValidationReport, run

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            f.write(b"id: test\n")
            contract_path = f.name

        args = _make_run_args(contract=contract_path, strict=True)

        mock_report = ValidationReport(
            contract_path=contract_path,
            contract_id="t",
            contract_version="1.0",
            validation_time=datetime.now(),
            duration=0.0,
        )
        mock_report.add_issue("warning", "quality", "some warn", "")

        with (
            patch("fluid_build.cli.contract_validation.cprint"),
            patch("fluid_build.cli.contract_validation.console_error"),
            patch("fluid_build.cli.contract_validation.output_text_report"),
            patch(
                "fluid_build.cli.contract_validation.ContractValidator",
                return_value=MagicMock(validate=MagicMock(return_value=mock_report)),
            ),
        ):
            result = run(args, MagicMock())
        assert result == 1

    def test_run_json_output_format(self):
        from fluid_build.cli.contract_validation import ValidationReport, run

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            f.write(b"id: test\n")
            contract_path = f.name

        args = _make_run_args(contract=contract_path, output_format="json")

        mock_report = ValidationReport(
            contract_path=contract_path,
            contract_id="t",
            contract_version="1.0",
            validation_time=datetime.now(),
            duration=0.0,
        )

        with (
            patch("fluid_build.cli.contract_validation.cprint"),
            patch("fluid_build.cli.contract_validation.output_json_report") as mock_json,
            patch(
                "fluid_build.cli.contract_validation.ContractValidator",
                return_value=MagicMock(validate=MagicMock(return_value=mock_report)),
            ),
        ):
            run(args, MagicMock())
        mock_json.assert_called_once()

    def test_run_validator_exception_returns_1(self):
        from fluid_build.cli.contract_validation import run

        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            f.write(b"id: test\n")
            contract_path = f.name

        args = _make_run_args(contract=contract_path)
        with (
            patch("fluid_build.cli.contract_validation.cprint"),
            patch("fluid_build.cli.contract_validation.console_error"),
            patch(
                "fluid_build.cli.contract_validation.ContractValidator",
                return_value=MagicMock(validate=MagicMock(side_effect=RuntimeError("boom"))),
            ),
        ):
            result = run(args, MagicMock())
        assert result == 1


class TestValidateContractSchemaExt(unittest.TestCase):
    def test_schema_exception_adds_error(self):
        v = _make_validator_new()
        v.contract = {}
        with patch(
            "fluid_build.cli.contract_validation.FluidSchemaManager",
            side_effect=RuntimeError("schema fail"),
        ):
            v._validate_contract_schema()
        errors = v.report.get_errors()
        assert any("Schema validation failed" in e.message for e in errors)

    def test_schema_warnings_added(self):
        v = _make_validator_new()
        v.contract = {}
        mock_sm = MagicMock()
        mock_result = MagicMock()
        mock_result.is_valid = True
        mock_result.errors = []
        mock_result.warnings = ["Deprecated field"]
        mock_sm.validate_contract.return_value = mock_result
        with patch("fluid_build.cli.contract_validation.FluidSchemaManager", return_value=mock_sm):
            v._validate_contract_schema()
        warnings = v.report.get_warnings()
        assert any("Deprecated field" in w.message for w in warnings)


class TestRunExposeQualityChecksExt(unittest.TestCase):
    def test_quality_checks_adds_issues(self):
        from fluid_build.cli.contract_validation import ValidationIssue

        v = _make_validator_new()
        mock_provider = MagicMock()
        mock_issue = ValidationIssue(
            severity="warning",
            category="quality",
            message="Row count low",
            path="rules[0]",
        )
        mock_provider.run_quality_checks.return_value = [mock_issue]
        v.validation_provider = mock_provider

        expose = {"id": "t1", "type": "table"}
        rules = [{"name": "row_count", "query": "SELECT COUNT(*)"}]
        v._run_expose_quality_checks(expose, rules, "exposes[0]")

        warnings = v.report.get_warnings()
        assert any("Row count low" in w.message for w in warnings)

    def test_quality_checks_exception_adds_warning(self):
        v = _make_validator_new()
        mock_provider = MagicMock()
        mock_provider.run_quality_checks.side_effect = RuntimeError("DQ fail")
        v.validation_provider = mock_provider

        v._run_expose_quality_checks({"id": "t1"}, [{"name": "x"}], "exposes[0]")
        warnings = v.report.get_warnings()
        assert any("Quality check execution failed" in w.message for w in warnings)
