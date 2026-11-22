# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A live Iceberg table the plan would update in place, in the OpenTofu engine, with
``tofu`` faked.

The table the state holds is planned again with its attributes left alone, the note says
what the contract wanted, and ``--allow-iceberg-update`` lets the update through, audited.
A Hive table keeps its in-place update. The fake ``tofu`` (``tests/iac/
test_iac_table_adoption_engine.py``) answers each ``tofu show -json`` with the next plan of
``shown``; ``tests/iac/test_iac_aws_iceberg_update_hold_moto.py`` runs the same flow with
the real ``tofu`` against moto.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from tests.iac.test_iac_table_adoption_engine import (  # noqa: F401 - tofu is a fixture
    BUCKET,
    CONTRACT,
    DATABASE,
    TABLE,
    _args,
    _glue_state,
    _held_attributes,
    _imported,
    _plan,
    tofu,
)

pytestmark = pytest.mark.unit

_LOG = logging.getLogger("test.iac.iceberg_hold")
#: Every resource the module declares is already in state: nothing to look up.
IN_STATE = [TABLE, BUCKET, DATABASE]
ONE_UPDATE = {"add": 0, "change": 1, "remove": 0}


def _in_state(actions: List[str], before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """The table the state holds, as the plan changes it: no import."""
    change = _imported(actions, before, after)
    del change["change"]["importing"]
    return {"resource_changes": [change]}


def _described(text: str) -> Dict[str, Any]:
    return dict(_glue_state(), description=text)


#: The contract gives the table a description it does not have.
UPDATE = _in_state(["update"], _glue_state(), _described("Order events"))
#: The plan made again, with the table's attributes left alone.
HELD = _in_state(["no-op"], _glue_state(), _glue_state())

HIVE_CONTRACT = copy.deepcopy(CONTRACT)
HIVE_CONTRACT["exposes"][0]["binding"]["format"] = "parquet"


@pytest.fixture
def planned(tofu):
    """The table is in state, and the plan updates it in place."""
    tofu["in_state"] = list(IN_STATE)
    tofu["summary"] = dict(ONE_UPDATE)
    return tofu


def _events(caplog: pytest.LogCaptureFixture, event: str) -> List[logging.LogRecord]:
    found = [r for r in caplog.records if f'"message": "{event}"' in r.getMessage()]
    assert all(record.levelno == logging.WARNING for record in found)
    return found


def _applied(tmp_path, capfd, caplog, **over: Any) -> str:
    """``fluid apply <contract>`` succeeds; what it printed."""
    with caplog.at_level(logging.WARNING, logger=_LOG.name):
        assert engine.apply_via_opentofu(_args(tmp_path, **over), _LOG) == 0
    return " ".join(capfd.readouterr().out.split())


def test_a_live_iceberg_table_the_state_holds_is_left_as_it_is(planned, tmp_path, capfd, caplog):
    planned["shown"] = [UPDATE, HELD]

    out = _applied(tmp_path, capfd, caplog)

    first, again = planned["plans"]
    assert _held_attributes(first) == ["parameters", "open_table_format_input"]
    assert _held_attributes(again)[2:] == [
        "description",
        "owner",
        "retention",
        "storage_descriptor",
    ]
    assert planned["apply"] == 1
    assert "(again: live Iceberg tables left as they are)" in out
    assert "sales.orders is a live Iceberg table" in out
    assert "description: none in the table, Order events in the contract" in out
    assert "re-run with --allow-iceberg-update" in out
    assert len(_events(caplog, "iceberg_table_update_held")) == 1
    assert not _events(caplog, "iceberg_table_update_accepted")


def test_with_the_flag_the_plan_updates_it_and_the_apply_audits_it(
    planned, tmp_path, capfd, caplog
):
    planned["shown"] = [UPDATE]

    out = _applied(tmp_path, capfd, caplog, allow_iceberg_update=True)

    (only,) = planned["plans"]
    assert "storage_descriptor" not in _held_attributes(only) and planned["apply"] == 1
    assert "sales.orders is updated in place (--allow-iceberg-update)" in out
    (accepted,) = _events(caplog, "iceberg_table_update_accepted")
    assert '"table": "sales.orders"' in accepted.getMessage()
    assert not _events(caplog, "iceberg_table_update_held")


@pytest.mark.parametrize("flag", [False, True])
def test_a_dry_run_audits_no_update(planned, tmp_path, caplog, flag):
    planned["shown"] = [UPDATE] if flag else [UPDATE, HELD]

    with caplog.at_level(logging.WARNING, logger=_LOG.name):
        assert (
            engine.apply_via_opentofu(
                _args(tmp_path, dry_run=True, allow_iceberg_update=flag), _LOG
            )
            == 0
        )

    assert planned["apply"] == 0
    assert not _events(caplog, "iceberg_table_update_accepted")


def test_a_table_the_plan_made_again_still_updates_is_refused(planned, tmp_path):
    planned["shown"] = [UPDATE, UPDATE]

    with pytest.raises(CLIError) as raised:
        engine.apply_via_opentofu(_args(tmp_path), _LOG)

    assert raised.value.event == "iceberg_table_update_in_place"
    assert "could not be planned without an in-place update" in raised.value.context["error"]
    assert planned["apply"] == 0


def test_a_hive_table_the_plan_updates_keeps_its_in_place_update(planned, tmp_path, capfd):
    planned["shown"] = [UPDATE]

    assert engine.apply_via_opentofu(_args(tmp_path, contract=HIVE_CONTRACT), _LOG) == 0

    (only,) = planned["plans"]
    assert planned["apply"] == 1
    assert "live Iceberg table" not in capfd.readouterr().out


def test_plan_tofu_saves_the_plan_that_leaves_it_alone_and_records_no_update(planned, tmp_path):
    planned["shown"] = [UPDATE, HELD]

    record = _plan(tmp_path)

    assert "icebergUpdates" not in record
    _first, again = planned["plans"]
    assert "storage_descriptor" in _held_attributes(again)


def test_plan_tofu_with_the_flag_records_the_table_it_updates_in_place(planned, tmp_path):
    planned["shown"] = [UPDATE]

    record = _plan(tmp_path, allow_iceberg_update=True)

    assert record["icebergUpdates"] == ["sales.orders"]
    (only,) = planned["plans"]
    assert "storage_descriptor" not in _held_attributes(only)
