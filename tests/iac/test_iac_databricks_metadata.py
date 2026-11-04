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

"""Unity Catalog metadata from the contract: comments, product tags, column tags.

The table's ``comment`` comes from the expose's description (else the product's), each
column's from its schema description; the product's identity and owner team are tags on the
table; a column the contract classifies (``sensitivity``) carries it as a column tag. All of
it from the one ``_compile`` pass, so ``fluid validate``, the emitter, the planner and
``fluid verify`` agree.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from fluid_build.iac import IAC_PLUGINS, build_module
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.providers import databricks as dbx

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

TABLE = "databricks_sql_table.tower_tickets_main_ops_tickets"
TAG = "databricks_entity_tag_assignment"


@pytest.fixture(autouse=True)
def _warehouse(monkeypatch):
    monkeypatch.setenv(dbx.WAREHOUSE_ENV, "wh-0123456789abcdef")


def _contract() -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "tower.tickets",
        "name": "Tower maintenance tickets",
        "domain": "network-operations",
        "description": "Every maintenance ticket raised against a tower.",
        "metadata": {
            "owner": {"team": "tower-ops", "email": "tower-ops@northwind.example"},
            "layer": "Silver",
            "productType": "ADP",
        },
        "exposes": [
            {
                "exposeId": "tickets",
                "kind": "table",
                "version": "1.2.0",
                "description": "One row per ticket, as the field team's system records it.",
                "binding": {
                    "platform": "databricks",
                    "format": "delta_table",
                    "location": {"database": "main", "schema": "ops", "table": "tickets"},
                },
                "contract": {
                    "schema": [
                        {
                            "name": "ticket_id",
                            "type": "string",
                            "required": True,
                            "description": "The ticket's id in the field system.",
                        },
                        {
                            "name": "engineer_email",
                            "type": "string",
                            "description": "Who worked the ticket.",
                            "sensitivity": "pii",
                        },
                        {"name": "tower_id", "type": "string", "sensitivity": "internal"},
                        {"name": "opened_at", "type": "timestamp"},
                    ]
                },
            }
        ],
    }


IDENTITY = {
    "fluid_product_id": "tower.tickets",
    "fluid_product_name": "Tower maintenance tickets",
    "fluid_domain": "network-operations",
    "fluid_layer": "Silver",
    "fluid_product_type": "ADP",
    "fluid_product_version": "1.2.0",
    "fluid_version": "0.7.7",
    "fluid_owner_team": "tower-ops",
}


#: A dbt build that writes the expose and runs where the operator runs it (no job).
_DBT_BUILD = {"id": "tickets_model", "engine": "dbt", "outputs": ["tickets"], "execution": {}}


def _module(contract: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(build_module(IAC_PLUGINS["databricks"], contract))


def _table(doc: Dict[str, Any]) -> Dict[str, Any]:
    return doc["resource"]["databricks_sql_table"]["tower_tickets_main_ops_tickets"]


def _tags(doc: Dict[str, Any], entity_type: str) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for body in doc["resource"].get(TAG, {}).values():
        if body["entity_type"] == entity_type:
            out.setdefault(body["entity_name"], {})[body["tag_key"]] = body["tag_value"]
    return out


def _refused(contract: Dict[str, Any]) -> UnsupportedBindingError:
    with pytest.raises(UnsupportedBindingError) as raised:
        IAC_PLUGINS["databricks"].emit(contract)
    return raised.value


# ── comments ────────────────────────────────────────────────────────────────
class TestComments:
    def test_the_table_comment_is_the_exposes_description(self):
        assert _table(_module(_contract()))["comment"] == (
            "One row per ticket, as the field team's system records it."
        )

    def test_without_one_it_is_the_products_description(self):
        contract = _contract()
        del contract["exposes"][0]["description"]
        assert _table(_module(contract))["comment"] == (
            "Every maintenance ticket raised against a tower."
        )

    def test_a_structured_product_description_gives_its_purpose(self):
        contract = _contract()
        del contract["exposes"][0]["description"]
        contract["description"] = {"purpose": "Tickets per tower.", "usage": "Join on tower_id."}
        assert _table(_module(contract))["comment"] == "Tickets per tower."

    def test_no_description_no_comment(self):
        contract = _contract()
        del contract["exposes"][0]["description"]
        del contract["description"]
        assert "comment" not in _table(_module(contract))

    def test_each_described_column_carries_its_comment(self):
        columns = _table(_module(_contract()))["column"]
        assert columns[0] == {
            "name": "ticket_id",
            "type": "string",
            "nullable": False,
            "comment": "The ticket's id in the field system.",
        }
        assert columns[1]["comment"] == "Who worked the ticket."
        assert "comment" not in columns[2] and "comment" not in columns[3]

    def test_surrounding_whitespace_is_not_part_of_a_comment(self):
        contract = _contract()
        contract["exposes"][0]["description"] = "  padded \n"
        contract["exposes"][0]["contract"]["schema"][0]["description"] = "   "
        table = _table(_module(contract))
        assert table["comment"] == "padded"
        assert "comment" not in table["column"][0]

    def test_an_interpolation_in_a_description_stays_a_literal(self):
        contract = _contract()
        contract["exposes"][0]["description"] = 'see ${file("/etc/passwd")}'
        rendered = build_module(IAC_PLUGINS["databricks"], contract)
        assert '$${file(\\"/etc/passwd\\")}' in rendered
        assert '"see ${file' not in rendered

    @pytest.mark.parametrize(
        "where",
        [
            ("exposes", 0, "description"),
            ("exposes", 0, "contract", "schema", 1, "description"),
            ("description",),
        ],
    )
    def test_a_backslash_is_refused_because_the_provider_would_unbalance_the_quote(self, where):
        contract = _contract()
        if where == ("description",):
            del contract["exposes"][0]["description"]
        target: Any = contract
        for key in where[:-1]:
            target = target[key]
        target[where[-1]] = "ends in a backslash \\"
        refusal = _refused(contract)
        assert refusal.kind == "databricks-comment-unsafe"
        assert "backslash" in str(refusal)
        errors, _ = dbx.validate_databricks_binding(contract)
        assert len(errors) == 1 and "databricks-comment-unsafe" not in errors[0]
        assert "backslash" in errors[0]

    def test_a_dbt_written_table_leaves_its_comments_to_dbt(self):
        contract = _contract()
        contract["builds"] = [_DBT_BUILD]
        table = _table(_module(contract))
        assert "comment" not in table
        assert all("comment" not in column for column in table["column"])
        assert {"comment", "column"} <= set(table["lifecycle"]["ignore_changes"])
        _errors, warnings = dbx.validate_databricks_binding(contract)
        assert any("persist_docs" in w and "exposes[0] (tickets)" in w for w in warnings)
        (managed,), _ = dbx.managed_objects(contract)
        assert managed.comment is None and managed.column_comments == ()


# ── product identity tags ───────────────────────────────────────────────────
class TestProductTags:
    def test_the_table_carries_the_products_identity_and_owner_team(self):
        doc = _module(_contract())
        assert _tags(doc, "tables") == {
            "${databricks_sql_table.tower_tickets_main_ops_tickets.id}": IDENTITY
        }

    def test_the_schema_does_not(self):
        # two products may share a schema; a product's identity is its table's
        assert _tags(_module(_contract()), "schemas") == {}

    def test_absent_fields_make_no_tag(self):
        contract = _contract()
        for key in ("domain", "metadata"):
            del contract[key]
        del contract["exposes"][0]["version"]
        (tags,) = _tags(_module(contract), "tables").values()
        assert tags == {
            "fluid_product_id": "tower.tickets",
            "fluid_product_name": "Tower maintenance tickets",
            "fluid_version": "0.7.7",
        }

    def test_a_value_is_kept_as_written_without_surrounding_whitespace(self):
        contract = _contract()
        contract["name"] = "  Tower tickets, v2: 50/50 = done.  "
        (tags,) = _tags(_module(contract), "tables").values()
        assert tags["fluid_product_name"] == "Tower tickets, v2: 50/50 = done."

    def test_a_value_longer_than_unity_catalog_allows_is_refused_not_cut(self):
        contract = _contract()
        contract["name"] = "x" * 257
        refusal = _refused(contract)
        assert refusal.kind == "databricks-tag-value-too-long"
        assert "fluid_product_name" in str(refusal) and "256" in str(refusal)
        contract["name"] = "x" * 256
        (tags,) = _tags(_module(contract), "tables").values()
        assert len(tags["fluid_product_name"]) == 256

    def test_a_label_that_meets_an_identity_tag_is_refused(self):
        contract = _contract()
        contract["labels"] = {"fluid-product-id": "spoofed"}
        refusal = _refused(contract)
        assert refusal.kind == "databricks-tag-collision"
        assert "fluid_product_id" in str(refusal)

    def test_identity_tags_count_toward_the_fifty(self):
        contract = _contract()
        contract["labels"] = {f"k{i:03d}": "v" for i in range(50 - len(IDENTITY))}
        (tags,) = _tags(_module(contract), "tables").values()
        assert len(tags) == 50
        contract["labels"]["one_more"] = "v"
        errors, _ = dbx.validate_databricks_binding(contract)
        # only the table is over: the schema carries the labels alone
        assert len(errors) == 1 and "51" in errors[0] and "exposes[0] (tickets)" in errors[0]

    def test_imports_and_planner_name_each_by_the_providers_id(self):
        ids = {
            a["resource_id"] for a in dbx.planned_objects(_contract()) if a["resource_type"] == TAG
        }
        assert "tables,main.ops.tickets,fluid_product_id" in ids
        assert "tables,main.ops.tickets,fluid_owner_team" in ids
        imports = {
            block.id
            for block in IAC_PLUGINS["databricks"].discover_imports(_contract())
            if block.to.startswith(TAG)
        }
        assert "tables,main.ops.tickets,fluid_layer" in imports

    def test_managed_objects_carry_them_for_verify(self):
        (table,), _ = dbx.managed_objects(_contract())
        assert dict(table.tags) == IDENTITY


# ── column classification tags ──────────────────────────────────────────────
class TestColumnTags:
    def test_a_classified_column_carries_its_sensitivity(self):
        doc = _module(_contract())
        ref = "${databricks_sql_table.tower_tickets_main_ops_tickets.id}"
        assert _tags(doc, "columns") == {
            f"{ref}.engineer_email": {"fluid_sensitivity": "pii"},
            f"{ref}.tower_id": {"fluid_sensitivity": "internal"},
        }

    def test_the_column_name_is_as_unity_catalog_stores_it(self):
        contract = _contract()
        contract["exposes"][0]["contract"]["schema"][1]["name"] = "Engineer_Email"
        ref = "${databricks_sql_table.tower_tickets_main_ops_tickets.id}"
        assert f"{ref}.engineer_email" in _tags(_module(contract), "columns")

    def test_imported_by_the_providers_id(self):
        imports = {
            block.id: block
            for block in IAC_PLUGINS["databricks"].discover_imports(_contract())
            if block.to.startswith(TAG)
        }
        block = imports["columns,main.ops.tickets.engineer_email,fluid_sensitivity"]
        assert block.best_effort

    def test_an_unclassified_contract_makes_no_column_tag(self):
        contract = _contract()
        for column in contract["exposes"][0]["contract"]["schema"]:
            column.pop("sensitivity", None)
        assert _tags(_module(contract), "columns") == {}

    def test_more_column_tags_than_a_table_holds_is_refused(self):
        contract = _contract()
        contract["exposes"][0]["contract"]["schema"] = [
            {"name": f"c{i}", "type": "string", "sensitivity": "internal"} for i in range(1001)
        ]
        refusal = _refused(contract)
        assert refusal.kind == "databricks-tags-too-many"
        assert "1001" in str(refusal) and "1000" in str(refusal)

    def test_a_dbt_written_table_still_gets_its_column_tags(self):
        contract = _contract()
        contract["builds"] = [_DBT_BUILD]
        assert len(_tags(_module(contract), "columns")) == 2

    def test_managed_objects_carry_columns_comments_and_tags_for_verify(self):
        (table,), _ = dbx.managed_objects(_contract())
        assert table.comment == "One row per ticket, as the field team's system records it."
        assert table.column_comments == (
            ("ticket_id", "The ticket's id in the field system."),
            ("engineer_email", "Who worked the ticket."),
        )
        assert table.column_tags == (
            ("engineer_email", "fluid_sensitivity", "pii"),
            ("tower_id", "fluid_sensitivity", "internal"),
        )


# ── certification ───────────────────────────────────────────────────────────
CERTIFICATION = "system.certification_status"


class TestCertification:
    def _table_tags(self, contract: Dict[str, Any]) -> Dict[str, str]:
        (tags,) = _tags(_module(contract), "tables").values()
        return tags

    def test_a_deprecated_product_is_deprecated_in_unity_catalog(self):
        contract = _contract()
        contract["lifecycle"] = {"state": "deprecated"}
        assert self._table_tags(contract)[CERTIFICATION] == "deprecated"

    def test_the_exposes_own_state_wins(self):
        contract = _contract()
        contract["lifecycle"] = {"state": "deprecated"}
        contract["exposes"][0]["lifecycle"] = {"state": "active"}
        assert CERTIFICATION not in self._table_tags(contract)
        contract["lifecycle"] = {"state": "active"}
        contract["exposes"][0]["lifecycle"] = {"state": "deprecated"}
        assert self._table_tags(contract)[CERTIFICATION] == "deprecated"

    @pytest.mark.parametrize("state", ["preview", "active", "retired"])
    def test_no_other_state_sets_it(self, state):
        contract = _contract()
        contract["lifecycle"] = {"state": state}
        assert CERTIFICATION not in self._table_tags(contract)

    def test_imported_by_the_providers_id_and_named_by_a_plan_notice(self):
        contract = _contract()
        contract["lifecycle"] = {"state": "deprecated"}
        imports = {
            block.id
            for block in IAC_PLUGINS["databricks"].discover_imports(contract)
            if block.to.startswith(TAG)
        }
        assert f"tables,main.ops.tickets,{CERTIFICATION}" in imports
        notices = IAC_PLUGINS["databricks"].plan_notices(contract)
        assert any(CERTIFICATION in n and "ASSIGN" in n for n in notices)
        assert not any(
            CERTIFICATION in n for n in IAC_PLUGINS["databricks"].plan_notices(_contract())
        )

    def test_it_counts_toward_the_fifty_and_verify_checks_it(self):
        contract = _contract()
        contract["lifecycle"] = {"state": "deprecated"}
        (table,), _ = dbx.managed_objects(contract)
        assert dict(table.tags)[CERTIFICATION] == "deprecated"
        contract["labels"] = {f"k{i:03d}": "v" for i in range(50 - len(IDENTITY))}
        errors, _ = dbx.validate_databricks_binding(contract)
        assert len(errors) == 1 and "51" in errors[0]
