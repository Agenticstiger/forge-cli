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

"""Databricks IaC plugin, Stage 1 unit: the emitted module, the refusals, the apply hooks.

Pure functions only: no ``tofu``, no network. What real ``tofu`` and the real
provider do with the module is pinned against a stand-in workspace in
``test_iac_databricks_fake_e2e.py``.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.cli import _apply_opentofu_engine as engine
from fluid_build.cli._common import CLIError
from fluid_build.iac import IAC_PLUGINS, build_module, cutover, provider_match, runner
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.module import LOCK_FILE_NAME, dependency_lock, write_dependency_lock
from fluid_build.iac.providers import databricks as dbx
from fluid_build.iac.runner import TofuResult
from fluid_build.iac.versions import PROVIDER_PINS

from ._databricks_cli import isolate_workspace, make_contract, run_fluid, write_contract

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

    def test_imports_cover_the_schema_the_table_and_their_grants(self):
        imports = IAC_PLUGINS["databricks"].discover_imports(_contract())
        assert [(b.to, b.id) for b in imports if not b.best_effort] == [
            ("databricks_schema.sales_orders_main_sales", "main.sales"),
            (TABLE, "main.sales.orders"),
        ]
        # Each grant by the provider's own id, best-effort: on a new table it fails,
        # and the grant's create sets the same privileges.
        grants = {b.id for b in imports if b.best_effort and b.to.startswith("databricks_grant.")}
        assert grants == {
            "table/main.sales.orders/analysts",
            "table/main.sales.orders/bi",
            "table/main.sales.orders/alice@corp.com",
            "schema/main.sales/analysts",
            "schema/main.sales/bi",
            "schema/main.sales/alice@corp.com",
        }
        # The rest are the product's own tags on the table (test_iac_databricks_metadata.py).
        assert all(
            b.to.startswith(("databricks_grant.", "databricks_entity_tag_assignment."))
            for b in imports
            if b.best_effort
        )

    def test_other_clouds_are_untouched(self):
        contract = _contract(**{"exposes.0.binding.platform": "snowflake"})
        assert IAC_PLUGINS["databricks"].emit(contract) == {}
        assert dbx.validate_databricks_binding(contract) == ([], [])


# ── refusals: validate and the emitter share one pass ───────────────────────
_REFUSALS = [
    ("host", {"exposes.0.binding.location.host": "evil.example.com"}, "host-in-contract"),
    ("iceberg", {"exposes.0.binding.format": "iceberg"}, "iceberg-unsupported"),
    (
        "iceberg-config",
        {"exposes.0.binding.icebergConfig": {"writeVersion": 2}},
        "iceberg-unsupported",
    ),
    ("format", {"exposes.0.binding.format": "parquet"}, "format-unsupported"),
    ("kind", {"exposes.0.kind": "view"}, "kind-unsupported"),
    ("catalog", {"exposes.0.binding.location.catalog": "unity"}, "location-field"),
    ("warehouse", {"exposes.0.binding.location.warehouse": "wh"}, "location-field"),
    ("no-table", {"exposes.0.binding.location.table": ""}, "location-incomplete"),
    ("packaging", {"packaging": {"mode": "isolated"}}, "packaging-unmapped"),
    (
        "binding-packaging",
        {"exposes.0.binding.packaging": {"mode": "isolated"}},
        "packaging-unmapped",
    ),
    (
        "masking",
        {"exposes.0.policy.privacy": {"masking": [{"column": "x"}]}},
        "governance-unmapped",
    ),
    (
        "row-level",
        {"exposes.0.policy.privacy": {"rowLevelPolicy": {"a": 1}}},
        "governance-unmapped",
    ),
    # rowFilters and columnRestrictions are mapped now (databricks_access.py): a malformed
    # entry is refused as one, never dropped.
    ("row-filters", {"exposes.0.policy.authz.rowFilters": [{"a": 1}]}, "row-filter-invalid"),
    (
        "column-restr",
        {"exposes.0.policy.authz.columnRestrictions": [{"a": 1}]},
        "column-restriction-invalid",
    ),
    (
        "lf",
        {"exposes.0.binding.governance": {"lakeFormation": {"grants": []}}},
        "governance-unmapped",
    ),
    ("encryption", {"exposes.0.binding.encryption": {"kms": "k"}}, "encryption-unmapped"),
    (
        "expire",
        {"exposes.0.lifecycle": {"expire": True, "retention": "P30D"}},
        "retention-unmapped",
    ),
    ("injection", {"exposes.0.binding.location.table": "o`; DROP TABLE x"}, "identifier"),
    (
        "column-type",
        {"exposes.0.contract.schema": [{"name": "a", "type": "decimal(1,x)"}]},
        "column-type",
    ),
    # Unmapped: would be created as string.
    (
        "typed-array",
        {"exposes.0.contract.schema": [{"name": "a", "type": "array<int>"}]},
        "column-type",
    ),
    # Databricks rejects both at CREATE TABLE, after the schema exists.
    (
        "precision",
        {"exposes.0.contract.schema": [{"name": "a", "type": "decimal(40,2)"}]},
        "column-type",
    ),
    (
        "scale",
        {"exposes.0.contract.schema": [{"name": "a", "type": "decimal(10,12)"}]},
        "column-type",
    ),
    # Past Python's int() digit limit: a refusal, not an unhandled ValueError.
    (
        "huge-precision",
        {"exposes.0.contract.schema": [{"name": "a", "type": "decimal(" + "9" * 5000 + ")"}]},
        "column-type",
    ),
    ("no-columns", {"exposes.0.contract.schema": []}, "no-columns"),
    (
        "dup-column",
        {"exposes.0.contract.schema": [{"name": "A", "type": "int"}, {"name": "a", "type": "int"}]},
        "column-duplicate",
    ),
    ("partition", {"exposes.0.binding.location.partitionBy": ["nope"]}, "partition-column"),
    (
        "dup-partition",
        {"exposes.0.binding.location.partitionBy": ["region", "Region"]},
        "partition-duplicate",
    ),
    # A location key the emitter would drop: an external path.
    ("path", {"exposes.0.binding.location.path": "s3://lake/orders/"}, "location-field"),
    # The region is written into the module, so only a plain region name passes.
    ("region-template", {"exposes.0.binding.location.region": "${x}"}, "region-invalid"),
    ("region-directive", {"exposes.0.binding.location.region": "%{if true}"}, "region-invalid"),
    ("region-space", {"exposes.0.binding.location.region": "EU WEST"}, "region-invalid"),
    ("region-long", {"exposes.0.binding.location.region": "a" * 80}, "region-invalid"),
    ("region-short", {"exposes.0.binding.location.region": "a"}, "region-invalid"),
    ("region-number", {"exposes.0.binding.location.region": 12}, "region-invalid"),
    ("region-quote", {"exposes.0.binding.location.region": 'eu"'}, "region-invalid"),
]


@pytest.mark.parametrize("label,overrides,kind", _REFUSALS, ids=[r[0] for r in _REFUSALS])
def test_refusal(label: str, overrides: Dict[str, Any], kind: str):
    contract = _contract(**overrides)
    errors, _ = dbx.validate_databricks_binding(contract)
    assert errors, label
    with pytest.raises(UnsupportedBindingError) as exc:
        IAC_PLUGINS["databricks"].emit(contract)
    assert exc.value.kind == f"databricks-{kind}"
    # The plan-only provider refuses it too: a refused table would leave no
    # action, a smaller plan instead of an error.
    with pytest.raises(UnsupportedBindingError):
        dbx.planned_objects(contract)


# ── residency: binding.location.region ──────────────────────────────────────
_METASTORE = "data.databricks_current_metastore.sales_orders_metastore.metastore_info[0].region"


class TestRegion:
    def test_a_declared_region_is_accepted(self):
        contract = _contract(**{"exposes.0.binding.location.region": "eu-central-1"})
        assert dbx.validate_databricks_binding(contract)[0] == []
        assert IAC_PLUGINS["databricks"].emit(contract)

    def test_the_metastore_is_read_and_every_schema_checks_it(self):
        doc = _module(_contract(**{"exposes.0.binding.location.region": "eu-central-1"}))
        assert doc["data"]["databricks_current_metastore"] == {"sales_orders_metastore": {}}
        assert doc["data"]["databricks_catalog"] == {"sales_orders_main": {"name": "main"}}
        schema = doc["resource"]["databricks_schema"]["sales_orders_main_sales"]
        (check,) = schema["lifecycle"]["precondition"]
        assert check["condition"] == f'${{lower({_METASTORE}) == "eu-central-1"}}'
        message = check["error_message"]
        assert "eu-central-1" in message and "${" + _METASTORE + "}" in message
        assert "DATABRICKS_HOST" in message and "binding.location.region" in message

    def test_without_a_region_the_module_is_unchanged(self):
        plain = _module(_contract())
        declared = _module(_contract(**{"exposes.0.binding.location.region": "eu-central-1"}))
        assert "databricks_current_metastore" not in plain["data"]
        assert "lifecycle" not in plain["resource"]["databricks_schema"]["sales_orders_main_sales"]
        # A declared region only adds the data source and the precondition.
        del declared["data"]["databricks_current_metastore"]
        del declared["resource"]["databricks_schema"]["sales_orders_main_sales"]["lifecycle"]
        assert declared == plain

    @pytest.mark.parametrize(
        "spelled", ["EU-Central-1", " eu-central-1", "eu-central-1 ", "eu-central-1\n"]
    )
    def test_a_region_not_written_as_the_policy_compares_it_is_refused(self, spelled):
        # Normalising it let US-WEST-2 pass deniedRegions: [us-west-2] (the policy
        # compares verbatim) and then plan against a us-west-2 workspace.
        contract = _contract(**{"exposes.0.binding.location.region": spelled})
        errors, _ = dbx.validate_databricks_binding(contract)
        assert any("region" in error for error in errors)
        with pytest.raises(UnsupportedBindingError) as exc:
            IAC_PLUGINS["databricks"].emit(contract)
        assert exc.value.kind == "databricks-region-invalid"

    def test_verify_checks_every_table_against_the_declared_region(self):
        contract = _contract(**{"exposes.0.binding.location.region": "eu-central-1"})
        second = copy.deepcopy(contract["exposes"][0])
        second["exposeId"] = "orders_b"
        second["binding"]["location"].update(table="orders_b")
        del second["binding"]["location"]["region"]
        contract["exposes"].append(second)
        tables, _ = dbx.managed_objects(contract)
        assert [table.region for table in tables] == ["eu-central-1", "eu-central-1"]

    def test_two_regions_in_one_contract_are_refused(self):
        contract = _contract(**{"exposes.0.binding.location.region": "eu-central-1"})
        second = copy.deepcopy(contract["exposes"][0])
        second["exposeId"] = "orders_us"
        second["binding"]["location"].update(table="orders_us", region="us-west-2")
        contract["exposes"].append(second)
        errors, _ = dbx.validate_databricks_binding(contract)
        assert len(errors) == 1 and "eu-central-1" in errors[0] and "us-west-2" in errors[0]
        with pytest.raises(UnsupportedBindingError) as exc:
            IAC_PLUGINS["databricks"].emit(contract)
        assert exc.value.kind == "databricks-region-conflict"

    def test_the_same_region_twice_is_one_data_source_and_no_conflict(self):
        contract = _contract(**{"exposes.0.binding.location.region": "eu-central-1"})
        second = copy.deepcopy(contract["exposes"][0])
        second["exposeId"] = "orders_b"
        second["binding"]["location"].update(table="orders_b", region="eu-central-1")
        contract["exposes"].append(second)
        assert dbx.validate_databricks_binding(contract)[0] == []
        doc = _module(contract)
        assert list(doc["data"]["databricks_current_metastore"]) == ["sales_orders_metastore"]

    def test_a_schema_two_exposes_share_keeps_the_check_whichever_declares_it(self):
        # The schema resource is claimed once per table and keeps the last body,
        # so taking the region per table lost the precondition when the last
        # expose in a shared schema declared none.
        contract = _contract(**{"exposes.0.binding.location.region": "eu-central-1"})
        second = copy.deepcopy(contract["exposes"][0])
        second["exposeId"] = "orders_b"
        second["binding"]["location"].update(table="orders_b")
        del second["binding"]["location"]["region"]
        contract["exposes"].append(second)
        assert dbx.validate_databricks_binding(contract)[0] == []
        schema = _module(contract)["resource"]["databricks_schema"]["sales_orders_main_sales"]
        (check,) = schema["lifecycle"]["precondition"]
        assert check["condition"] == f'${{lower({_METASTORE}) == "eu-central-1"}}'

    def test_every_schema_is_checked_when_one_expose_declares_the_region(self):
        contract = _contract(**{"exposes.0.binding.location.region": "eu-central-1"})
        second = copy.deepcopy(contract["exposes"][0])
        second["exposeId"] = "events"
        second["binding"]["location"].update(schema="events", table="events")
        del second["binding"]["location"]["region"]
        contract["exposes"].append(second)
        schemas = _module(contract)["resource"]["databricks_schema"]
        assert sorted(schemas) == ["sales_orders_main_events", "sales_orders_main_sales"]
        assert all("precondition" in body.get("lifecycle", {}) for body in schemas.values())

    def test_the_import_and_plan_paths_are_unaffected(self):
        plain, declared = _contract(), _contract(
            **{"exposes.0.binding.location.region": "eu-central-1"}
        )
        plugin = IAC_PLUGINS["databricks"]
        assert plugin.discover_imports(declared) == plugin.discover_imports(plain)
        assert dbx.planned_objects(declared) == dbx.planned_objects(plain)
        (table,) = dbx.managed_objects(declared)[0]
        assert table.region == "eu-central-1"


class TestSovereigntyOnEmit:
    """``fluid apply`` and ``generate iac`` reach the module through ``emit`` alone,
    so the policy is held there, as the GCP emitter holds it."""

    @staticmethod
    def _sovereign(mode: str = "strict", **policy: Any) -> Dict[str, Any]:
        contract = _contract()
        contract["sovereignty"] = {"enforcementMode": mode, **policy}
        return contract

    def test_no_region_under_strict_is_refused_on_emit(self):
        from fluid_build._errors import SovereigntyViolationError

        contract = self._sovereign(jurisdiction="EU")
        with pytest.raises(SovereigntyViolationError) as exc:
            IAC_PLUGINS["databricks"].emit(contract)
        assert "orders" in str(exc.value) and "no region" in str(exc.value)

    def test_a_declared_denied_region_is_refused_on_emit(self):
        from fluid_build._errors import SovereigntyViolationError

        contract = self._sovereign(deniedRegions=["us-west-2"])
        contract["exposes"][0]["binding"]["location"]["region"] = "us-west-2"
        with pytest.raises(SovereigntyViolationError) as exc:
            IAC_PLUGINS["databricks"].emit(contract)
        assert "us-west-2" in str(exc.value)
        # A deny-list-only policy names its deny-list, not a missing jurisdiction.
        assert "denies us-west-2" in exc.value.why and "None" not in exc.value.why

    def test_a_declared_allowed_region_is_emitted(self):
        contract = self._sovereign(jurisdiction="EU", allowedRegions=["eu-central-1"])
        contract["exposes"][0]["binding"]["location"]["region"] = "eu-central-1"
        assert IAC_PLUGINS["databricks"].emit(contract)

    def test_advisory_only_warns(self, caplog):
        contract = self._sovereign("advisory", jurisdiction="EU")
        with caplog.at_level(logging.WARNING):
            assert IAC_PLUGINS["databricks"].emit(contract)
        assert "databricks sovereignty" in caplog.text

    def test_validate_reports_the_policy_once_not_from_the_binding_check(self):
        # fluid validate runs the policy itself; the binding check stays silent on it.
        assert dbx.validate_databricks_binding(self._sovereign(jurisdiction="EU"))[0] == []


# ── governance tags ─────────────────────────────────────────────────────────
SCHEMA_TAGS = {
    "cost_center": "cx-001",
    "data_classification": "confidential",
    "fluid_jurisdiction": "eu",
    "fluid_regulation": "gdpr",
    "fluid_residency": "true",
    "gdpr_article": "art6-1b-contractual",
}
#: The product's own tags on the table, from ``_contract()``'s id, name and fluidVersion
#: (the rest of them: test_iac_databricks_metadata.py).
PRODUCT_TAGS = {
    "fluid_product_id": "sales.orders",
    "fluid_product_name": "Orders",
    "fluid_version": "0.7.6",
}
TABLE_TAGS = {**SCHEMA_TAGS, **PRODUCT_TAGS, "retention": "7y", "sensitivity": "high"}
TAG = "databricks_entity_tag_assignment"


def _governed(**overrides: Any) -> Dict[str, Any]:
    """A CP360-like contract: contract and expose labels, and a sovereignty policy."""
    contract = _contract(**overrides)
    contract["labels"] = {
        "data-classification": "confidential",
        "cost-center": "cx-001",
        "gdpr-article": "art6-1b-contractual",
    }
    contract["exposes"][0]["labels"] = {"sensitivity": "high", "retention": "7y"}
    contract["sovereignty"] = {
        "jurisdiction": "EU",
        "regulatoryFramework": ["GDPR"],
        "dataResidency": True,
        "enforcementMode": "advisory",
    }
    return contract


def _tags_on(doc: Dict[str, Any], entity_type: str) -> Dict[str, str]:
    return {
        body["tag_key"]: body["tag_value"]
        for body in doc["resource"].get(TAG, {}).values()
        if body["entity_type"] == entity_type
    }


class TestGovernanceTags:
    def test_the_schema_and_the_table_carry_the_contracts_governance_as_tags(self):
        doc = _module(_governed())
        assert _tags_on(doc, "schemas") == SCHEMA_TAGS
        assert _tags_on(doc, "tables") == TABLE_TAGS

    def test_a_dash_in_a_label_is_an_underscore_in_the_tag_key_and_the_value_is_kept(self):
        doc = _module(_governed())
        schema_tag = doc["resource"][TAG]["sales_orders_main_sales_schema_tag_data_classification"]
        assert schema_tag == {
            "entity_type": "schemas",
            "entity_name": "${databricks_schema.sales_orders_main_sales.id}",
            "tag_key": "data_classification",
            "tag_value": "confidential",
        }
        table_tag = doc["resource"][TAG]["sales_orders_main_sales_orders_table_tag_sensitivity"]
        assert table_tag == {
            "entity_type": "tables",
            "entity_name": "${databricks_sql_table.sales_orders_main_sales_orders.id}",
            "tag_key": "sensitivity",
            "tag_value": "high",
        }
        assert doc["resource"][TAG]["sales_orders_main_sales_schema_tag_gdpr_article"][
            "tag_value"
        ] == ("art6-1b-contractual")
        assert not any("-" in body["tag_key"] for body in doc["resource"][TAG].values())

    def test_a_contract_without_labels_tags_only_the_table_with_the_product(self):
        doc = _module(_contract())
        assert _tags_on(doc, "schemas") == {}
        assert _tags_on(doc, "tables") == PRODUCT_TAGS
        tag_imports = [
            block.id
            for block in IAC_PLUGINS["databricks"].discover_imports(_contract())
            if "tag_assignment" in block.to
        ]
        assert sorted(tag_imports) == sorted(f"tables,main.sales.orders,{k}" for k in PRODUCT_TAGS)
        assert {
            a["resource_id"] for a in dbx.planned_objects(_contract()) if a["resource_type"] == TAG
        } == set(tag_imports)
        (table,), _ = dbx.managed_objects(_contract())
        assert dict(table.tags) == PRODUCT_TAGS and table.schema_tags == ()

    def test_two_exposes_in_one_schema_claim_one_body_per_schema_tag(self):
        contract = _governed()
        second = copy.deepcopy(contract["exposes"][0])
        second["exposeId"] = "lines"
        second["binding"]["location"]["table"] = "lines"
        second["labels"] = {"sensitivity": "low"}
        contract["exposes"].append(second)
        doc = _module(contract)
        schema_tags = [b for b in doc["resource"][TAG].values() if b["entity_type"] == "schemas"]
        assert len(schema_tags) == len(SCHEMA_TAGS)
        assert _tags_on(doc, "schemas") == SCHEMA_TAGS
        tables = {
            b["entity_name"]: b["tag_value"]
            for b in doc["resource"][TAG].values()
            if b["tag_key"] == "sensitivity"
        }
        assert tables == {
            "${databricks_sql_table.sales_orders_main_sales_orders.id}": "high",
            "${databricks_sql_table.sales_orders_main_sales_lines.id}": "low",
        }
        imports = [i for i in IAC_PLUGINS["databricks"].discover_imports(contract) if "tag" in i.to]
        assert len({i.to for i in imports}) == len(imports)

    def test_existing_assignments_are_imported_best_effort_by_the_providers_id(self):
        imports = {
            block.to: block
            for block in IAC_PLUGINS["databricks"].discover_imports(_governed())
            if block.to.startswith(TAG)
        }
        assert len(imports) == len(SCHEMA_TAGS) + len(TABLE_TAGS)
        schema = imports[f"{TAG}.sales_orders_main_sales_schema_tag_data_classification"]
        assert schema.id == "schemas,main.sales,data_classification"
        table = imports[f"{TAG}.sales_orders_main_sales_orders_table_tag_sensitivity"]
        assert table.id == "tables,main.sales.orders,sensitivity"
        assert all(block.best_effort for block in imports.values())

    def test_the_planner_names_each_assignment_by_its_import_id(self):
        ids = {
            a["resource_id"] for a in dbx.planned_objects(_governed()) if a["resource_type"] == TAG
        }
        assert "schemas,main.sales,fluid_jurisdiction" in ids
        assert "tables,main.sales.orders,retention" in ids
        assert len(ids) == len(SCHEMA_TAGS) + len(TABLE_TAGS)

    def test_two_labels_that_meet_at_one_tag_key_are_refused_never_overwritten(self):
        contract = _governed()
        contract["labels"] = {"cost-center": "a", "cost_center": "b"}
        for call in (
            lambda: IAC_PLUGINS["databricks"].emit(contract),
            lambda: dbx.planned_objects(contract),
        ):
            with pytest.raises(UnsupportedBindingError) as raised:
                call()
            assert raised.value.kind == "databricks-tag-collision"
            assert "cost_center" in str(raised.value)
        errors, _ = dbx.validate_databricks_binding(contract)
        # the schema and the table both carry the contract's labels
        assert len(errors) == 2 and all("cost_center" in e for e in errors)

    def test_more_tags_than_a_securable_holds_is_refused_never_dropped(self):
        contract = _contract()
        contract["labels"] = {f"k{i:03d}": "v" for i in range(51)}
        with pytest.raises(UnsupportedBindingError) as raised:
            IAC_PLUGINS["databricks"].emit(contract)
        assert raised.value.kind == "databricks-tags-too-many"
        assert "51" in str(raised.value) and "50" in str(raised.value)
        errors, _ = dbx.validate_databricks_binding(contract)
        # the schema and the table both carry them: one refusal for each securable
        assert len(errors) == 2 and all("51" in e for e in errors)
        # and counted past governance_labels' own cap of 60
        contract["labels"] = {f"k{i:03d}": "v" for i in range(75)}
        assert "75" in dbx.validate_databricks_binding(contract)[0][0]

    def test_exactly_fifty_tags_fit(self):
        contract = _contract()
        contract["labels"] = {f"k{i:03d}": "v" for i in range(50 - len(PRODUCT_TAGS))}
        doc = _module(contract)
        assert len(_tags_on(doc, "schemas")) == 47 and len(_tags_on(doc, "tables")) == 50

    def test_the_table_alone_over_the_cap_is_refused_once(self):
        contract = _contract()
        contract["labels"] = {f"k{i:03d}": "v" for i in range(48)}
        contract["exposes"][0]["labels"] = {"x1": "a", "x2": "b", "x3": "c"}
        errors, _ = dbx.validate_databricks_binding(contract)
        assert len(errors) == 1 and "exposes[0] (orders)" in errors[0]

    def test_managed_objects_carry_the_expected_tags_for_verify(self):
        (table,), _ = dbx.managed_objects(_governed())
        assert dict(table.tags) == TABLE_TAGS
        assert dict(table.schema_tags) == SCHEMA_TAGS


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
        ({"principal": "group:x", "permissions": ["read"], "resources": ["$.exposes[0]"]}, "scope"),
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


def test_a_bare_decimal_warns_that_it_keeps_no_fraction():
    schema = [
        {"name": "price", "type": "number"},
        {"name": "fee", "type": "decimal(12)"},
        {"name": "rate", "type": "decimal(12,4)"},
    ]
    errors, warnings = dbx.validate_databricks_binding(
        _contract(**{"exposes.0.contract.schema": schema})
    )
    assert errors == []
    assert len(warnings) == 1 and "price, fee with no scale" in warnings[0]
    assert "DECIMAL(10,0)" in warnings[0] and "rate" not in warnings[0]


@pytest.mark.parametrize(
    "declared,emitted",
    [
        ("number(12)", "decimal(12,0)"),
        ("decimal(038, 2)", "decimal(38,2)"),
        ("number", "decimal"),
        ("decimal(18,2)", "decimal(18,2)"),
    ],
)
def test_a_decimal_is_emitted_as_databricks_reports_it(declared: str, emitted: str):
    # The provider compares the declared type with the live one as text: any
    # other spelling plans a type change, refused, on every apply after the first.
    contract = _contract(**{"exposes.0.contract.schema": [{"name": "a", "type": declared}]})
    [table] = IAC_PLUGINS["databricks"].emit(contract)["databricks_sql_table"].values()
    assert table["column"][0]["type"] == emitted
    assert (
        dbx._changed_columns([{"name": "a", "type": emitted}], [{"name": "a", "type": declared}])
        == []
    )


def test_fluid_validate_reports_the_refusals(monkeypatch, tmp_path: Path, capfd):
    isolate_workspace(
        monkeypatch,
        tmp_path,
        host="https://adb-1.azuredatabricks.net",
        warehouse_id=WAREHOUSE,
        plugin_cache=str(tmp_path),
    )
    contract = make_contract()
    contract["exposes"][0]["binding"]["location"]["host"] = "evil.example.com"
    rc, out = run_fluid(capfd, "validate", write_contract(tmp_path, contract))
    assert rc != 0 and "sets binding.location.host" in out


@pytest.mark.parametrize(
    "command",
    [
        ("generate", "iac"),
        # apply checks for the tofu binary before it emits, so without one it never refuses
        pytest.param(
            ("apply",),
            marks=pytest.mark.skipif(runner.tofu_path() is None, reason="tofu not installed"),
        ),
    ],
)
def test_a_sovereignty_refusal_prints_its_reason_and_fix(
    monkeypatch, tmp_path: Path, capfd, command
):
    isolate_workspace(
        monkeypatch,
        tmp_path,
        host="https://adb-1.azuredatabricks.net",
        warehouse_id=WAREHOUSE,
        plugin_cache=str(tmp_path),
    )
    contract = make_contract(region="europe-west3")
    contract["sovereignty"] = {
        "allowedRegions": ["me-central-1", "me-south-1"],
        "enforcementMode": "strict",
    }
    path = write_contract(tmp_path, contract)
    extra = ("--out", str(tmp_path / "iac")) if command[0] == "generate" else ("--yes",)
    rc, out = run_fluid(capfd, *command, path, *extra)
    # the typed-error panel: colours and its border cut the wrapped text
    out = " ".join(re.sub(r"\x1b\[[0-9;]*m|[│╭╮╰╯─]", " ", out).split())
    assert rc != 0
    assert "Region 'europe-west3' not in allowed regions list" in out
    assert "allows me-central-1, me-south-1" in out
    assert "Set binding.location.region on every databricks expose" in out


def test_a_databricks_build_beside_non_cloud_exposes_still_meets_the_host_guard(
    monkeypatch, tmp_path: Path, capfd
):
    # Its only cloud is the build's: the contract must still compile through the
    # Databricks plugin, or apply goes native and the dbt runner sends
    # DATABRICKS_TOKEN to the host the contract names.
    isolate_workspace(
        monkeypatch,
        tmp_path,
        host="https://adb-1.azuredatabricks.net",
        warehouse_id=WAREHOUSE,
        plugin_cache=str(tmp_path),
    )
    contract = make_contract()
    contract["exposes"] = [
        {
            "exposeId": "events",
            "kind": "table",
            "binding": {"platform": "kafka", "format": "json", "location": {"topic": "events"}},
            "contract": {"schema": [{"name": "id", "type": "string"}]},
        }
    ]
    contract["builds"] = [
        {
            "id": "b1",
            "engine": "dbt",
            "pattern": "embedded-logic",
            "repository": "./",
            "execution": {
                "trigger": {"type": "manual"},
                "runtime": {"platform": "databricks", "resources": {"host": "127.0.0.1:8999"}},
            },
        }
    ]
    rc, out = run_fluid(capfd, "generate", "iac", write_contract(tmp_path, contract))
    assert rc != 0 and "databricks-host-in-contract" in out


def test_fluid_validate_fails_closed_when_the_check_cannot_run(monkeypatch):
    from fluid_build.cli import validate as validate_cli

    class _Result:
        is_valid = True

        def __init__(self) -> None:
            self.errors: List[str] = []

        def add_error(self, message: str) -> None:
            self.errors.append(message)

    def broken(contract):
        raise RuntimeError("boom")

    monkeypatch.setattr(dbx, "validate_databricks_binding", broken)
    result = _Result()
    validate_cli._databricks_binding_check(result, _contract())
    assert result.is_valid is False and "NOT checked" in result.errors[0]


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


def test_principals_that_fold_to_one_name_keep_their_own_grants():
    # safe_ident turns both into data_eng: one address, a collision the contract
    # cannot rename away. The principals are distinct identities.
    contract = _contract(**{"exposes.0.policy.authz.readers": ["data-eng", "data_eng"]})
    contract["accessPolicy"] = {}
    assert dbx.validate_databricks_binding(contract) == ([], [])
    grants = IAC_PLUGINS["databricks"].emit(contract)["databricks_grant"]
    on_table = sorted(g["principal"] for g in grants.values() if "table" in g)
    assert on_table == ["data-eng", "data_eng"]


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

    @pytest.mark.parametrize(
        "before,after,reorder",
        [
            # Kept columns reordered while one is added: added by name, never moved.
            (("a", "b"), ("b", "a", "c"), (["a", "b"], ["b", "a"])),
            # A swap at an equal count: renaming a onto b, which exists, fails.
            (("a", "b"), ("b", "a"), (["a", "b"], ["b", "a"])),
            (("a", "b"), ("a", "x"), None),
            (("a", "b"), ("a", "b", "c"), None),
            (("a", "b", "c"), ("a", "c"), None),
        ],
    )
    def test_a_reorder_the_provider_cannot_reach(self, before, after, reorder):
        assert dbx.column_reorder(_cols(*before), _cols(*after)) == reorder


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

    def test_a_reorder_is_refused_not_gated(self):
        with pytest.raises(UnsupportedBindingError) as exc:
            IAC_PLUGINS["databricks"].plan_data_changes(
                _plan(["update"], _cols("a", "b"), _cols("b", "a", "c"))
            )
        assert exc.value.kind == "databricks-column-reorder"
        assert "live order" in " ".join(exc.value.remediation)


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

    def test_an_unreachable_workspace(self):
        out = "Error: cannot read schema: dial tcp 127.0.0.1:9: connect: connection refused"
        refusal = self.plugin.explain_plan_failure(out, _contract(), [])
        assert refusal.kind == "databricks-workspace-unreachable"

    RESIDENCY = (
        "The contract declares binding.location.region eu-central-1, but the workspace "
        "DATABRICKS_HOST names is in us-west-2. Residency is checked against the workspace's "
        "metastore. Point DATABRICKS_HOST at a workspace in eu-central-1, or correct "
        "binding.location.region."
    )

    def _residency_json(self, padding=2000):
        diagnostic = {
            "severity": "error",
            "summary": "Resource precondition failed",
            "detail": self.RESIDENCY,
            "range": {"filename": "main.tf.json"},
            "snippet": {
                "code": "x" * padding,
                "values": [
                    {
                        "traversal": "data.databricks_current_metastore.m.metastore_info[0].region",
                        "statement": 'is "us-west-2"',
                    }
                ],
            },
        }
        return json.dumps({"@level": "error", "diagnostic": diagnostic})

    def test_a_residency_failure_is_one_clean_line_with_both_regions_and_the_fix(self):
        out = self._residency_json()
        assert len(out) > 1600, "the engine's tail would cut the sentence"
        refusal = self.plugin.explain_plan_failure(out, _contract(), [])
        text = str(refusal)
        assert refusal.kind == "databricks-residency-region"
        assert "\n" not in text and not refusal.remediation
        assert "eu-central-1" in text and "us-west-2" in text
        assert "DATABRICKS_HOST" in text and "binding.location.region" in text

    def test_a_residency_failure_in_the_wrapped_human_output(self):
        out = "Error: Resource precondition failed\n\n  on main.tf.json line 3:\n\n" + "\n".join(
            "  " + line for line in __import__("textwrap").wrap(self.RESIDENCY, 60)
        )
        text = str(self.plugin.explain_plan_failure(out, _contract(), []))
        assert "eu-central-1" in text and "us-west-2" in text and "\n" not in text

    def test_the_live_region_comes_from_the_diagnostic_values_when_the_text_lacks_it(self):
        diagnostic = {
            "detail": "The contract declares binding.location.region eu-central-1, but the "
            "workspace DATABRICKS_HOST names is in (unknown).",
            "snippet": {"values": [{"statement": 'is "us-west-2"'}]},
        }
        out = json.dumps({"diagnostic": diagnostic})
        text = str(self.plugin.explain_plan_failure(out, _contract(), []))
        assert "us-west-2" in text and "eu-central-1" in text

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

    def test_removing_a_label_destroys_only_its_assignment_which_is_not_data_loss(self):
        removals = [(f"{TAG}.t", TAG)]
        counted, revoked = engine._data_bearing_changes(
            {"add": 0, "change": 0, "remove": 1}, removals
        )
        assert counted["remove"] == 0 and revoked == [f"{TAG}.t"]
        # the gate lets it through without --allow-data-loss
        engine._enforce_data_loss_gate(
            argparse.Namespace(allow_data_loss=False), _LOG, "databricks", counted, counted, []
        )

    def test_an_assignment_beside_a_table_destroy_still_gates_the_table(self):
        counted, revoked = engine._data_bearing_changes(
            {"add": 0, "change": 0, "remove": 2},
            [(f"{TAG}.t", TAG), (TABLE, "databricks_sql_table")],
        )
        assert counted["remove"] == 1 and revoked == [f"{TAG}.t"]

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

    def test_a_plan_the_plugin_refuses_stops_the_apply(self, monkeypatch):
        shown = _plan(["update"], _cols("a", "b"), _cols("b", "a", "c"))
        monkeypatch.setattr(engine.runner, "tofu_show_plan", lambda *a, **k: shown)
        with pytest.raises(CLIError) as exc:
            engine._in_place_changes(IAC_PLUGINS["databricks"], {"change": 1}, "/w", {})
        assert exc.value.event == "unsupported_binding"
        assert exc.value.context["kind"] == "databricks-column-reorder"

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
    def test_strict_adoption_tolerates_only_absence(self, monkeypatch, capfd, stderr, adopted):
        calls = []
        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: [])

        def fake_import(workdir, address, rid, env=None):
            calls.append(address)
            return TofuResult("import", 1 if stderr else 0, "", stderr, [])

        monkeypatch.setattr(engine.runner, "tofu_import", fake_import)
        imports = IAC_PLUGINS["databricks"].discover_imports(_contract())
        engine._adopt_existing(IAC_PLUGINS["databricks"], _contract(), [], "/w", {}, _LOG)
        assert calls == [block.to for block in imports]
        out = "".join(capfd.readouterr())
        assert (f"adopted {len(imports)} pre-existing" in out) is adopted

    def test_a_failed_grant_import_never_stops_the_apply(self, monkeypatch):
        # A grant's securable may not exist yet; its create replaces what an import
        # would have adopted, so only the schema and the table import strictly.
        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: [])

        def fake_import(workdir, address, rid, env=None):
            ok = not address.startswith("databricks_grant.")
            return TofuResult("import", 0 if ok else 1, "", "" if ok else "Error: 404", [])

        monkeypatch.setattr(engine.runner, "tofu_import", fake_import)
        engine._adopt_existing(IAC_PLUGINS["databricks"], _contract(), [], "/w", {}, _LOG)

    def test_an_in_place_override_is_audited(self, caplog):
        changes = {"add": 0, "change": 1, "remove": 0}
        lost = ["main.sales.orders: drops column(s) b"]
        with caplog.at_level(logging.WARNING):
            engine._enforce_data_loss_gate(
                argparse.Namespace(allow_data_loss=True), _LOG, "databricks", changes, changes, lost
            )
        audit = [r.getMessage() for r in caplog.records]
        audit = [m for m in audit if "opentofu_destructive_gate_override" in m]
        assert len(audit) == 1 and "in_place_data_loss" in audit[0]
        assert "drops column(s) b" in audit[0]

    def test_strict_adoption_stops_on_any_other_failure(self, monkeypatch):
        monkeypatch.setattr(engine.runner, "tofu_state_list", lambda *a, **k: [])
        monkeypatch.setattr(
            engine.runner,
            "tofu_import",
            lambda *a, **k: TofuResult("import", 1, "", "Error: 500 Internal Server Error", []),
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
        # `fluid generate iac --shadow` reads the same names off both engines.
        from fluid_build.iac.shadow import shadow_compare

        report = shadow_compare(
            _contract(), plugin=IAC_PLUGINS["databricks"], native_actions=provider.plan(_contract())
        )
        # Grants are OpenTofu-only, as other clouds' IAM members are.
        assert report.ok and {r.kind for r in report.matched} == {"schema", "table"}
        with pytest.raises(ProviderError):
            provider.apply([])
        assert provider.capabilities()["apply"] is False


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


@pytest.mark.parametrize(
    "value", [{1: "a", "b": "c"}, {__import__("datetime").date(2024, 1, 1): "x"}]
)
def test_a_contract_json_cannot_key_reuses_no_pause_decision(value):
    contract = _contract()
    contract["metadata"] = {"free_form": value}
    assert dbx._contract_key(contract) is None
    IAC_PLUGINS["databricks"].emit(contract)  # no TypeError from the key
