# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Unity Catalog metric views through the real ``fluid`` CLI, ``tofu`` and provider, against a stand-in.

Pinned (Stage 1: real ``tofu`` and ``databricks/databricks`` 1.140.0, the stand-in workspace of
``_fake_databricks.py`` and its stand-in for the SDK client): ``fluid apply`` creates the table
with OpenTofu and then the metric view of its ``semantics`` on the warehouse, with ``SELECT`` for
the table's readers; a second apply plans nothing and runs no metric view statement;
``--mode dry-run`` prints the ``CREATE VIEW`` and runs none; a contract that drops the semantics
drops the view; ``fluid validate`` refuses what metric view YAML cannot express.

Not proven here: what real Databricks accepts and answers (the Free Edition run in the PR,
HONESTLY_TESTED.md).
"""

from __future__ import annotations

import copy
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

VIEW = "main.sales.orders_metrics"
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
    assert "done: create metric view main.sales.orders_metrics" in out
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
    assert "CREATE VIEW `main`.`sales`.`orders_metrics` WITH METRICS LANGUAGE YAML" in out
    assert "after apply: grant SELECT to analysts" in out
    assert "dry-run: none of these statements ran." in out
    assert VIEW not in fake.tables and _view_statements(fake) == []


def test_dropping_the_semantics_drops_the_view(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, _contract()))[0] == 0
    assert VIEW in fake.tables
    rc, out = apply_contract(capfd, write_contract(workspace, _contract(None)))
    assert rc == 0, out
    assert VIEW not in fake.tables
    assert "dropped metric view main.sales.orders_metrics" in out


def test_validate_refuses_what_the_yaml_cannot_express(workspace, fake, capfd):
    semantics = copy.deepcopy(SEMANTICS)
    semantics["metrics"].append({"name": "net", "type": "derived", "expr": "revenue - 1"})
    rc, out = run_fluid(capfd, "validate", write_contract(workspace, _contract(semantics)))
    assert rc != 0, out
    assert "derived metric" in out
    assert fake.sql_log == []
