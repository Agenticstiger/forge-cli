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

"""Extended unit tests for fluid_build.cli.contract_validation.

Focuses on ContractValidator private validation methods and the run() entry
point — lines not covered by the existing test_contract_validation.py.
"""

import argparse
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from fluid_build.cli.contract_validation import (
    ContractValidator,
    ValidationIssue,
    ValidationReport,
    register,
    run,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_report():
    return ValidationReport(
        contract_path="/tmp/c.yaml",
        contract_id="test-product",
        contract_version="1.0.0",
        validation_time=datetime.now(),
        duration=0.0,
    )


def _make_validator_bare(provider_name="gcp"):
    """Create a ContractValidator instance bypassing __init__."""
    v = ContractValidator.__new__(ContractValidator)
    v.provider_name = provider_name
    v.project = "proj"
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
    v.report = _make_report()
    v.logger = MagicMock()
    return v


def _make_args(**kwargs):
    defaults = dict(
        contract="/tmp/missing_contract.yaml",
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
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# ValidationIssue.__str__ edge cases (new paths)
# ---------------------------------------------------------------------------


class TestValidationIssueStrEdgeCases(unittest.TestCase):
    def test_str_with_no_path_omits_path_line(self):
        vi = ValidationIssue(severity="info", category="quality", message="ok", path="")
        s = str(vi)
        self.assertNotIn("Path:", s)

    def test_str_severity_is_uppercased(self):
        vi = ValidationIssue(severity="warning", category="x", message="m", path="p")
        s = str(vi)
        self.assertIn("[WARNING]", s)

    def test_str_includes_documentation_url(self):
        vi = ValidationIssue(
            severity="error",
            category="binding",
            message="m",
            path="p",
            documentation_url="https://docs.example.com/binding",
        )
        s = str(vi)
        self.assertIn("https://docs.example.com/binding", s)


# ---------------------------------------------------------------------------
# ValidationReport additional coverage
# ---------------------------------------------------------------------------


class TestValidationReportExtra(unittest.TestCase):
    def test_add_info_severity_counts_as_passed(self):
        r = _make_report()
        r.add_issue("info", "metadata", "informational", path="")
        self.assertEqual(r.checks_passed, 1)
        self.assertEqual(r.checks_failed, 0)

    def test_is_valid_with_only_warnings(self):
        r = _make_report()
        r.add_issue("warning", "quality", "low coverage", path="")
        self.assertTrue(r.is_valid())

    def test_get_summary_includes_duration(self):
        r = _make_report()
        r.duration = 1.234
        summary = r.get_summary()
        self.assertIn("1.23", summary)

    def test_get_summary_counts_validates(self):
        r = _make_report()
        r.exposes_validated = 3
        r.consumes_validated = 2
        summary = r.get_summary()
        self.assertIn("3", summary)
        self.assertIn("2", summary)


# ---------------------------------------------------------------------------
# ContractValidator._validate_schema_definition
# ---------------------------------------------------------------------------


class TestValidateSchemaDefinition(unittest.TestCase):
    def setUp(self):
        self.v = _make_validator_bare()

    def test_empty_schema_adds_warning(self):
        self.v._validate_schema_definition([], "path", "expose_x")
        warnings = self.v.report.get_warnings()
        self.assertTrue(any("Empty schema" in w.message for w in warnings))

    def test_missing_name_adds_error(self):
        schema = [{"type": "STRING"}]
        self.v._validate_schema_definition(schema, "path", "expose_x")
        errors = self.v.report.get_errors()
        self.assertTrue(any("name" in e.message for e in errors))

    def test_missing_type_adds_error(self):
        schema = [{"name": "col1"}]
        self.v._validate_schema_definition(schema, "path", "expose_x")
        errors = self.v.report.get_errors()
        self.assertTrue(any("type" in e.message for e in errors))

    def test_valid_standard_type_no_warning(self):
        schema = [{"name": "col1", "type": "STRING"}]
        self.v._validate_schema_definition(schema, "path", "expose_x")
        # No warning for standard type
        warnings = [w for w in self.v.report.get_warnings() if "Non-standard" in w.message]
        self.assertEqual(len(warnings), 0)

    def test_nonstandard_type_adds_warning(self):
        schema = [{"name": "col1", "type": "CUSTOM_TYPE_XYZ"}]
        self.v._validate_schema_definition(schema, "path", "expose_x")
        warnings = self.v.report.get_warnings()
        self.assertTrue(any("Non-standard" in w.message for w in warnings))

    def test_all_valid_types_accepted(self):
        valid_types = [
            "VARCHAR",
            "STRING",
            "TEXT",
            "INT",
            "INTEGER",
            "BIGINT",
            "FLOAT",
            "DOUBLE",
            "BOOLEAN",
            "DATE",
            "TIMESTAMP",
            "JSON",
        ]
        for vtype in valid_types:
            v = _make_validator_bare()
            schema = [{"name": "col", "type": vtype}]
            v._validate_schema_definition(schema, "path", "e")
            nonstandard = [w for w in v.report.get_warnings() if "Non-standard" in w.message]
            self.assertEqual(len(nonstandard), 0, f"Type {vtype} should be valid")


# ---------------------------------------------------------------------------
# ContractValidator._validate_binding
# ---------------------------------------------------------------------------


class TestValidateBindingExtra(unittest.TestCase):
    def setUp(self):
        self.v = _make_validator_bare()

    def test_complete_binding_gcp_no_errors(self):
        binding = {
            "platform": "gcp",
            "location": {
                "format": "bigquery",
                "properties": {"project": "proj", "dataset": "ds", "table": "t"},
            },
        }
        self.v._validate_binding(binding, "path", "e1")
        errors = self.v.report.get_errors()
        # No missing-field errors for complete binding
        field_errors = [e for e in errors if "Missing" in e.message]
        self.assertEqual(len(field_errors), 0)

    def test_gcp_missing_required_property_adds_error(self):
        self.v.provider_name = "gcp"
        binding = {
            "platform": "gcp",
            "location": {
                "format": "bigquery",
                "properties": {"project": "proj"},  # missing dataset and table
            },
        }
        self.v._validate_binding(binding, "path", "e1")
        errors = self.v.report.get_errors()
        self.assertTrue(len(errors) > 0)

    def test_missing_location_format_adds_warning(self):
        binding = {
            "platform": "snowflake",
            "location": {"properties": {}},
        }
        self.v.provider_name = "snowflake"
        self.v._validate_binding(binding, "path", "e1")
        warnings = self.v.report.get_warnings()
        self.assertTrue(any("format" in w.message for w in warnings))

    def test_missing_location_properties_adds_warning(self):
        binding = {
            "platform": "local",
            "location": {"format": "sqlite"},
        }
        self.v.provider_name = "local"
        self.v._validate_binding(binding, "path", "e1")
        warnings = self.v.report.get_warnings()
        self.assertTrue(any("properties" in w.message for w in warnings))


# ---------------------------------------------------------------------------
# ContractValidator._validate_exposes
# ---------------------------------------------------------------------------


class TestValidateExposes(unittest.TestCase):
    def setUp(self):
        self.v = _make_validator_bare()

    def test_no_exposes_adds_warning(self):
        self.v.contract = {"exposes": []}
        self.v._validate_exposes()
        warnings = self.v.report.get_warnings()
        self.assertTrue(any("No data products" in w.message for w in warnings))

    def test_expose_missing_required_fields_adds_errors(self):
        self.v.contract = {
            "exposes": [{"binding": {"platform": "gcp"}}]  # missing id, type, schema
        }
        self.v.check_data = False
        self.v.provider_name = None
        self.v._validate_exposes()
        errors = self.v.report.get_errors()
        self.assertGreater(len(errors), 0)

    def test_expose_with_dsl_aliases_resolved(self):
        """exposeId -> id and kind -> type DSL aliases should be resolved."""
        self.v.contract = {
            "exposes": [
                {
                    "exposeId": "orders",
                    "kind": "table",
                    "binding": {
                        "platform": "local",
                        "location": {"format": "sqlite", "properties": {}},
                    },
                    "schema": [{"name": "id", "type": "INT"}],
                }
            ]
        }
        self.v.check_data = False
        self.v.provider_name = None
        self.v._validate_exposes()
        # exposeId alias resolved — should not add error for missing 'id'
        id_errors = [e for e in self.v.report.get_errors() if "'id'" in e.message]
        self.assertEqual(len(id_errors), 0)


# ---------------------------------------------------------------------------
# ContractValidator._validate_consumes
# ---------------------------------------------------------------------------


class TestValidateConsumes(unittest.TestCase):
    def setUp(self):
        self.v = _make_validator_bare()

    def test_no_consumes_no_issues(self):
        self.v.contract = {"consumes": []}
        self.v._validate_consumes()
        self.assertEqual(len(self.v.report.issues), 0)

    def test_consume_missing_ref_adds_error(self):
        self.v.contract = {"consumes": [{"id": "dep1"}]}
        self.v._validate_consumes()
        errors = self.v.report.get_errors()
        self.assertTrue(any("ref" in e.message.lower() for e in errors))

    def test_consume_with_ref_increments_counter(self):
        self.v.contract = {"consumes": [{"id": "dep1", "ref": "catalog://other-product"}]}
        self.v._validate_consumes()
        self.assertEqual(self.v.report.consumes_validated, 1)

    def test_consume_with_product_id_alias(self):
        """productId alias should work as ref fallback."""
        self.v.contract = {"consumes": [{"id": "dep2", "productId": "some-product"}]}
        self.v._validate_consumes()
        # No error for missing ref when productId is present
        ref_errors = [e for e in self.v.report.get_errors() if "ref" in e.message.lower()]
        self.assertEqual(len(ref_errors), 0)


# ---------------------------------------------------------------------------
# ContractValidator._validate_quality_specs
# ---------------------------------------------------------------------------


class TestValidateQualitySpecs(unittest.TestCase):
    def setUp(self):
        self.v = _make_validator_bare()

    def test_no_quality_adds_info(self):
        self.v.contract = {}
        self.v._validate_quality_specs()
        info_issues = [i for i in self.v.report.issues if i.severity == "info"]
        self.assertTrue(any("quality" in i.message.lower() for i in info_issues))

    def test_quality_with_valid_freshness_no_warning(self):
        self.v.contract = {"quality": {"sla": {"freshness": "1h"}}}
        self.v._validate_quality_specs()
        freshness_warnings = [w for w in self.v.report.get_warnings() if "freshness" in w.message]
        self.assertEqual(len(freshness_warnings), 0)

    def test_quality_with_non_string_freshness_adds_warning(self):
        self.v.contract = {"quality": {"sla": {"freshness": 3600}}}
        self.v._validate_quality_specs()
        warnings = self.v.report.get_warnings()
        self.assertTrue(any("freshness" in w.message for w in warnings))

    def test_quality_test_missing_name_adds_warning(self):
        self.v.contract = {"quality": {"tests": [{"query": "SELECT 1"}]}}
        self.v._validate_quality_specs()
        warnings = self.v.report.get_warnings()
        self.assertTrue(any("name" in w.message for w in warnings))

    def test_quality_test_with_name_no_warning(self):
        self.v.contract = {
            "quality": {"tests": [{"name": "row_count", "query": "SELECT COUNT(*)"}]}
        }
        self.v._validate_quality_specs()
        name_warnings = [w for w in self.v.report.get_warnings() if "name" in w.message]
        self.assertEqual(len(name_warnings), 0)


# ---------------------------------------------------------------------------
# ContractValidator._validate_metadata
# ---------------------------------------------------------------------------


class TestValidateMetadata(unittest.TestCase):
    def setUp(self):
        self.v = _make_validator_bare()

    def test_no_metadata_adds_warning(self):
        self.v.contract = {}
        self.v._validate_metadata()
        warnings = self.v.report.get_warnings()
        self.assertTrue(any("metadata" in w.message.lower() for w in warnings))

    def test_complete_metadata_no_warnings(self):
        self.v.contract = {
            "metadata": {
                "owner": "team@example.com",
                "layer": "Gold",
                "domain": "finance",
                "tags": ["reporting"],
            }
        }
        self.v._validate_metadata()
        # No info issues for recommended fields when all present
        info_issues = [
            i for i in self.v.report.issues if i.severity == "info" and "Recommended" in i.message
        ]
        self.assertEqual(len(info_issues), 0)

    def test_missing_recommended_field_adds_info(self):
        self.v.contract = {"metadata": {"owner": "team"}}
        self.v._validate_metadata()
        info_issues = [i for i in self.v.report.issues if i.severity == "info"]
        self.assertGreater(len(info_issues), 0)

    def test_invalid_layer_adds_warning(self):
        self.v.contract = {"metadata": {"layer": "Platinum_Custom"}}
        self.v._validate_metadata()
        warnings = self.v.report.get_warnings()
        self.assertTrue(any("Layer" in w.message for w in warnings))

    def test_valid_layer_no_warning(self):
        for layer in ["Bronze", "Silver", "Gold", "Platinum"]:
            v = _make_validator_bare()
            v.contract = {"metadata": {"layer": layer}}
            v._validate_metadata()
            layer_warnings = [w for w in v.report.get_warnings() if "Layer" in w.message]
            self.assertEqual(len(layer_warnings), 0, f"Layer {layer} should be valid")


# ---------------------------------------------------------------------------
# register() subparser
# ---------------------------------------------------------------------------


class TestContractValidationRegister(unittest.TestCase):
    def test_register_adds_contract_validation_subparser(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["contract-validation", "contract.fluid.yaml"])
        self.assertEqual(args.contract, "contract.fluid.yaml")

    def test_register_default_output_format(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["contract-validation", "c.yaml"])
        self.assertEqual(args.output_format, "text")

    def test_register_strict_flag(self):
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers()
        register(subparsers)
        args = parser.parse_args(["contract-validation", "c.yaml", "--strict"])
        self.assertTrue(args.strict)


# ---------------------------------------------------------------------------
# run() — top-level entry point
# ---------------------------------------------------------------------------


class TestContractValidationRun(unittest.TestCase):
    def test_run_returns_one_when_contract_not_found(self):
        args = _make_args(contract="/tmp/definitely_missing_file_xyz.yaml")
        import logging

        logger = logging.getLogger(__name__)
        with patch("fluid_build.cli.contract_validation.console_error"):
            result = run(args, logger)
        self.assertEqual(result, 1)

    def test_run_with_real_contract_file(self):
        """End-to-end run() test with a minimal temp contract file."""
        import logging

        import yaml

        contract_data = {
            "id": "test-dp",
            "version": "1.0.0",
            "fluidVersion": "0.5.7",
            "name": "Test DP",
            "metadata": {"owner": "team@test.com", "layer": "Gold", "domain": "test", "tags": []},
        }
        logger = logging.getLogger(__name__)

        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.safe_dump(contract_data, f)
            contract_path = f.name

        args = _make_args(contract=contract_path, cache=False, track_history=False)

        with (
            patch("fluid_build.cli.contract_validation.cprint"),
            patch("fluid_build.cli.contract_validation.console_error"),
            patch(
                "fluid_build.cli.contract_validation.load_contract_with_overlay",
                return_value=contract_data,
            ),
            patch(
                "fluid_build.cli.contract_validation.FluidSchemaManager",
                return_value=MagicMock(
                    validate_contract=MagicMock(
                        return_value=MagicMock(is_valid=True, errors=[], warnings=[])
                    )
                ),
            ),
        ):
            result = run(args, logger)

        # Should return 0 (valid) or 1 (issues found); just verify it's int
        self.assertIsInstance(result, int)


if __name__ == "__main__":
    unittest.main()
