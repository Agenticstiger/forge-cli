# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Unity Catalog metric views through the real ``fluid`` CLI, ``tofu`` and provider, against a stand-in.

Pinned (Stage 1: real ``tofu`` and ``databricks/databricks`` 1.140.0, the stand-in workspace of
``_fake_databricks.py`` and its stand-in for the SDK client): ``fluid apply`` creates the table
with OpenTofu and then the metric view of its ``semantics`` on the warehouse, with ``SELECT`` for
the table's readers; a second apply plans nothing and runs no metric view statement;
``--mode dry-run`` prints the ``CREATE VIEW`` and runs none; a contract that drops the semantics
drops the view; a table that moves to another schema, with its view, masks and row filter, leaves
nothing in the old schema and OpenTofu destroys it in the same apply (the view dropped before the
apply, the functions once the table is gone, then the schema's destroy resumed), which a dry run
prints without running, and so does removing the last expose in a schema; ``fluid validate``
refuses what metric view YAML cannot express; the product's Genie space
(``extensions.databricksGenie``, converged after the metric views) lists the view the same apply
made among its tables.

Not proven here: what real Databricks accepts and answers (the Free Edition run in the PR,
HONESTLY_TESTED.md).
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from fluid_build.iac import runner
from fluid_build.iac.providers import databricks_enforce as enforce

from ._databricks_cli import (  # noqa: F401 — plugin_cache is a fixture
    apply_contract,
    isolate_workspace,
    make_contract,
    plugin_cache,
    run_fluid,
    write_contract,
)
from ._fake_databricks import FakeDatabricks

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.databricks,
    pytest.mark.skipif(runner.tofu_path() is None, reason="tofu binary not installed"),
]

VIEW = "main.sales.orders_semantic_model"
SEMANTICS: Dict[str, Any] = {
    "name": "orders_metrics",
    "description": "Orders, as the shop counts them.",
    "entities": [{"name": "order_id", "type": "primary", "expr": "id"}],
    "dimensions": [{"name": "region", "type": "categorical", "expr": "region"}],
    "measures": [
        {"name": "revenue", "agg": "sum", "expr": "amount"},
        {"name": "orders", "agg": "count_distinct", "expr": "id"},
    ],
    "metrics": [
        {"name": "eu_revenue", "type": "simple", "measure": "revenue", "filter": "region = 'eu'"},
        {
            "name": "revenue_per_order",
            "type": "ratio",
            "numerator": "revenue",
            "denominator": "orders",
        },
    ],
}


@pytest.fixture
def fake() -> Iterator[FakeDatabricks]:
    server = FakeDatabricks().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def workspace(monkeypatch, tmp_path: Path, fake: FakeDatabricks, plugin_cache: str) -> Path:
    isolate_workspace(
        monkeypatch,
        tmp_path,
        host=fake.endpoint,
        warehouse_id=fake.warehouse_id,
        plugin_cache=plugin_cache,
    )
    # The stand-in's client in place of databricks-sdk's WorkspaceClient, so the hooks that run
    # on the warehouse around tofu apply reach the same workspace without the [databricks] extra.
    monkeypatch.setattr(enforce, "default_client", fake.client)
    return tmp_path


def _contract(semantics: Any = SEMANTICS) -> Dict[str, Any]:
    contract = make_contract()
    if semantics is not None:
        contract["exposes"][0]["semantics"] = copy.deepcopy(semantics)
    return contract


def _view_statements(fake: FakeDatabricks, since: int = 0) -> List[str]:
    return [s for s in fake.sql_log[since:] if " VIEW " in f" {s} " or s.startswith("GRANT")]


def test_apply_creates_the_metric_view_and_a_second_apply_changes_nothing(workspace, fake, capfd):
    path = write_contract(workspace, _contract())
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    view = fake.tables[VIEW]
    assert view["table_type"] == "METRIC_VIEW"
    assert view["properties"]["fluid.product_id"] == "dbx.fake.orders"
    assert view["comment"] == "Orders, as the shop counts them."
    assert fake.grants[("table", VIEW)] == {"analysts": ["SELECT"], "bi": ["SELECT"]}
    assert "done: create metric view main.sales.orders_semantic_model" in out
    assert "MEASURE(revenue) / NULLIF(MEASURE(orders), 0)" in view["view_definition"]

    before = len(fake.sql_log)
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "tofu plan: +0 ~0 -0" in out
    assert _view_statements(fake, before) == []


def test_dry_run_prints_the_metric_view_and_runs_nothing(workspace, fake, capfd):
    rc, out = apply_contract(capfd, write_contract(workspace, _contract()), "--mode", "dry-run")
    assert rc == 0, out
    assert "Unity Catalog metric views (run on the SQL warehouse after tofu apply)" in out
    assert "CREATE VIEW `main`.`sales`.`orders_semantic_model` WITH METRICS LANGUAGE YAML" in out
    assert "after apply: grant SELECT to analysts" in out
    assert "dry-run: none of these statements ran." in out
    assert VIEW not in fake.tables and _view_statements(fake) == []


def test_dropping_the_semantics_drops_the_view(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, _contract()))[0] == 0
    assert VIEW in fake.tables
    rc, out = apply_contract(capfd, write_contract(workspace, _contract(None)))
    assert rc == 0, out
    assert VIEW not in fake.tables
    assert "dropped metric view main.sales.orders_semantic_model" in out


