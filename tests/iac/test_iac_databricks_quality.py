# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Databricks data quality monitoring, Stage 1 unit: what asks for it, the monitor per schema,
its adoption by a derived import id, the apply notice and the ungated removal.

Pure functions only: no ``tofu``, no network. ``test_iac_databricks_quality_fake_e2e.py`` pins
what real ``tofu`` and the provider do with the module against the stand-in workspace.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, List

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.iac import IAC_PLUGINS, build_module
from fluid_build.iac.importer import ImportBlock, import_section
from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.providers import databricks_quality as dq
from fluid_build.iac.runner import TofuResult

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

_LOG = logging.getLogger("test.iac.databricks_quality")
MONITOR = "databricks_data_quality_monitor"
KEY = "iot_meters_main_iot"


@pytest.fixture(autouse=True)
def _warehouse(monkeypatch):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, "wh-0123456789abcdef")


def _expose(table: str, schema: str = "iot", **fields: Any) -> Dict[str, Any]:
    return {
        "exposeId": table,
        "kind": "table",
        "binding": {
            "platform": "databricks",
            "format": "delta_table",
            "location": {"database": "main", "schema": schema, "table": table},
        },
        "contract": {"schema": [{"name": "id", "type": "bigint"}]},
        **fields,
    }


def _contract(*exposes: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "iot.meters",
        "name": "Meters",
        "exposes": list(exposes) or [_expose("readings", qos={"freshnessSLO": "PT15M"})],
    }


