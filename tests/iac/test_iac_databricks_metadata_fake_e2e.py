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

"""Unity Catalog metadata through the real ``fluid`` CLI, ``tofu`` and provider, against a stand-in.

Pinned (Stage 1: real ``tofu`` and ``databricks/databricks`` 1.140.0, the stand-in workspace of
``_fake_databricks.py``): ``fluid apply`` writes the table comment and the column comments
(an apostrophe included, through the provider's own ``COMMENT '...'`` escaping), the product's
identity tags on the table and a classified column's ``fluid_sensitivity`` column tag; a second
plan is empty; ``fluid verify`` then passes, and fails once a comment or a column tag is changed
by hand; a changed description is an in-place update, never a replace; removing a column's
classification destroys only its column tag; a table created before any of this is adopted and
gains its comments in place.

Not proven here: what real Databricks stores for a comment or a column tag, and whether a
dbt ``CREATE OR REPLACE`` keeps column tags (the live tier, HONESTLY_TESTED.md).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from fluid_build.cli._verify_databricks import verify_databricks_expose
from fluid_build.iac import runner

from ._databricks_cli import (  # noqa: F401 — plugin_cache is a fixture
    PRODUCT_TAGS,
    apply_contract,
    isolate_workspace,
    make_contract,
    plugin_cache,
    write_contract,
)
from ._fake_databricks import FakeDatabricks

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.databricks,
    pytest.mark.skipif(runner.tofu_path() is None, reason="tofu binary not installed"),
]

TABLE = "main.sales.orders"
#: ``make_contract``'s own tags, and the expose's version that ``_described`` adds.
TABLE_TAGS = {**PRODUCT_TAGS, "fluid_product_version": "2.0.0"}


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
    return tmp_path


def _described(**changes: Any) -> Dict[str, Any]:
    contract = make_contract()
    expose = contract["exposes"][0]
    expose["version"] = "2.0.0"
    expose["description"] = changes.get("description", "One row per order, as the shop's sees it.")
    schema = expose["contract"]["schema"]
    schema[0]["description"] = "The order's id."
    if changes.get("classified", True):
        schema[1]["sensitivity"] = "confidential"
    return contract


def _verify(fake: FakeDatabricks, contract: Dict[str, Any]) -> Dict[str, Any]:
    fake.grant_catalog("main", "analysts", "USE_CATALOG")
    fake.grant_catalog("main", "bi", "USE_CATALOG")
    return verify_databricks_expose(
        "orders",
        contract["exposes"][0],
        contract=copy.deepcopy(contract),
        client_factory=fake.client,
    )


def test_apply_writes_comments_and_tags_a_second_plan_is_empty_and_verify_passes(
    workspace, fake, capfd
):
    contract = _described()
    path = write_contract(workspace, contract)

    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    table = fake.tables[TABLE]
    assert table["comment"] == "One row per order, as the shop's sees it."
    assert [c.get("comment") for c in table["columns"]] == ["The order's id.", None, None]
    assert fake.tags_on("tables", TABLE) == TABLE_TAGS
    assert fake.tags_on("columns", f"{TABLE}.amount") == {"fluid_sensitivity": "confidential"}
    assert fake.tags_on("columns", f"{TABLE}.region") == {}
    assert fake.tags_on("schemas", "main.sales") == {}
    # the owner is a tag, never set: the table stays the deploy identity's
    assert not any("OWNER TO" in statement for statement in fake.sql_log)
    assert fake.unknown == []

    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    assert rc == 0, out
    assert "tofu plan: +0 ~0 -0" in out

    result = _verify(fake, contract)
    assert result["status"] == "match", result["dimensions"]["governance_tags"]

    fake.tables[TABLE]["comment"] = "edited by hand"
    fake.tags[("columns", f"{TABLE}.amount", "fluid_sensitivity")] = "public"
    tags = _verify(fake, contract)["dimensions"]["governance_tags"]
    assert tags["status"] == "fail"
    assert {f["entity"] for f in tags["findings"]} == {
        f"table {TABLE}",
        f"column {TABLE}.amount",
    }


def test_a_changed_description_updates_the_comment_in_place(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, _described()))[0] == 0
    marker = fake.tables[TABLE]["marker"]

    rc, out = apply_contract(
        capfd, write_contract(workspace, _described(description="Orders, one per row."))
    )
    assert rc == 0, out
    assert fake.tables[TABLE]["comment"] == "Orders, one per row."
    assert fake.tables[TABLE]["marker"] == marker and fake.replaced == []
    assert any(s.startswith("COMMENT ON TABLE") for s in fake.sql_log)


def test_removing_a_classification_destroys_only_its_column_tag(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, _described()))[0] == 0
    marker = fake.tables[TABLE]["marker"]

    path = write_contract(workspace, _described(classified=False))
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert fake.tags_on("columns", f"{TABLE}.amount") == {}
    assert fake.tags_on("tables", TABLE) == TABLE_TAGS
    assert fake.tables[TABLE]["marker"] == marker and fake.replaced == []
    assert "tofu plan: +0 ~0 -0" in apply_contract(capfd, path, "--mode", "dry-run")[1]


def test_a_table_created_without_comments_is_adopted_and_gains_them_in_place(
    workspace, fake, capfd
):
    fake.create_table(
        TABLE,
        [("id", "bigint", False), ("amount", "double", True), ("region", "string", True)],
        properties={"delta.columnMapping.mode": "name"},
    )
    marker = fake.tables[TABLE]["marker"]

    rc, out = apply_contract(capfd, write_contract(workspace, _described()))
    assert rc == 0, out
    assert fake.tables[TABLE]["comment"] == "One row per order, as the shop's sees it."
    assert fake.tables[TABLE]["columns"][0]["comment"] == "The order's id."
    assert fake.tables[TABLE]["marker"] == marker and fake.replaced == []


def test_a_deprecated_product_gets_the_certification_system_tag(workspace, fake, capfd):
    contract = _described()
    contract["lifecycle"] = {"state": "deprecated"}
    path = write_contract(workspace, contract)

    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "system.certification_status is set on main.sales.orders (deprecated)" in out
    assert fake.tags_on("tables", TABLE)["system.certification_status"] == "deprecated"
    assert "tofu plan: +0 ~0 -0" in apply_contract(capfd, path, "--mode", "dry-run")[1]

    del contract["lifecycle"]
    rc, out = apply_contract(capfd, write_contract(workspace, contract))
    assert rc == 0, out
    assert "system.certification_status" not in fake.tags_on("tables", TABLE)
