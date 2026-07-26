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

"""Regression tests: ``fluid test`` must not report a failure as a pass.

Each test here pins a defect found running ``fluid test`` against a live
Snowflake account, where a genuinely-violated contract exited 0 with a
green table.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fluid_build.cli.contract_validation import CheckOutcome, ContractValidator, ValidationReport
from fluid_build.cli.test import _build_check_rows, _output_junit
from fluid_build.providers.validation_provider import ValidationIssue


def _make_report(**overrides) -> ValidationReport:
    defaults = dict(
        contract_path="c.yaml",
        contract_id="silver.test.v1",
        contract_version="1.0.0",
        validation_time=datetime(2026, 1, 1, 12, 0, 0),
        duration=0.1,
    )
    defaults.update(overrides)
    return ValidationReport(**defaults)


def _make_validator(**kwargs) -> ContractValidator:
    v = ContractValidator(Path("c.yaml"), use_cache=False, **kwargs)
    v.report = _make_report()
    return v


# ---------------------------------------------------------------------------
# Severity vocabulary — 'critical' and 'warn' are schema-legal
# ---------------------------------------------------------------------------


class TestSeverityGating:
    """``$defs.dqRule.severity`` is ['info','warn','error','critical'].

    Classifying with ``== "error"`` excluded the *highest* severity, so a
    failing ``critical`` rule exited 0 and appeared under "checks passed".
    """

    @pytest.mark.parametrize("severity", ["error", "critical", "fatal"])
    def test_error_class_severities_fail_the_report(self, severity):
        r = _make_report()
        r.add_issue(severity, "quality", "gate failed", "q")
        assert r.is_valid() is False
        assert len(r.get_errors()) == 1
        assert r.checks_failed == 1

    @pytest.mark.parametrize("severity", ["warn", "warning"])
    def test_warn_class_severities_are_warnings(self, severity):
        r = _make_report()
        r.add_issue(severity, "quality", "soft gate", "q")
        assert r.is_valid() is True
        assert len(r.get_warnings()) == 1

    def test_strict_sees_schema_warn_severity(self):
        """``--strict`` reads ``get_warnings()``; ``warn`` must land there."""
        r = _make_report()
        r.add_issue("warn", "quality", "soft gate", "q")
        assert r.get_warnings()


# ---------------------------------------------------------------------------
# Check accounting
# ---------------------------------------------------------------------------


class TestCheckAccounting:
    def test_advisory_issues_are_not_passed_checks(self):
        r = _make_report()
        r.add_issue("warning", "binding", "advisory", "b")
        r.add_issue("info", "metadata", "advisory", "m")
        r.add_issue("critical", "quality", "failed", "q")
        # Previously: checks_passed == 2 (the two advisories) and
        # checks_failed == 0 despite the failed critical rule.
        assert r.checks_passed == 0
        assert r.checks_failed == 1

    def test_non_error_rule_failure_still_counts_as_failed(self):
        r = _make_report()
        r.record_check(CheckOutcome(name="e.r1", category="quality", passed=False, severity="warn"))
        assert r.checks_failed == 1
        assert r.checks_passed == 0

    def test_error_rule_failure_counted_once(self):
        r = _make_report()
        r.add_issue("error", "quality", "failed", "q")
        r.record_check(
            CheckOutcome(name="e.r1", category="quality", passed=False, severity="error")
        )
        assert r.checks_failed == 1

    def test_quality_outcomes_recorded_per_rule(self):
        v = _make_validator()
        v.validation_provider = MagicMock()
        v.validation_provider.run_quality_checks.return_value = [
            ValidationIssue(
                severity="critical",
                category="quality",
                message="balance below zero",
                path="contract.dq.rules.balance_non_negative",
                expected=">= 0",
                actual="-998.97",
            )
        ]
        rules = [
            {"id": "balance_non_negative", "type": "accuracy", "severity": "critical"},
            {"id": "id_unique", "type": "uniqueness", "severity": "error"},
        ]
        v._run_expose_quality_checks({"id": "customers"}, rules, "exposes[0]")

        assert v.report.checks_passed == 1
        assert v.report.checks_failed == 1
        assert {c.name: c.passed for c in v.report.checks} == {
            "customers.balance_non_negative": False,
            "customers.id_unique": True,
        }

    def test_unattributable_issues_record_no_passes(self):
        """A provider that could not execute the rules proves nothing.

        Inferring "these rules passed" from a batch that never ran is
        the false claim this accounting exists to prevent.
        """
        v = _make_validator()
        v.validation_provider = MagicMock()
        v.validation_provider.run_quality_checks.return_value = [
            ValidationIssue(
                severity="warning",
                category="quality",
                message="Cannot run quality checks: unable to resolve table reference",
                path="contract.dq.rules",
            )
        ]
        v._run_expose_quality_checks(
            {"id": "customers"}, [{"id": "r1", "type": "accuracy"}], "exposes[0]"
        )
        assert v.report.checks == []
        assert v.report.checks_passed == 0


# ---------------------------------------------------------------------------
# Issue detail plumbing
# ---------------------------------------------------------------------------


class TestQualityIssueDetail:
    def test_expected_actual_and_path_survive(self):
        v = _make_validator()
        v.validation_provider = MagicMock()
        v.validation_provider.run_quality_checks.return_value = [
            ValidationIssue(
                severity="error",
                category="quality",
                message="completeness too low",
                path="contract.dq.rules.phone_complete",
                expected=">= 1.0",
                actual="0.9000",
                suggestion="backfill PHONE",
            )
        ]
        v._run_expose_quality_checks({"id": "customers"}, [{"id": "phone_complete"}], "exposes[0]")
        issue = v.report.issues[0]
        # Previously dropped by a four-positional-arg add_issue call.
        assert issue.expected == ">= 1.0"
        assert issue.actual == "0.9000"
        assert issue.suggestion == "backfill PHONE"
        # Previously "exposes[0].dq.contract.dq.rules.phone_complete".
        assert issue.path == "exposes[0].contract.dq.rules.phone_complete"


# ---------------------------------------------------------------------------
# Cache identity
# ---------------------------------------------------------------------------


class TestResourceCacheKey:
    def test_key_is_the_resource_not_the_expose_id(self):
        """Two contracts sharing an ``exposeId`` must not share a cache entry.

        Keying on ``provider:exposeId`` let a contract bound to a
        nonexistent table validate against another table's cached schema
        and report "Resource exists / All fields match" with exit 0.
        """
        v = _make_validator()
        v.provider_name = "snowflake"

        def _expose(table):
            return {
                "exposeId": "shared_expose",
                "binding": {
                    "platform": "snowflake",
                    "location": {
                        "database": "FLUID_TEST",
                        "schema": "FORGE",
                        "table": table,
                    },
                },
            }

        real = v._resource_cache_key(_expose("ORDERS"), "shared_expose")
        ghost = v._resource_cache_key(_expose("TOTALLY_ABSENT_TABLE"), "shared_expose")
        assert real != ghost
        assert real == "snowflake:FLUID_TEST.FORGE.ORDERS"

    def test_provider_defaults_are_part_of_the_identity(self):
        """Same table name under two databases is two different tables."""
        v = _make_validator()
        v.provider_name = "snowflake"
        expose = {
            "exposeId": "t",
            "binding": {"platform": "snowflake", "location": {"table": "CUSTOMERS"}},
        }

        v.validation_provider = MagicMock(database="DB_A", schema="S", project_id=None)
        key_a = v._resource_cache_key(expose, "t")
        v.validation_provider = MagicMock(database="DB_B", schema="S", project_id=None)
        key_b = v._resource_cache_key(expose, "t")
        assert key_a != key_b

    def test_falls_back_to_expose_id_without_a_location(self):
        v = _make_validator()
        v.provider_name = "snowflake"
        v.validation_provider = MagicMock(database=None, schema=None, project_id=None)
        key = v._resource_cache_key({"exposeId": "t", "binding": {}}, "t")
        assert key == "snowflake:t"


# ---------------------------------------------------------------------------
# Binding warnings must be satisfiable
# ---------------------------------------------------------------------------


class TestBindingWarnings:
    def test_schema_valid_binding_emits_no_warnings(self):
        """``format``/``properties`` are siblings of ``location``.

        ``$defs.bindingLocation`` is ``additionalProperties: false`` and
        defines neither, so checking ``location.format`` /
        ``location.properties`` produced two warnings per expose that no
        contract could ever satisfy — ``--strict`` was unpassable.
        """
        v = _make_validator()
        v.provider_name = "snowflake"
        v._validate_binding(
            {
                "platform": "snowflake",
                "format": "snowflake_table",
                "location": {"database": "D", "schema": "S", "table": "T"},
            },
            "exposes[0].binding",
            "customers",
        )
        assert v.report.issues == []


# ---------------------------------------------------------------------------
# Dead quality / metadata checks
# ---------------------------------------------------------------------------


class TestQualityAndMetadataAdvice:
    def test_no_quality_notice_when_rules_are_declared(self):
        """The root ``quality`` key is not a property of any 0.7.x schema.

        Reporting "No quality specifications defined" off it fired on
        100% of contracts, including runs that had just failed five live
        DQ rules.
        """
        v = _make_validator()
        v.contract = {
            "exposes": [
                {
                    "exposeId": "c",
                    "contract": {"dq": {"rules": [{"id": "r1", "type": "accuracy"}]}},
                }
            ]
        }
        v._validate_quality_specs()
        assert v.report.issues == []

    def test_notice_when_no_rules_anywhere(self):
        v = _make_validator()
        v.contract = {"exposes": [{"exposeId": "c"}]}
        v._validate_quality_specs()
        assert len(v.report.issues) == 1
        assert "No data-quality rules declared" in v.report.issues[0].message

    def test_domain_is_checked_at_the_root(self):
        """``metadata`` forbids ``domain`` — advice to add it there fails validate."""
        v = _make_validator()
        v.contract = {
            "domain": "finance",
            "metadata": {"owner": {"team": "t"}, "layer": "Gold", "tags": ["a"]},
        }
        v._validate_metadata()
        assert [i for i in v.report.issues if "domain" in i.message] == []

    def test_missing_root_domain_is_reported_against_the_root(self):
        v = _make_validator()
        v.contract = {"metadata": {"owner": {"team": "t"}, "layer": "Gold", "tags": ["a"]}}
        v._validate_metadata()
        domain_issues = [i for i in v.report.issues if "domain" in i.message]
        assert len(domain_issues) == 1
        assert domain_issues[0].path == "domain"


# ---------------------------------------------------------------------------
# Renderer: skipped checks must not render as passes
# ---------------------------------------------------------------------------


class TestCheckRows:
    def test_no_data_skips_the_live_checks(self):
        """``--no-data`` runs no resource or field check at all.

        The renderer printed a green tick whenever the matching issue
        list was empty, so "not checked" was indistinguishable from
        "checked and correct" — including for a table that does not exist.
        """
        report = _make_report(data_checks_performed=False, exposes_validated=1)
        rows = {name: status for status, name, _ in _build_check_rows(report)}
        assert rows["Resource exists"] == "skip"
        assert rows["Schema fields"] == "skip"
        assert rows["Row count / SLA"] == "skip"
        assert rows["Quality tests"] == "skip"

    def test_missing_resource_skips_the_field_comparison(self):
        report = _make_report(exposes_validated=1)
        report.add_issue("error", "missing_resource", "Table 'X' does not exist", "b")
        rows = {name: status for status, name, _ in _build_check_rows(report)}
        assert rows["Resource exists"] == "fail"
        assert rows["Schema fields"] == "skip"

    def test_failing_critical_rule_renders_as_a_failure(self):
        report = _make_report(exposes_validated=1)
        report.add_issue("critical", "quality", "balance below zero", "q")
        rows = {name: status for status, name, _ in _build_check_rows(report)}
        assert rows["Quality tests"] == "fail"

    def test_passing_rules_render_as_a_pass(self):
        report = _make_report(exposes_validated=1)
        report.record_check(CheckOutcome(name="c.r1", category="quality", passed=True))
        rows = dict((name, (status, detail)) for status, name, detail in _build_check_rows(report))
        assert rows["Quality tests"][0] == "pass"
        assert "1 rule(s) passed" in rows["Quality tests"][1]


# ---------------------------------------------------------------------------
# JUnit granularity
# ---------------------------------------------------------------------------


class TestJUnitOutput:
    def test_one_testcase_per_rule_including_passes(self, tmp_path, capsys):
        """Folding five failing rules into one ``<testcase name="quality">``
        showed CI a single red test and hid the passing rules entirely."""
        import xml.etree.ElementTree as ET

        report = _make_report()
        report.record_check(CheckOutcome(name="c.r1", category="quality", passed=True))
        report.record_check(
            CheckOutcome(
                name="c.r2",
                category="quality",
                passed=False,
                severity="critical",
                message="balance below zero",
                expected=">= 0",
                actual="-998.97",
            )
        )
        out = tmp_path / "r.xml"
        _output_junit(report, str(out))
        capsys.readouterr()

        ts = ET.parse(out).getroot()
        names = [tc.get("name") for tc in ts.findall("testcase")]
        assert "c.r1" in names
        assert "c.r2" in names
        assert ts.get("failures") == "1"

        failed = [tc for tc in ts.findall("testcase") if tc.get("name") == "c.r2"][0]
        failure = failed.find("failure")
        assert failure is not None
        assert "critical" in failure.get("type")
        assert "-998.97" in failure.text


# ---------------------------------------------------------------------------
# {{ env.* }} resolution
# ---------------------------------------------------------------------------


class TestEnvTemplateResolution:
    """``fluid test`` must resolve ``{{ env.VAR }}`` like every sibling command.

    ``plan`` / ``apply`` / ``verify`` / ``publish`` all resolve them;
    validation did not, so the raw placeholder text was quoted straight
    into a Snowflake identifier and the shipped
    ``examples/snowflake/smoke`` contract reported its own live table as
    ``Table '"{{ env.SNOWFLAKE_DATABASE }}"...' does not exist``.
    """

    def _write(self, tmp_path: Path) -> Path:
        import yaml

        contract = {
            "fluidVersion": "0.7.5",
            "kind": "DataProduct",
            "id": "silver.test.env_v1",
            "name": "env probe",
            "description": "env template probe",
            "domain": "testing",
            "metadata": {"layer": "Silver", "owner": {"team": "t", "email": "t@e.com"}},
            "exposes": [
                {
                    "exposeId": "smoke_table",
                    "kind": "table",
                    "binding": {
                        "platform": "snowflake",
                        "format": "snowflake_table",
                        "location": {
                            "database": "{{ env.SNOWFLAKE_DATABASE }}",
                            "schema": "{{ env.SNOWFLAKE_SCHEMA }}",
                            "table": "SMOKE_TABLE",
                        },
                    },
                }
            ],
        }
        path = tmp_path / "contract.fluid.yaml"
        path.write_text(yaml.safe_dump(contract), encoding="utf-8")
        return path

    def test_binding_location_is_resolved(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SNOWFLAKE_DATABASE", "FLUID_TEST")
        monkeypatch.setenv("SNOWFLAKE_SCHEMA", "FORGE")
        path = self._write(tmp_path)

        v = ContractValidator(path, use_cache=False, check_data=False, track_history=False)
        v.validate()

        location = v.contract["exposes"][0]["binding"]["location"]
        assert location["database"] == "FLUID_TEST"
        assert location["schema"] == "FORGE"

    def test_unset_placeholders_are_left_intact(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SNOWFLAKE_DATABASE", raising=False)
        monkeypatch.setenv("SNOWFLAKE_SCHEMA", "FORGE")
        path = self._write(tmp_path)

        v = ContractValidator(path, use_cache=False, check_data=False, track_history=False)
        v.validate()

        location = v.contract["exposes"][0]["binding"]["location"]
        assert location["database"] == "{{ env.SNOWFLAKE_DATABASE }}"


# ---------------------------------------------------------------------------
# doctor: Snowflake readiness
# ---------------------------------------------------------------------------


class TestDoctorSnowflakeRow:
    """``fluid doctor`` had rows for GCP and AWS and none for Snowflake.

    On an account whose only configured provider is Snowflake it printed
    an unconditional "All critical features available!" carrying no
    information about the provider actually in use — while
    ``contract_validation`` hard-errors without the connector.
    """

    def test_row_is_present_in_the_feature_checks(self):
        from fluid_build.cli.doctor import _check_fluid_features

        _, checks = _check_fluid_features()
        names = [c["check"] for c in checks]
        assert "Snowflake Provider Actions" in names

    def test_missing_connector_is_reported_and_non_fatal(self, monkeypatch):
        import importlib.metadata as md

        from fluid_build.cli import doctor as doctor_mod

        real_version = md.version

        def _fake_version(name):
            if name == "snowflake-connector-python":
                raise md.PackageNotFoundError(name)
            return real_version(name)

        monkeypatch.setattr(md, "version", _fake_version)
        row = doctor_mod._check_snowflake_readiness()
        assert row["ok"] is True
        assert "not installed" in row["details"]
        assert row["status"].startswith("\u26a0")

    def test_missing_credentials_are_named(self, monkeypatch):
        from fluid_build.cli import doctor as doctor_mod
        from fluid_build.providers.snowflake.util import config as sf_config

        monkeypatch.setattr(
            sf_config, "resolve_snowflake_settings", lambda **_kw: {"account": None}
        )
        row = doctor_mod._check_snowflake_readiness()
        assert row["ok"] is True
        assert "SNOWFLAKE_ACCOUNT" in row["details"]

    def test_configured_account_reports_available(self, monkeypatch):
        from fluid_build.cli import doctor as doctor_mod
        from fluid_build.providers.snowflake.util import config as sf_config

        monkeypatch.setattr(
            sf_config,
            "resolve_snowflake_settings",
            lambda **_kw: {
                "account": "ZSCXYPE-CU29385",
                "user": "jeffwatson",
                "password": "x",
                "role": "ACCOUNTADMIN",
                "warehouse": "COMPUTE_WH",
            },
        )
        row = doctor_mod._check_snowflake_readiness()
        assert row["status"].startswith("\u2705")
        assert "ZSCXYPE-CU29385" in row["details"]
