# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``--allow-iceberg-update`` through the real ``fluid`` entry point, with ``tofu`` faked.

The engine flow is pinned in ``tests/iac/test_iac_iceberg_update_hold_engine.py`` and run
against the real ``tofu`` and moto in ``tests/iac/test_iac_aws_iceberg_update_hold_moto.py``;
here the flags, the note and the refusals as the CLI renders them, and the plan-to-apply
binding.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fluid_build.forge.core.plan_digest import TOFU_PLAN_FIELD, inject_digests, verify_plan_binding
from tests.cli.test_adopt_tables_cli import _contract, _main, _out
from tests.iac.test_iac_iceberg_update_hold_engine import HELD, IN_STATE, ONE_UPDATE, UPDATE
from tests.iac.test_iac_table_adoption_engine import tofu  # noqa: F401 - a fixture

pytestmark = pytest.mark.unit

FLAG = "--allow-iceberg-update"


@pytest.fixture
def planned(tofu):  # noqa: F811 - the fixture above
    """The Iceberg table is in state, and the plan updates it in place."""
    tofu["in_state"] = list(IN_STATE)
    tofu["summary"] = dict(ONE_UPDATE)
    return tofu


def test_the_flag_is_off_unless_given_on_apply_plan_and_ship():
    from fluid_build.cli import build_parser

    parser = build_parser()

    assert parser.parse_args(["apply", "c.fluid.yaml"]).allow_iceberg_update is False
    assert parser.parse_args(["apply", "c.fluid.yaml", FLAG]).allow_iceberg_update is True
    assert parser.parse_args(["plan", "c.fluid.yaml"]).allow_iceberg_update is False
    assert parser.parse_args(["plan", "c.fluid.yaml", "--tofu", FLAG]).allow_iceberg_update
    assert parser.parse_args(["ship", "c.fluid.yaml"]).allow_iceberg_update is False
    assert parser.parse_args(["ship", "c.fluid.yaml", FLAG]).allow_iceberg_update is True


@pytest.mark.parametrize("command", ["apply", "plan", "ship"])
def test_help_names_the_flag(command: str, capfd):
    try:
        code = _main(command, "--help")
    except SystemExit as done:  # argparse's own help exits
        code = int(done.code or 0)

    assert code == 0
    assert FLAG in capfd.readouterr().out


