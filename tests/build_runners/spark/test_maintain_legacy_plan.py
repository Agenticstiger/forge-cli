# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A plan made before per-action binding lists no ``actions`` in its ``spark.maintain``
payload: the per-action comparison skips it and the generic compare refuses it
(``apply_spark_artifact_mismatch``). An ``actions: []`` payload is compared as before."""

from __future__ import annotations

import copy

import pytest

from fluid_build.cli import _spark_apply as _apply
from fluid_build.cli._common import CLIError
from tests.maintenance.conftest import maintained, spark_block

pytestmark = pytest.mark.unit


def _plan(contract, drop_actions: bool):
    actions = []
    for i, a in enumerate(_apply.planned_actions(contract)):
        a = copy.deepcopy(a)
        if drop_actions and a["op"] == "spark.maintain":
            del a["payload"]["actions"]
        actions.append({"step": 4 + i, "depends_on": ["x"], **a})
    return {"actions": actions}


def test_a_payload_without_actions_is_skipped_by_the_per_action_diff() -> None:
    contract = maintained(spark_block())
    planned = _plan(contract, drop_actions=True)["actions"]
    compiled = [a for a in _apply.planned_actions(contract)]
    assert _apply.maintenance_action_changes(planned, compiled) == ([], [])


def test_a_payload_without_actions_is_refused_as_an_artifact_mismatch() -> None:
    contract = maintained(spark_block())
    with pytest.raises(CLIError) as err:
        _apply.prepare(
            contract,
            build_id=None,
            verified_plan=_plan(contract, drop_actions=True),
            dry_run=True,
        )
    assert err.value.event == "apply_spark_artifact_mismatch"


def test_an_empty_actions_list_is_still_compared_and_accepted() -> None:
    contract = maintained(spark_block())
    plan = _plan(contract, drop_actions=False)
    (maintain,) = [a for a in plan["actions"] if a["op"] == "spark.maintain"]
    assert maintain["payload"]["actions"] == []
    assert _apply.prepare(contract, build_id=None, verified_plan=plan, dry_run=True)
