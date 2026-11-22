# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What the import checks before it writes: the schema refuses, the contract rules report.

A proposal the JSON schema does not accept is a bug in the importer or a value in the
conventions, and nothing is written. The contract rules ``fluid validate`` runs after the schema
are another matter: a Databricks binding asks for columns, a schema and a warehouse, a
Snowflake-managed Iceberg binding for a warehouse or a bucket, and no code holds any of them, so
refusing for them stopped a whole directory of hundreds of products for one table in a Unity
Catalog. Those contracts are written and the report says what ``fluid validate`` will ask for.
"""

from __future__ import annotations

import copy
import textwrap
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build._errors import FluidUserError
from fluid_build.cli.import_workflow import _pipeline
from fluid_build.cli.import_workflow.pyspark import PySparkImporter
from fluid_build.cli.import_workflow.registry import ImportReport

SESSION = "from pyspark.sql import SparkSession\n\nspark = SparkSession.builder.getOrCreate()\n"
DELTA = SESSION + 'spark.table("main.raw.x").write.format("delta").saveAsTable("main.cur.orders")\n'
PLAIN = SESSION + 'spark.table("db.a").write.saveAsTable("db.b")\n'
MARK = "`fluid validate` reports an error until a person completes it"


def propose(root: Path, monkeypatch, conventions: str) -> Any:
    files = {"jobs/delta.py": DELTA, "jobs/plain.py": PLAIN, "conventions.yaml": conventions}
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    monkeypatch.chdir(root)
    return PySparkImporter().import_to_contracts(
        "jobs", options={"conventions": "conventions.yaml"}
    )


def findings(report: ImportReport, product: str) -> List[str]:
    return [x for x in report.required_defaults if MARK in x and x.startswith(f"{product}:")]


@pytest.mark.parametrize(
    "conventions, asks_for",
    [
        (
            "catalogs:\n  main: {platform: databricks, format: delta_table}\n",
            "binding.location.schema",
        ),
        (
            "catalogs:\n  main: {platform: databricks, format: delta_table}\n",
            "contract.schema columns",
        ),
        (
            "catalogs:\n  main: {platform: snowflake, format: iceberg}\n",
            "binding.location.warehouse",
        ),
        (
            "catalogs:\n  main: {platform: aws, format: iceberg, catalog: my_glue}\n",
            "is not a catalog kind FLUID knows",
        ),
    ],
    ids=["unity schema", "unity columns", "snowflake iceberg", "a catalog that is no kind"],
)
def test_a_contract_that_asks_for_what_only_a_person_knows_is_written_and_the_report_says_so(
    tmp_path, monkeypatch, conventions, asks_for
):
    contracts, report = propose(tmp_path, monkeypatch, conventions)
    assert sorted(c["id"] for c in contracts) == ["cur.orders", "db.b"]  # the other one is written
    said = findings(report, "cur.orders")
    assert any(asks_for in line for line in said), said
    assert findings(report, "db.b") == []


def test_the_environment_of_the_shell_never_decides_whether_a_contract_is_written(
    tmp_path, monkeypatch
):
    conventions = "catalogs:\n  main: {platform: databricks, format: delta_table}\n"
    monkeypatch.delenv("DATABRICKS_WAREHOUSE_ID", raising=False)
    without, _ = propose(tmp_path / "a", monkeypatch, conventions)
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "abc123")
    with_it, _ = propose(tmp_path / "b", monkeypatch, conventions)
    assert [c["id"] for c in without] == [c["id"] for c in with_it]
    assert without == with_it


def test_a_proposal_with_nothing_for_a_person_to_complete_has_no_such_line(tmp_path, monkeypatch):
    conventions = "catalogs:\n  main: {platform: aws, format: iceberg, catalog: glue}\n"
    _contracts, report = propose(tmp_path, monkeypatch, conventions)
    assert not [x for x in report.required_defaults if MARK in x]


# --- the schema still refuses ------------------------------------------------------------


def one_contract(tmp_path, monkeypatch) -> Dict[str, Any]:
    contracts, _report = propose(tmp_path, monkeypatch, "defaults: {domain: retail}\n")
    return contracts[0]


def test_a_proposal_the_schema_does_not_accept_is_refused_before_anything_is_written(
    tmp_path, monkeypatch
):
    broken = one_contract(tmp_path, monkeypatch)
    broken["kind"] = "NotAKind"
    with pytest.raises(FluidUserError) as refused:
        _pipeline._check_valid([broken], ImportReport())
    assert "is not a valid contract" in str(refused.value.what)


def test_findings_past_the_limit_are_counted_not_listed(tmp_path, monkeypatch):
    base = propose(
        tmp_path, monkeypatch, "catalogs:\n  main: {platform: databricks, format: delta_table}\n"
    )[0][0]
    contracts = []
    for number in range(60):
        contract = copy.deepcopy(base)
        contract["id"] = f"cur.t{number}"
        contracts.append(contract)
    report = ImportReport()
    _pipeline._check_valid(contracts, report)
    lines = [x for x in report.required_defaults if MARK in x]
    assert len(lines) == _pipeline.MAX_RULE_LINES
    last = report.required_defaults[-1]
    assert last.endswith("`fluid validate` finding(s) on these products are not listed")
    assert int(last.split()[0]) >= 1
