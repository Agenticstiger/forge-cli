# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Databricks Phase 2 apply hooks, Stage 1 unit: the plugin's plan hooks and the engine's gates.

Pure functions only: no ``tofu``, no network. Plans are ``tofu show -json``-shaped
dicts; the engine functions are driven directly, as ``test_iac_databricks.py`` does.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.iac import IAC_PLUGINS
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.naming import digest_ident
from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.source import contract_source

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

_LOG = logging.getLogger("test.iac.databricks.phase2")
WAREHOUSE = "wh-0123456789abcdef"
PLUGIN = IAC_PLUGINS["databricks"]
MAPPING = "delta.columnMapping.mode"


@pytest.fixture(autouse=True)
def _warehouse(monkeypatch):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)


def _contract(**overrides: Any) -> Dict[str, Any]:
    """``orders`` (written by a dbt build) and ``customers`` (written by no one)."""
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "sales.orders",
        "name": "Orders",
        "accessPolicy": {
            "grants": [{"principal": "group:analysts", "permissions": ["read"]}],
        },
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {"database": "main", "schema": "sales", "table": "orders"},
                },
                "policy": {"authz": {"readers": ["group:bi"]}},
                "contract": {
                    "schema": [
                        {"name": "id", "type": "bigint"},
                        {"name": "amount", "type": "decimal(12,0)"},
                        {"name": "region", "type": "string"},
                    ]
                },
            },
            {
                "exposeId": "customers",
                "kind": "table",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {"database": "main", "schema": "sales", "table": "customers"},
                },
                "contract": {"schema": [{"name": "cid", "type": "bigint"}]},
            },
        ],
        "builds": [
            {
                "id": "transform",
                "engine": "dbt",
                "repository": "https://github.com/o/r?tag=v1",
                "outputs": ["orders"],
                "properties": {"model": "orders"},
            }
        ],
    }
    for path, value in overrides.items():
        target = contract
        keys = path.split("__")
        for key in keys[:-1]:
            target = target[int(key)] if isinstance(target, list) else target.setdefault(key, {})
        target[keys[-1]] = value
    return contract


# ── plan_data_changes: the protocol upgrade ─────────────────────────────────
def _update(before: Dict[str, Any], after: Dict[str, Any], actions=("update",)) -> Dict[str, Any]:
    named = {"catalog_name": "main", "schema_name": "sales", "name": "orders"}
    cols = [{"name": "id", "type": "bigint"}]
    return {
        "resource_changes": [
            {
                "address": "databricks_sql_table.sales_orders_main_sales_orders",
                "type": "databricks_sql_table",
                "change": {
                    "actions": list(actions),
                    "before": {**named, "column": cols, **before},
                    "after": {**named, "column": cols, **after},
                    "after_unknown": {},
                },
            }
        ]
    }


