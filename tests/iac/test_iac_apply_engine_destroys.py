# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The engine's side of a destroy that must wait for the apply's own (``_planned_destroys``,
``_prepare_destroys``, ``_resume_blocked_destroys``), with ``tofu`` stubbed.

Pinned: the plugin is handed only what the saved plan deletes outright; a failed apply is
resumed only when every resource it failed on is one of those deletes and no error is left
unaccounted for, the plugin cleared something, and the plan made after it holds nothing but those
deletes. Not proven here: a real ``tofu`` run (``test_iac_databricks_metrics_fake_e2e.py``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.iac.base import UnsupportedBindingError

pytestmark = [pytest.mark.unit]

SCHEMA = "databricks_schema.p_main_sales"
DESTROYED = [
    {"address": SCHEMA, "type": "databricks_schema", "before": {"id": "main.sales"}},
    {
        "address": "databricks_sql_table.p_main_sales_orders",
        "type": "databricks_sql_table",
        "before": {"id": "main.sales.orders"},
    },
]


def _result(command: str, rc: int = 0, events: Optional[List[Dict[str, Any]]] = None) -> Any:
    return engine.runner.TofuResult(command, rc, "", "", events=events or [])


def _errored(address: str, action: str = "delete") -> Dict[str, Any]:
    return {"type": "apply_errored", "hook": {"resource": {"addr": address}, "action": action}}


DIAGNOSTIC = {
    "type": "diagnostic",
    "diagnostic": {"severity": "error", "summary": "cannot delete schema: Schema is not empty"},
}


def _plan(*removed: str, add: int = 0, change: int = 0) -> Any:
    events: List[Dict[str, Any]] = [
        {
            "type": "planned_change",
            "change": {"action": "delete", "resource": {"addr": a, "resource_type": "x"}},
        }
        for a in removed
    ]
    events.append(
        {
            "type": "change_summary",
            "changes": {"add": add, "change": change, "remove": len(removed)},
        }
    )
    return _result("plan", events=events)


class Plugin:
    def __init__(self, cleared: Any = True):
        self.cleared = cleared
        self.calls: List[List[Dict[str, Any]]] = []
        self.prepared: List[Dict[str, Any]] = []

    def prepare_destroys(self, contract, destroyed, say=None, dry_run=False):
        self.prepared.append({"destroyed": destroyed, "dry_run": dry_run})

    def unblock_destroys(self, contract, blocked, say=None):
        self.calls.append(list(blocked))
        if isinstance(self.cleared, Exception):
            raise self.cleared
        return self.cleared


@pytest.fixture
def tofu(monkeypatch):
    calls: Dict[str, Any] = {
        "plan": 0,
        "apply": 0,
        "next_plan": _plan(SCHEMA),
        "shown": None,
        "applies": [],
    }

    def plan(*a, **k):
        calls["plan"] += 1
        return calls["next_plan"]

    def apply(*a, **k):
        calls["apply"] += 1
        if calls["applies"]:
            return calls["applies"].pop(0)
        return _result("apply", events=[{"type": "change_summary", "changes": {"remove": 1}}])

    def shown(*a, **k):
        calls["show"] = calls.get("show", 0) + 1
        return calls["shown"]

    monkeypatch.setattr(engine.runner, "tofu_plan", plan)
    monkeypatch.setattr(engine.runner, "tofu_apply", apply)
    monkeypatch.setattr(engine.runner, "tofu_show_plan", shown)
    return calls


def _said(capfd) -> str:
    """What the engine printed, whitespace collapsed (Rich wraps at the terminal width)."""
    return " ".join(capfd.readouterr().out.split())


def _resume(plugin: Any, *events: Dict[str, Any]) -> Any:
    failed = _result("apply", 1, list(events))
    return engine._resume_blocked_destroys(plugin, {}, failed, DESTROYED, "/wd", {})


def test_the_plugin_gets_only_what_the_plan_deletes_outright(tofu):
    def change(address: str, actions: List[str], mode: str = "managed") -> Dict[str, Any]:
        return {
            "address": address,
            "mode": mode,
            "type": address.split(".")[0],
            "change": {"actions": actions, "before": {"id": address}},
        }

    tofu["shown"] = {
        "resource_changes": [
            change("databricks_schema.gone", ["delete"]),
            change("databricks_sql_table.replaced", ["delete", "create"]),
            change("databricks_schema.new", ["create"]),
            change("databricks_grant.kept", ["no-op"]),
            change("data.databricks_catalog.c", ["delete"], mode="data"),
        ]
    }
    found = engine._planned_destroys(Plugin(), {"remove": 2}, "/wd", {})
    assert found == [
        {
            "address": "databricks_schema.gone",
            "type": "databricks_schema",
            "before": {"id": "databricks_schema.gone"},
        }
    ]


def test_nothing_is_read_for_a_plugin_without_the_hook_or_a_plan_that_removes_nothing(tofu):
    assert engine._planned_destroys(object(), {"remove": 3}, "/wd", {}) == []
    assert engine._planned_destroys(Plugin(), {"remove": 0}, "/wd", {}) == []
    assert "show" not in tofu


def test_a_plan_that_cannot_be_read_back_is_said(tofu, capfd):
    assert engine._planned_destroys(Plugin(), {"remove": 1}, "/wd", {}) == []
    assert "the saved plan could not be read back" in _said(capfd)


def test_prepare_gets_the_deletes_and_the_dry_run_flag():
    plugin = Plugin()
    engine._prepare_destroys(plugin, {}, DESTROYED, dry_run=True)
    engine._prepare_destroys(plugin, {}, [], dry_run=False)  # nothing destroyed: not called
    assert plugin.prepared == [{"destroyed": DESTROYED, "dry_run": True}]


def test_a_refusal_in_prepare_stops_the_run():
    class Refusing(Plugin):
        def prepare_destroys(self, contract, destroyed, say=None, dry_run=False):
            raise UnsupportedBindingError("databricks-x", "refused", ("fix it",))

    with pytest.raises(CLIError) as exc:
        engine._prepare_destroys(Refusing(), {}, DESTROYED)
    assert exc.value.event == "opentofu_destroy_preparation_failed"


def test_a_destroy_that_waited_is_resumed_once_the_plugin_cleared_it(tofu, capfd):
    plugin = Plugin()
    resumed = _resume(plugin, _errored(SCHEMA), DIAGNOSTIC)
    assert resumed is not None
    second, waited = resumed
    assert waited == 1 and second.ok
    assert plugin.calls == [[DESTROYED[0]]]
    assert tofu["plan"] == 1 and tofu["apply"] == 1
    assert "tofu plan (the destroys that waited): +0 ~0 -1" in _said(capfd)


@pytest.mark.parametrize(
    "events",
    [
        pytest.param([_errored("databricks_schema.unplanned"), DIAGNOSTIC], id="not-planned"),
        pytest.param([_errored(SCHEMA, "create"), DIAGNOSTIC], id="a-create-failed"),
        pytest.param([_errored(SCHEMA), DIAGNOSTIC, DIAGNOSTIC], id="an-unaccounted-error"),
        pytest.param([DIAGNOSTIC], id="no-resource-failed"),
        pytest.param(
            [_errored(SCHEMA), _errored("databricks_grant.g", "create"), DIAGNOSTIC, DIAGNOSTIC],
            id="another-resource-failed",
        ),
    ],
)
def test_any_other_failure_is_left_as_it_is(tofu, events):
    plugin = Plugin()
    assert _resume(plugin, *events) is None
    assert plugin.calls == [] and tofu["plan"] == 0 and tofu["apply"] == 0


@pytest.mark.parametrize("cleared", [False, RuntimeError("boom")], ids=["nothing", "raised"])
def test_nothing_cleared_is_not_resumed(tofu, cleared):
    assert _resume(Plugin(cleared), _errored(SCHEMA), DIAGNOSTIC) is None
    assert tofu["plan"] == 0 and tofu["apply"] == 0


@pytest.mark.parametrize(
    "plan",
    [
        pytest.param(lambda: _plan(SCHEMA, add=1), id="adds"),
        pytest.param(lambda: _plan(SCHEMA, change=1), id="changes"),
        pytest.param(lambda: _plan("databricks_schema.other"), id="removes-another"),
        pytest.param(lambda: _plan(), id="removes-nothing"),
        pytest.param(lambda: _result("plan", 1), id="fails"),
    ],
)
def test_a_second_plan_holding_anything_else_is_not_applied(tofu, capfd, plan):
    tofu["next_plan"] = plan()
    assert _resume(Plugin(), _errored(SCHEMA), DIAGNOSTIC) is None
    assert tofu["plan"] == 1 and tofu["apply"] == 0
    assert "not destroyed again: the plan made after the apply holds more than" in _said(capfd)


def test_a_plugin_without_the_hook_is_never_resumed(tofu):
    class Bare:
        pass

    failed = _result("apply", 1, [_errored(SCHEMA), DIAGNOSTIC])
    assert engine._resume_blocked_destroys(Bare(), {}, failed, DESTROYED, "/wd", {}) is None
    assert tofu["plan"] == 0


def test_a_clean_apply_is_not_resumed(tofu):
    result, waited = engine._apply_saved_plan(Plugin(), {}, DESTROYED, "/wd", {})
    assert result.ok and waited == 0
    assert tofu["apply"] == 1 and tofu["plan"] == 0


def test_an_apply_that_failed_only_at_a_destroy_that_waited_is_resumed(tofu):
    tofu["applies"].append(_result("apply", 1, [_errored(SCHEMA), DIAGNOSTIC]))
    result, waited = engine._apply_saved_plan(Plugin(), {}, DESTROYED, "/wd", {})
    assert result.ok and waited == 1
    assert tofu["apply"] == 2 and tofu["plan"] == 1


def test_an_apply_that_failed_otherwise_keeps_its_own_failure(tofu):
    failed = _result("apply", 1, [DIAGNOSTIC])
    tofu["applies"].append(failed)
    result, waited = engine._apply_saved_plan(Plugin(), {}, DESTROYED, "/wd", {})
    assert result is failed and waited == 0
    assert tofu["apply"] == 1 and tofu["plan"] == 0


def test_after_a_resume_the_totals_are_the_plans_own():
    def summary(**changes: int) -> Any:
        return _result("apply", events=[{"type": "change_summary", "changes": changes}])

    planned = {"add": 11, "change": 0, "remove": 11}
    second = summary(add=0, change=0, remove=1)
    assert engine._applied(planned, second, 1) == planned
    alone = summary(add=2, change=1, remove=0)
    assert engine._applied({"add": 9, "change": 9, "remove": 9}, alone, 0) == {
        "add": 2,
        "change": 1,
        "remove": 0,
    }
