# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What a product made outside OpenTofu in a schema its contract stopped using, against a stand-in.

Pinned (``iac/providers/databricks_vacated.py`` through the plugin's ``prepare_destroys`` and
``unblock_destroys``, against ``_fake_databricks.py``): a table that moved from ``main.sales`` to
``main.other`` leaves its metric view, dropped before the apply, and its mask and row filter
functions, named before the apply and dropped once the table is gone, after which the schema can
be deleted; a dry run prints the statements and runs none; only this product's views and the
deploy identity's objects are dropped, never a function a table still uses, nor anything in a
schema the contract still names or that another environment of the product uses; a schema that
cannot be read, or a drop that fails, drops nothing more and does not ask for the destroy again.

Not proven here: the engine's resume of the destroy (``test_iac_apply_engine_destroys.py``) and
the whole apply through ``tofu`` (``test_iac_databricks_metrics_fake_e2e.py``).
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterator, List

import pytest

from fluid_build.iac import IAC_PLUGINS
from fluid_build.iac.providers import databricks_vacated as vacated_mod
from fluid_build.iac.providers.databricks_enforce import WAREHOUSE_ENV

from ._databricks_cli import make_contract
from ._fake_databricks import DEPLOY_USER, FakeDatabricks, _execute

pytestmark = [pytest.mark.unit, pytest.mark.databricks]

PLUGIN = IAC_PLUGINS["databricks"]
OLD = "main.sales.orders"
VIEW = "main.sales.orders_semantic_model"
LIVE = [("id", "bigint", False), ("amount", "double", True), ("region", "string", True)]
SEMANTICS: Dict[str, Any] = {
    "name": "orders_metrics",
    "dimensions": [{"name": "region", "type": "categorical", "expr": "region"}],
    "measures": [{"name": "revenue", "agg": "sum", "expr": "amount"}],
}
#: The moved contract's plan, as the engine reads it (``tofu show -json``): the old schema and its
#: table, destroyed outright.
SCHEMA_DELETE = {
    "address": "databricks_schema.dbx_fake_orders_main_sales",
    "type": "databricks_schema",
    "before": {"id": "main.sales", "catalog_name": "main", "name": "sales"},
}
DESTROYED = [
    SCHEMA_DELETE,
    {
        "address": "databricks_sql_table.dbx_fake_orders_main_sales_orders",
        "type": "databricks_sql_table",
        "before": {"id": OLD, "catalog_name": "main", "schema_name": "sales", "name": "orders"},
    },
]


def _contract(schema: str = "sales") -> Dict[str, Any]:
    """``orders`` with a metric view, ``region`` masked and a row filter for the analysts."""
    contract = make_contract()
    expose = contract["exposes"][0]
    expose["binding"]["location"]["schema"] = schema
    expose["semantics"] = copy.deepcopy(SEMANTICS)
    authz = expose["policy"]["authz"]
    authz["columnRestrictions"] = [
        {
            "principal": "group:analysts",
            "columns": ["region"],
            "access": "mask",
            "mask": "last_four",
        }
    ]
    authz["rowFilters"] = [{"principal": "group:analysts", "where": "region = 'eu'"}]
    return contract


@pytest.fixture
def fake(monkeypatch) -> Iterator[FakeDatabricks]:
    server = FakeDatabricks().start()
    monkeypatch.setenv(WAREHOUSE_ENV, server.warehouse_id)
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-secret-token-value")  # pragma: allowlist secret
    try:
        yield server
    finally:
        server.stop()


class Run:
    """The plugin's hooks against ``fake``, with what they print kept."""

    def __init__(self, fake: FakeDatabricks):
        self.fake = fake
        self.said: List[str] = []

    def deploy(self, doc: Dict[str, Any]) -> None:
        """What an apply of ``doc`` leaves: the table (OpenTofu's), then its masks, row filter
        and metric view (the hooks')."""
        location = doc["exposes"][0]["binding"]["location"]
        table = f"{location['database']}.{location['schema']}.{location['table']}"
        self.fake.create_table(table, LIVE)
        PLUGIN.post_apply(doc, say=self.said.append, client_factory=self.fake.client)

    def prepare(self, destroyed: List[Dict[str, Any]] = DESTROYED, **kw: Any) -> None:
        PLUGIN.prepare_destroys(
            _contract("other"),
            destroyed,
            say=self.said.append,
            client_factory=kw.pop("client_factory", self.fake.client),
            **kw,
        )

    def unblock(self, **kw: Any) -> bool:
        return PLUGIN.unblock_destroys(
            _contract("other"),
            [SCHEMA_DELETE],
            say=self.said.append,
            client_factory=kw.pop("client_factory", self.fake.client),
        )

    def drops(self, since: int) -> List[str]:
        return [s for s in self.fake.sql_log[since:] if s.startswith("DROP")]

    def destroy_table(self, full: str) -> None:
        """What ``tofu apply`` does to the old table before it comes to the schema."""
        ok, err = _execute(self.fake, "DROP TABLE " + ".".join(f"`{p}`" for p in full.split(".")))
        assert ok, err

    def delete_schema(self, full: str) -> str:
        """OpenTofu's destroy of the schema (the Schemas API); the error, or ``""``."""
        try:
            self.fake.client().api_client.do("DELETE", f"/api/2.1/unity-catalog/schemas/{full}")
        except Exception as exc:  # noqa: BLE001 — the stand-in's error is the answer
            return str(exc)
        return ""

    def held(self, schema: str) -> List[str]:
        objects = [*self.fake.tables, *self.fake.functions]
        return sorted(n for n in objects if n.rsplit(".", 1)[0] == schema)

    @property
    def text(self) -> str:
        return "\n".join(self.said)