class TestProtocolUpgrade:
    def test_taking_over_a_table_without_column_mapping_is_refused(self):
        plan = _update({"properties": {}}, {"properties": {MAPPING: "name"}})
        with pytest.raises(UnsupportedBindingError) as exc:
            PLUGIN.plan_data_changes(plan)
        assert exc.value.kind == "databricks-adopt-protocol-upgrade"
        assert "main.sales.orders" in str(exc.value)

    def test_the_remediation_has_the_backtick_quoted_alter_table(self):
        plan = _update({"properties": None}, {"properties": {MAPPING: "name"}})
        with pytest.raises(UnsupportedBindingError) as exc:
            PLUGIN.plan_data_changes(plan)
        assert (
            "ALTER TABLE `main`.`sales`.`orders` SET TBLPROPERTIES "
            "('delta.columnMapping.mode' = 'name')"
        ) in " ".join(exc.value.remediation)

    def test_a_before_with_neither_properties_nor_effective_properties_is_refused(self):
        plan = _update({}, {"properties": {MAPPING: "name"}})
        with pytest.raises(UnsupportedBindingError):
            PLUGIN.plan_data_changes(plan)

    def test_a_different_live_mode_is_still_an_upgrade_to_name(self):
        plan = _update(
            {"properties": {MAPPING: "id"}, "effective_properties": {}},
            {"properties": {MAPPING: "name"}},
        )
        with pytest.raises(UnsupportedBindingError):
            PLUGIN.plan_data_changes(plan)

    def test_no_refusal_when_the_live_table_has_it_in_properties(self):
        plan = _update({"properties": {MAPPING: "name"}}, {"properties": {MAPPING: "name"}})
        assert PLUGIN.plan_data_changes(plan) == []

    def test_no_refusal_when_the_live_table_has_it_in_effective_properties(self):
        plan = _update(
            {"properties": {}, "effective_properties": {MAPPING: "name"}},
            {"properties": {MAPPING: "name"}},
        )
        assert PLUGIN.plan_data_changes(plan) == []

    def test_no_refusal_when_the_plan_does_not_set_it(self):
        plan = _update({"properties": {}}, {"properties": {"other": "1"}})
        assert PLUGIN.plan_data_changes(plan) == []
        assert PLUGIN.plan_data_changes(_update({}, {})) == []

    def test_a_dbt_written_table_plans_no_properties_so_adopting_it_is_not_refused(self):
        # The module sets no column mapping on a table dbt writes.
        plan = _update({"properties": {}}, {"properties": None})
        assert PLUGIN.plan_data_changes(plan) == []

    @pytest.mark.parametrize("actions", [["create"], ["no-op"], ["delete", "create"]])
    def test_only_an_in_place_update_is_checked(self, actions):
        plan = _update({"properties": {}}, {"properties": {MAPPING: "name"}}, actions)
        assert PLUGIN.plan_data_changes(plan) == []

    def test_other_resource_types_are_ignored(self):
        plan = _update({"properties": {}}, {"properties": {MAPPING: "name"}})
        plan["resource_changes"][0]["type"] = "databricks_schema"
        assert PLUGIN.plan_data_changes(plan) == []

    def test_the_refusal_comes_before_a_data_loss_line(self):
        plan = _update(
            {"properties": {}, "column": [{"name": "a"}, {"name": "b"}]},
            {"properties": {MAPPING: "name"}, "column": [{"name": "a"}]},
        )
        with pytest.raises(UnsupportedBindingError):
            PLUGIN.plan_data_changes(plan)


# ── suppressed_drift ────────────────────────────────────────────────────────
def _live(name: str, columns: List[Dict[str, str]], schema: str = "sales") -> Dict[str, Any]:
    return {
        "type": "databricks_sql_table",
        "values": {"catalog_name": "main", "schema_name": schema, "name": name, "column": columns},
    }


