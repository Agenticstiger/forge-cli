# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid test``: JUnit golden output, the results-table flags and the Unity Catalog
results writer (rows, insert statements, write_results) against a fake client."""

from __future__ import annotations

import argparse
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


def test_insert_statements_batches_25_rows():
    stmts = rt.insert_statements("`c`.`s`.`t`", [row_dict(i) for i in range(60)])
    assert [len(p) // len(rt.RESULT_COLUMNS) for _, p in stmts] == [25, 25, 10]


def test_insert_statement_markers_casts_and_param_values():
    ((sql, params),) = rt.insert_statements("`c`.`s`.`t`", [row_dict(0), row_dict(1)])
    n = len(rt.RESULT_COLUMNS)
    assert sql.startswith("INSERT INTO `c`.`s`.`t` (run_id, run_time,")
    assert f"CAST(:r0c{col('run_time')} AS TIMESTAMP)" in sql
    assert f"CAST(:r1c{col('passed')} AS BOOLEAN)" in sql
    assert f":r0c{col('run_id')}," in sql and "CAST(:r0c0" not in sql
    assert len(params) == 2 * n
    by_name = {p["name"]: p for p in params}
    assert by_name[f"r0c{col('passed')}"]["value"] == "true"
    assert by_name[f"r1c{col('passed')}"]["value"] == "false"
    assert by_name[f"r0c{col('run_time')}"]["value"] == "2026-01-02T03:04:05.000006Z"
    assert "value" not in by_name[f"r0c{col('layer')}"]


def test_insert_statement_never_inlines_values():
    row = row_dict(0)
    row["message"] = "x'); DROP TABLE t; --"
    ((sql, _),) = rt.insert_statements("`c`.`s`.`t`", [row])
    assert "DROP TABLE" not in sql


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
    assert [c[0] for c in api.calls[:3]] == ["POST", "GET", "POST"]
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