def _metric_view(fake: FakeDatabricks, full: str, product: str, source: str = OLD) -> None:
    ok, err = _execute(
        fake,
        f"CREATE VIEW {full} WITH METRICS LANGUAGE YAML TBLPROPERTIES ('fluid.product_id' = "
        f"'{product}') AS $$version: 1.1\nsource: {source}\nmeasures:\n- name: n\n  expr: "
        "COUNT(1)\n$$",
    )
    assert ok, err


def test_the_view_goes_before_the_apply_and_the_functions_once_their_table_is_gone(fake):
    run = Run(fake)
    run.deploy(_contract())
    functions = [n for n in run.held("main.sales") if n.split(".")[2].startswith("fluid_")]
    assert len(functions) == 2 and VIEW in fake.tables

    before = len(fake.sql_log)
    run.prepare()
    assert run.drops(before) == ["DROP VIEW IF EXISTS `main`.`sales`.`orders_semantic_model`"]
    assert VIEW not in fake.tables and ("table", VIEW) not in fake.grants
    for name in functions:  # still the old table's masks and row filter
        assert f"after tofu destroys {OLD}: drop function {name}" in run.text
        assert name in fake.functions

    # tofu apply destroys the table, then is refused the schema: the functions are still in it.
    run.destroy_table(OLD)
    assert "Schema is not empty" in run.delete_schema("main.sales")
    before = len(fake.sql_log)
    assert run.unblock() is True
    assert sorted(run.drops(before)) == sorted(
        "DROP FUNCTION IF EXISTS " + ".".join(f"`{p}`" for p in name.split("."))
        for name in functions
    )
    assert f"done: drop function {functions[0]}: no table uses it" in run.text
    assert run.held("main.sales") == []
    assert run.delete_schema("main.sales") == "" and "main.sales" not in fake.schemas


def test_a_dry_run_prints_the_drops_and_runs_none(fake):
    run = Run(fake)
    run.deploy(_contract())
    before, run.said[:] = len(fake.sql_log), []
    run.prepare(dry_run=True)
    assert fake.sql_log[before:] == []
    assert VIEW in fake.tables
    assert run.said[1:3] == [
        "  main.sales",
        f"    before apply: drop metric view {VIEW}: it reads {OLD}, which this apply destroys",
    ]
    assert "Schemas this apply destroys: what this product made there outside OpenTofu" in run.text
    assert "      DROP VIEW IF EXISTS `main`.`sales`.`orders_semantic_model`" in run.text
    assert f"    after tofu destroys {OLD}: drop function main.sales.fluid_rf_orders_" in run.text
    assert "      DROP FUNCTION IF EXISTS `main`.`sales`.`fluid_mask_orders_region_" in run.text
    assert run.said[-1] == "    dry-run: none of these statements ran."


