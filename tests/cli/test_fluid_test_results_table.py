# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid test``: JUnit golden output, the results-table flags and the Unity Catalog
results writer (rows, insert statements, write_results) against a fake client."""

from __future__ import annotations

import argparse
import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest

from fluid_build.cli import _test_results_databricks as rt
from fluid_build.cli import test as fluid_test
from fluid_build.cli._common import CLIError
from fluid_build.cli.contract_validation import CheckOutcome, ValidationReport

# ---------------------------------------------------------------- helpers


def make_report(*, version="0.0.0", when=None):
    return ValidationReport(
        contract_path="c.yaml",
        contract_id="prod",
        contract_version=version,
        validation_time=when or datetime(2026, 11, 12, 10, 0, 0, tzinfo=timezone.utc),
        duration=1.2,
    )


def rule_check(name, passed, **kw):
    expose, _, rid = name.rpartition(".")
    base = dict(
        name=name,
        category="quality",
        passed=passed,
        severity="error",
        message="msg",
        expected="1.0",
        actual="0.5",
        rule_id=rid,
        rule_type="completeness",
        expose_id=expose,
    )
    base.update(kw)
    return CheckOutcome(**base)


def contract(**extra):
    base = {
        "version": "2.0.0",
        "metadata": {"layer": "Silver"},
        "exposes": [
            {
                "id": "e1",
                "version": "1.4.0",
                "binding": {
                    "platform": "databricks",
                    "location": {"database": "main", "schema": "energy", "table": "meters"},
                },
            }
        ],
    }
    base.update(extra)
    return base


# ------------------------------------------------------------ JUnit golden


def test_junit_golden_testcase_names_order_and_failure_counts(tmp_path):
    report = make_report()
    report.record_check(rule_check("e1.c1", True))
    report.record_check(rule_check("e1.u1", False, rule_type="uniqueness"))
    report.add_issue("error", "quality", "u1 failed", "exposes[0].dq.rules.u1")
    report.add_issue("error", "missing_field", "column b missing", "fields.b")
    report.add_issue("warning", "mode_mismatch", "b is nullable", "fields.b.mode")
    report.add_issue("warning", "overlay", "overlay hit another entry", "x")
    out = tmp_path / "j.xml"
    fluid_test._output_junit(report, str(out))
    suite = ET.parse(out).getroot()
    cases = [(c.get("classname"), c.get("name")) for c in suite.findall("testcase")]
    assert cases == [
        ("fluid.test.prod.quality", "e1.c1"),
        ("fluid.test.prod.quality", "e1.u1"),
        *[
            ("fluid.test.prod", n)
            for n in (
                "schema",
                "connection",
                "binding",
                "missing_resource",
                "missing_field",
                "type_mismatch",
                "mode_mismatch",
                "extra_field",
                "empty_table",
                "row_count_below_threshold",
                "metadata",
                "drift",
                "overlay",
            )
        ],
    ]
    assert suite.get("tests") == "15" and suite.get("failures") == "2"
    failing = {c.get("name") for c in suite.findall("testcase") if c.find("failure") is not None}
    assert failing == {"e1.u1", "missing_field"}
    warned = {c.get("name") for c in suite.findall("testcase") if c.find("system-out") is not None}
    assert warned == {"mode_mismatch", "overlay"}
    rule_fail = suite.findall("testcase")[1].find("failure")
    assert rule_fail.get("type") == "DataQualityFailure[error]"


def test_junit_without_rule_checks_keeps_quality_rollup_case(tmp_path):
    report = make_report()
    out = tmp_path / "j.xml"
    fluid_test._output_junit(report, str(out))
    names = [c.get("name") for c in ET.parse(out).getroot().findall("testcase")]
    assert names == list(fluid_test_cases().STRUCTURAL_CATEGORIES)


def fluid_test_cases():
    from fluid_build.cli import _test_cases

    return _test_cases


# --------------------------------------------------------------- flags


def args_for(path, **kw):
    ns = dict(contract=str(path), engine="native", results_table=None, results_dashboard=None)
    ns.update(kw)
    return argparse.Namespace(**ns)


@pytest.fixture
def contract_file(tmp_path):
    p = tmp_path / "contract.fluid.yaml"
    p.write_text("fluidVersion: 0.7.3\nid: x\n", encoding="utf-8")
    return p


@pytest.fixture
def no_validator(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("validation must not start")

    monkeypatch.setattr("fluid_build.cli.contract_validation.ContractValidator", boom)


LOG = logging.getLogger("t")


def test_dashboard_without_table_is_exit_2(contract_file, no_validator):
    with pytest.raises(CLIError) as ei:
        fluid_test.run(args_for(contract_file, results_dashboard="/Workspace/Shared/a/b"), LOG)
    assert ei.value.exit_code == 2 and ei.value.event == "test_results_dashboard_needs_table"


def test_results_table_with_soda_engine_is_exit_2(contract_file, no_validator):
    with pytest.raises(CLIError) as ei:
        fluid_test.run(args_for(contract_file, results_table="c.s.t", engine="soda"), LOG)
    assert ei.value.exit_code == 2 and ei.value.event == "test_results_table_needs_native_engine"


@pytest.mark.parametrize("bad", ["a.b", "a.b.c.d", "bad-name.s.t", "a..t", "a.b.t`x"])
def test_malformed_results_table_fails_before_validation(contract_file, no_validator, bad):
    with pytest.raises(CLIError) as ei:
        fluid_test.run(args_for(contract_file, results_table=bad), LOG)
    assert ei.value.exit_code == 2 and ei.value.event == "test_results_table_invalid"


def test_malformed_dashboard_path_fails_before_validation(contract_file, no_validator):
    with pytest.raises(CLIError) as ei:
        fluid_test.run(
            args_for(contract_file, results_table="c.s.t", results_dashboard="relative/x"), LOG
        )
    assert ei.value.exit_code == 2


# ------------------------------------------------------ parse / refuse


def test_parse_results_table_returns_parts():
    assert rt.parse_results_table("main.dq.results") == ("main", "dq", "results")


def test_refuse_product_schema_is_case_insensitive():
    with pytest.raises(CLIError) as ei:
        rt.refuse_product_schema(("MAIN", "Energy", "results"), contract())
    assert ei.value.exit_code == 2 and ei.value.event == "test_results_table_in_product_schema"


def test_refuse_product_schema_allows_other_schema():
    rt.refuse_product_schema(("main", "dq", "results"), contract())


def test_refuse_product_schema_accepts_catalog_key():
    c = contract()
    loc = c["exposes"][0]["binding"]["location"]
    loc["catalog"] = loc.pop("database")
    with pytest.raises(CLIError):
        rt.refuse_product_schema(("main", "energy", "r"), c)


# ---------------------------------------------------------------- rows


def rows_for(report, c=None):
    return rt.results_rows(report, c or contract(), run_id="run-1", fluid_version="9.9")


def test_rule_rows_then_one_structural_row_per_category():
    report = make_report()
    report.record_check(rule_check("e1.c1", True))
    report.add_issue("warning", "overlay", "o", "p")
    rows = rows_for(report)
    builtin, extra, _ = fluid_test_cases().structural_categories(report)
    assert [r["rule_id"] for r in rows] == ["c1", *builtin, *extra]
    assert rows[0]["rule_type"] == "completeness"
    assert {r["rule_type"] for r in rows[1:]} == {"structure"}


def test_rule_row_fields_come_from_check_and_expose():
    report = make_report()
    report.record_check(rule_check("e1.c1", False, severity="Warning"))
    row = rows_for(report)[0]
    assert row["product_id"] == "prod" and row["layer"] == "Silver"
    assert row["expose_id"] == "e1" and row["table_name"] == "main.energy.meters"
    assert row["severity"] == "warning" and row["passed"] is False
    assert (row["observed_value"], row["threshold"]) == ("0.5", "1.0")
    assert row["fluid_version"] == "9.9" and row["run_id"] == "run-1"


def test_product_version_prefers_expose_version():
    report = make_report(version="0.0.0")
    report.record_check(rule_check("e1.c1", True))
    assert rows_for(report)[0]["product_version"] == "1.4.0"


def test_product_version_falls_back_to_contract_version_never_report_placeholder():
    c = contract()
    del c["exposes"][0]["version"]
    report = make_report(version="0.0.0")
    report.record_check(rule_check("e1.c1", True))
    assert {r["product_version"] for r in rows_for(report, c)} == {"2.0.0"}


def test_product_version_none_when_nothing_declared():
    c = contract()
    del c["exposes"][0]["version"], c["version"]
    report = make_report(version="0.0.0")
    report.record_check(rule_check("e1.c1", True))
    assert rows_for(report, c)[0]["product_version"] is None


def test_run_time_is_converted_to_utc():
    tz = timezone(timedelta(hours=5))
    report = make_report(when=datetime(2026, 11, 12, 15, 0, 0, tzinfo=tz))
    row = rows_for(report)[0]
    assert row["run_time"].utcoffset() == timedelta(0) and row["run_time"].hour == 10


def test_message_is_truncated_to_2000():
    report = make_report()
    report.record_check(rule_check("e1.c1", False, message="x" * 5000))
    msg = rows_for(report)[0]["message"]
    assert len(msg) == 2000 and msg.endswith("...")


def test_structural_row_for_error_and_clean_category():
    report = make_report()
    report.add_issue("error", "missing_field", "b missing", "p", expected="b", actual=None)
    rows = {r["rule_id"]: r for r in rows_for(report)}
    bad, ok = rows["missing_field"], rows["schema"]
    assert bad["passed"] is False and bad["severity"] == "error" and bad["threshold"] == "b"
    assert bad["message"] == "b missing" and bad["table_name"] == "main.energy.meters"
    assert ok["passed"] is True and ok["message"] == "OK" and ok["severity"] is None


def test_structural_warning_does_not_fail_the_row():
    report = make_report()
    report.add_issue("warning", "mode_mismatch", "nullable", "p")
    row = {r["rule_id"]: r for r in rows_for(report)}["mode_mismatch"]
    assert row["passed"] is True and row["severity"] == "warning"


# --------------------------------------------------- insert statements


def row_dict(i):
    return {
        "run_id": "r",
        "run_time": datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
        "product_id": "p",
        "rule_id": f"rule{i}",
        "passed": i % 2 == 0,
    }


def col(name):
    return [n for n, _, _ in rt.RESULT_COLUMNS].index(name)


@pytest.mark.parametrize("count", [1, 60])
def test_insert_is_one_statement_with_one_rows_parameter(count):
    sql, params = rt.insert_statement("`c`.`s`.`t`", [row_dict(i) for i in range(count)])
    assert isinstance(sql, str)
    assert [p["name"] for p in params] == ["rows"]
    payload = json.loads(params[0]["value"])
    assert isinstance(payload, list) and len(payload) == count
    names = {n for n, _, _ in rt.RESULT_COLUMNS}
    assert all(set(item) == names for item in payload)
    assert [item["rule_id"] for item in payload] == [f"rule{i}" for i in range(count)]


def test_insert_statement_unpacks_the_json_array_in_sql():
    sql, _ = rt.insert_statement("`c`.`s`.`t`", [row_dict(0)])
    assert sql.startswith("INSERT INTO `c`.`s`.`t` (run_id, run_time,")
    assert "from_json(:rows, 'ARRAY<STRUCT<" in sql and "explode(" in sql
    assert "CAST(r.run_time AS TIMESTAMP)" in sql and "CAST(r.passed AS BOOLEAN)" in sql


def test_insert_statement_payload_encodes_booleans_times_and_nulls():
    _, params = rt.insert_statement("`c`.`s`.`t`", [row_dict(0), row_dict(1)])
    first, second = json.loads(params[0]["value"])
    assert first["passed"] == "true" and second["passed"] == "false"
    assert first["run_time"] == "2026-01-02T03:04:05.000006Z"
    assert first["layer"] is None


def test_insert_statement_never_inlines_values():
    row = row_dict(0)
    row["message"] = "x'); DROP TABLE t; --"
    row["rule_id"] = "very-distinctive-rule-id"
    sql, params = rt.insert_statement("`c`.`s`.`t`", [row])
    assert "DROP TABLE" not in sql and "very-distinctive-rule-id" not in sql
    assert "DROP TABLE" in params[0]["value"]


# ---------------------------------------------------------- write_results


class FakeApi:
    def __init__(self, columns=None, fail_on=None, fail_message=""):
        self.calls = []
        self.columns = columns if columns is not None else [n for n, _, _ in rt.RESULT_COLUMNS]
        self.fail_on = fail_on
        self.fail_message = fail_message

    def do(self, method, path, query=None, body=None, **kw):
        self.calls.append((method, path, body))
        if method == "GET":
            return {"name": "t", "columns": [{"name": c} for c in self.columns]}
        statement = body["statement"]
        if self.fail_on and statement.startswith(self.fail_on):
            return {
                "statement_id": "s",
                "status": {"state": "FAILED", "error": {"message": self.fail_message}},
            }
        return {"statement_id": "s", "status": {"state": "SUCCEEDED"}, "result": {}}

    def statements(self):
        return [c[2]["statement"] for c in self.calls if c[0] == "POST"]


class FakeClient:
    def __init__(self, api):
        self.api_client = api


def write(api, table="main.dq.results", c=None, report=None):
    report = report or make_report()
    if not report.checks:
        report.record_check(rule_check("e1.c1", True))
    return rt.write_results(
        report,
        c or contract(),
        table,
        client_factory=lambda: FakeClient(api),
        warehouse_id="wh",
        run_id="run-1",
    )


def test_write_results_creates_checks_columns_then_inserts(capsys):
    api = FakeApi()
    name, count = write(api)
    sql = api.statements()
    assert name == "main.dq.results" and count == 1 + 12
    assert sql[0].startswith("CREATE TABLE IF NOT EXISTS `main`.`dq`.`results` (")
    assert "COMMENT 'One fluid test invocation'" in sql[0] and "USING DELTA" in sql[0]
    assert [c[0] for c in api.calls] == ["POST", "GET", "POST"]
    assert len(sql) == 2
    assert api.calls[1][1] == "/api/2.1/unity-catalog/tables/main.dq.results"
    assert sql[1].startswith("INSERT INTO `main`.`dq`.`results`")
    out = capsys.readouterr()
    assert out.out == "" and "13 row(s) appended to main.dq.results" in out.err


def test_write_results_mismatched_table_writes_nothing():
    api = FakeApi(columns=["run_id", "other"])
    with pytest.raises(CLIError) as ei:
        write(api)
    assert ei.value.event == "test_results_table_mismatch" and ei.value.exit_code == 1
    assert "run_time" in ei.value.context["missing_columns"]
    assert not any(s.startswith("INSERT") for s in api.statements())


def test_write_results_missing_schema_has_create_schema_fix():
    api = FakeApi(
        fail_on="CREATE TABLE", fail_message="[SCHEMA_NOT_FOUND] schema main.dq not found"
    )
    with pytest.raises(CLIError) as ei:
        write(api)
    assert ei.value.exit_code == 1 and ei.value.event == "test_results_write_failed"
    assert "CREATE SCHEMA `main`.`dq`" in ei.value.context["fix"]


def test_write_results_permission_failure_names_grants():
    api = FakeApi(fail_on="INSERT", fail_message="PERMISSION_DENIED: no")
    with pytest.raises(CLIError) as ei:
        write(api)
    assert "MODIFY" in ei.value.context["fix"]


def test_write_results_unknown_failure_has_no_fix():
    api = FakeApi(fail_on="CREATE", fail_message="boom")
    with pytest.raises(CLIError) as ei:
        write(api)
    assert "fix" not in ei.value.context and "boom" in ei.value.context["error"]


def test_write_results_refuses_product_schema_before_any_call():
    api = FakeApi()
    with pytest.raises(CLIError) as ei:
        write(api, table="main.energy.results")
    assert ei.value.exit_code == 2 and api.calls == []


def test_write_results_status_goes_to_stderr_only(capsys):
    write(FakeApi())
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("Results:")


def test_write_results_without_the_sdk_names_the_extra():
    def no_sdk():
        raise ModuleNotFoundError("No module named 'databricks'")

    with pytest.raises(CLIError) as ei:
        rt.write_results(
            make_report(), contract(), "main.dq.results", client_factory=no_sdk, warehouse_id="wh"
        )
    assert ei.value.event == "test_results_write_failed"
    assert "data-product-forge[databricks]" in ei.value.context["fix"]


# ------------------------------------------- unattributed quality issues


def test_unattributed_quality_issue_keeps_a_quality_structural_case():
    report = make_report()
    report.record_check(rule_check("e1.c1", True))
    report.add_issue("error", "quality", "Quality check execution failed", "exposes[1].dq")
    builtin, extra, by_category = fluid_test_cases().structural_categories(report)
    assert "quality" in builtin or "quality" in extra
    assert [i.path for i in by_category["quality"]] == ["exposes[1].dq"]
    rows = rows_for(report)
    (quality,) = [r for r in rows if r["rule_id"] == "quality"]
    assert quality["rule_type"] == "structure" and quality["passed"] is False


def test_quality_roll_up_keeps_only_the_issues_no_rule_case_carries():
    report = make_report()
    report.record_check(rule_check("e1.c1", False))
    report.add_issue("error", "quality", "rule failed", "exposes[0].contract.dq.rules.c1")
    report.add_issue("error", "quality", "could not run", "exposes[1].dq")
    _, _, by_category = fluid_test_cases().structural_categories(report)
    assert [i.path for i in by_category["quality"]] == ["exposes[1].dq"]


def test_rule_attributed_quality_issues_leave_no_quality_structural_case(tmp_path):
    report = make_report()
    report.record_check(rule_check("e1.c1", False))
    report.add_issue("error", "quality", "rule failed", "exposes[0].contract.dq.rules.c1")
    builtin, extra, by_category = fluid_test_cases().structural_categories(report)
    assert "quality" not in builtin and "quality" not in extra and "quality" not in by_category
    assert "quality" not in [r["rule_id"] for r in rows_for(report)]
    out = tmp_path / "j.xml"
    fluid_test._output_junit(report, str(out))
    names = [c.get("name") for c in ET.parse(out).getroot().findall("testcase")]
    assert "quality" not in names


# ------------------------------------------------- early flag refusals


def test_no_data_with_results_table_is_refused_before_validation(contract_file, no_validator):
    with pytest.raises(CLIError) as ei:
        fluid_test.run(args_for(contract_file, results_table="c.s.t", no_data=True), LOG)
    assert ei.value.exit_code == 2
    assert ei.value.event == "test_results_table_needs_live_checks"


def write_contract(tmp_path, text=None):
    p = tmp_path / "contract.fluid.yaml"
    if text is None:
        text = (
            "fluidVersion: 0.7.3\nid: x\nexposes:\n  - id: e1\n    binding:\n"
            "      platform: databricks\n      location:\n        database: main\n"
            "        schema: energy\n        table: meters\n"
        )
    p.write_text(text, encoding="utf-8")
    return p


def test_results_table_in_the_product_schema_is_refused_before_validation(tmp_path, no_validator):
    path = write_contract(tmp_path)
    with pytest.raises(CLIError) as ei:
        fluid_test.run(args_for(path, results_table="main.energy.results"), LOG)
    assert ei.value.exit_code == 2
    assert ei.value.event == "test_results_table_in_product_schema"


def test_unparseable_contract_skips_the_early_product_schema_check(tmp_path):
    path = write_contract(tmp_path, "exposes: [unclosed\n  - : :\n")
    table, dashboard = fluid_test._results_options(
        args_for(path, results_table="main.energy.results"), path
    )
    assert table == "main.energy.results" and dashboard is None


def test_a_malformed_exposes_is_left_to_the_validator():
    rt.refuse_product_schema(("main", "dq", "results"), {"exposes": 5})
    rt.refuse_product_schema(("main", "dq", "results"), {"exposes": "x"})