def _module(contract: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(build_module(IAC_PLUGINS["databricks"], contract))


def _dq(**dq_fields: Any) -> Dict[str, Any]:
    return {"schema": [{"name": "id", "type": "bigint"}], "dq": dq_fields}


# ── what asks for it ────────────────────────────────────────────────────────
class TestMonitoringRequest:
    @pytest.mark.parametrize(
        "fields, asked",
        [
            ({"qos": {"freshnessSLO": "PT15M"}}, ("qos.freshnessSLO",)),
            ({"qos": {"completenessTarget": 0.98}}, ("qos.completenessTarget",)),
            (
                {"observability": {"defaultSLIs": {"freshness": {"threshold": "PT1H"}}}},
                ("observability.defaultSLIs.freshness",),
            ),
            (
                {"observability": {"defaultSLIs": {"completeness": {"threshold": 0.9}}}},
                ("observability.defaultSLIs.completeness",),
            ),
            (
                {"contract": _dq(rules=[{"id": "f", "type": "freshness", "severity": "warn"}])},
                ("contract.dq.rules[0] (freshness)",),
            ),
            (
                {
                    "contract": _dq(
                        rules=[
                            {"id": "u", "type": "uniqueness", "severity": "error"},
                            {"id": "c", "type": "completeness", "severity": "error"},
                            {"id": "a", "type": "anomaly_detection", "severity": "info"},
                        ]
                    )
                },
                ("contract.dq.rules[1] (completeness)", "contract.dq.rules[2] (anomaly_detection)"),
            ),
        ],
    )
    def test_each_freshness_or_completeness_slo_asks(self, fields, asked):
        assert dq.monitoring_request(_expose("t", **fields)) == asked

    @pytest.mark.parametrize(
        "fields",
        [
            {},
            {"qos": {"availability": "99.9%", "latencyP95": "PT5S"}},
            {"observability": {"defaultSLIs": {"latency": {"threshold": "PT5S"}}}},
            {"observability": {"defaultSLIs": {"freshness": {"enabled": False}}}},
            {
                "observability": {
                    "defaultSLIs": {"enabled": False, "freshness": {"threshold": "PT1H"}}
                }
            },
            {"contract": _dq(rules=[{"id": "u", "type": "uniqueness", "severity": "error"}])},
            # malformed shapes the schema refuses never crash the emitter
            {"qos": ["PT15M"], "observability": "x", "contract": {"dq": {"rules": "x"}}},
        ],
    )
    def test_nothing_else_asks(self, fields):
        assert dq.monitoring_request(_expose("t", **fields)) == ()

    def test_monitoring_disabled_opts_out_whatever_the_slos(self):
        expose = _expose(
            "t",
            qos={"freshnessSLO": "PT15M", "completenessTarget": 0.98},
            contract=_dq(
                monitoring={"enabled": False},
                rules=[{"id": "f", "type": "freshness", "severity": "warn"}],
            ),
        )
        assert dq.monitoring_request(expose) == ()

    def test_monitoring_enabled_alone_asks_for_nothing(self):
        # the schema's default; an SLO is what says what to watch
        assert dq.monitoring_request(_expose("t", contract=_dq(monitoring={"enabled": True}))) == ()


# ── one monitor per schema ──────────────────────────────────────────────────
class _T:
    def __init__(self, full_name: str, exposure: Dict[str, Any]):
        self.catalog, self.schema, _ = full_name.split(".")
        self.full_name = full_name
        self.where = full_name
        self.exposure = exposure


ASKS = {"qos": {"freshnessSLO": "PT15M"}}


class TestPlanMonitors:
    def test_one_per_schema_with_its_other_tables_excluded_sorted(self):
        monitors = dq.plan_monitors(
            [
                _T("main.iot.z_notes", {}),
                _T("main.iot.readings", ASKS),
                _T("main.ops.alarms", ASKS),
                _T("main.iot.a_notes", {}),
                _T("main.ref.codes", {}),
            ]
        )
        assert monitors == [
            dq.SchemaMonitor(
                "main", "iot", ("main.iot.readings",), ("main.iot.a_notes", "main.iot.z_notes")
            ),
            dq.SchemaMonitor("main", "ops", ("main.ops.alarms",), ()),
        ]

    def test_no_table_asks_no_monitor(self):
        assert dq.plan_monitors([_T("main.iot.readings", {})]) == []

    def test_the_body_watches_the_schema_by_its_id(self):
        ref = dbx.tofu_ref("databricks_schema.k.schema_id")
        plain = dq.SchemaMonitor("main", "iot", ("main.iot.r",), ())
        assert plain.body(ref) == {
            "object_type": "schema",
            "object_id": ref,
            "anomaly_detection_config": {},
        }
        skipping = dq.SchemaMonitor("main", "iot", ("main.iot.r",), ("main.iot.n",))
        assert skipping.body(ref)["anomaly_detection_config"] == {
            "excluded_table_full_names": ["main.iot.n"]
        }


class TestWindowWarnings:
    @pytest.mark.parametrize("window", ["P1D", "PT24H"])
    def test_one_day_is_what_it_counts_over(self, window):
        expose = {**ASKS, "contract": _dq(monitoring={"window": window})}
        assert dq.window_warnings([_T("main.iot.r", expose)]) == []

    def test_another_window_is_not_applied(self):
        expose = {**ASKS, "contract": _dq(monitoring={"window": "PT1H"})}
        (warning,) = dq.window_warnings([_T("main.iot.r", expose)])
        assert "contract.dq.monitoring.window PT1H is not applied" in warning

    @pytest.mark.parametrize(
        "expose",
        [
            {"contract": _dq(monitoring={"window": "PT1H"})},  # not monitored
            {**ASKS, "contract": _dq(monitoring={"window": "soon"})},  # the schema refuses it
        ],
    )
    def test_silent_otherwise(self, expose):
        assert dq.window_warnings([_T("main.iot.r", expose)]) == []


# ── in the module ───────────────────────────────────────────────────────────
class TestEmitter:
    def test_an_slo_puts_a_monitor_on_the_owned_schema(self):
        module = _module(
            _contract(
                _expose("readings", qos={"freshnessSLO": "PT15M", "completenessTarget": 0.98}),
                _expose("notes"),
            )
        )
        assert module["resource"][MONITOR] == {
            KEY: {
                "object_type": "schema",
                "object_id": f"${{databricks_schema.{KEY}.schema_id}}",
                "anomaly_detection_config": {"excluded_table_full_names": ["main.iot.notes"]},
                # after the tables: the scan enabling it starts must find them
                "depends_on": [
                    f"databricks_sql_table.{KEY}_readings",
                    f"databricks_sql_table.{KEY}_notes",
                ],
            }
        }
        assert KEY in module["resource"]["databricks_schema"]
        assert set(module["resource"]["databricks_sql_table"]) == {
            f"{KEY}_readings",
            f"{KEY}_notes",
        }

    def test_no_slo_leaves_the_module_as_it_was(self):
        assert MONITOR not in _module(_contract(_expose("readings")))["resource"]

    def test_opting_out_removes_only_the_monitor(self):
        on = _module(_contract())
        opted_out = _contract(
            _expose(
                "readings",
                qos={"freshnessSLO": "PT15M"},
                contract=_dq(monitoring={"enabled": False}),
            )
        )
        off = _module(opted_out)
        assert MONITOR not in off["resource"]
        del on["resource"][MONITOR]
        assert on == off

    def test_each_schema_gets_its_own(self):
        module = _module(
            _contract(
                _expose("readings", qos={"freshnessSLO": "PT15M"}),
                _expose("alarms", schema="ops", qos={"completenessTarget": 0.99}),
            )
        )
        assert sorted(module["resource"][MONITOR]) == [KEY, "iot_meters_main_ops"]

    def test_the_monitor_is_adopted_by_the_schema_id_in_state(self):
        imports = IAC_PLUGINS["databricks"].discover_imports(_contract())
        schema, monitor = (
            next(b for b in imports if b.to == f"databricks_schema.{KEY}"),
            next(b for b in imports if b.to == f"{MONITOR}.{KEY}"),
        )
        assert imports.index(schema) < imports.index(monitor)
        assert monitor == ImportBlock(
            f"{MONITOR}.{KEY}",
            "schema,{}",
            best_effort=True,
            id_from=(f"databricks_schema.{KEY}", "schema_id"),
        )
        # never rendered into a module: its id is known only once the schema is in state
        assert import_section([monitor]) == {}
        assert import_section([schema, monitor]) == {"import": [{"to": schema.to, "id": schema.id}]}

    def test_plan_names_the_monitor_and_the_apply_says_what_it_does(self):
        contract = _contract(_expose("readings", qos={"freshnessSLO": "PT15M"}), _expose("notes"))
        (action,) = [a for a in dbx.planned_objects(contract) if a["resource_type"] == MONITOR]
        assert action == {
            "op": "databricks.quality_monitor",
            "resource_type": MONITOR,
            "resource_id": "main.iot",
            "monitored": ["main.iot.readings"],
            "excluded": ["main.iot.notes"],
        }
        (notice,) = [n for n in IAC_PLUGINS["databricks"].plan_notices(contract) if "quality" in n]
        assert notice.startswith(
            "data quality monitoring is on for schema main.iot (watched: main.iot.readings; "
            "excluded: main.iot.notes)."
        )
        assert "not from the contract's SLO values" in notice
        assert "DATA_QUALITY_MONITORING" in notice
        assert "system.data_quality_monitoring.table_results" in notice

    def test_validate_warns_about_a_window_it_cannot_apply(self):
        contract = _contract(
            _expose(
                "readings",
                qos={"freshnessSLO": "PT15M"},
                contract=_dq(monitoring={"window": "PT1H"}),
            )
        )
        errors, warnings = dbx.validate_databricks_binding(contract)
        assert errors == []
        assert any("window PT1H is not applied" in w for w in warnings)


# ── the engine ──────────────────────────────────────────────────────────────
class TestEngine:
    def test_removing_a_monitor_is_not_data_loss(self):
        removals = [(f"{MONITOR}.{KEY}", MONITOR)]
        changes = {"add": 0, "change": 0, "remove": 1}
        counted, revoked = engine._data_bearing_changes(changes, removals)
        assert counted["remove"] == 0 and revoked == []
        assert engine._retired_definitions(changes, removals) == [f"{MONITOR}.{KEY}"]

    def _state(self, schema_id: Any) -> List[Dict[str, Any]]:
        return [
            {
                "address": f"databricks_schema.{KEY}",
                "type": "databricks_schema",
                "values": {"name": "iot", "schema_id": schema_id},
            }
        ]

    def test_a_derived_import_id_comes_from_state(self, monkeypatch):
        block = ImportBlock(
            f"{MONITOR}.{KEY}", "schema,{}", id_from=(f"databricks_schema.{KEY}", "schema_id")
        )
        monkeypatch.setattr(
            engine.runner, "tofu_state_resources", lambda *a, **k: self._state("u-1")
        )
        assert engine._import_id(block, "/w", {}) == "schema,u-1"
        for missing in ([], self._state(None), self._state("")):
            monkeypatch.setattr(
                engine.runner, "tofu_state_resources", lambda *a, _m=missing, **k: _m
            )
            assert engine._import_id(block, "/w", {}) is None
        literal = ImportBlock("databricks_schema.x", "main.iot")
        assert engine._import_id(literal, "/w", {}) == "main.iot"

    def test_adoption_imports_the_monitor_after_its_schema(self, monkeypatch):
        state: List[Dict[str, Any]] = []
        calls: List[tuple] = []
        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: [])
        monkeypatch.setattr(engine.runner, "tofu_state_resources", lambda *a, **k: list(state))

        def fake_import(workdir, address, rid, env=None):
            calls.append((address, rid))
            if address == f"databricks_schema.{KEY}":
                state.extend(self._state("u-42"))
            return TofuResult("import", 0, "", "", [])

        monkeypatch.setattr(engine.runner, "tofu_import", fake_import)
        contract = _contract()
        engine._adopt_existing(IAC_PLUGINS["databricks"], contract, [], "/w", {}, _LOG)
        assert (f"{MONITOR}.{KEY}", "schema,u-42") in calls
        addresses = [address for address, _ in calls]
        assert addresses.index(f"databricks_schema.{KEY}") < addresses.index(f"{MONITOR}.{KEY}")

    def test_no_schema_in_state_no_monitor_import(self, monkeypatch):
        calls: List[str] = []
        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: [])
        monkeypatch.setattr(engine.runner, "tofu_state_resources", lambda *a, **k: [])

        def fake_import(workdir, address, rid, env=None):
            calls.append(address)
            return TofuResult("import", 1, "", "Cannot import non-existent remote object", [])

        monkeypatch.setattr(engine.runner, "tofu_import", fake_import)
        engine._adopt_existing(
            IAC_PLUGINS["databricks"], copy.deepcopy(_contract()), [], "/w", {}, _LOG
        )
        assert f"{MONITOR}.{KEY}" not in calls
        assert f"databricks_schema.{KEY}" in calls