def test_only_this_products_and_this_identitys_objects_go_and_never_a_bound_function(fake):
    run = Run(fake)
    run.deploy(_contract())
    ours = set(run.held("main.sales")) & set(fake.functions)  # the old table's mask and filter
    # Another environment's table in the same schema, its row filter bound, and its view.
    fake.create_table("main.sales.orders_stg", LIVE)
    kept_filter = "main.sales.fluid_rf_orders_stg_12345678"
    fake.add_function(kept_filter, [("region", "string")], "BOOLEAN", "true", owner=DEPLOY_USER)
    ok, err = _execute(
        fake,
        "ALTER TABLE `main`.`sales`.`orders_stg` SET ROW FILTER "
        "`main`.`sales`.`fluid_rf_orders_stg_12345678` ON (`region`)",
    )
    assert ok, err
    _metric_view(
        fake, "main.sales.orders_stg_semantic_model", "dbx.fake.orders", "main.sales.orders_stg"
    )
    _metric_view(fake, "main.sales.theirs_semantic_model", "other.product")
    _metric_view(fake, "main.sales.handed_semantic_model", "dbx.fake.orders")
    fake.tables["main.sales.handed_semantic_model"]["owner"] = "someone@example.test"
    fake.add_function(
        "main.sales.normalize", [("x", "string")], "STRING", "upper(x)", owner=DEPLOY_USER
    )
    planted = "main.sales.fluid_mask_orders_amount_0badc0de"
    fake.add_function(planted, [("val", "double")], "DOUBLE", "val")  # attacker-owned
    orphan = "main.sales.fluid_rf_orders_00c0ffee"
    fake.add_function(orphan, [("region", "string")], "BOOLEAN", "true", owner=DEPLOY_USER)

    before = len(fake.sql_log)
    run.prepare()
    assert sorted(run.drops(before)) == [
        "DROP FUNCTION IF EXISTS `main`.`sales`.`fluid_rf_orders_00c0ffee`",
        "DROP VIEW IF EXISTS `main`.`sales`.`orders_semantic_model`",
    ]
    note = next(line for line in run.said if "OpenTofu cannot destroy main.sales" in line)
    for left in (
        "main.sales.orders_stg (a managed table this apply keeps)",
        "main.sales.orders_stg_semantic_model (a metric view over main.sales.orders_stg, which "
        "this apply keeps)",
        "main.sales.theirs_semantic_model (a metric view of product 'other.product')",
        "main.sales.handed_semantic_model (owned by someone@example.test, not the deploy "
        "identity)",
        "main.sales.normalize (a function this module did not create)",
        f"{planted} (owned by attacker@example.test, not the deploy identity)",
        f"{kept_filter} (the row filter or a mask of main.sales.orders_stg)",
    ):
        assert left in note

    run.destroy_table(OLD)
    before = len(fake.sql_log)
    assert run.unblock() is False  # the schema still holds what is not this run's to drop
    assert len(ours) == 2 and len(run.drops(before)) == 2 and not ours & set(fake.functions)
    assert {kept_filter, planted, "main.sales.normalize"} <= set(fake.functions)
    assert fake.tables["main.sales.orders_stg"]["row_filter"]["function_name"] == kept_filter
    for view in ("orders_stg", "theirs", "handed"):
        assert f"main.sales.{view}_semantic_model" in fake.tables


def test_a_schema_the_contract_still_names_is_not_looked_in(fake):
    run = Run(fake)
    run.deploy(_contract())
    before = len(fake.queries)
    PLUGIN.prepare_destroys(_contract(), DESTROYED, say=run.said.append, client_factory=fake.client)
    assert fake.queries[before:] == []
    assert VIEW in fake.tables


def test_another_environment_of_the_product_keeps_its_view_and_functions(fake):
    """prod and staging of one product: an overlay that changes only the schema, one catalog and
    one deploy identity. Prod's plan destroys prod's old schema, never staging's."""
    run = Run(fake)
    run.deploy(_contract())
    run.deploy(_contract("sales_stg"))
    staging = run.held("main.sales_stg")
    assert "main.sales_stg.orders_semantic_model" in staging and len(staging) == 4

    run.prepare()
    run.destroy_table(OLD)
    assert run.unblock() is True
    assert run.held("main.sales") == []
    assert run.held("main.sales_stg") == staging


def test_a_schema_that_cannot_be_read_drops_nothing(fake):
    run = Run(fake)
    run.deploy(_contract())
    fake.fail_route("GET", "/unity-catalog/functions", status=503, message="unavailable")
    before = len(fake.sql_log)
    run.prepare()
    assert run.drops(before) == [] and VIEW in fake.tables
    assert "not checked: main.sales could not be read" in run.text
    run.destroy_table(OLD)
    assert run.unblock() is False


def test_a_drop_that_fails_is_named_and_the_destroy_is_not_asked_for_again(fake):
    run = Run(fake)
    run.deploy(_contract())
    run.prepare()
    run.destroy_table(OLD)
    fake.fail_on("DROP FUNCTION", "[INTERNAL_ERROR] injected failure")
    assert run.unblock() is False
    assert "FAILED: drop function main.sales.fluid_" in run.text


def test_without_a_client_nothing_is_checked_and_it_says_so(fake):
    def broken() -> Any:
        raise RuntimeError("no credential")

    run = Run(fake)
    run.prepare(client_factory=broken)
    assert "main.sales: what this product made outside OpenTofu there" in run.text
    assert "was not checked: no Databricks client" in run.text
    assert run.unblock(client_factory=broken) is False


def test_vacated_takes_the_schemas_the_plan_destroys_that_the_contract_does_not_name():
    destroyed = [
        *DESTROYED,
        {
            "address": "databricks_schema.k",
            "type": "databricks_schema",
            "before": {"id": "main.kept"},
        },
        {
            "address": "databricks_sql_table.k",
            "type": "databricks_sql_table",
            "before": {"catalog_name": "main", "schema_name": "kept", "name": "t"},
        },
        {"address": "databricks_grant.g", "type": "databricks_grant", "before": {"id": "main.x"}},
        {"address": "databricks_schema.odd", "type": "databricks_schema", "before": None},
    ]
    found = vacated_mod.vacated(destroyed, {"main.kept"}, "dbx.fake.orders")
    assert found.schemas == ("main.sales",)
    assert found.tables == frozenset({OLD})
    assert found.product_id == "dbx.fake.orders"
    # Without catalog_name and name, the id names the schema.
    only_id = [{"type": "databricks_schema", "before": {"id": "main.legacy"}}]
    assert vacated_mod.vacated(only_id, (), "p").schemas == ("main.legacy",)