def test_plan_without_tofu_refuses_the_flag_and_writes_no_plan(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    contract = _contract(tmp_path)

    assert _main("plan", str(contract), FLAG) == 1

    out = _out(capsys)
    assert "ERR_PLAN_ALLOW_ICEBERG_UPDATE_NEEDS_TOFU" in out and "--tofu" in out
    assert not (tmp_path / "plan.json").exists()


def test_apply_leaves_a_live_iceberg_table_as_it_is_and_says_what_waits(planned, tmp_path, capsys):
    planned["shown"] = [UPDATE, HELD]
    contract = _contract(tmp_path)

    assert _main("apply", str(contract), "--yes") == 0

    captured = capsys.readouterr()
    out = " ".join(captured.out.split())
    assert "sales.orders is a live Iceberg table" in out
    assert "description: none in the table, Order events in the contract" in out
    assert "change it through the table's Iceberg catalog" in out
    assert "iceberg_table_update_held" in captured.err + captured.out
    assert len(planned["plans"]) == 2 and planned["apply"] == 1


def test_apply_with_the_flag_updates_it_and_audits_the_update(planned, tmp_path, capsys):
    planned["shown"] = [UPDATE]
    contract = _contract(tmp_path)

    assert _main("apply", str(contract), "--yes", FLAG) == 0

    captured = capsys.readouterr()
    assert "updated in place with no VersionId" in " ".join(captured.out.split())
    assert "iceberg_table_update_accepted" in captured.err + captured.out
    assert len(planned["plans"]) == 1 and planned["apply"] == 1


def test_plan_tofu_binds_the_update_and_applying_that_plan_audits_it(planned, tmp_path, capsys):
    planned["shown"] = UPDATE
    contract = _contract(tmp_path)

    assert _main("plan", str(contract), "--tofu", FLAG) == 0

    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert plan[TOFU_PLAN_FIELD]["icebergUpdates"] == ["sales.orders"]
    verify_plan_binding(plan)
    capsys.readouterr()

    assert _main("apply", "plan.json", "--yes") == 0

    captured = capsys.readouterr()
    out = " ".join(captured.out.split())
    assert "the saved plan updates in place, as its plan asked" in out
    assert "iceberg_table_update_accepted" in captured.err + captured.out
    assert planned["apply"] == 1


def _plan_without_its_update_record(planned, tmp_path: Path, *, bind_again: bool) -> None:
    """``fluid plan --tofu --allow-iceberg-update``, then its record of the update removed:
    as an edit (the digest no longer matches), or bound again (a plan made before the hold)."""
    planned["shown"] = UPDATE
    assert _main("plan", str(_contract(tmp_path)), "--tofu", FLAG) == 0
    path = tmp_path / "plan.json"
    plan = json.loads(path.read_text(encoding="utf-8"))
    del plan[TOFU_PLAN_FIELD]["icebergUpdates"]
    path.write_text(json.dumps(inject_digests(plan, force=True) if bind_again else plan))


def test_an_edited_update_record_is_refused_as_plan_tamper(planned, tmp_path, capsys):
    _plan_without_its_update_record(planned, tmp_path, bind_again=False)
    capsys.readouterr()

    assert _main("apply", "plan.json", "--yes") == 1

    assert "PLAN_TAMPER" in _out(capsys)
    assert planned["apply"] == 0


def test_a_saved_plan_that_updates_a_table_its_record_does_not_name_is_refused(
    planned, tmp_path, capsys
):
    """A saved plan made by a release before the hold, bound again: it updates the table in
    place with no record of it, and the apply's own flag cannot add one."""
    _plan_without_its_update_record(planned, tmp_path, bind_again=True)
    capsys.readouterr()

    assert _main("apply", "plan.json", "--yes", FLAG) == 1

    out = _out(capsys)
    assert "ERR_ICEBERG_TABLE_UPDATE_IN_PLACE" in out
    assert "its record does not name" in out and "sales.orders" in out
    assert f"{FLAG} changes nothing here" in out
    assert planned["apply"] == 0


@pytest.mark.parametrize("flag", [False, True])
def test_under_the_binding_escape_hatch_an_edited_record_lets_nothing_through(
    planned, tmp_path, capsys, flag
):
    """``--no-verify-plan-binding`` skips the digest that would refuse an edited record, so
    the record is ignored: the update is refused and nothing is applied, unless the apply's
    own command line carries the flag, which then makes it and audits it."""
    _plan_without_its_update_record(planned, tmp_path, bind_again=False)
    path = tmp_path / "plan.json"
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan[TOFU_PLAN_FIELD]["icebergUpdates"] = ["sales.orders"]
    path.write_text(json.dumps(plan), encoding="utf-8")
    capsys.readouterr()

    argv = ["apply", "plan.json", "--yes", "--no-verify-plan-binding", *([FLAG] if flag else [])]
    code = _main(*argv)

    captured = capsys.readouterr()
    out = " ".join(captured.out.split())
    assert "iceberg_table_update_record_ignored" in captured.err + captured.out
    assert "is not bound to it, so it is ignored" in out
    if flag:
        assert code == 0 and planned["apply"] == 1
        assert "iceberg_table_update_accepted" in captured.err + captured.out
    else:
        assert code == 1 and planned["apply"] == 0
        assert "ERR_ICEBERG_TABLE_UPDATE_IN_PLACE" in out


def test_ship_passes_the_flag_to_its_apply_stage_only(tmp_path: Path):
    """``fluid ship --allow-iceberg-update`` reaches ``fluid apply`` (stage 7), never the
    plan of stage 6; a flag only: no environment variable sets it."""
    from fluid_build.cli import _ship_stages as st
    from fluid_build.cli import build_parser

    parser = build_parser()
    ws = st.Workspace(tmp_path)
    on = st.resolve_options(parser.parse_args(["ship", "c.fluid.yaml", FLAG]), "c.fluid.yaml", {})
    off = st.resolve_options(
        parser.parse_args(["ship", "c.fluid.yaml"]),
        "c.fluid.yaml",
        {"ALLOW_ICEBERG_UPDATE": "true", "FLUID_ALLOW_ICEBERG_UPDATE": "1"},
    )

    assert FLAG in (st.build_stage(7, on, ws).argv or [])
    assert FLAG not in (st.build_stage(6, on, ws).argv or [])
    assert FLAG not in (st.build_stage(7, off, ws).argv or [])
