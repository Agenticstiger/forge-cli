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

"""Databricks IaC plugin, tier 1: the emitted module, the refusals, and the apply hooks.

Pure functions only: no ``tofu``, no network. What real ``tofu`` and the real
provider do with the module is tier 3 (``test_iac_databricks_fake_e2e.py``).
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.iac import IAC_PLUGINS, build_module, cutover, provider_match
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.module import LOCK_FILE_NAME, dependency_lock, write_dependency_lock
from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.runner import TofuResult
from fluid_build.iac.versions import PROVIDER_PINS

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

_LOG = logging.getLogger("test.iac.databricks")
WAREHOUSE = "wh-0123456789abcdef"
TABLE = "databricks_sql_table.sales_orders_main_sales_orders"


@pytest.fixture(autouse=True)
def _warehouse(monkeypatch):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, WAREHOUSE)


def _contract(**overrides: Any) -> Dict[str, Any]:
    contract: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "sales.orders",
        "name": "Orders",
        "accessPolicy": {
            "grants": [
                {"principal": "group:analysts", "permissions": ["read"]},
                {"principal": "user:alice@corp.com", "permissions": ["read", "write"]},
            ]
        },
        "exposes": [
            {
                "exposeId": "orders",
                "kind": "table",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {"database": "Main", "schema": "sales", "table": "orders"},
                },
                "policy": {"authz": {"readers": ["group:analysts", "group:bi"]}},
                "contract": {
                    "schema": [
                        {"name": "ID", "type": "bigint", "required": True},
                        {"name": "amount", "type": "decimal(18, 2)"},
                        {"name": "region", "type": "string"},
                    ]
                },
            }
        ],
    }
    for path, value in overrides.items():
        target = contract
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[int(key)] if isinstance(target, list) else target.setdefault(key, {})
        target[keys[-1]] = value
    return contract


def _module(contract: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(build_module(IAC_PLUGINS["databricks"], contract))


# ── routing ─────────────────────────────────────────────────────────────────
class TestRouting:
    def test_alias_detects_the_platform(self):
        assert provider_match.canonical_cloud("databricks") == "databricks"
        assert provider_match.detect_clouds(_contract()) == ["databricks"]

    def test_databricks_applies_through_opentofu(self):
        assert cutover.default_engine("databricks") == "opentofu"


# ── the module ──────────────────────────────────────────────────────────────
class TestModule:
    def test_catalog_is_read_never_created(self):
        doc = _module(_contract())
        assert doc["data"] == {"databricks_catalog": {"sales_orders_main": {"name": "main"}}}
        assert "databricks_catalog" not in doc["resource"]

    def test_schema_is_owned_without_force_destroy(self):
        schema = _module(_contract())["resource"]["databricks_schema"]["sales_orders_main_sales"]
        assert schema == {
            "catalog_name": "${data.databricks_catalog.sales_orders_main.name}",
            "name": "sales",
        }

    def test_table(self):
        table = _module(_contract())["resource"]["databricks_sql_table"][
            "sales_orders_main_sales_orders"
        ]
        assert table["warehouse_id"] == WAREHOUSE
        assert table["table_type"] == "MANAGED"
        assert table["data_source_format"] == "DELTA"
        assert table["properties"] == {"delta.columnMapping.mode": "name"}
        assert table["schema_name"] == "${databricks_schema.sales_orders_main_sales.name}"
        # Identifiers lower-cased; types through the dbt engine's Databricks table.
        assert table["column"] == [
            {"name": "id", "type": "bigint", "nullable": False},
            {"name": "amount", "type": "decimal(18,2)", "nullable": True},
            {"name": "region", "type": "string", "nullable": True},
        ]
        assert "partitions" not in table

    def test_partitions(self):
        doc = _module(_contract(**{"exposes.0.binding.location.partitionBy": ["Region"]}))
        assert doc["resource"]["databricks_sql_table"]["sales_orders_main_sales_orders"][
            "partitions"
        ] == ["region"]

    def test_one_grant_per_principal_with_merged_privileges(self):
        grants = _module(_contract())["resource"]["databricks_grant"]
        table = {g["principal"]: g["privileges"] for g in grants.values() if "table" in g}
        schema = {g["principal"]: g["privileges"] for g in grants.values() if "schema" in g}
        # analysts is named by accessPolicy AND policy.authz.readers: one resource,
        # since databricks_grant is authoritative for its principal.
        assert table == {
            "alice@corp.com": ["MODIFY", "SELECT"],
            "analysts": ["SELECT"],
            "bi": ["SELECT"],
        }
        assert schema == {p: ["USE_SCHEMA"] for p in table}

    def test_binding_principals_map_logical_names(self):
        contract = _contract(
            **{
                "exposes.0.binding.principals": {
                    "group:analysts": "data-analysts",
                    "group:bi": [],
                    "user:alice@corp.com": "alice@corp.com",
                }
            }
        )
        grants = _module(contract)["resource"]["databricks_grant"]
        assert sorted({g["principal"] for g in grants.values()}) == [
            "alice@corp.com",
            "data-analysts",
        ]

    def test_provider_block_bounds_retries_and_pins_the_provider(self):
        doc = _module(_contract())
        assert doc["provider"] == {"databricks": {"retry_timeout_seconds": 60}}
        assert doc["terraform"]["required_providers"] == {
            "databricks": {"source": "databricks/databricks", "version": "~> 1.140.0"}
        }

    def test_credential_free_and_deterministic(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-not-a-real-token")
        monkeypatch.setenv("DATABRICKS_HOST", "https://adb-1.azuredatabricks.net")
        first = build_module(IAC_PLUGINS["databricks"], _contract())
        assert first == build_module(IAC_PLUGINS["databricks"], _contract())
        assert "dapi" not in first and "azuredatabricks" not in first
        assert "force_destroy" not in first

    def test_imports_cover_the_schema_and_the_table(self):
        imports = IAC_PLUGINS["databricks"].discover_imports(_contract())
        assert [(b.to, b.id) for b in imports] == [
            ("databricks_schema.sales_orders_main_sales", "main.sales"),
            (TABLE, "main.sales.orders"),
        ]

    def test_other_clouds_are_untouched(self):
        contract = _contract(**{"exposes.0.binding.platform": "snowflake"})
        assert IAC_PLUGINS["databricks"].emit(contract) == {}
        assert dbx.validate_databricks_binding(contract) == ([], [])


# ── refusals: validate and the emitter share one pass ───────────────────────
_REFUSALS = [
    ("host", {"exposes.0.binding.location.host": "evil.example.com"}, "host-in-contract"),
    ("iceberg", {"exposes.0.binding.format": "iceberg"}, "iceberg-unsupported"),
    ("iceberg-config", {"exposes.0.binding.icebergConfig": {"writeVersion": 2}}, "iceberg"),
    ("format", {"exposes.0.binding.format": "parquet"}, "format-unsupported"),
    ("kind", {"exposes.0.kind": "view"}, "kind-unsupported"),
    ("catalog", {"exposes.0.binding.location.catalog": "unity"}, "location-field"),
    ("warehouse", {"exposes.0.binding.location.warehouse": "wh"}, "location-field"),
    ("no-table", {"exposes.0.binding.location.table": ""}, "location-incomplete"),
    ("packaging", {"packaging": {"mode": "isolated"}}, "packaging-unmapped"),
    ("binding-packaging", {"exposes.0.binding.packaging": {"mode": "isolated"}}, "packaging"),
    ("masking", {"exposes.0.policy.privacy": {"masking": [{"column": "x"}]}}, "governance"),
    ("row-level", {"exposes.0.policy.privacy": {"rowLevelPolicy": {"a": 1}}}, "governance"),
    ("row-filters", {"exposes.0.policy.authz.rowFilters": [{"a": 1}]}, "governance"),
    ("column-restr", {"exposes.0.policy.authz.columnRestrictions": [{"a": 1}]}, "governance"),
    ("lf", {"exposes.0.binding.governance": {"lakeFormation": {"grants": []}}}, "governance"),
    ("encryption", {"exposes.0.binding.encryption": {"kms": "k"}}, "encryption-unmapped"),
    ("expire", {"exposes.0.lifecycle": {"expire": True, "retention": "P30D"}}, "retention"),
    ("injection", {"exposes.0.binding.location.table": "o`; DROP TABLE x"}, "identifier"),
    ("column-type", {"exposes.0.contract.schema": [{"name": "a", "type": "decimal(1,x)"}]}, "type"),
    ("no-columns", {"exposes.0.contract.schema": []}, "no-columns"),
    (
        "dup-column",
        {"exposes.0.contract.schema": [{"name": "A", "type": "int"}, {"name": "a", "type": "int"}]},
        "column-duplicate",
    ),
    ("partition", {"exposes.0.binding.location.partitionBy": ["nope"]}, "partition-column"),
]


@pytest.mark.parametrize("label,overrides,kind", _REFUSALS, ids=[r[0] for r in _REFUSALS])
def test_refusal(label: str, overrides: Dict[str, Any], kind: str):
    contract = _contract(**overrides)
    errors, _ = dbx.validate_databricks_binding(contract)
    assert errors, label
    with pytest.raises(UnsupportedBindingError) as exc:
        IAC_PLUGINS["databricks"].emit(contract)
    assert exc.value.kind.startswith("databricks-") and kind in exc.value.kind


def test_a_build_that_names_its_own_host_is_refused():
    # The dbt runner reads runtime.resources.host before DATABRICKS_HOST and
    # would send the workspace token there.
    contract = _contract()
    contract["builds"] = [
        {
            "id": "dbt_daily",
            "engine": "dbt",
            "execution": {
                "runtime": {"platform": "databricks", "resources": {"host": "evil.example.com"}}
            },
        }
    ]
    with pytest.raises(UnsupportedBindingError) as exc:
        IAC_PLUGINS["databricks"].emit(contract)
    assert exc.value.kind == "databricks-host-in-contract"
    assert "builds[0]" in str(exc.value)


def test_unset_warehouse_is_refused(monkeypatch):
    monkeypatch.delenv(dbx.WAREHOUSE_ENV)
    with pytest.raises(UnsupportedBindingError) as exc:
        IAC_PLUGINS["databricks"].emit(_contract())
    assert exc.value.kind == "databricks-warehouse-unset"
    assert dbx.WAREHOUSE_ENV in " ".join(exc.value.remediation)


@pytest.mark.parametrize(
    "grant,kind",
    [
        (
            {"principal": "group:x", "permissions": ["read"], "conditions": {"ip": "1"}},
            "conditions",
        ),
        ({"principal": "group:x", "permissions": ["read"], "resources": ["$.exposes"]}, "scope"),
        ({"principal": "user:bob@corp.example", "permissions": ["read"]}, "principal-placeholder"),
        ({"principal": "serviceAccount:sa@p.iam", "permissions": ["read"]}, "principal-unmapped"),
    ],
)
def test_grant_refusals(grant: Dict[str, Any], kind: str):
    contract = _contract()
    contract["accessPolicy"]["grants"].append(grant)
    with pytest.raises(UnsupportedBindingError) as exc:
        IAC_PLUGINS["databricks"].emit(contract)
    assert kind in exc.value.kind


def test_a_mapped_identity_must_be_bare():
    contract = _contract(**{"exposes.0.binding.principals": {"group:analysts": "group:analysts"}})
    contract["exposes"][0]["policy"] = {}
    contract["accessPolicy"]["grants"] = contract["accessPolicy"]["grants"][:1]
    with pytest.raises(UnsupportedBindingError) as exc:
        IAC_PLUGINS["databricks"].emit(contract)
    assert exc.value.kind == "principal-invalid"


def test_validate_reports_every_refusal_at_once(monkeypatch):
    monkeypatch.delenv(dbx.WAREHOUSE_ENV)
    contract = _contract(
        **{
            "exposes.0.binding.location.host": "h",
            "exposes.0.policy.privacy": {"masking": [{"column": "x"}]},
        }
    )
    contract["accessPolicy"]["grants"].append(
        {"principal": "user:bob@corp.example", "permissions": ["read"]}
    )
    errors, warnings = dbx.validate_databricks_binding(contract)
    joined = "\n".join(errors)
    for needle in ("location.host", "masking", dbx.WAREHOUSE_ENV, "reserved top-level domain"):
        assert needle in joined
    assert warnings == []


def test_two_schemas_meeting_at_one_address_are_refused():
    contract = _contract(**{"exposes.0.binding.location.database": "a_b"})
    contract["exposes"][0]["binding"]["location"]["schema"] = "c"
    second = copy.deepcopy(contract["exposes"][0])
    second["exposeId"] = "other"
    second["binding"]["location"].update({"database": "a", "schema": "b_c"})
    contract["exposes"].append(second)
    with pytest.raises(UnsupportedBindingError) as exc:
        IAC_PLUGINS["databricks"].emit(contract)
    assert exc.value.kind == "databricks-name-collision"


def test_two_exposes_on_one_table_are_refused():
    contract = _contract()
    second = copy.deepcopy(contract["exposes"][0])
    second["exposeId"] = "orders_again"
    contract["exposes"].append(second)
    with pytest.raises(UnsupportedBindingError) as exc:
        IAC_PLUGINS["databricks"].emit(contract)
    assert exc.value.kind == "databricks-table-duplicate"


def test_env_templates_validate_as_one_segment():
    contract = _contract(**{"exposes.0.binding.location.database": "{{ env.DBX_CATALOG }}"})
    assert dbx.validate_databricks_binding(contract) == ([], [])


def test_a_template_still_unresolved_at_emit_is_refused():
    # `fluid apply` resolves templates first and leaves a missing variable intact.
    contract = _contract(**{"exposes.0.binding.location.database": "{{ env.DBX_CATALOG }}"})
    with pytest.raises(UnsupportedBindingError) as exc:
        IAC_PLUGINS["databricks"].emit(contract)
    assert exc.value.kind == "databricks-identifier" and "not set" in str(exc.value)


# ── column changes, as the provider diffs them ──────────────────────────────
def _cols(*names: str) -> List[Dict[str, Any]]:
    return [{"name": n, "type": "string"} for n in names]


class TestColumnChanges:
    def test_count_change_drops_by_name(self):
        assert dbx.column_changes(_cols("a", "b", "c"), _cols("a", "c")) == (["b"], [])

    def test_equal_count_renames_by_position(self):
        assert dbx.column_changes(_cols("a", "b"), _cols("a", "x")) == ([], [("b", "x")])

    def test_add_only(self):
        assert dbx.column_changes(_cols("a"), _cols("a", "b")) == ([], [])


def _plan(actions, before_cols, after_cols, after_unknown=None) -> Dict[str, Any]:
    named = {"catalog_name": "main", "schema_name": "sales", "name": "orders"}
    return {
        "resource_changes": [
            {
                "address": TABLE,
                "type": "databricks_sql_table",
                "change": {
                    "actions": actions,
                    "before": {**named, "column": before_cols},
                    "after": {**named, "column": after_cols},
                    "after_unknown": after_unknown or {},
                },
            }
        ]
    }


class TestPlanDataChanges:
    def test_a_dropped_column_is_data_loss(self):
        lost = IAC_PLUGINS["databricks"].plan_data_changes(
            _plan(["update"], _cols("a", "b"), _cols("a"))
        )
        assert lost == ["main.sales.orders: drops column(s) b"]

    def test_a_positional_rename_is_gated_too(self):
        # [id, ssn] -> [id, tier] could mean drop ssn, add tier: the provider
        # renames, and ssn's values would stay under tier.
        lost = IAC_PLUGINS["databricks"].plan_data_changes(
            _plan(["update"], _cols("id", "ssn"), _cols("id", "tier"))
        )
        assert lost == [
            "main.sales.orders: renames column ssn to tier by position, so ssn's values "
            "stay under tier"
        ]

    def test_an_added_column_is_not(self):
        assert (
            IAC_PLUGINS["databricks"].plan_data_changes(
                _plan(["update"], _cols("a"), _cols("a", "b"))
            )
            == []
        )

    def test_a_replace_is_left_to_the_destroy_count(self):
        assert (
            IAC_PLUGINS["databricks"].plan_data_changes(
                _plan(["delete", "create"], _cols("a", "b"), _cols("a"))
            )
            == []
        )

    def test_unknown_columns_fail_closed(self):
        lost = IAC_PLUGINS["databricks"].plan_data_changes(
            _plan(["update"], _cols("a"), None, after_unknown={"column": True})
        )
        assert lost and "cannot be ruled out" in lost[0]


# ── plan-time errors become fix-its ─────────────────────────────────────────
def _state(columns) -> List[Dict[str, Any]]:
    return [
        {
            "type": "databricks_sql_table",
            "values": {
                "catalog_name": "main",
                "schema_name": "sales",
                "name": "orders",
                "column": columns,
            },
        }
    ]


class TestExplainPlanFailure:
    plugin = IAC_PLUGINS["databricks"]
    live = [
        {"name": "id", "type": "bigint", "nullable": False},
        {"name": "amount", "type": "double", "nullable": True},
        {"name": "region", "type": "string", "nullable": True},
    ]

    def test_type_change_names_the_column(self):
        out = '{"diagnostic":{"summary":"changing the \'type\' of an existing column is not supported"}}'
        refusal = self.plugin.explain_plan_failure(out, _contract(), _state(self.live))
        assert refusal.kind == "databricks-column-type-change"
        assert "main.sales.orders.amount: double in the live table, decimal(18,2)" in str(refusal)
        assert any("--allow-data-loss" in step for step in refusal.remediation)

    def test_mixed_change(self):
        out = "Error: detected changes in both number of columns and existing column field values"
        contract = _contract()
        contract["exposes"][0]["contract"]["schema"].append({"name": "new", "type": "string"})
        refusal = self.plugin.explain_plan_failure(out, contract, _state(self.live))
        assert refusal.kind == "databricks-column-change-mixed"
        assert "amount" in str(refusal)

    def test_missing_catalog(self):
        out = "Error: cannot read catalog: Catalog 'nope' does not exist."
        refusal = self.plugin.explain_plan_failure(out, _contract(), [])
        assert refusal.kind == "databricks-catalog-missing" and "'nope'" in str(refusal)

    def test_identity_column(self):
        out = "changing the 'identity' type of an existing column is not supported"
        refusal = self.plugin.explain_plan_failure(out, _contract(), [])
        assert refusal.kind == "databricks-column-identity"

    def test_no_credentials(self):
        out = (
            "Error: cannot configure default credentials, please check "
            "https://docs.databricks.com/en/dev-tools/auth.html to configure credentials"
        )
        refusal = self.plugin.explain_plan_failure(out, _contract(), [])
        assert refusal.kind == "databricks-credentials-missing"
        assert "DATABRICKS_TOKEN" in " ".join(refusal.remediation)

    def test_anything_else_is_left_alone(self):
        assert self.plugin.explain_plan_failure("Error: 503", _contract(), []) is None


# ── the apply engine's hooks ────────────────────────────────────────────────
class TestEngine:
    def test_a_grant_revoke_is_not_data_loss(self):
        counted, revoked = engine._data_bearing_changes(
            {"add": 0, "change": 0, "remove": 1},
            [("databricks_grant.g", "databricks_grant")],
        )
        assert counted["remove"] == 0 and revoked == ["databricks_grant.g"]

    def test_a_table_destroy_still_is(self):
        counted, _ = engine._data_bearing_changes(
            {"add": 0, "change": 0, "remove": 1},
            [(TABLE, "databricks_sql_table")],
        )
        assert counted["remove"] == 1

    def test_in_place_changes_read_the_saved_plan(self, monkeypatch):
        shown = _plan(["update"], _cols("a", "b"), _cols("a"))
        monkeypatch.setattr(engine.runner, "tofu_show_plan", lambda *a, **k: shown)
        lost = engine._in_place_changes(IAC_PLUGINS["databricks"], {"change": 1}, "/w", {})
        assert lost == ["main.sales.orders: drops column(s) b"]

    def test_in_place_changes_fail_closed_when_the_plan_is_unreadable(self, monkeypatch):
        monkeypatch.setattr(engine.runner, "tofu_show_plan", lambda *a, **k: None)
        lost = engine._in_place_changes(IAC_PLUGINS["databricks"], {"change": 1}, "/w", {})
        assert lost and "cannot be ruled out" in lost[0]

    def test_no_update_means_no_extra_tofu_call(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("tofu show should not run")

        monkeypatch.setattr(engine.runner, "tofu_show_plan", boom)
        assert engine._in_place_changes(IAC_PLUGINS["databricks"], {"change": 0}, "/w", {}) == []

    def test_plan_failure_carries_the_fix_it(self, monkeypatch):
        monkeypatch.setattr(engine.runner, "tofu_state_resources", lambda *a, **k: [])
        plan = TofuResult(
            command="plan",
            returncode=1,
            stdout="cannot read catalog: Catalog 'nope' does not exist.",
            stderr="",
            events=[],
        )
        err = engine._plan_failure(IAC_PLUGINS["databricks"], _contract(), plan, "/w", {})
        assert err.event == "opentofu_plan_failed"
        assert err.context["kind"] == "databricks-catalog-missing"
        assert err.suggestions == err.context["remediation"]

    @pytest.mark.parametrize(
        "stderr,adopted",
        [("Error: Cannot import non-existent remote object", False), ("", True)],
    )
    def test_strict_adoption_tolerates_only_absence(self, monkeypatch, stderr, adopted):
        calls = []
        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: [])

        def fake_import(workdir, address, rid, env=None):
            calls.append(address)
            return TofuResult("import", 1 if stderr else 0, "", stderr, [])

        monkeypatch.setattr(engine.runner, "tofu_import", fake_import)
        engine._adopt_existing(IAC_PLUGINS["databricks"], _contract(), [], "/w", {}, _LOG)
        assert len(calls) == 2

    def test_strict_adoption_stops_on_any_other_failure(self, monkeypatch):
        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: [])
        monkeypatch.setattr(
            engine.runner,
            "tofu_import",
            lambda *a, **k: TofuResult("import", 1, "", "dial tcp: connection refused", []),
        )
        with pytest.raises(CLIError) as exc:
            engine._adopt_existing(IAC_PLUGINS["databricks"], _contract(), [], "/w", {}, _LOG)
        assert exc.value.event == "opentofu_adoption_failed"
        assert "main.sales" in exc.value.context["error"]

    def test_an_adoption_failure_keeps_its_cause(self, monkeypatch):
        # The data-source context tofu prints after the error pushed the cause out.
        stderr = "Error: PERMISSION_DENIED: no USE SCHEMA\n\n" + "  with data.x,\n" * 60
        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: [])
        monkeypatch.setattr(engine.runner, "tofu_state_resources", lambda *a, **k: [])
        monkeypatch.setattr(
            engine.runner, "tofu_import", lambda *a, **k: TofuResult("import", 1, "", stderr, [])
        )
        with pytest.raises(CLIError) as exc:
            engine._adopt_existing(IAC_PLUGINS["databricks"], _contract(), [], "/w", {}, _LOG)
        assert "Error: PERMISSION_DENIED: no USE SCHEMA" in exc.value.context["error"]


# ── the committed lock ──────────────────────────────────────────────────────
class TestDependencyLock:
    def test_the_lock_pins_a_version_the_constraint_allows(self):
        text = dependency_lock(IAC_PLUGINS["databricks"])
        assert 'provider "registry.opentofu.org/databricks/databricks"' in text
        assert 'version     = "1.140.0"' in text
        assert PROVIDER_PINS["databricks"]["version"] == "~> 1.140.0"
        # One zh: checksum per released platform (darwin/linux/windows x amd64/arm64).
        assert text.count('"zh:') == 6

    def test_written_next_to_the_module(self, tmp_path: Path):
        write_dependency_lock(IAC_PLUGINS["databricks"], tmp_path)
        assert (tmp_path / LOCK_FILE_NAME).read_text() == dependency_lock(IAC_PLUGINS["databricks"])

    def test_plugins_without_a_lock_write_nothing(self, tmp_path: Path):
        write_dependency_lock(IAC_PLUGINS["aws"], tmp_path)
        assert not (tmp_path / LOCK_FILE_NAME).exists()


# ── the plan-only provider ──────────────────────────────────────────────────
class TestPlanner:
    def test_registered_and_plans_what_the_module_manages(self):
        from fluid_build import providers
        from fluid_build.providers.base import ProviderError

        providers.discover_providers()
        provider = providers.PROVIDERS["databricks"]()
        ids = {(a["resource_type"], a["resource_id"]) for a in provider.plan(_contract())}
        assert ("databricks_sql_table", "main.sales.orders") in ids
        assert ("databricks_schema", "main.sales") in ids
        assert ("databricks_catalog", "main") in ids
        assert ("databricks_grant", "table/main.sales.orders/analysts") in ids
        assert ("databricks_grant", "schema/main.sales/analysts") in ids
        with pytest.raises(ProviderError):
            provider.apply([])
        assert provider.capabilities()["apply"] is False

    def test_refuses_what_the_emitter_refuses(self):
        # The refused table would leave no action: an empty plan, exit 0.
        contract = _contract()
        contract["exposes"][0]["binding"]["location"]["host"] = "adb-1.azuredatabricks.net"
        with pytest.raises(UnsupportedBindingError) as exc:
            dbx.planned_objects(contract)
        assert exc.value.kind == "databricks-host-in-contract"


def test_an_import_failure_the_plugin_knows_gets_its_fix_it(monkeypatch):
    # `tofu import` reads data sources first: a missing catalog fails the import.
    monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: [])
    monkeypatch.setattr(engine.runner, "tofu_state_resources", lambda *a, **k: [])
    monkeypatch.setattr(
        engine.runner,
        "tofu_import",
        lambda *a, **k: TofuResult(
            "import", 1, "", "Error: cannot read catalog: Catalog 'nope' does not exist.", []
        ),
    )
    with pytest.raises(CLIError) as exc:
        engine._adopt_existing(IAC_PLUGINS["databricks"], _contract(), [], "/w", {}, _LOG)
    assert exc.value.event == "opentofu_plan_failed"
    assert exc.value.context["kind"] == "databricks-catalog-missing"


def test_the_native_simple_apply_never_takes_the_platform():
    # The plan-only provider is registered, but only the OpenTofu engine applies
    # Databricks: a mixed contract must not hand its table to the local provider.
    from fluid_build.cli.apply import _simple_apply_provider

    local = {
        "exposeId": "orders_csv",
        "kind": "table",
        "binding": {"platform": "local", "format": "csv", "location": {"path": "out/o.csv"}},
    }
    contract = _contract()
    contract["exposes"].append(local)
    with pytest.raises(CLIError) as exc:
        _simple_apply_provider(contract, _LOG)
    assert exc.value.event == "apply_platform_unsupported"
    assert "exposes[0].binding.platform: databricks" in exc.value.context["error"]
