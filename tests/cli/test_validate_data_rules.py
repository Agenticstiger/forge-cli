# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid validate`` runs the data-rules lint (RFC-data-rules.md, "Validator lint (v1)").

Through the real parser and ``validate.run``: an error fails the run and reaches the JSON
document's ``issues[]`` with its pointer, its catalog code and its fix; a note is shown and
``--strict`` never counts it; a 0.7.6 contract that uses a field is told to declare 0.7.7; and a
contract without the new fields gets the output it always had.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

import fluid_build.cli as cli_mod
from fluid_build.cli.validate import run as validate_run

pytestmark = pytest.mark.unit

LOG = logging.getLogger("test.validate_data_rules")


def _expose(expose_id: str, schema: list, **extra: Any) -> Dict[str, Any]:
    return {
        "exposeId": expose_id,
        "kind": "table",
        "binding": {
            "platform": "local",
            "format": "parquet",
            "location": {"path": f"{expose_id}/"},
        },
        "contract": {"schema": schema, **extra.pop("contract", {})},
        **extra,
    }


CONTRACT: Dict[str, Any] = {
    "fluidVersion": "0.7.7",
    "kind": "DataProduct",
    "id": "silver.billing.invoices",
    "name": "Invoices",
    "metadata": {"layer": "Silver", "owner": {"team": "billing-platform"}},
    "exposes": [
        _expose(
            "invoices",
            [
                {"name": "invoice_id", "type": "string", "required": True},
                {"name": "contract_id", "type": "string"},
                {"name": "net", "type": "decimal(12,2)"},
                {"name": "tax_rate", "type": "decimal(5,4)"},
                {"name": "gross", "type": "decimal(12,2)"},
                {"name": "issue_date", "type": "date"},
                {"name": "due_date", "type": "date"},
                {
                    "name": "tower_id",
                    "type": "string",
                    "identifierFormat": [{"prefixes": ["TWR"], "mask": "99999"}],
                    "placeholders": {"prefixes": ["TBD_"]},
                },
            ],
            contract={
                "invariants": [
                    {
                        "id": "gross_from_net",
                        "derive": "gross",
                        "expr": "ROUND(net * (1 + tax_rate), 2)",
                    },
                    {
                        "id": "due_after_issue",
                        "check": "issue_date <= due_date",
                        "mustBeLessOrEqualTo": 0.5,
                        "unit": "percent",
                    },
                ]
            },
            semantics={
                "entities": [{"name": "contract", "type": "foreign", "expr": "contract_id"}],
                "relationships": [
                    {
                        "name": "contract",
                        "to": "contracts",
                        "fromColumns": ["contract_id"],
                        "toColumns": ["contract_id"],
                        "cardinality": "many_to_one",
                    }
                ],
            },
        ),
        _expose(
            "contracts",
            [
                {"name": "contract_id", "type": "string", "required": True},
                {"name": "monthly_fee", "type": "decimal(12,2)"},
            ],
            semantics={
                "entities": [{"name": "contract", "type": "primary", "expr": "contract_id"}]
            },
        ),
    ],
}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FLUID_ENV", raising=False)
    return tmp_path


def _write(contract: Dict[str, Any]) -> str:
    path = Path("contract.fluid.yaml")
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    return str(path)


def _fluid(*argv: str) -> int:
    args = cli_mod.build_parser().parse_args(list(argv))
    return validate_run(args, LOG)


def _json(contract: Dict[str, Any], capsys, *flags: str) -> Dict[str, Any]:
    _fluid("validate", _write(contract), "--offline", "--format", "json", *flags)
    return json.loads(capsys.readouterr().out)


def _data_rule_issues(doc: Dict[str, Any]) -> list:
    return [i for i in doc.get("issues", []) if i["code"] == "ERR_CONTRACT_DATA_RULES"]


def test_the_rules_validate(workspace: Path, capsys) -> None:
    assert _fluid("validate", _write(CONTRACT), "--offline", "--strict") == 0
    out = capsys.readouterr().out
    assert "Valid FLUID contract" in out


def test_an_error_fails_the_run_with_its_pointer_and_fix(workspace: Path, capsys) -> None:
    contract = copy.deepcopy(CONTRACT)
    contract["exposes"][0]["contract"]["invariants"][0]["expr"] = "net * (1 + tax_rate)"
    assert _fluid("validate", _write(contract), "--offline") == 1
    capsys.readouterr()
    doc = _json(contract, capsys)
    assert doc["valid"] is False
    [issue] = _data_rule_issues(doc)
    assert issue["severity"] == "error"
    assert issue["pointer"] == "/exposes/0/contract/invariants/0/expr"
    assert "wrap it in ROUND(x, 2)" in issue["message"]
    assert issue["fix"].startswith("wrap the value in ROUND")
    assert issue["message"] in doc["errors"]


def test_sql_outside_the_subset_names_its_position(workspace: Path, capsys) -> None:
    contract = copy.deepcopy(CONTRACT)
    contract["exposes"][0]["contract"]["invariants"][1]["check"] = "invoice_id LIKE 'A%'"
    doc = _json(contract, capsys)
    [issue] = _data_rule_issues(doc)
    assert "not FLUID SQL/1 at character 12" in issue["message"]
    assert "contract.quality[]" in issue["message"]


def test_a_note_never_fails_strict(workspace: Path, capsys) -> None:
    contract = copy.deepcopy(CONTRACT)
    contract["exposes"][0]["contract"]["invariants"][1]["check"] = "net / tax_rate > 1"
    assert _fluid("validate", _write(contract), "--offline", "--strict") == 0
    out = capsys.readouterr().out
    assert "Validation Notes:" in out
    doc = _json(contract, capsys, "--strict")
    assert doc["valid"] is True
    [issue] = _data_rule_issues(doc)
    assert issue["severity"] == "note"
    assert "a / outside ROUND" in issue["message"]


def test_076_is_told_to_declare_077(workspace: Path, capsys) -> None:
    contract = copy.deepcopy(CONTRACT)
    contract["fluidVersion"] = "0.7.6"
    for expose in contract["exposes"]:
        expose.pop("semantics", None)
    doc = _json(contract, capsys)
    assert doc["valid"] is False
    joined = " ".join(doc["errors"])
    assert "'invariants' (rules about valid data) is a field of the 0.7.7 preview schema" in joined
    assert not _data_rule_issues(doc)  # the lint leaves 0.7.6 to the schema


def test_a_contract_without_the_fields_is_untouched(workspace: Path, capsys) -> None:
    contract = copy.deepcopy(CONTRACT)
    for expose in contract["exposes"]:
        expose["contract"].pop("invariants", None)
        for column in expose["contract"]["schema"]:
            column.pop("identifierFormat", None)
            column.pop("placeholders", None)
    doc = _json(contract, capsys, "--strict")
    assert doc["valid"] is True
    assert not _data_rule_issues(doc)
    assert "notes" not in doc


def test_a_crash_in_the_lint_fails_closed(
    workspace: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fluid_build.data_rules as data_rules

    def boom(contract: Any) -> list:
        raise RuntimeError("broken")

    monkeypatch.setattr(data_rules, "lint_contract", boom)
    doc = _json(CONTRACT, capsys)
    assert doc["valid"] is False
    assert any("Data-rules check could not run" in e for e in doc["errors"])
