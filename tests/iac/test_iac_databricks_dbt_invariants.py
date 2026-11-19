# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Declared invariants as tests of a Databricks dbt job (``databricks_dbt_tests.py``).

Each declared rule on an expose the build writes is one singular test in
``<test path>/fluid_dq/<model>/``, written in the Databricks dialect from the parsed
expression; one the job cannot run (no exact SQL, a link to an expose the build does not
write) is refused, like any other contract rule the job cannot run. The files are rendered
with the same dbt-shaped Jinja context as the DQ-rule tests, and their SQL parses as
Databricks SQL.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.iac.providers import databricks_dbt_tests as dq

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

sys.path.insert(0, str(Path(__file__).parent))
import test_iac_databricks_dbt_tests as base  # noqa: E402


def _contract(**invariant: Any) -> Dict[str, Any]:
    contract = base._contract(rules=[])
    contract["fluidVersion"] = "0.7.7"
    orders = contract["exposes"][0]
    orders["contract"]["invariants"] = [
        {
            "id": "amount_positive",
            "check": "amount >= 0",
            "mustBeLessOrEqualTo": 5,
            "unit": "percent",
        },
        {"id": "status_known", "check": "status IN ('NEW', 'SHIPPED')", "severity": "warning"},
        *([invariant] if invariant else []),
    ]
    return contract


def test_each_declared_rule_is_a_test_file_of_the_job(tmp_path: Path) -> None:
    base._project(tmp_path)
    compiled = base._compile(_contract(), tmp_path)
    assert not compiled.refusals, base._kinds(compiled)
    uploaded = base._uploaded(compiled)
    tests = sorted(p for p in uploaded if p.startswith("tests/fluid_dq/orders/"))
    assert tests == [
        "tests/fluid_dq/orders/fluid_amount_positive.sql",
        "tests/fluid_dq/orders/fluid_status_known.sql",
    ]
    sql, configs, _ = base._render(uploaded["tests/fluid_dq/orders/fluid_status_known.sql"])
    assert configs["severity"] == "warn" and configs["tags"] == [dq.TAG]
    assert configs["meta"] == {"fluid_rule_id": "status_known", "fluid_expose_id": "orders"}
    assert "`main`.`sales`.`orders`" in sql  # the model, through ref()
    sqlglot = pytest.importorskip("sqlglot")
    sqlglot.parse_one(sql.split("\n", 2)[-1] if sql.startswith("\n") else sql, read="databricks")
    (job,) = compiled.jobs
    assert set(job.tests) == {"fluid_amount_positive", "fluid_status_known"}


def test_the_percent_band_is_exact_in_the_test(tmp_path: Path) -> None:
    base._project(tmp_path)
    uploaded = base._uploaded(base._compile(_contract(), tmp_path))
    sql, _, _ = base._render(uploaded["tests/fluid_dq/orders/fluid_amount_positive.sql"])
    assert "100 * `violating` <= 5 * `covered`" in sql


def test_a_rule_with_no_databricks_sql_is_refused(tmp_path: Path) -> None:
    base._project(tmp_path)
    compiled = base._compile(_contract(id="bad", check="amount LIKE 'x'"), tmp_path)
    assert "databricks-job-dq-rule" in base._kinds(compiled)
    assert any("'bad'" in str(r) for r in compiled.refusals)
    assert not [p for p in base._uploaded(compiled) if "fluid_dq" in p]


def test_a_rule_that_reads_an_expose_the_build_does_not_write_is_refused(tmp_path: Path) -> None:
    contract = _contract(id="fee", check="amount <= cust.limit_amount")
    orders = contract["exposes"][0]
    orders["semantics"] = {
        "relationships": [
            {
                "name": "cust",
                "to": "customers",
                "fromColumns": ["customer_id"],
                "toColumns": ["customer_id"],
                "cardinality": "many_to_one",
            }
        ]
    }
    customers = copy.deepcopy(orders)
    customers.pop("semantics")
    customers["exposeId"] = "customers"
    customers["binding"]["location"]["table"] = "customers"
    customers["contract"] = {
        "schema": [
            {"name": "customer_id", "type": "string"},
            {"name": "limit_amount", "type": "decimal(12,2)"},
        ]
    }
    contract["exposes"].append(customers)
    base._project(tmp_path)
    compiled = base._compile(contract, tmp_path)
    from fluid_build.data_rules._compile import compile_rules

    rules = [
        (r.id, r.status, r.reads, r.reason) for r in compile_rules(contract, dialect="databricks")
    ]
    assert any("'fee'" in str(r) and "does not write" in str(r) for r in compiled.refusals), (
        [str(r) for r in compiled.refusals],
        rules,
    )


def test_expected_tests_and_the_report_name_the_declared_rules() -> None:
    contract = _contract()
    checks = dq.expected_tests(contract, contract["builds"][0])
    names = [c.name for c in checks]
    assert names == ["fluid_amount_positive", "fluid_status_known"]
    logs = "12:00:01  1 of 2 PASS fluid_amount_positive [PASS]\n12:00:02  2 of 2 WARN 3 fluid_status_known [WARN 3]\n"
    lines = dq.report(logs, checks)
    assert lines[0] == "   contract rules as dbt tests: 1 pass, 1 warn"
    assert "status IN ('NEW', 'SHIPPED') holds" in lines[2] and "severity warn" in lines[2]


def test_a_contract_below_the_preview_writes_no_invariant_test(tmp_path: Path) -> None:
    contract = _contract()
    contract["fluidVersion"] = "0.7.6"
    contract["exposes"][0]["contract"].pop("invariants")
    base._project(tmp_path)
    compiled = base._compile(contract, tmp_path)
    assert not [p for p in base._uploaded(compiled) if "fluid_dq" in p]
