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

"""``fluid verify`` on a local csv/parquet output: the gate and the console.

* ``--strict`` on a file with NONE of the declared columns (the one-row
  ``demo_col`` placeholder a native ``amend`` lands) exited 0 with
  "non-critical mismatch(es) downgraded to warning": the local-file result
  carried no severity, so it was never CRITICAL. A local output whose
  columns differ from the contract now fails ``--strict``, the rule dbt
  applies to an enforced model contract (missing and extra columns both).
* The console read the warehouse result shape, so a file the JSON report
  called ``match`` (rows, 8/8 columns) printed "Table Rows: 0" and three
  FAIL lines.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import List

import pytest

from fluid_build.cli import verify as verify_cmd

duckdb = pytest.importorskip("duckdb")

LOG = logging.getLogger("test.verify_local_file_strict")

_CONTRACT = """\
fluidVersion: "0.7.5"
kind: DataProduct
id: demo.verify
name: Verify Demo
description: Inline SQL build.
domain: Demo
metadata:
  layer: Bronze
  owner: {team: dp, email: dp@example.com}
builds:
  - id: make_rows
    description: Inline rows.
    pattern: embedded-logic
    engine: sql
    properties:
      sql: "SELECT 1 AS id, 'a' AS name"
    outputs: [rows]
exposes:
  - exposeId: rows
    kind: table
    binding:
      platform: local
      format: parquet
      location:
        path: ./out/rows.parquet
    contract:
      schema:
        - {name: id, type: INTEGER, required: true}
        - {name: name, type: VARCHAR, required: false}
"""


@pytest.fixture
def contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(_CONTRACT, encoding="utf-8")
    (tmp_path / "out").mkdir()
    return path


def _land(contract: Path, select: str) -> None:
    target = (contract.parent / "out" / "rows.parquet").as_posix().replace("'", "''")
    duckdb.sql(f"COPY ({select}) TO '{target}' (FORMAT parquet)")


def _verify(contract: Path, *flags: str) -> int:
    parser = argparse.ArgumentParser(prog="fluid")
    sub = parser.add_subparsers(dest="cmd")
    verify_cmd.register(sub)
    argv: List[str] = ["verify", str(contract), "--out", "report.json", *flags]
    return verify_cmd.run(parser.parse_args(argv), LOG)


def _report(contract: Path) -> dict:
    doc = json.loads((contract.parent / "report.json").read_text(encoding="utf-8"))
    return doc["results"]["rows"]


class TestStrictFailsOnTheWrongColumns:
    def test_placeholder_with_none_of_the_declared_columns_fails(self, contract: Path) -> None:
        _land(contract, "SELECT 1 AS demo_col")
        assert _verify(contract, "--strict") == 1
        result = _report(contract)
        assert result["severity"]["level"] == "CRITICAL"
        assert result["dimensions"]["schema_structure"]["missing_fields"] == ["id", "name"]

    def test_an_undeclared_extra_column_fails(self, contract: Path) -> None:
        _land(contract, "SELECT 1 AS id, 'a' AS name, 'x' AS surprise")
        assert _verify(contract, "--strict") == 1

    def test_a_missing_declared_column_fails(self, contract: Path) -> None:
        _land(contract, "SELECT 1 AS id")
        assert _verify(contract, "--strict") == 1

    def test_matching_file_passes_strict(self, contract: Path) -> None:
        _land(contract, "SELECT 1 AS id, 'a' AS name")
        assert _verify(contract, "--strict") == 0

    def test_without_strict_a_mismatch_is_reported_not_fatal(self, contract: Path) -> None:
        _land(contract, "SELECT 1 AS demo_col")
        assert _verify(contract) == 0
        assert _report(contract)["status"] == "mismatch"


class TestConsoleRendersTheLocalFileShape:
    def test_rows_and_columns_match_the_json_report(
        self, contract: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _land(contract, "SELECT i AS id, 'a' AS name FROM range(3) t(i)")
        assert _verify(contract) == 0
        out = capsys.readouterr().out
        assert "Rows: 3" in out
        assert "All 2 declared columns present" in out
        assert "Table Rows: 0" not in out
        assert "FAIL" not in out
        assert _report(contract)["row_count"] == 3

    def test_a_mismatch_names_the_missing_and_extra_columns(
        self, contract: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _land(contract, "SELECT 1 AS demo_col")
        _verify(contract)
        out = capsys.readouterr().out
        assert "Missing in file: id, name" in out
        assert "demo_col" in out
        assert "Type mismatches" not in out


def test_a_quote_in_the_contract_directory_is_a_path_not_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The anchored path carries the contract's directory into DuckDB's
    # ``read_parquet('...')``; it must arrive as one string literal. Python
    # ``repr`` breaks on a name holding both quote kinds: it escapes the
    # single quote with a backslash, which DuckDB reads as a literal.
    cdir = tmp_path / 'it\'s "here"'
    (cdir / "out").mkdir(parents=True)
    (cdir / "contract.fluid.yaml").write_text(_CONTRACT, encoding="utf-8")
    monkeypatch.chdir(cdir)
    _land(cdir / "contract.fluid.yaml", "SELECT 1 AS id, 'a' AS name")
    assert _verify(cdir / "contract.fluid.yaml", "--strict") == 0
    assert _report(cdir / "contract.fluid.yaml")["row_count"] == 1