class TestSuppressedDrift:
    def test_matching_columns_report_nothing(self):
        live = _live(
            "orders",
            [
                {"name": "id", "type": "bigint"},
                {"name": "amount", "type": "decimal(12,0)"},
                {"name": "region", "type": "string"},
            ],
        )
        assert PLUGIN.suppressed_drift(_contract(), [live]) == []

    def test_the_provider_normalises_types_before_comparing(self):
        # long == bigint and decimal(12) == decimal(12,0), as the provider sees them.
        live = _live(
            "orders",
            [
                {"name": "id", "type": "long"},
                {"name": "amount", "type": "decimal(12)"},
                {"name": "region", "type": "STRING"},
            ],
        )
        assert PLUGIN.suppressed_drift(_contract(), [live]) == []

    def test_a_type_mismatch_is_reported_as_declared_versus_live(self):
        live = _live(
            "orders",
            [
                {"name": "id", "type": "string"},
                {"name": "amount", "type": "decimal(12,0)"},
                {"name": "region", "type": "string"},
            ],
        )
        assert PLUGIN.suppressed_drift(_contract(), [live]) == [
            {
                "table": "main.sales.orders",
                "type_mismatches": [{"column": "id", "declared": "bigint", "live": "string"}],
                "missing": [],
                "extra": [],
            }
        ]

    def test_missing_and_extra_columns_are_reported(self):
        live = _live(
            "orders",
            [
                {"name": "id", "type": "bigint"},
                {"name": "amount", "type": "decimal(12,0)"},
                {"name": "added_by_dbt", "type": "string"},
            ],
        )
        (record,) = PLUGIN.suppressed_drift(_contract(), [live])
        assert record["missing"] == ["region"]
        assert record["extra"] == ["added_by_dbt"]
        assert record["type_mismatches"] == []

    def test_column_and_table_names_compare_lower_case(self):
        live = _live("ORDERS", [{"name": "ID", "type": "bigint"}], schema="Sales")
        live["values"]["catalog_name"] = "MAIN"
        (record,) = PLUGIN.suppressed_drift(_contract(), [live])
        assert record["table"] == "main.sales.orders"
        assert record["missing"] == ["amount", "region"] and record["extra"] == []

    def test_a_table_no_dbt_build_writes_is_ignored(self):
        live = _live("customers", [{"name": "something_else", "type": "string"}])
        assert PLUGIN.suppressed_drift(_contract(), [live]) == []

    def test_everything_is_ignored_when_no_dbt_build_writes_anything(self):
        contract = _contract(builds__0__engine="python")
        live = _live("orders", [{"name": "other", "type": "string"}])
        assert PLUGIN.suppressed_drift(contract, [live]) == []

    def test_other_resources_and_unknown_tables_are_ignored(self):
        schema = {"type": "databricks_schema", "values": {"name": "sales"}}
        stranger = _live("not_in_the_contract", [{"name": "x", "type": "string"}])
        assert PLUGIN.suppressed_drift(_contract(), [schema, stranger]) == []

    def test_a_table_with_no_live_columns_is_all_missing(self):
        (record,) = PLUGIN.suppressed_drift(_contract(), [_live("orders", [])])
        assert record["missing"] == ["id", "amount", "region"]


# ── plan_notices ────────────────────────────────────────────────────────────
class TestPlanNotices:
    def test_readers_are_listed_per_catalog_with_the_grant_to_run(self):
        (notice,) = PLUGIN.plan_notices(_contract())
        assert "analysts, bi also need USE CATALOG on catalog main" in notice
        assert "GRANT USE CATALOG ON CATALOG `main` TO `analysts`" in notice
        assert "GRANT USE CATALOG ON CATALOG `main` TO `bi`" in notice
        assert "fluid verify checks it" in notice

    def test_one_notice_per_catalog(self):
        contract = _contract(exposes__1__binding__location__database="other")
        contract["exposes"][1]["policy"] = {"authz": {"readers": ["group:ops"]}}
        notices = PLUGIN.plan_notices(contract)
        assert len(notices) == 2
        assert "catalog main" in notices[1] or "catalog main" in notices[0]
        assert any("ON CATALOG `other` TO `ops`" in n for n in notices)

    def test_a_backtick_in_a_principal_is_doubled(self):
        contract = _contract(
            exposes__0__binding__principals={
                "group:analysts": "we`ird",
                "group:bi": "bi",
            }
        )
        (notice,) = PLUGIN.plan_notices(contract)
        assert "TO `we``ird`" in notice
        assert "TO `we`ird`" not in notice

    def test_no_readers_means_no_notice(self):
        contract = _contract(accessPolicy={"grants": []})
        del contract["exposes"][0]["policy"]
        assert PLUGIN.plan_notices(contract) == []

    def test_a_contract_without_databricks_exposes_has_none(self):
        assert PLUGIN.plan_notices({"fluidVersion": "0.7.6", "kind": "DataProduct"}) == []


