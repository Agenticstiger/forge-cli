# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""ContractValidator: Databricks provider wiring, ``schema`` dq rules decided from the
live column comparison, and CheckOutcomes that carry rule id/type/expose id."""

from __future__ import annotations

from datetime import datetime

import pytest

from fluid_build.cli import contract_validation as cv
from fluid_build.providers.quality_engine import QualityCheckResult
from fluid_build.providers.validation_provider import (
    FieldSchema,
    ResourceSchema,
    ResourceType,
    ValidationProvider,
    ValidationResult,
)

CONTRACT_COLUMNS = [
    {"name": "a", "type": "string"},
    {"name": "b", "type": "int"},
]


class FakeProvider(ValidationProvider):
    """Deterministic provider: canned live schema, canned quality results."""

    def __init__(self, live=None, results="unset", issues=None, schema_error=None):
        super().__init__({})
        self.live = live
        self.results = results
        self.issues = issues or []
        self.schema_error = schema_error
        self.quality_calls = []

    @property
    def provider_name(self):
        return "databricks"

    def validate_connection(self):
        return True

    def get_resource_schema(self, resource_spec):
        if self.schema_error:
            raise self.schema_error
        return self.live

    def validate_resource(self, contract_spec, actual_schema):
        if actual_schema is None:
            from fluid_build.providers.validation_provider import ValidationIssue

            return ValidationResult(
                "x",
                False,
                [
                    ValidationIssue(
                        severity="error",
                        category="missing_resource",
                        message="Table does not exist",
                        path="p",
                    )
                ],
                None,
            )
        expected = [FieldSchema(c["name"], c["type"], "NULLABLE") for c in contract_spec["schema"]]
        issues = self.compare_schemas(expected, actual_schema.fields)
        return ValidationResult("x", not issues, issues, actual_schema)

    def normalize_type(self, provider_type):
        return provider_type.lower()

    def run_quality_checks(self, resource_spec, rules):
        self.quality_calls.append(("checks", rules))
        return self.issues

    def run_quality_results(self, resource_spec, rules):
        self.quality_calls.append(("results", rules))
        return None if self.results == "unset" else self.results


def live(*cols):
    return ResourceSchema(
        ResourceType.TABLE, "c.s.t", [FieldSchema(n, t, "NULLABLE") for n, t in cols], row_count=1
    )


def make_validator(provider, tmp_path):
    v = cv.ContractValidator(
        tmp_path / "contract.fluid.yaml",
        provider_name="databricks",
        use_cache=False,
        track_history=False,
    )
    v.report = cv.ValidationReport(
        contract_path="x",
        contract_id="id",
        contract_version="1",
        validation_time=datetime.now(),
        duration=0.0,
    )
    v.validation_provider = provider
    return v


def expose(rules):
    return {
        "id": "e1",
        "type": "table",
        "binding": {
            "platform": "databricks",
            "location": {"database": "c", "schema": "s", "table": "t"},
        },
        "schema": CONTRACT_COLUMNS,
        "contract": {"dq": {"rules": rules}},
    }


SCHEMA_RULE = {"id": "conforms", "type": "schema", "selector": "*", "severity": "error"}


def quality_issues(v):
    return [i for i in v.report.issues if i.category == "quality"]


def check(v, name):
    (c,) = [c for c in v.report.checks if c.name == name]
    return c


# ------------------------------------------------------------- provider wiring


def detect(tmp_path, monkeypatch, available):
    v = cv.ContractValidator(tmp_path / "c.yaml", use_cache=False, track_history=False)
    v.contract = {"exposes": [{"binding": {"platform": "databricks"}}]}
    v.report = cv.ValidationReport("x", "id", "1", datetime.now(), 0.0)
    monkeypatch.setattr(cv, "DATABRICKS_VALIDATION_AVAILABLE", available)
    v._detect_and_validate_provider()
    return v


def test_databricks_provider_is_built_when_sdk_available(tmp_path, monkeypatch):
    from fluid_build.providers.databricks_validation import DatabricksValidationProvider

    v = detect(tmp_path, monkeypatch, True)
    assert isinstance(v.validation_provider, DatabricksValidationProvider)
    assert [i for i in v.report.issues if i.category == "binding"] == []


def test_databricks_without_sdk_is_binding_error_naming_sdk(tmp_path, monkeypatch):
    v = detect(tmp_path, monkeypatch, False)
    assert v.validation_provider is None
    (issue,) = v.report.issues
    assert issue.severity == "error" and issue.category == "binding"
    assert "databricks-sdk" in issue.message


# --------------------------------------------------- schema rules are not sent


def test_schema_rules_are_not_sent_to_the_provider(tmp_path):
    other = {"id": "c1", "type": "completeness", "selector": "a"}
    p = FakeProvider(live=live(("a", "string"), ("b", "int")), results=[])
    v = make_validator(p, tmp_path)
    v._validate_single_expose(expose([SCHEMA_RULE, other]), 0)
    assert p.quality_calls and all(
        [r["id"] for r in rules] == ["c1"] for _, rules in p.quality_calls
    )


def test_only_schema_rules_means_no_provider_quality_call(tmp_path):
    p = FakeProvider(live=live(("a", "string"), ("b", "int")))
    v = make_validator(p, tmp_path)
    v._validate_single_expose(expose([SCHEMA_RULE]), 0)
    assert p.quality_calls == []


def test_schema_rule_passes_when_columns_match(tmp_path):
    v = make_validator(FakeProvider(live=live(("a", "string"), ("b", "int"))), tmp_path)
    v._validate_single_expose(expose([SCHEMA_RULE]), 0)
    c = check(v, "e1.conforms")
    assert c.passed and c.rule_type == "schema" and c.rule_id == "conforms" and c.expose_id == "e1"
    assert c.actual == "all contract columns match" and quality_issues(v) == []


def test_schema_rule_ignores_extra_live_column(tmp_path):
    v = make_validator(
        FakeProvider(live=live(("a", "string"), ("b", "int"), ("extra", "string"))), tmp_path
    )
    v._validate_single_expose(expose([SCHEMA_RULE]), 0)
    assert check(v, "e1.conforms").passed


def test_schema_rule_fails_on_missing_column_at_rule_severity(tmp_path):
    rule = {**SCHEMA_RULE, "severity": "warning"}
    v = make_validator(FakeProvider(live=live(("a", "string"))), tmp_path)
    v._validate_single_expose(expose([rule]), 0)
    c = check(v, "e1.conforms")
    assert not c.passed and c.severity == "warning"
    assert c.actual == "1 column(s) missing, 0 type change(s)"
    (issue,) = quality_issues(v)
    assert issue.severity == "warning" and issue.path.endswith("dq.rules.conforms")


def test_schema_rule_fails_on_type_change(tmp_path):
    v = make_validator(FakeProvider(live=live(("a", "string"), ("b", "bigint"))), tmp_path)
    v._validate_single_expose(expose([SCHEMA_RULE]), 0)
    c = check(v, "e1.conforms")
    assert not c.passed and c.actual == "0 column(s) missing, 1 type change(s)"
    assert quality_issues(v)[0].severity == "error"


def test_schema_rule_fails_when_table_missing(tmp_path):
    v = make_validator(FakeProvider(live=None), tmp_path)
    v._validate_single_expose(expose([SCHEMA_RULE]), 0)
    c = check(v, "e1.conforms")
    assert not c.passed and c.actual == "table does not exist"


def test_schema_rule_not_executed_when_comparison_did_not_run(tmp_path):
    p = FakeProvider(schema_error=RuntimeError("no network"))
    v = make_validator(p, tmp_path)
    v._validate_single_expose(expose([SCHEMA_RULE]), 0)
    c = check(v, "e1.conforms")
    assert not c.passed and c.actual == "not executed"


def test_schema_rule_not_decided_without_check_data(tmp_path):
    v = make_validator(FakeProvider(live=live(("a", "string"), ("b", "int"))), tmp_path)
    v.check_data = False
    v._validate_single_expose(expose([SCHEMA_RULE]), 0)
    assert v.report.checks == []


# ------------------------------------------------- outcomes for passing rules


def test_provider_results_list_gives_passing_outcomes_expected_and_actual(tmp_path):
    results = [
        QualityCheckResult("c1", "completeness", "a", True, "error", "ok", "1.0", "1.0000"),
        QualityCheckResult("u1", "uniqueness", "a", False, "warning", "dupes", "1.0", "0.5000"),
    ]
    rules = [
        {"id": "c1", "type": "completeness", "selector": "a"},
        {"id": "u1", "type": "uniqueness", "selector": "a", "severity": "warning"},
    ]
    p = FakeProvider(live=live(("a", "string"), ("b", "int")), results=results)
    v = make_validator(p, tmp_path)
    v._validate_single_expose(expose(rules), 0)
    ok = check(v, "e1.c1")
    assert ok.passed and (ok.expected, ok.actual) == ("1.0", "1.0000")
    assert (ok.rule_id, ok.rule_type, ok.expose_id) == ("c1", "completeness", "e1")
    bad = check(v, "e1.u1")
    assert not bad.passed and bad.actual == "0.5000" and bad.severity == "warning"
    assert [i.path for i in quality_issues(v)] == ["exposes[0].contract.dq.rules.u1"]


def test_results_none_falls_back_to_run_quality_checks_and_still_tags_outcomes(tmp_path):
    rules = [{"id": "c1", "type": "completeness", "selector": "a"}]
    p = FakeProvider(live=live(("a", "string"), ("b", "int")), results="unset")
    v = make_validator(p, tmp_path)
    v._validate_single_expose(expose(rules), 0)
    assert [k for k, _ in p.quality_calls] == ["results", "checks"]
    c = check(v, "e1.c1")
    assert c.passed and (c.rule_id, c.rule_type, c.expose_id) == ("c1", "completeness", "e1")


def test_provider_exception_becomes_issue_not_crash(tmp_path):
    class Boom(FakeProvider):
        def run_quality_results(self, resource_spec, rules):
            raise RuntimeError("warehouse down")

    v = make_validator(Boom(live=live(("a", "string"), ("b", "int"))), tmp_path)
    v._validate_single_expose(expose([{"id": "c1", "type": "completeness", "selector": "a"}]), 0)
    assert any("warehouse down" in i.message for i in v.report.issues)


@pytest.mark.parametrize("value", ["Schema", " schema "])
def test_rule_kind_normalises_case_and_space(value):
    assert cv._rule_kind({"type": value}) == "schema"
    assert cv._rule_kind("nope") == "" and cv._rule_kind({}) == ""
