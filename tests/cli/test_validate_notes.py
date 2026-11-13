# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid validate`` notes: what is worth saying but asks nothing of the author.

A Databricks expose that a dbt build writes gets its table and column comments
from dbt (its ``CREATE OR REPLACE``, with ``persist_docs``), so ``fluid apply``
sets none of the contract's descriptions, and validate says so. There is
nothing to fix, so the message is a note, not a warning: ``--strict`` passes
(stage 2 of the pipelines ``fluid generate ci`` writes runs it on the bundle),
and the text is still shown, in the text output, the JSON document and a
bundle's report.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest
import yaml

import fluid_build.cli as cli_mod
from fluid_build.cli.bundle import run as bundle_run
from fluid_build.cli.validate import output_text_results
from fluid_build.cli.validate import run as validate_run
from fluid_build.schema_manager import ValidationResult

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

LOG = logging.getLogger("test.validate_notes")

#: The note, word for word as the emitter writes it.
NOTE = (
    "exposes[0] (orders) is written by dbt, whose CREATE OR REPLACE sets the table and "
    "column comments: Unity Catalog shows the dbt model's descriptions (with "
    "persist_docs), not the contract's, so fluid apply sets none."
)

#: A Databricks table with descriptions, written by a dbt build the operator runs.
_CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.6",
    "kind": "DataProduct",
    "id": "sales.orders",
    "name": "Orders",
    "description": "Every order, one row each.",
    "metadata": {"owner": {"team": "data-platform"}, "layer": "Silver"},
    "exposes": [
        {
            "exposeId": "orders",
            "kind": "table",
            "description": "One row per order.",
            "binding": {
                "platform": "databricks",
                "format": "delta_table",
                "location": {"database": "main", "schema": "sales", "table": "orders"},
            },
            "contract": {
                "schema": [
                    {
                        "name": "order_id",
                        "type": "string",
                        "required": True,
                        "description": "The order's id.",
                    },
                    {"name": "amount", "type": "double"},
                ]
            },
        }
    ],
    "builds": [{"id": "orders_model", "engine": "dbt", "outputs": ["orders"], "execution": {}}],
}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_ENV", raising=False)
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh-0123456789abcdef")
    return tmp_path


def _squash(text: str) -> str:
    """``text`` without whitespace: Rich may fold a long line."""
    return "".join(text.split())


def _write(contract: Dict[str, Any]) -> str:
    path = Path("contract.fluid.yaml")
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return str(path)


def _fluid(*argv: str) -> int:
    """``fluid <argv>`` through the real parser (``bundle`` or ``validate``)."""
    args = cli_mod.build_parser().parse_args(list(argv))
    return (bundle_run if argv[0] == "bundle" else validate_run)(args, LOG)


def _without_descriptions(contract: Dict[str, Any]) -> Dict[str, Any]:
    """No table comment (the expose's description, else the product's) and no column one."""
    contract = copy.deepcopy(contract)
    del contract["description"]
    expose = contract["exposes"][0]
    del expose["description"]
    for column in expose["contract"]["schema"]:
        column.pop("description", None)
    return contract


class TestAContractFile:
    def test_strict_passes_and_the_note_is_shown(self, workspace: Path, capsys) -> None:
        assert _fluid("validate", _write(_CONTRACT), "--offline", "--strict") == 0
        out = capsys.readouterr().out
        assert "Validation Notes:" in out
        assert _squash(NOTE) in _squash(out)
        assert "Validation Warnings:" not in out
        assert "treated as errors" not in out

    def test_json_lists_it_under_notes(self, workspace: Path, capsys) -> None:
        argv = ("validate", _write(_CONTRACT), "--offline", "--strict", "--format", "json")
        assert _fluid(*argv) == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["valid"] is True
        assert doc["warnings"] == []
        assert doc["notes"] == [NOTE]

    def test_quiet_prints_errors_only(self, workspace: Path, capsys) -> None:
        assert _fluid("validate", _write(_CONTRACT), "--offline", "--strict", "--quiet") == 0
        assert "persist_docs" not in capsys.readouterr().out

    def test_without_descriptions_there_is_no_note(self, workspace: Path, capsys) -> None:
        contract = _write(_without_descriptions(_CONTRACT))
        assert _fluid("validate", contract, "--offline", "--strict", "--format", "json") == 0
        doc = json.loads(capsys.readouterr().out)
        # Without a note the document is the one it always was.
        assert list(doc) == ["valid", "schema_version", "errors", "warnings", "validation_time"]


class TestStrictCountsWarningsOnly:
    """A note next to a warning: ``--strict`` fails on the warning and names only it."""

    def _result(self) -> ValidationResult:
        result = ValidationResult(is_valid=True, schema_version="0.7.6")
        result.add_note("a note")
        return result

    def test_a_note_alone_passes(self, capsys) -> None:
        args = SimpleNamespace(quiet=True, verbose=False, strict=True)
        assert output_text_results(self._result(), args, LOG) == 0
        assert capsys.readouterr().out.strip() == ""

    def test_a_warning_fails_and_the_note_is_no_error(self, capsys) -> None:
        result = self._result()
        result.add_warning("a warning")
        args = SimpleNamespace(quiet=True, verbose=False, strict=True)
        assert output_text_results(result, args, LOG) == 1
        out = capsys.readouterr().out
        assert "ERROR (--strict): a warning" in out
        assert "a note" not in out

    def test_a_note_keeps_the_result_valid(self) -> None:
        result = self._result()
        assert result.is_valid and result.notes == ["a note"] and result.warnings == []


class TestABundle:
    """Stage 2 of a generated pipeline: ``fluid validate runtime/bundle.tgz --strict``."""

    def _bundle(self, capsys) -> None:
        Path("runtime").mkdir()
        argv = ("bundle", _write(_CONTRACT), "--format", "tgz", "--out", "runtime/bundle.tgz")
        assert _fluid(*argv) == 0
        capsys.readouterr()

    def test_strict_passes_and_lists_the_note(self, workspace: Path, capsys) -> None:
        self._bundle(capsys)
        argv = ("validate", "runtime/bundle.tgz", "--offline", "--strict")
        assert _fluid(*argv, "--report", "runtime/validate-report.json") == 0
        out = capsys.readouterr().out
        assert _squash(f"[info] contract: contract.resolved.yaml: {NOTE}") in _squash(out)
        assert "treated as errors" not in out
        report = json.loads(Path("runtime/validate-report.json").read_text(encoding="utf-8"))
        assert report["status"] == "pass"
        assert [i["message"] for i in report["issues"] if i["severity"] == "info"] == [NOTE]
        assert not [i for i in report["issues"] if i["severity"] == "warning"]

    def test_json_carries_it_as_info(self, workspace: Path, capsys) -> None:
        self._bundle(capsys)
        argv = ("validate", "runtime/bundle.tgz", "--offline", "--strict", "--format", "json")
        assert _fluid(*argv) == 0
        report = json.loads(capsys.readouterr().out)
        (note,) = [i for i in report["issues"] if i["severity"] == "info"]
        assert (note["code"], note["message"]) == ("CONTRACT-RULE", NOTE)