# ── planned_objects: jobs and files for the native planner ──────────────────
class TestPlannedObjects:
    def test_a_git_job_is_one_ensure_action(self):
        actions = dbx.planned_objects(
            _contract(builds__0__execution={"runtime": {"platform": "databricks"}})
        )
        (job,) = [a for a in actions if a["resource_type"] == "databricks_job"]
        assert job == {
            "op": "databricks.job.ensure",
            "resource_type": "databricks_job",
            "resource_id": "fluid sales.orders transform",
            "build": "transform",
            "source": "GIT",
            "trigger": "manual",
        }
        assert not [a for a in actions if a["resource_type"] == "databricks_workspace_file"]

    def test_a_workspace_job_plans_a_file_action_per_uploaded_file(self, tmp_path):
        project = tmp_path / "dbt"
        (project / "models").mkdir(parents=True)
        (project / "dbt_project.yml").write_text("name: p\n", encoding="utf-8")
        (project / "models" / "m.sql").write_text("select 1", encoding="utf-8")
        contract = _contract(
            builds__0__repository="./dbt",
            builds__0__execution={"runtime": {"platform": "databricks"}},
        )
        with contract_source(tmp_path / "contract.fluid.yaml"):
            actions = dbx.planned_objects(contract)
        files = [a for a in actions if a["resource_type"] == "databricks_workspace_file"]
        root = f"~/.fluid/{digest_ident('sales.orders')}/{digest_ident('transform')}/files"
        assert [a["resource_id"] for a in files] == [
            f"{root}/dbt_project.yml",
            f"{root}/models/m.sql",
        ]
        assert {a["op"] for a in files} == {"databricks.workspace_file.ensure"}

    def test_a_refused_job_refuses_the_plan(self):
        contract = _contract(
            builds__0__execution={
                "runtime": {"platform": "databricks"},
                "trigger": {"type": "event"},
            }
        )
        with pytest.raises(UnsupportedBindingError) as exc:
            dbx.planned_objects(contract)
        assert exc.value.kind == "databricks-job-trigger-unmapped"


# ── engine: the data-loss gate and what is not gated ────────────────────────
JOB = ("databricks_job.sales_orders_transform", "databricks_job")
FILE = ("databricks_workspace_file.sales_orders_transform_m", "databricks_workspace_file")
TABLE = ("databricks_sql_table.sales_orders_main_sales_orders", "databricks_sql_table")
GRANT = ("databricks_grant.sales_orders_grant", "databricks_grant")


def _summary(remove: int) -> Dict[str, int]:
    return {"add": 0, "change": 0, "remove": remove}


class TestDataLossGate:
    def test_run_definitions_are_the_job_and_the_workspace_file(self):
        assert engine.RUN_DEFINITION_RESOURCE_TYPES == {
            "databricks_job",
            "databricks_workspace_file",
        }

    def test_removed_jobs_and_files_are_not_data_bearing(self):
        counted, revoked = engine._data_bearing_changes(_summary(2), [JOB, FILE])
        assert counted == {"add": 0, "change": 0, "remove": 0}
        assert revoked == []

    def test_a_removed_table_still_counts_beside_jobs_files_and_grants(self):
        removals = [JOB, FILE, GRANT, TABLE]
        counted, revoked = engine._data_bearing_changes(_summary(4), removals)
        assert counted["remove"] == 1
        assert revoked == [GRANT[0]]

    def test_other_counts_pass_through(self):
        counted, _ = engine._data_bearing_changes({"add": 3, "change": 2, "remove": 1}, [JOB])
        assert counted == {"add": 3, "change": 2, "remove": 0}

    def test_removals_that_do_not_match_the_summary_fail_closed(self):
        # The plan's events account for 2 of 3 removals: every one counts.
        counted, revoked = engine._data_bearing_changes(_summary(3), [JOB, FILE])
        assert counted["remove"] == 3 and revoked == []
        counted, revoked = engine._data_bearing_changes(_summary(1), [JOB, FILE])
        assert counted["remove"] == 1 and revoked == []
        counted, revoked = engine._data_bearing_changes(_summary(1), [])
        assert counted["remove"] == 1 and revoked == []

    def test_retired_definitions_lists_jobs_and_files_only(self):
        assert engine._retired_definitions(_summary(4), [JOB, TABLE, FILE, GRANT]) == [
            JOB[0],
            FILE[0],
        ]

    def test_retired_definitions_is_empty_when_the_events_do_not_match_the_summary(self):
        assert engine._retired_definitions(_summary(3), [JOB, FILE]) == []
        assert engine._retired_definitions(_summary(1), [JOB, FILE]) == []

    def test_retired_definitions_is_empty_with_no_removals(self):
        assert engine._retired_definitions(_summary(0), []) == []
        assert engine._retired_definitions({}, []) == []


