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

"""Tests for fluid_build.cli.contract_validation — data classes and validation logic."""

from datetime import datetime

from fluid_build.cli.contract_validation import ContractValidator, ValidationIssue, ValidationReport

# ── ValidationIssue ──


class TestValidationIssue:
    def test_basic_str(self):
        vi = ValidationIssue(
            severity="error",
            category="schema",
            message="Missing field",
            path="exposes[0].id",
        )
        s = str(vi)
        assert "[ERROR]" in s
        assert "schema" in s
        assert "Missing field" in s
        assert "Path: exposes[0].id" in s

    def test_str_with_expected_actual(self):
        vi = ValidationIssue(
            severity="warning",
            category="binding",
            message="Type mismatch",
            path="x",
            expected="STRING",
            actual="INT64",
        )
        s = str(vi)
        assert "Expected: STRING" in s
        assert "Actual: INT64" in s

    def test_str_with_suggestion_and_docs(self):
        vi = ValidationIssue(
            severity="info",
            category="quality",
            message="Consider SLA",
            path="",
            suggestion="Add SLA block",
            documentation_url="https://docs.example.com",
        )
        s = str(vi)
        assert "Suggestion:" in s
        assert "Docs:" in s

    def test_str_without_optional_fields(self):
        vi = ValidationIssue(severity="error", category="meta", message="m", path="")
        s = str(vi)
        assert "Expected" not in s
        assert "Suggestion" not in s


# ── ValidationReport ──


class TestValidationReport:
    def _make_report(self, **overrides):
        defaults = dict(
            contract_path="/tmp/c.yaml",
            contract_id="my-product",
            contract_version="1.0.0",
            validation_time=datetime.now(),
            duration=0.5,
        )
        defaults.update(overrides)
        return ValidationReport(**defaults)

    def test_initial_state(self):
        r = self._make_report()
        assert r.issues == []
        assert r.checks_passed == 0
        assert r.checks_failed == 0
        assert r.is_valid() is True

    def test_add_error(self):
        r = self._make_report()
        r.add_issue("error", "schema", "bad field", path="x")
        assert r.checks_failed == 1
        assert r.checks_passed == 0
        assert r.is_valid() is False

    def test_add_warning_counts_as_passed(self):
        r = self._make_report()
        r.add_issue("warning", "binding", "not ideal", path="y")
        assert r.checks_passed == 1
        assert r.checks_failed == 0
        assert r.is_valid() is True

    def test_get_errors(self):
        r = self._make_report()
        r.add_issue("error", "a", "e1", path="")
        r.add_issue("warning", "b", "w1", path="")
        r.add_issue("error", "c", "e2", path="")
        assert len(r.get_errors()) == 2

    def test_get_warnings(self):
        r = self._make_report()
        r.add_issue("warning", "a", "w1", path="")
        r.add_issue("info", "b", "i1", path="")
        assert len(r.get_warnings()) == 1

    def test_get_summary_valid(self):
        r = self._make_report()
        r.exposes_validated = 2
        r.consumes_validated = 1
        summary = r.get_summary()
        assert "VALID" in summary
        assert "2 exposed" in summary or "2" in summary
        assert "1" in summary  # consumed

    def test_get_summary_invalid(self):
        r = self._make_report()
        r.add_issue("error", "x", "fail", path="")
        summary = r.get_summary()
        assert "INVALID" in summary
        assert "1 error" in summary or "1 check" in summary

    def test_add_issue_all_fields(self):
        r = self._make_report()
        r.add_issue(
            severity="error",
            category="schema",
            message="mismatch",
            path="p",
            expected="A",
            actual="B",
            suggestion="fix it",
            documentation_url="https://example.com",
        )
        issue = r.issues[0]
        assert issue.expected == "A"
        assert issue.actual == "B"
        assert issue.suggestion == "fix it"
        assert issue.documentation_url == "https://example.com"

    def test_multiple_errors_and_warnings_counting(self):
        r = self._make_report()
        for _ in range(3):
            r.add_issue("error", "x", "e", path="")
        for _ in range(5):
            r.add_issue("warning", "y", "w", path="")
        assert r.checks_failed == 3
        assert r.checks_passed == 5


# ── ContractValidator private method tests ──


class TestValidateBinding:
    """Test _validate_binding — pure dict validation."""

    def _make_validator(self, provider_name="gcp"):
        v = ContractValidator.__new__(ContractValidator)
        v.provider_name = provider_name
        v.report = ValidationReport(
            contract_path="/tmp/c.yaml",
            contract_id="test",
            contract_version="1.0.0",
            validation_time=datetime.now(),
            duration=0,
        )
        return v

    def test_missing_platform(self):
        v = self._make_validator()
        v._validate_binding({}, "path", "e1")
        errors = [i for i in v.report.issues if i.severity == "error"]
        assert any("platform" in i.message for i in errors)

    def test_missing_location(self):
        v = self._make_validator()
        v._validate_binding({"platform": "gcp"}, "path", "e1")
        errors = [i for i in v.report.issues if i.severity == "error"]
        assert any("location" in i.message for i in errors)

    def test_missing_format_warning(self):
        v = self._make_validator()
        v._validate_binding(
            {"platform": "gcp", "location": {"properties": {}}},
            "path",
            "e1",
        )
        warnings = v.report.get_warnings()
        assert any("format" in w.message for w in warnings)

    def test_gcp_missing_required_props(self):
        v = self._make_validator("gcp")
        v._validate_binding(
            {"platform": "gcp", "location": {"format": "table", "properties": {}}},
            "path",
            "e1",
        )
        errors = v.report.get_errors()
        # Should flag missing project, dataset, table
        assert len(errors) >= 1

    def test_valid_binding_no_errors(self):
        v = self._make_validator("aws")
        v._validate_binding(
            {"platform": "aws", "location": {"format": "table", "properties": {"bucket": "x"}}},
            "path",
            "e1",
        )
        assert len(v.report.get_errors()) == 0


class TestValidateSchemaDefinition:
    def _make_validator(self):
        v = ContractValidator.__new__(ContractValidator)
        v.provider_name = "gcp"
        v.report = ValidationReport(
            contract_path="/tmp/c.yaml",
            contract_id="test",
            contract_version="1.0.0",
            validation_time=datetime.now(),
            duration=0,
        )
        return v

    def test_empty_schema_warning(self):
        v = self._make_validator()
        v._validate_schema_definition([], "path", "e1")
        assert len(v.report.get_warnings()) >= 1

    def test_missing_name(self):
        v = self._make_validator()
        v._validate_schema_definition([{"type": "STRING"}], "path", "e1")
        errors = v.report.get_errors()
        assert any("name" in i.message for i in errors)

    def test_missing_type(self):
        v = self._make_validator()
        v._validate_schema_definition([{"name": "col"}], "path", "e1")
        errors = v.report.get_errors()
        assert any("type" in i.message for i in errors)

    def test_valid_columns(self):
        v = self._make_validator()
        v._validate_schema_definition(
            [{"name": "id", "type": "INTEGER"}, {"name": "name", "type": "STRING"}],
            "path",
            "e1",
        )
        assert len(v.report.get_errors()) == 0

    def test_nonstandard_type_warning(self):
        v = self._make_validator()
        v._validate_schema_definition(
            [{"name": "x", "type": "SUPERTYPE"}],
            "path",
            "e1",
        )
        warnings = v.report.get_warnings()
        assert any("Non-standard" in w.message for w in warnings)
