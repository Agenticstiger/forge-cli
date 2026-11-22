# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Adopting an existing Glue table: the import candidates and the match decision.

The plan changes below have the shape ``tofu show -json`` gives an imported
``aws_glue_catalog_table`` (OpenTofu 1.13, hashicorp/aws 6.73, read from moto): the
table as the provider read it in ``before``, the contract's table in ``after``.
``tests/iac/test_iac_aws_table_adoption_moto.py`` runs the same decisions on a real
plan.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Sequence

import pytest

from fluid_build.iac import get_iac_plugin
from fluid_build.iac.adoption import TableReview, planned_imports, review_tables
from fluid_build.iac.importer import ImportBlock, import_section
from fluid_build.iac.providers.aws_adoption import review_glue_table

pytestmark = pytest.mark.unit

ADDRESS = "aws_glue_catalog_table.sales_orders_sales_orders"
LOCATION = "s3://lake/silver/orders"


def _column(
    name: str,
    type_: str,
    *,
    comment: str = "",
    field_id: Optional[int] = None,
    current: Optional[bool] = None,
) -> Dict[str, Any]:
    """A column as the provider reads it; ``field_id`` gives it Iceberg's parameters."""
    parameters: Dict[str, str] = {}
    if field_id is not None:
        parameters = {
            "iceberg.field.id": str(field_id),
            "iceberg.field.optional": "true",
            "iceberg.field.current": "false" if current is False else "true",
        }
    return {"comment": comment, "name": name, "parameters": parameters, "type": type_}


def _table(
    columns: Sequence[Dict[str, Any]],
    *,
    location: str = LOCATION,
    input_format: str = "",
    output_format: str = "",
    serde: Optional[Dict[str, Any]] = None,
    storage_parameters: Optional[Dict[str, str]] = None,
    parameters: Optional[Dict[str, str]] = None,
    owner: Optional[str] = None,
    description: str = "",
    partition_keys: Sequence[Dict[str, str]] = (),
    **extra: Any,
) -> Dict[str, Any]:
    """An ``aws_glue_catalog_table`` state as ``tofu show -json`` prints it."""
    state: Dict[str, Any] = {
        "arn": "arn:aws:glue:us-east-1::table/sales/orders",
        "catalog_id": "123456789012",
        "database_name": "sales",
        "description": description,
        "id": "123456789012:sales:orders",
        "name": "orders",
        "open_table_format_input": [],
        "owner": owner,
        "parameters": dict(parameters or {}),
        "partition_index": [],
        "partition_keys": [dict(key, comment=key.get("comment", "")) for key in partition_keys],
        "region": "us-east-1",
        "retention": 0,
        "storage_descriptor": [
            {
                "additional_locations": [],
                "bucket_columns": [],
                "columns": [dict(column) for column in columns],
                "compressed": False,
                "input_format": input_format,
                "location": location,
                "number_of_buckets": 0,
                "output_format": output_format,
                "parameters": dict(storage_parameters or {}),
                "schema_reference": [],
                "ser_de_info": [serde] if serde else [],
                "skewed_info": [],
                "sort_columns": [],
                "stored_as_sub_directories": False,
            }
        ],
        "table_type": "EXTERNAL_TABLE",
        "target_table": [],
        "view_definition": [],
        "view_expanded_text": "",
        "view_original_text": "",
    }
    state.update(extra)
    return state