# ── engine: plan notices ────────────────────────────────────────────────────
class TestReportPlanNotices:
    def test_each_notice_is_a_note_line(self, capsys):
        class Plugin:
            def plan_notices(self, contract):
                return ["first thing", "second thing"]

        engine._report_plan_notices(Plugin(), {}, _LOG)
        assert capsys.readouterr().out.splitlines() == [
            "  note: first thing",
            "  note: second thing",
        ]

    def test_the_databricks_plugin_notice_reaches_the_output(self, capsys):
        engine._report_plan_notices(PLUGIN, _contract(), _LOG)
        out = capsys.readouterr().out
        assert out.startswith("  note: analysts, bi also need USE CATALOG")

    def test_a_plugin_exception_is_swallowed(self, capsys, caplog):
        class Plugin:
            def plan_notices(self, contract):
                raise RuntimeError("boom with detail")

        caplog.set_level(logging.DEBUG, logger=_LOG.name)
        engine._report_plan_notices(Plugin(), {}, _LOG)
        assert capsys.readouterr().out == ""
        # The debug line names the exception type only.
        assert "RuntimeError" in caplog.text and "boom with detail" not in caplog.text

    def test_a_plugin_without_notices_prints_nothing(self, capsys):
        engine._report_plan_notices(object(), {}, _LOG)
        assert capsys.readouterr().out == ""

    def test_a_non_callable_hook_is_ignored(self, capsys):
        class Plugin:
            plan_notices = "not callable"

        engine._report_plan_notices(Plugin(), {}, _LOG)
        assert capsys.readouterr().out == ""


# ── engine: credential overlay ──────────────────────────────────────────────
class TestCredentialOverlay:
    def test_a_refusal_is_a_typed_cli_error_with_kind_and_remediation(self):
        with pytest.raises(CLIError) as exc:
            engine.credential_overlay(PLUGIN, {})
        assert exc.value.event == "unsupported_binding"
        assert exc.value.context["kind"] == "databricks-host-unset"
        assert (
            isinstance(exc.value.context["remediation"], list) and exc.value.context["remediation"]
        )
        assert "DATABRICKS_HOST" in exc.value.context["error"]

    def test_ambiguous_credentials_carry_their_kind(self):
        env = {
            "DATABRICKS_HOST": "https://adb-1.azuredatabricks.net",
            "DATABRICKS_TOKEN": "fake-token-value",
            "DATABRICKS_CLIENT_ID": "id",
            "DATABRICKS_CLIENT_SECRET": "fake-secret-value",
        }
        with pytest.raises(CLIError) as exc:
            engine.credential_overlay(PLUGIN, env)
        assert exc.value.context["kind"] == "databricks-auth-ambiguous"
        assert "fake-token-value" not in str(exc.value.context)
        assert "fake-secret-value" not in str(exc.value.context)

    def test_a_good_environment_returns_the_overlay(self):
        env: Optional[Dict[str, str]] = {
            "DATABRICKS_HOST": "https://adb-1.azuredatabricks.net",
            "DATABRICKS_TOKEN": "fake-token-value",
        }
        overlay = engine.credential_overlay(PLUGIN, env)
        assert overlay["DATABRICKS_AUTH_TYPE"] == "pat"

    def test_another_exception_is_not_converted(self):
        class Plugin:
            def credential_env(self, env):
                raise KeyError("unrelated")

        with pytest.raises(KeyError):
            engine.credential_overlay(Plugin(), {})
