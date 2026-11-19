# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Which declared rules each engine can run (``fluid_build.data_rules._coverage``), and the
``fluid generate dbt-tests`` flags that print it, write it, and refuse a required rule a
selected adapter cannot run."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

import pytest
import yaml

from fluid_build import fluid_sql as fs
from fluid_build.cli._common import CLIError
from fluid_build.data_rules._coverage import as_json, coverage, render_table, unmet

pytestmark = pytest.mark.unit

sys.path.insert(0, str(Path(__file__).parent))
from test_compile import _billing  # noqa: E402


def _contract() -> Dict[str, Any]:
    contract = _billing()
    invoices = contract["exposes"][0]
    invoices["contract"]["invariants"].append({"id": "bad", "check": "net LIKE 'x'"})
    status = next(c for c in invoices["contract"]["schema"] if c["name"] == "status")
    status["placeholders"] = {"values": ["?"]}  # declared, no bound: nothing to test
    return contract


def test_each_rule_has_a_verdict_per_engine() -> None:
    rows = {row.id: row for row in coverage(_contract())}
    assert rows["gross_from_net"].engines.keys() == set(fs.DIALECTS)
    assert all(rows["gross_from_net"].runs_on(d) for d in fs.DIALECTS)
    bad = rows["bad"].engines["duckdb"]
    assert bad.status == "unsupported" and "validator refuses it" in bad.reason
    assert rows["status.placeholders"].engines["snowflake"].status == "skipped"


def test_the_table_and_the_json_say_the_same() -> None:
    rows = coverage(_contract())
    table = render_table(rows)
    assert table.splitlines()[0].split()[:3] == ["rule", "expose", "duckdb"]
    assert "bad (invoices) on every engine: the validator refuses it" in table
    data = as_json(rows)
    assert data["engines"] == list(fs.DIALECTS)
    bad = next(r for r in data["rules"] if r["id"] == "bad")
    assert (
        bad["engines"]["athena"]["status"] == "unsupported" and bad["engines"]["athena"]["reason"]
    )
    assert render_table([]).startswith("No declared rules")


def test_unmet_lists_required_rules_an_adapter_cannot_run() -> None:
    rows = coverage(_contract())
    missed = unmet(rows, ["bad", "gross_from_net"], {"duckdb": "duckdb"})
    assert [(r.id, a) for r, a, _ in missed] == [("bad", "duckdb")]
    # A rule named by id must be tested; a placeholder declaration with no bound is not.
    assert [a for _, a, _ in unmet(rows, ["status.placeholders"], {"duckdb": "duckdb"})] == [
        "duckdb"
    ]
    assert len(unmet(rows, ["*"], {"duckdb": "duckdb", "athena": "athena"})) == 2


# -- the CLI ---------------------------------------------------------------------------------


def _run(tmp_path: Path, contract: Dict[str, Any], **flags: Any) -> int:
    from fluid_build.cli.generate_dbt_tests import run

    (tmp_path / "dbt_project.yml").write_text("name: p\n")
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    args = SimpleNamespace(
        contract=str(path),
        out=str(tmp_path / "models" / "schema.yml"),
        env=None,
        dbt_tests_key="data_tests",
        macros_dir=None,
        coverage=False,
        coverage_out=None,
        require_rule=[],
        require_all_rules=False,
        adapter=[],
    )
    for key, value in flags.items():
        setattr(args, key, value)
    return run(args, logging.getLogger("t"))


def test_coverage_prints_and_writes_the_report(tmp_path: Path, capsys: Any) -> None:
    out = tmp_path / "cov" / "coverage.json"
    assert _run(tmp_path, _contract(), coverage=True, coverage_out=str(out)) == 0
    printed = capsys.readouterr().out
    assert "gross_from_net" in printed and "Why not:" in printed
    assert json.loads(out.read_text())["rules"][0]["id"] == "gross_from_net"


def test_a_required_rule_no_selected_adapter_can_run_refuses_generation(tmp_path: Path) -> None:
    with pytest.raises(CLIError) as refused:
        _run(tmp_path, _contract(), require_rule=["bad"], adapter=["athena"])
    assert refused.value.event == "dbt_tests_required_rule_unsupported"
    assert "bad on athena" in refused.value.context["detail"]
    assert not (tmp_path / "models" / "schema.yml").exists()  # nothing written
    assert _run(tmp_path, _contract(), require_rule=["gross_from_net"], adapter=["athena"]) == 0


def test_require_all_rules_uses_the_contracts_platform_adapters_by_default(tmp_path: Path) -> None:
    with pytest.raises(CLIError) as refused:
        _run(tmp_path, _contract(), require_all_rules=True)
    assert "bad on duckdb" in refused.value.context["detail"]  # the exposes bind to local
    contract = _contract()
    contract["exposes"][0]["contract"]["invariants"].pop()
    assert _run(tmp_path, contract, require_all_rules=True) == 0


def test_unknown_rule_ids_and_adapters_are_refused(tmp_path: Path) -> None:
    with pytest.raises(CLIError) as refused:
        _run(tmp_path, _contract(), require_rule=["nope"])
    assert refused.value.event == "dbt_tests_unknown_required_rule"
    with pytest.raises(CLIError) as refused:
        _run(tmp_path, _contract(), require_rule=["bad"], adapter=["oracle"])
    assert refused.value.event == "dbt_tests_unknown_adapter"


def test_the_flags_are_on_the_command() -> None:
    from fluid_build.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "generate",
            "dbt-tests",
            "--coverage",
            "--coverage-out",
            "c.json",
            "--require-rule",
            "a",
            "--require-rule",
            "b",
            "--require-all-rules",
            "--adapter",
            "duckdb",
        ]
    )
    assert args.coverage and args.coverage_out == "c.json"
    assert args.require_rule == ["a", "b"] and args.require_all_rules
    assert args.adapter == ["duckdb"]


def test_a_requirement_on_rules_below_the_preview_fails(tmp_path: Path) -> None:
    contract = _contract()
    contract["fluidVersion"] = "0.7.6"
    with pytest.raises(CLIError) as refused:
        _run(tmp_path, contract, require_all_rules=True)
    assert refused.value.event == "dbt_tests_rules_need_preview"
    assert "0.7.7" in refused.value.context["detail"]
    for expose in contract["exposes"]:
        expose["contract"].pop("invariants", None)
        expose.pop("semantics", None)
        for column in expose["contract"]["schema"]:
            column.pop("placeholders", None)
    assert _run(tmp_path, contract, require_all_rules=True) == 0  # nothing declared: met
