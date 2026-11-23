# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Each plugin maintenance action is bound one by one in the plan: ``spark.maintain`` lists its
name, kind, shipping package and the sha256 of its statements, and ``fluid apply`` refuses an
action whose SQL changed (naming it, never the SQL) and only warns about a package that changed
under the same SQL."""

from __future__ import annotations

import copy
import json
import logging

import pytest

from fluid_build.cli import _spark_apply as _apply
from fluid_build.cli._common import CLIError
from fluid_build.forge.core.plan_digest import (
    MAINTENANCE_ACTIONS_CHANGED,
    PlanBindingError,
    compute_plan_digest,
    verify_plan_binding,
)
from fluid_build.maintenance import plugins
from fluid_build.maintenance.job import compiled_actions
from fluid_build.maintenance.plugins import action_origin, reset_maintenance_plugin_cache
from fluid_build.maintenance.spec import CustomAction, TableMaintenance
from tests.maintenance.conftest import FakeEntryPoint

EVENT = "apply_plan_digest_maintenance_actions_changed"
OLD_SQL = "DELETE FROM {table} WHERE id = 1"
NEW_SQL = "DELETE FROM {table} WHERE id = 2"
TABLE = "fluid.commerce.order_lines"
ACTION = {"name": "erase", "kind": "gdpr-erasure"}
ENTRY_KEYS = {"name", "kind", "distribution", "version", "statementsSha256"}


def _kind(sql):
    class Erasure:
        def statements(self, table, options):
            return [sql.format(table=table)]

    return Erasure


@pytest.fixture(autouse=True)
def clean_plugin_cache():
    reset_maintenance_plugin_cache()
    yield
    reset_maintenance_plugin_cache()


@pytest.fixture
def install(monkeypatch):
    """``install(sql, version="1.0", name="gdpr-erasure", dist="acme-pack")``: one fake entry
    point of the maintenance group, replacing the last one."""
    from fluid_build import plugin_manager

    def go(sql=OLD_SQL, version="1.0", name="gdpr-erasure", dist="acme-pack"):
        ep = FakeEntryPoint(name, _kind(sql), dist=dist)
        ep.dist.version = version
        monkeypatch.setattr(
            plugin_manager,
            "_entry_points",
            lambda group: [ep] if group == "fluid_build.maintenance_actions" else [],
        )
        reset_maintenance_plugin_cache()
        return ep

    return go


@pytest.fixture
def maintained(contract, install):
    install()
    contract["exposes"][0]["iceberg"] = {
        "maintenance": {
            "schedule": "0 3 * * *",
            "compaction": {},
            "actions": [dict(ACTION)],
        }
    }
    return contract


def _bound_plan(contract, **kwargs):
    return {
        "actions": [
            {"step": 4 + i, "depends_on": ["x"], **a}
            for i, a in enumerate(_apply.planned_actions(contract))
        ],
        **kwargs,
    }


def _maintain(contract):
    (action,) = [a for a in _apply.planned_actions(contract) if a["op"] == "spark.maintain"]
    return action


def _refusal(contract, plan):
    with pytest.raises(CLIError) as err:
        _apply.prepare(contract, build_id=None, verified_plan=plan, dry_run=True)
    assert err.value.event == EVENT
    assert err.value.context["kind"] == MAINTENANCE_ACTIONS_CHANGED
    return err.value


# --- the payload ------------------------------------------------------------------------


def test_the_payload_lists_each_action_with_its_hash_and_package(maintained):
    payload = _maintain(maintained)["payload"]
    (entry,) = payload["actions"]
    assert set(entry) == ENTRY_KEYS
    assert (entry["name"], entry["kind"]) == ("erase", "gdpr-erasure")
    assert (entry["distribution"], entry["version"]) == ("acme-pack", "1.0")
    sha = entry["statementsSha256"]
    assert len(sha) == 64 and sha == sha.lower() and int(sha, 16) >= 0
    text = json.dumps(payload)
    assert "DELETE" not in text and "WHERE" not in text


def test_a_table_without_plugin_actions_lists_none(contract):
    contract["exposes"][0]["iceberg"] = {"maintenance": {"schedule": "0 3 * * *", "compaction": {}}}
    assert _maintain(contract)["payload"]["actions"] == []


def test_other_statements_give_another_hash(maintained, install):
    before = _maintain(maintained)["payload"]["actions"][0]["statementsSha256"]
    install(NEW_SQL)
    after = _maintain(maintained)["payload"]["actions"][0]["statementsSha256"]
    assert before != after


def test_planning_twice_gives_one_plan_digest(maintained):
    def plan():
        return {"actions": _apply.planned_actions(maintained), "bindingMode": "raw"}

    assert compute_plan_digest(plan()) == compute_plan_digest(plan())


def test_a_hand_edited_statements_hash_is_a_plan_tamper(maintained):
    plan = {"bindingMode": "raw", "actions": _apply.planned_actions(maintained)}
    plan["planDigest"] = compute_plan_digest(plan)
    verify_plan_binding(plan, bundle_path=None)  # the untouched plan passes
    forged = copy.deepcopy(plan)
    maintain = next(a for a in forged["actions"] if a["op"] == "spark.maintain")
    maintain["payload"]["actions"][0]["statementsSha256"] = "0" * 64
    with pytest.raises(PlanBindingError) as err:
        verify_plan_binding(forged, bundle_path=None)
    assert err.value.kind == "plan-tamper"


# --- prepare ----------------------------------------------------------------------------


def test_prepare_takes_the_plan_it_made(maintained):
    plan = _bound_plan(maintained)
    assert _apply.prepare(maintained, build_id=None, verified_plan=plan, dry_run=True)


def test_prepare_refuses_statements_that_changed_since_the_plan(maintained, install):
    plan = _bound_plan(maintained)
    install(NEW_SQL)
    err = _refusal(maintained, plan)
    message = err.context["error"]
    assert "exposes[order_lines].iceberg.maintenance.actions[erase]" in message
    assert message.count("acme-pack 1.0") == 2
    assert "SQL hash" in message and "differs" in message
    for shown in (message, json.dumps(err.context), str(err)):
        assert "DELETE" not in shown and "WHERE" not in shown


def test_a_version_bump_under_the_same_sql_only_warns(maintained, install, caplog, monkeypatch):
    plan = _bound_plan(maintained)
    install(OLD_SQL, version="2.0")
    shown = []
    monkeypatch.setattr("fluid_build.cli.console.warning", shown.append)
    with caplog.at_level(logging.WARNING, logger="fluid_build.cli._spark_apply"):
        assert _apply.prepare(maintained, build_id=None, verified_plan=plan, dry_run=True)
    logged = [
        r.getMessage()
        for r in caplog.records
        if "spark_maintenance_action_provenance" in r.getMessage()
    ]
    assert len(logged) == 1 and len(shown) == 1
    for line in (logged[0], shown[0]):
        assert "acme-pack 1.0" in line and "acme-pack 2.0" in line
        assert "DELETE" not in line


def test_an_action_added_since_the_plan_is_refused(maintained):
    plan = _bound_plan(maintained)
    maintained["exposes"][0]["iceberg"]["maintenance"]["actions"].append(
        {"name": "second", "kind": "gdpr-erasure"}
    )
    message = _refusal(maintained, plan).context["error"]
    assert "actions[second]" in message and "not planned" in message


def test_an_action_removed_since_the_plan_is_refused(maintained):
    plan = _bound_plan(maintained)
    maintained["exposes"][0]["iceberg"]["maintenance"]["actions"] = []
    message = _refusal(maintained, plan).context["error"]
    assert "actions[erase]" in message and "not compiled now" in message


# --- provenance -------------------------------------------------------------------------


def test_a_kind_from_a_test_loader_has_no_package():
    tm = TableMaintenance(
        expose_id="order_lines",
        backend="spark",
        database="commerce",
        table="order_lines",
        actions=(CustomAction("erase", "gdpr-erasure"),),
    )
    (action,) = compiled_actions(tm, TABLE, load=lambda name: _kind(OLD_SQL)())
    assert (action.distribution, action.version) == (None, None)
    assert action.plan_entry()["distribution"] is None
    assert action_origin("gdpr-erasure") == (None, None)


def test_the_registry_records_the_package(install):
    install()
    plugins.action_for("gdpr-erasure")
    assert action_origin("gdpr-erasure") == ("acme-pack", "1.0")


@pytest.mark.parametrize(
    "dist, version",
    [
        ("acme pack", "1.0"),
        ("acme\x07pack", "1.0"),
        ("acme[pack]", "1.0"),
        ("acme-pack", "1 0"),
        ("acme-pack", "1.0\n"),
        ("acme-pack", "[1.0]"),
    ],
)
def test_an_odd_name_or_version_is_not_recorded(install, dist, version):
    install(dist=dist, version=version)
    plugins.action_for("gdpr-erasure")
    recorded_dist, recorded_version = action_origin("gdpr-erasure")
    assert None in (recorded_dist, recorded_version)
    assert (recorded_dist, recorded_version) != ("acme-pack", "1.0")
    # A part that does not read is unknown, never cut down to a name that would read.
    assert recorded_dist in (None, dist) and recorded_version in (None, version)


def test_a_name_with_a_space_is_unknown_not_truncated(install):
    install(dist="acme pack", version="1.0")
    plugins.action_for("gdpr-erasure")
    assert action_origin("gdpr-erasure") == (None, "1.0")


# --- maintenance_action_changes ---------------------------------------------------------


def _action(**entries):
    return {
        "op": "spark.maintain",
        "id": "order_lines",
        "payload": {"actions": list(entries.values())},
    }


def _entry(name="erase", sha="a" * 64, dist="acme-pack", version="1.0"):
    return {
        "name": name,
        "kind": "k",
        "distribution": dist,
        "version": version,
        "statementsSha256": sha,
    }


def test_identical_actions_change_nothing():
    a = _action(e=_entry())
    assert _apply.maintenance_action_changes([a], [copy.deepcopy(a)]) == ([], [])


def test_a_changed_hash_is_a_refusal_naming_both_prefixes():
    refusals, warnings = _apply.maintenance_action_changes(
        [_action(e=_entry(sha="a" * 64))], [_action(e=_entry(sha="b" * 64))]
    )
    assert warnings == [] and len(refusals) == 1
    assert "aaaaaaaaaaaa" in refusals[0] and "bbbbbbbbbbbb" in refusals[0]
    assert "a" * 13 not in refusals[0]


def test_a_changed_package_is_a_warning():
    refusals, warnings = _apply.maintenance_action_changes(
        [_action(e=_entry())], [_action(e=_entry(version="2.0"))]
    )
    assert refusals == [] and len(warnings) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"op": "spark.maintain", "id": "order_lines", "payload": {"actions": "nope"}},
        {"op": "spark.maintain", "id": "order_lines", "payload": {"actions": [{"x": 1}, 5, None]}},
        {"op": "spark.maintain", "id": "order_lines", "payload": None},
        {"op": "spark.maintain", "id": "order_lines"},
    ],
)
def test_malformed_payloads_do_not_raise(bad):
    good = _action(e=_entry())
    for planned, compiled in (([bad], [good]), ([good], [bad]), ([bad], [bad])):
        refusals, warnings = _apply.maintenance_action_changes(planned, compiled)
        assert isinstance(refusals, list) and isinstance(warnings, list)
