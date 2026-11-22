# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``--adopt-tables`` through the real ``fluid`` entry point, with ``tofu`` faked.

The engine flow is pinned in ``tests/iac/test_iac_table_adoption_engine.py`` and run
against the real ``tofu`` and moto in ``tests/iac/test_iac_aws_table_adoption_moto.py``;
here the flags, the refusals as the CLI renders them, and the plan-to-apply binding.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

from fluid_build.forge.core.plan_digest import TOFU_PLAN_FIELD, verify_plan_binding
from tests.iac.test_iac_table_adoption_engine import (  # noqa: F401 - tofu is a fixture
    ACCOUNT,
    CONTRACT,
    DIFFERING,
    TABLE,
    _made_by_forge,
    tofu,
)

pytestmark = pytest.mark.unit

#: The engine tests' contract, schema-valid for ``fluid plan`` and ``fluid apply``.
VALID: Dict[str, Any] = {
    "fluidVersion": "0.7.7",
    "kind": "DataProduct",
    "metadata": {"owner": {"team": "sales"}},
    **copy.deepcopy(CONTRACT),
}
VALID["exposes"][0]["kind"] = "table"


def _contract(tmp_path: Path) -> Path:
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(VALID, sort_keys=False), encoding="utf-8")
    return path


def _main(*argv: str) -> int:
    from fluid_build.cli import main

    return main(list(argv))


def _out(capsys: pytest.CaptureFixture) -> str:
    return " ".join(capsys.readouterr().out.split())


def test_the_flag_is_off_unless_given_on_apply_and_on_plan():
    from fluid_build.cli import build_parser

    parser = build_parser()

    assert parser.parse_args(["apply", "c.fluid.yaml"]).adopt_tables is False
    assert parser.parse_args(["apply", "c.fluid.yaml", "--adopt-tables"]).adopt_tables is True
    assert parser.parse_args(["plan", "c.fluid.yaml"]).adopt_tables is False
    assert parser.parse_args(["plan", "c.fluid.yaml", "--tofu", "--adopt-tables"]).adopt_tables


@pytest.mark.parametrize("command", ["apply", "plan"])
def test_help_names_the_flag(command: str, capfd):
    try:
        code = _main(command, "--help")
    except SystemExit as done:  # argparse's own help exits
        code = int(done.code or 0)

    assert code == 0
    assert "--adopt-tables" in capfd.readouterr().out


def test_plan_without_tofu_refuses_the_flag_and_writes_no_plan(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    contract = _contract(tmp_path)

    assert _main("plan", str(contract), "--adopt-tables") == 1

    out = _out(capsys)
    assert "ERR_PLAN_ADOPT_TABLES_NEEDS_TOFU" in out and "--tofu" in out
    assert not (tmp_path / "plan.json").exists()


def test_apply_refuses_an_existing_table_without_the_flag(tofu, tmp_path, capsys):
    contract = _contract(tmp_path)

    assert _main("apply", str(contract), "--yes") == 1

    out = _out(capsys)
    assert "ERR_TABLE_ADOPTION_REQUIRES_FLAG" in out
    assert "sales.orders" in out and "--adopt-tables" in out
    assert "recorded no product id" not in out and "fluid_product_id=sales.orders" not in out
    assert tofu["plans"] == [] and tofu["apply"] == 0


def test_apply_names_the_one_time_remedy_for_a_forge_made_table_without_a_product_id(
    tofu, tmp_path, capsys, monkeypatch
):
    _made_by_forge(monkeypatch, product=None)
    contract = _contract(tmp_path)

    assert _main("apply", str(contract), "--yes") == 1

    out = _out(capsys)
    assert "ERR_TABLE_ADOPTION_REQUIRES_FLAG" in out
    assert "forge made sales.orders but recorded no product id on it" in out
    assert "add fluid_product_id=sales.orders to its Parameters" in out
    assert "--version-id <the VersionId it read>" in out
    assert tofu["plans"] == [] and tofu["apply"] == 0


def test_apply_with_the_flag_adopts_a_matching_table_through_an_import_block(
    tofu, tmp_path, capsys
):
    contract = _contract(tmp_path)

    assert _main("apply", str(contract), "--yes", "--adopt-tables") == 0

    (module,) = tofu["plans"]
    assert {"to": TABLE, "id": f"{ACCOUNT}:sales:orders"} in module["import"]
    assert tofu["apply"] == 1
    assert "sales.orders matches the contract" in _out(capsys)


def test_apply_with_the_flag_refuses_a_table_that_differs(tofu, tmp_path, capsys):
    tofu["shown"] = DIFFERING
    contract = _contract(tmp_path)

    assert _main("apply", str(contract), "--yes", "--adopt-tables") == 1

    out = _out(capsys)
    assert "ERR_TABLE_ADOPTION_MISMATCH" in out
    assert "column amount: decimal(10,2) in the table, decimal(12,2) in the contract" in out
    assert tofu["apply"] == 0


def test_plan_tofu_binds_the_adoption_and_applying_that_plan_runs_it(tofu, tmp_path, capsys):
    contract = _contract(tmp_path)

    assert _main("plan", str(contract), "--tofu", "--adopt-tables") == 0

    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert plan[TOFU_PLAN_FIELD]["adoptedTables"] == ["sales.orders"]
    verify_plan_binding(plan)
    capsys.readouterr()

    assert _main("apply", "plan.json", "--yes") == 0

    captured = capsys.readouterr()
    assert "the saved plan adopts sales.orders" in " ".join(captured.out.split())
    assert tofu["apply"] == 1
    # The run that applies the saved plan audits the take-over (the CLI logs it to stderr).
    assert "table_adoption_accepted" in captured.err + captured.out


def test_ship_passes_the_flag_to_its_apply_stage_only(tmp_path: Path):
    """``fluid ship --adopt-tables`` reaches ``fluid apply`` (stage 7), never the plan of
    stage 6, which runs without ``--tofu``; a flag only, as ``--yes`` is."""
    from fluid_build.cli import _ship_stages as st
    from fluid_build.cli import build_parser

    parser = build_parser()
    ws = st.Workspace(tmp_path)
    on = st.resolve_options(
        parser.parse_args(["ship", "c.fluid.yaml", "--adopt-tables"]), "c.fluid.yaml", {}
    )
    off = st.resolve_options(
        parser.parse_args(["ship", "c.fluid.yaml"]), "c.fluid.yaml", {"ADOPT_TABLES": "true"}
    )

    assert "--adopt-tables" in (st.build_stage(7, on, ws).argv or [])
    assert "--adopt-tables" not in (st.build_stage(6, on, ws).argv or [])
    assert "--adopt-tables" not in (st.build_stage(7, off, ws).argv or [])


def test_an_edited_adoption_record_is_refused_as_plan_tamper(tofu, tmp_path, capsys):
    contract = _contract(tmp_path)
    assert _main("plan", str(contract), "--tofu", "--adopt-tables") == 0
    path = tmp_path / "plan.json"
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan[TOFU_PLAN_FIELD]["adoptedTables"] = []
    path.write_text(json.dumps(plan), encoding="utf-8")
    capsys.readouterr()

    assert _main("apply", "plan.json", "--yes") == 1

    assert "PLAN_TAMPER" in _out(capsys)
    assert tofu["apply"] == 0