def _change(
    before: Dict[str, Any],
    after: Dict[str, Any],
    actions: Sequence[str] = ("update",),
    action_reason: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """A resource change; ``action_reason`` sits beside ``change``, as OpenTofu prints it."""
    resource: Dict[str, Any] = {
        "address": ADDRESS,
        "mode": "managed",
        "type": "aws_glue_catalog_table",
        "name": "sales_orders_sales_orders",
        "change": {
            "actions": list(actions),
            "before": before,
            "after": after,
            "after_unknown": {},
            "importing": {"id": "123456789012:sales:orders"},
            **extra,
        },
    }
    if action_reason:
        resource["action_reason"] = action_reason
    return resource


#: forge's own table body in main.tf.json (``aws._emit_glue``), the parts the review reads.
ICEBERG = {
    "name": "orders",
    "open_table_format_input": {"iceberg_input": {"metadata_operation": "CREATE", "version": "2"}},
    "parameters": {"classification": "iceberg", "managed_by": "fluid"},
    "storage_descriptor": {
        "columns": [{"name": "id", "type": "bigint"}, {"name": "amount", "type": "decimal(12,2)"}],
        "location": LOCATION,
    },
}
PARQUET = {
    "name": "orders",
    "parameters": {"classification": "parquet", "managed_by": "fluid"},
    "storage_descriptor": {
        "columns": [{"name": "id", "type": "bigint"}, {"name": "amount", "type": "decimal(12,2)"}],
        "location": LOCATION + "/",
    },
}
HIVE_PARQUET = {
    "input_format": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
    "output_format": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
}
PARQUET_SERDE = {
    "name": "",
    "parameters": {"serialization.format": "1"},
    "serialization_library": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
}

#: An Iceberg table as Iceberg's Glue catalog writes it: every column carries its field id.
LIVE_ICEBERG = _table(
    [_column("id", "bigint", field_id=1), _column("amount", "decimal(12,2)", field_id=2)],
    parameters={"previous_metadata_location": LOCATION + "/metadata/00000-a.metadata.json"},
)
#: What the plan makes of it under a matching contract: the provider keeps each column's
#: parameters (Optional + Computed), the contract decides the rest.
PLANNED_ICEBERG = copy.deepcopy(LIVE_ICEBERG)


def _review(change: Dict[str, Any], declared: Dict[str, Any] = ICEBERG) -> TableReview:
    differences, updates = review_glue_table(change["change"], declared)
    return TableReview(ADDRESS, "sales.orders", tuple(differences), tuple(updates))


def _with_columns(state: Dict[str, Any], columns: List[Dict[str, Any]]) -> Dict[str, Any]:
    state = copy.deepcopy(state)
    state["storage_descriptor"][0]["columns"] = columns
    return state


# ── a table that matches ───────────────────────────────────────────────────


def test_a_table_that_matches_the_contract_is_adopted_with_nothing_to_change():
    review = _review(_change(LIVE_ICEBERG, PLANNED_ICEBERG, actions=("no-op",)))

    assert review.matches and review.differences == () and review.updates == ()


def test_what_the_apply_still_changes_in_place_is_listed_and_does_not_refuse():
    """Description, comments, an owner, Iceberg's columns of older schemas: Glue metadata
    the contract cannot declare or that Iceberg writes back, none of it data."""
    before = _table(
        [
            _column("id", "bigint", comment="order id", field_id=1),
            _column("amount", "decimal(12, 2)", field_id=2),
            _column("legacy", "string", field_id=3, current=False),
        ],
        owner="hadoop",
        description="written by the stream",
        location=LOCATION + "/",
    )
    after = _table(
        [
            _column("id", "bigint", comment="the order", field_id=1),
            _column("amount", "decimal(12,2)", field_id=2),
        ],
        description="Orders",
        location=LOCATION,
    )

    review = _review(_change(before, after))

    assert review.matches, review.differences
    updates = " | ".join(review.updates)
    assert "description" in updates
    assert "comment of column id" in updates
    assert "owner hadoop" in updates
    assert "legacy" in updates and "older Iceberg schemas" in updates
    assert "amount" in updates and "decimal(12,2)" in updates
    assert "trailing slash" in updates


# ── columns ─────────────────────────────────────────────────────────────────


def test_a_column_the_table_lacks_is_named():
    after = _with_columns(
        LIVE_ICEBERG,
        [_column("id", "bigint"), _column("amount", "decimal(12,2)"), _column("note", "string")],
    )

    review = _review(_change(LIVE_ICEBERG, after))

    assert review.differences == ("column note (string): in the contract, not in the table",)


def test_a_column_the_contract_lacks_is_named():
    after = _with_columns(LIVE_ICEBERG, [_column("id", "bigint")])

    review = _review(_change(LIVE_ICEBERG, after))

    assert review.differences == (
        "column amount (decimal(12,2)): in the table, not in the contract",
    )


def test_a_column_of_another_type_is_named_with_both_types():
    after = _with_columns(LIVE_ICEBERG, [_column("id", "bigint"), _column("amount", "double")])

    review = _review(_change(LIVE_ICEBERG, after))

    assert review.differences == (
        "column amount: decimal(12,2) in the table, double in the contract",
    )


def test_the_same_columns_in_another_order_are_named():
    after = _with_columns(
        LIVE_ICEBERG, [_column("amount", "decimal(12,2)"), _column("id", "bigint")]
    )

    review = _review(_change(LIVE_ICEBERG, after))

    assert review.differences == (
        "column order: the table has (id, amount), the contract (amount, id)",
    )


def test_a_column_named_in_another_case_is_an_update_not_a_difference():
    """Glue, Hive and Athena resolve a column's name whatever its case."""
    after = _with_columns(
        LIVE_ICEBERG, [_column("ID", "bigint"), _column("amount", "decimal(12,2)")]
    )

    review = _review(_change(LIVE_ICEBERG, after))

    assert review.matches, review.differences
    assert review.updates == ("writes the name of column id as ID",)


@pytest.mark.parametrize("live_type", ["integer", "INT", "Int"])
def test_a_type_the_data_loss_gate_reads_as_changed_is_a_difference(live_type):
    """Spacing aside, a spelling the gate counts as a type change (``integer`` for ``int``,
    another case) is refused here, never reported as a match the gate then holds."""
    from fluid_build.iac.providers.aws import glue_column_type_changes

    before = _with_columns(LIVE_ICEBERG, [_column("id", live_type), _column("amount", "double")])
    after = _with_columns(LIVE_ICEBERG, [_column("id", "int"), _column("amount", "double")])
    change = _change(before, after)

    review = _review(change)

    assert review.differences == (f"column id: {live_type} in the table, int in the contract",)
    assert glue_column_type_changes({"resource_changes": [change]}), "the gate agrees"


def test_a_column_of_an_older_iceberg_schema_is_not_compared():
    before = _with_columns(
        LIVE_ICEBERG,
        [
            _column("id", "bigint", field_id=1),
            _column("amount", "decimal(12,2)", field_id=2),
            _column("note", "int", field_id=3, current=False),
        ],
    )

    review = _review(_change(before, PLANNED_ICEBERG))

    assert review.matches, review.differences


def test_a_contract_that_declares_no_columns_cannot_be_compared():
    declared = copy.deepcopy(ICEBERG)
    declared["storage_descriptor"]["columns"] = []

    review = _review(_change(LIVE_ICEBERG, PLANNED_ICEBERG, actions=("no-op",)), declared)

    assert len(review.differences) == 1
    assert "declares no columns" in review.differences[0]


# ── properties ──────────────────────────────────────────────────────────────


def test_another_location_is_named():
    after = copy.deepcopy(PLANNED_ICEBERG)
    after["storage_descriptor"][0]["location"] = "s3://lake/gold/orders"

    review = _review(_change(LIVE_ICEBERG, after))

    assert review.differences == (
        f"location: {LOCATION} in the table, s3://lake/gold/orders in the contract",
    )


def test_an_s3a_location_is_the_same_location():
    before = copy.deepcopy(LIVE_ICEBERG)
    before["storage_descriptor"][0]["location"] = "s3a://lake/silver/orders"

    review = _review(_change(before, PLANNED_ICEBERG))

    assert review.matches, review.differences


def test_a_hive_table_under_an_iceberg_binding_is_refused():
    before = _table(
        [_column("id", "bigint"), _column("amount", "decimal(12,2)")],
        serde=PARQUET_SERDE,
        parameters={"classification": "parquet", "EXTERNAL": "TRUE"},
    )
    before["storage_descriptor"][0].update(HIVE_PARQUET)
    after = copy.deepcopy(before)
    after["storage_descriptor"][0].update(input_format=None, output_format=None)

    review = _review(_change(before, after))

    text = " | ".join(review.differences)
    assert "input format: org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat" in text
    assert "output format" in text


def test_an_iceberg_table_under_a_parquet_binding_is_refused():
    after = copy.deepcopy(LIVE_ICEBERG)
    after["storage_descriptor"][0].update(HIVE_PARQUET, ser_de_info=[PARQUET_SERDE])
    after["storage_descriptor"][0]["location"] = LOCATION + "/"

    review = _review(_change(LIVE_ICEBERG, after), PARQUET)

    text = " | ".join(review.differences)
    assert "the table is an Iceberg table" in text and "format parquet" in text
    assert "input format: none in the table" in text
    assert "SerDe" in text


def test_an_iceberg_table_under_a_binding_without_storage_formats_is_refused():
    declared = copy.deepcopy(PARQUET)
    declared["parameters"]["classification"] = "csv"

    review = _review(_change(LIVE_ICEBERG, PLANNED_ICEBERG), declared)

    assert review.differences == (
        "format: the table is an Iceberg table (its columns carry Iceberg field ids), "
        "but the binding declares format csv",
    )


def test_a_table_type_parameter_glue_shows_is_not_an_iceberg_one():
    """The provider hides ``table_type`` only when it is ICEBERG, so one it shows is not."""
    before = _table(
        [_column("id", "bigint"), _column("amount", "decimal(12,2)")],
        parameters={"table_type": "DELTA"},
    )

    review = _review(_change(before, copy.deepcopy(before)))

    assert review.differences == (
        "format: the table's table_type is DELTA, so it is not an Iceberg table, "
        "but the binding declares format iceberg",
    )


def test_serde_and_storage_parameters_the_apply_would_remove_are_named():
    before = _table(
        [_column("id", "bigint"), _column("amount", "decimal(12,2)")],
        serde={**PARQUET_SERDE, "parameters": {"serialization.format": "1", "path": "s3://x"}},
        storage_parameters={"skip.header.line.count": "1"},
    )
    before["storage_descriptor"][0].update(HIVE_PARQUET)
    after = copy.deepcopy(before)
    after["storage_descriptor"][0]["ser_de_info"] = [PARQUET_SERDE]
    after["storage_descriptor"][0]["parameters"] = {}
    after["storage_descriptor"][0]["location"] = LOCATION + "/"
    before["storage_descriptor"][0]["location"] = LOCATION + "/"

    review = _review(_change(before, after), PARQUET)

    assert set(review.differences) == {
        "SerDe parameter path: s3://x in the table, not in the contract",
        "storage parameter skip.header.line.count: 1 in the table, not in the contract",
    }


def test_a_serde_parameter_only_the_contract_sets_is_an_update():
    before = _table(
        [_column("id", "bigint"), _column("amount", "decimal(12,2)")],
        serde={**PARQUET_SERDE, "parameters": {}},
        location=LOCATION + "/",
    )
    before["storage_descriptor"][0].update(HIVE_PARQUET)
    after = copy.deepcopy(before)
    after["storage_descriptor"][0]["ser_de_info"] = [PARQUET_SERDE]

    review = _review(_change(before, after), PARQUET)

    assert review.matches, review.differences
    assert review.updates == ("sets SerDe parameter serialization.format to 1",)


def test_partition_keys_that_differ_are_named():
    before = copy.deepcopy(LIVE_ICEBERG)
    before["partition_keys"] = [{"comment": "", "name": "day", "type": "date"}]

    review = _review(_change(before, PLANNED_ICEBERG))

    assert review.differences == ("partition keys: (day date) in the table, none in the contract",)


def test_another_table_type_is_named():
    before = copy.deepcopy(LIVE_ICEBERG)
    before["table_type"] = "VIRTUAL_VIEW"

    review = _review(_change(before, PLANNED_ICEBERG))

    assert review.differences == (
        "table type: VIRTUAL_VIEW in the table, EXTERNAL_TABLE in the contract",
    )


def test_a_storage_setting_the_apply_would_change_is_named():
    before = copy.deepcopy(LIVE_ICEBERG)
    before["storage_descriptor"][0]["additional_locations"] = [LOCATION + "/data"]

    review = _review(_change(before, PLANNED_ICEBERG))

    assert review.differences == (
        f"storage setting additional_locations: {LOCATION}/data in the table, "
        "none in the contract",
    )


def test_an_attribute_no_rule_knows_is_refused_rather_than_let_through():
    after = copy.deepcopy(PLANNED_ICEBERG)
    after["view_definition"] = [{"is_protected": True}]

    review = _review(_change(LIVE_ICEBERG, after))

    assert len(review.differences) == 1
    assert review.differences[0].startswith("view_definition: ")


def test_an_attribute_known_only_after_the_apply_is_not_a_difference():
    after = copy.deepcopy(PLANNED_ICEBERG)
    after["arn"] = None
    after["description"] = None
    change = _change(LIVE_ICEBERG, after)
    change["change"]["after_unknown"] = {"arn": True, "description": True}

    review = _review(change)

    assert review.matches and review.updates == ()


def test_text_from_the_catalog_is_shown_printable():
    after = _with_columns(LIVE_ICEBERG, [_column("id", "bigint")])
    before = _with_columns(
        LIVE_ICEBERG, [_column("id", "bigint"), _column("evil\x1b[2Jname\n", "string")]
    )

    review = _review(_change(before, after))

    assert review.differences == (
        "column evil?[2Jname? (string): in the table, not in the contract",
    )


# ── the plan, read for its imports ──────────────────────────────────────────


def test_a_replace_is_refused_whatever_the_attributes_say():
    change = _change(
        LIVE_ICEBERG,
        PLANNED_ICEBERG,
        actions=("delete", "create"),
        action_reason="replace_by_triggers",
    )
    shown = {"resource_changes": [change]}

    (review,) = review_tables(
        shown, {ADDRESS: "sales.orders"}, {ADDRESS: ICEBERG}, review_glue_table
    )

    assert review.differences == (
        "OpenTofu would replace it (a replace_triggered_by trigger fires), which drops the "
        "table and its Lake Formation permissions",
    )


def test_a_replace_names_the_attributes_that_cannot_change_in_place():
    change = _change(
        LIVE_ICEBERG,
        PLANNED_ICEBERG,
        actions=("create", "delete"),
        action_reason="replace_because_cannot_update",
        replace_paths=[["open_table_format_input", 0, "iceberg_input"]],
    )
    shown = {"resource_changes": [change]}

    (review,) = review_tables(
        shown, {ADDRESS: "sales.orders"}, {ADDRESS: ICEBERG}, review_glue_table
    )

    assert review.differences == (
        "OpenTofu would replace it (open_table_format_input.0.iceberg_input cannot change "
        "in place), which drops the table and its Lake Formation permissions",
    )


def test_a_table_the_plan_does_not_import_is_a_difference():
    (review,) = review_tables(
        {"resource_changes": []}, {ADDRESS: "sales.orders"}, {ADDRESS: ICEBERG}, review_glue_table
    )

    assert review.differences == ("the plan does not import it",)


def test_planned_imports_are_the_managed_changes_with_an_import():
    data = {"address": "data.aws_caller_identity.c", "mode": "data", "change": {"importing": {}}}
    plain = _change(LIVE_ICEBERG, PLANNED_ICEBERG)
    plain = {**plain, "address": "aws_glue_catalog_table.other"}
    plain["change"] = {k: v for k, v in plain["change"].items() if k != "importing"}
    imported = _change(LIVE_ICEBERG, PLANNED_ICEBERG)

    found = planned_imports({"resource_changes": [data, plain, imported]})

    assert list(found) == [ADDRESS]


# ── the import candidates ───────────────────────────────────────────────────


def _contract(*bindings: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": "sales.orders",
        "exposes": [
            {
                "exposeId": f"e{index}",
                "binding": binding,
                "contract": {"schema": [{"name": "id", "type": "bigint"}]},
            }
            for index, binding in enumerate(bindings)
        ],
    }


ICEBERG_BINDING = {
    "platform": "aws",
    "format": "iceberg",
    "location": {"database": "sales", "table": "orders", "bucket": "lake", "path": "silver/orders"},
}
PARQUET_BINDING = {
    "platform": "aws",
    "format": "parquet",
    "location": {"database": "sales", "table": "refunds", "bucket": "lake", "path": "refunds/"},
}


def test_each_glue_table_candidate_names_its_table_and_nothing_else_does(monkeypatch):
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")

    blocks = get_iac_plugin("aws").discover_imports(_contract(ICEBERG_BINDING, PARQUET_BINDING))

    tables = {block.to: block.table for block in blocks if block.table}
    assert tables == {
        "aws_glue_catalog_table.sales_orders_sales_orders": "sales.orders",
        "aws_glue_catalog_table.sales_orders_sales_refunds": "sales.refunds",
    }
    assert all(block.table is None for block in blocks if block.to not in tables)
    assert {block.to: block.id for block in blocks if block.table} == {
        "aws_glue_catalog_table.sales_orders_sales_orders": "123456789012:sales:orders",
        "aws_glue_catalog_table.sales_orders_sales_refunds": "123456789012:sales:refunds",
    }


def test_a_binding_without_a_format_is_looked_for_as_the_emitter_declares_it(monkeypatch):
    """The emitter declares a Glue table for it (parquet by default), so the look-up and
    ``adoptable_tables`` name it the same way."""
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    binding = {k: v for k, v in PARQUET_BINDING.items() if k != "format"}
    contract = _contract(binding)
    plugin = get_iac_plugin("aws")

    offered = {block.to: block.table for block in plugin.discover_imports(contract) if block.table}

    assert (
        offered
        == plugin.adoptable_tables(contract)
        == {"aws_glue_catalog_table.sales_orders_sales_refunds": "sales.refunds"}
    )


def test_adoptable_tables_are_every_glue_table_the_contract_names_account_or_not(monkeypatch):
    monkeypatch.delenv("AWS_ACCOUNT_ID", raising=False)
    lakekeeper = {
        **ICEBERG_BINDING,
        "location": {**ICEBERG_BINDING["location"], "table": "events", "catalog": "lakekeeper"},
    }

    named = get_iac_plugin("aws").adoptable_tables(
        _contract(ICEBERG_BINDING, PARQUET_BINDING, lakekeeper)
    )

    assert named == {
        "aws_glue_catalog_table.sales_orders_sales_orders": "sales.orders",
        "aws_glue_catalog_table.sales_orders_sales_refunds": "sales.refunds",
    }


def test_an_iceberg_table_is_held_still_on_adoption_and_a_hive_table_is_not():
    plugin = get_iac_plugin("aws")

    assert plugin.adoption_hold(ICEBERG) == (
        "description",
        "owner",
        "retention",
        "storage_descriptor",
    )
    assert plugin.adoption_hold(PARQUET) == ()


def test_the_rendered_import_block_carries_only_its_address_and_id():
    block = ImportBlock(to=ADDRESS, id="123456789012:sales:orders", table="sales.orders")

    assert import_section([block]) == {
        "import": [{"to": ADDRESS, "id": "123456789012:sales:orders"}]
    }


@pytest.mark.parametrize(
    "parameters, made",
    [
        ({"managed_by": "fluid", "fluid_product_id": "sales.orders"}, True),
        ({"managed_by": " Fluid ", "fluid_product_id": "sales.orders"}, True),
        ({"managed_by": "fluid", "classification": "iceberg"}, False),
        ({"managed_by": " Fluid "}, False),
        ({"managed_by": "fluid", "fluid_product_id": "billing.invoices"}, False),
        ({"managed_by": "fluid", "fluid_product_id": "Sales.Orders"}, False),
        ({"managed_by": "fluid", "fluid_product_id": ""}, False),
        ({"managed_by": "airflow", "fluid_product_id": "sales.orders"}, False),
        ({"fluid_product_id": "sales.orders"}, False),
        ({"classification": "parquet"}, False),
        (None, False),
    ],
)
def test_a_table_is_forge_made_only_with_forge_s_marker_and_this_product_s_id(parameters, made):
    """The marker alone is not enough: a table with no product id may be any product's."""
    values = {} if parameters is None else {"parameters": parameters}

    assert get_iac_plugin("aws").table_made_by_forge(values, {"id": "sales.orders"}) is made


@pytest.mark.parametrize(
    "recorded, made",
    [
        ("fluidVersion: 0.7.7\nid: sales.orders\nname: Orders\n", True),
        ("id: 'sales.orders'\nexposes:\n- id: other\n", True),
        ("fluidVersion: 0.7.7\nid: billing.invoices\n", False),
        ("description: |\n  id: sales.orders\nid: billing.invoices\n", False),
        ("fluidVersion: 0.7.7\n", False),
        ('{"id": "sales.orders", "name": "Orders"}', False),
        ("id:\nname: Orders\n", False),
        ("id: ''\nname: Orders\n", False),
        ("metadata:\n  id: sales.orders\n", False),
        ("", False),
    ],
)
def test_a_table_made_before_the_product_id_belongs_to_the_product_its_record_names(recorded, made):
    """A table forge made before it set ``fluid_product_id`` is matched by the id of the
    contract it records. A record that names another id, or none it can read (JSON, an
    empty or indented id, no id line at all), is not this product's."""
    values = {"parameters": {"managed_by": "fluid", "fluid_contract": recorded}}

    assert get_iac_plugin("aws").table_made_by_forge(values, {"id": "sales.orders"}) is made


@pytest.mark.parametrize(
    "product_id, recorded, made",
    [
        ("sales.orders", "id: billing.invoices\n", True),
        ("billing.invoices", "id: sales.orders\n", False),
        ("", "id: sales.orders\n", False),
    ],
)
def test_the_product_id_parameter_decides_before_the_recorded_contract(product_id, recorded, made):
    """The record is read only for a table without ``fluid_product_id``."""
    parameters = {
        "managed_by": "fluid",
        "fluid_product_id": product_id,
        "fluid_contract": recorded,
    }

    made_by_forge = get_iac_plugin("aws").table_made_by_forge(
        {"parameters": parameters}, {"id": "sales.orders"}
    )

    assert made_by_forge is made


@pytest.mark.parametrize(
    "parameters, unnamed",
    [
        ({"managed_by": "fluid"}, True),
        ({"managed_by": "fluid", "fluid_contract": "fluidVersion: 0.7.7\n"}, True),
        ({"managed_by": "fluid", "fluid_contract": '{"id": "sales.orders"}'}, True),
        ({"managed_by": "fluid", "fluid_product_id": ""}, True),
        ({"managed_by": "fluid", "fluid_product_id": "billing.invoices"}, False),
        ({"managed_by": "fluid", "fluid_contract": "id: billing.invoices\n"}, False),
        ({"managed_by": "fluid", "fluid_product_id": "sales.orders"}, False),
        ({"classification": "iceberg"}, False),
        (None, False),
    ],
)
def test_a_table_names_no_product_only_with_forge_s_marker_and_no_readable_id(parameters, unnamed):
    """Only such a table gets the remedy of recording the id: one that names another
    product is that product's, and one without the marker is not forge's."""
    values = {} if parameters is None else {"parameters": parameters}

    assert get_iac_plugin("aws").table_names_no_product(values) is unnamed


def test_a_contract_without_an_id_makes_no_table_its_own():
    parameters = {"managed_by": "fluid", "fluid_product_id": "", "fluid_contract": "name: x\n"}

    assert get_iac_plugin("aws").table_made_by_forge({"parameters": parameters}, {}) is False


@pytest.mark.parametrize("notes", ["", "n" * 60_000])
def test_every_glue_table_forge_makes_names_its_product_whatever_the_contract_s_size(notes):
    """The record of the contract is dropped over 50 KB; the product id never is."""
    contract = _contract(ICEBERG_BINDING, PARQUET_BINDING)
    if notes:
        contract["metadata"] = {"notes": notes}

    tables = get_iac_plugin("aws").emit(contract)["aws_glue_catalog_table"]

    assert {key: body["parameters"]["fluid_product_id"] for key, body in tables.items()} == {
        "sales_orders_sales_orders": "sales.orders",
        "sales_orders_sales_refunds": "sales.orders",
    }
    recorded = ["fluid_contract" in body["parameters"] for body in tables.values()]
    assert recorded == [not notes, not notes]