def test_validate_refuses_what_the_yaml_cannot_express(workspace, fake, capfd):
    semantics = copy.deepcopy(SEMANTICS)
    semantics["metrics"].append({"name": "net", "type": "derived", "expr": "revenue - 1"})
    rc, out = run_fluid(capfd, "validate", write_contract(workspace, _contract(semantics)))
    assert rc != 0, out
    assert "derived metric" in out
    assert fake.sql_log == []


RESTRICTED: Dict[str, Any] = {
    "columnRestrictions": [
        {
            "principal": "group:analysts",
            "columns": ["region"],
            "access": "mask",
            "mask": "last_four",
        }
    ],
    "rowFilters": [{"principal": "group:analysts", "where": "region = 'eu'"}],
}


def _governed(schema: str = "sales") -> Dict[str, Any]:
    """The metric view contract with ``region`` masked and a row filter for the analysts, its
    table in ``main.<schema>``."""
    contract = _contract()
    expose = contract["exposes"][0]
    expose["binding"]["location"]["schema"] = schema
    expose["policy"]["authz"].update(copy.deepcopy(RESTRICTED))
    return contract


def _held(fake: FakeDatabricks, schema: str) -> List[str]:
    return sorted(n for n in [*fake.tables, *fake.functions] if n.rsplit(".", 1)[0] == schema)


def _totals(out: str, what: str) -> str:
    found = re.search(rf"{what}: (\+\d+ ~\d+ -\d+)", out)
    assert found, out
    return found.group(1)


def test_a_table_that_moves_schema_takes_its_view_and_functions_and_its_old_schema_goes(
    workspace, fake, capfd
):
    rc, out = apply_contract(capfd, write_contract(workspace, _governed()))
    assert rc == 0, out
    old = _held(fake, "main.sales")
    assert VIEW in old and sum(n.split(".")[2].startswith("fluid_") for n in old) == 2

    moved = write_contract(workspace, _governed("other"))
    before = len(fake.sql_log)
    rc, out = apply_contract(capfd, moved, "--allow-data-loss", "--mode", "dry-run")
    assert rc == 0, out
    assert (
        f"before apply: drop metric view {VIEW}: it reads main.sales.orders, which this apply "
        "destroys DROP VIEW IF EXISTS `main`.`sales`.`orders_semantic_model`"
    ) in out
    assert "after tofu destroys main.sales.orders: drop function main.sales.fluid_rf_orders_" in out
    assert "DROP FUNCTION IF EXISTS `main`.`sales`.`fluid_mask_orders_region_" in out
    assert "dry-run: none of these statements ran." in out
    assert _held(fake, "main.sales") == old
    assert not [s for s in fake.sql_log[before:] if s.startswith("DROP")]

    rc, out = apply_contract(capfd, moved, "--allow-data-loss")
    assert rc == 0, out
    assert "main.sales" not in fake.schemas and _held(fake, "main.sales") == []
    assert "tofu plan (the destroys that waited): +0 ~0 -1" in out
    assert _totals(out, "tofu apply complete") == _totals(out, "tofu plan")
    assert fake.tables["main.other.orders_semantic_model"]["table_type"] == "METRIC_VIEW"
    bound = fake.tables["main.other.orders"]["row_filter"]["function_name"]
    assert bound.startswith("main.other.fluid_rf_orders_")

    before = len(fake.sql_log)
    rc, out = apply_contract(capfd, moved)
    assert rc == 0, out
    assert "tofu plan: +0 ~0 -0" in out
    assert not [s for s in fake.sql_log[before:] if s.startswith(("DROP", "CREATE"))]


def test_removing_the_last_expose_in_a_schema_destroys_that_schema(workspace, fake, capfd):
    contract = _governed()
    crm = copy.deepcopy(contract["exposes"][0])
    crm["exposeId"] = "orders_crm"
    crm["binding"]["location"]["schema"] = "crm"
    contract["exposes"].append(crm)
    rc, out = apply_contract(capfd, write_contract(workspace, contract))
    assert rc == 0, out
    assert "main.crm.orders_semantic_model" in fake.tables

    del contract["exposes"][1]
    rc, out = apply_contract(capfd, write_contract(workspace, contract), "--allow-data-loss")
    assert rc == 0, out
    assert "main.crm" not in fake.schemas and _held(fake, "main.crm") == []
    assert VIEW in fake.tables and "main.sales" in fake.schemas


def test_the_products_genie_space_holds_the_metric_view_the_apply_made(workspace, fake, capfd):
    # Genie reads the contract's semantic model through the metric view: the space is converged
    # after the metric views, so the first apply already lists the view beside its table.
    semantics = {**SEMANTICS, "labels": {"ai-example-questions": "What is revenue per order?"}}
    contract = _contract(semantics)
    contract["extensions"] = {"databricksGenie": {}}
    rc, out = apply_contract(capfd, write_contract(workspace, contract))
    assert rc == 0, out
    (space,) = fake.genie_spaces.values()
    tables = json.loads(space["serialized_space"])["data_sources"]["tables"]
    assert [t["identifier"] for t in tables] == ["main.sales.orders", VIEW]
    assert "1 table(s), 1 metric view(s)" in out
