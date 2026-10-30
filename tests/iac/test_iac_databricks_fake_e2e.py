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

"""Databricks through the real ``fluid`` CLI, ``tofu`` and provider, against a stand-in.

Every command is the real ``fluid`` entry point, run in-process: routing,
validation, the OpenTofu engine, its data-loss gate and the provider's own diff
all run for real. Only the workspace is ``_fake_databricks.FakeDatabricks``, an
in-process stand-in for the Unity Catalog, SQL statement and permissions APIs,
so no Databricks account or credential is involved.

Pinned (the Databricks plan's Phase 1 exit criteria, and its 0b findings):
validate, plan, ``generate iac --validate`` and ``apply --mode dry-run`` route
to OpenTofu; a second plan is empty; a replace is refused without
``--allow-data-loss``; a grant revoke is not; a dropped column is refused like a
destroy (it is an in-place ``~``); an added column applies in place; a table
that exists but is not in state is adopted, not replaced; a column type change
and a missing catalog fail at plan with a message that names the column or the
catalog.

Not proven here: what real Databricks accepts (SQL dialect, warehouse state,
privilege names, Unity Catalog storage); that is the manual live tier.

Skipped unless ``tofu`` is on PATH; ``tofu init`` needs the registry, a
provider cache or a filesystem mirror for ``databricks/databricks``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List

import pytest

from fluid_build.iac import runner

from ._databricks_cli import (  # noqa: F401 — plugin_cache is a fixture
    COLUMNS,
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

TABLE = "main.sales.orders"


@pytest.fixture
def fake() -> Iterator[FakeDatabricks]:
    server = FakeDatabricks().start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def workspace(monkeypatch, tmp_path: Path, fake: FakeDatabricks, plugin_cache: str) -> Path:
    """The fake as the only workspace reachable: no profile, no real host or token."""
    isolate_workspace(
        monkeypatch,
        tmp_path,
        host=fake.endpoint,
        warehouse_id=fake.warehouse_id,
        plugin_cache=plugin_cache,
    )
    return tmp_path


def _columns(fake: FakeDatabricks) -> List[str]:
    return [c["name"] for c in fake.tables[TABLE]["columns"]]


def test_validate_plan_generate_and_dry_run_route_to_opentofu(workspace, fake, capfd):
    path = write_contract(workspace, make_contract())

    rc, out = run_fluid(capfd, "validate", path)
    assert rc == 0, out
    rc, out = run_fluid(capfd, "plan", path, "--out", "plan.json")
    assert rc == 0, out
    rc, out = run_fluid(capfd, "generate", "iac", path, "--out", "iac", "--validate")
    assert rc == 0, out
    assert "OpenTofu validation passed" in out
    assert (workspace / "iac" / ".terraform.lock.hcl").is_file()

    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    assert rc == 0, out
    assert "OpenTofu engine — provider: databricks" in out
    assert "dry-run: plan only" in out
    # A dry-run creates nothing in the workspace.
    assert fake.schemas == {} and fake.tables == {} and fake.sql_log == []
    assert fake.unknown == []


def test_apply_then_a_second_plan_is_empty(workspace, fake, capfd):
    path = write_contract(workspace, make_contract())
    assert run_fluid(capfd, "plan", path, "--out", "plan.json")[0] == 0

    rc, out = apply_contract(capfd, "plan.json")
    assert rc == 0, out
    assert "main.sales" in fake.schemas
    assert _columns(fake) == ["id", "amount", "region"]
    assert fake.tables[TABLE]["properties"] == {"delta.columnMapping.mode": "name"}
    assert fake.grants[("table", TABLE)] == {"analysts": ["SELECT"], "bi": ["SELECT"]}
    assert fake.grants[("schema", "main.sales")] == {
        "analysts": ["USE_SCHEMA"],
        "bi": ["USE_SCHEMA"],
    }

    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    assert rc == 0, out
    assert "tofu plan: +0 ~0 -0" in out
    assert fake.unknown == []


def test_a_replace_is_refused_without_allow_data_loss(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, make_contract()))[0] == 0
    marker = fake.tables[TABLE]["marker"]

    # Partitions cannot change in place: the provider replaces the table.
    path = write_contract(workspace, make_contract(partitions=("region",)))
    rc, out = apply_contract(capfd, path)
    assert rc == 1 and "opentofu_data_loss_gate" in out and "plan destroys" in out
    assert fake.tables[TABLE]["marker"] == marker

    rc, out = apply_contract(capfd, path, "--allow-data-loss")
    assert rc == 0, out
    assert fake.tables[TABLE]["partitions"] == ["region"]
    assert fake.tables[TABLE]["marker"] != marker


def test_a_grant_revoke_is_not_gated(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, make_contract()))[0] == 0

    rc, out = apply_contract(
        capfd, write_contract(workspace, make_contract(readers=("group:analysts",)))
    )
    assert rc == 0, out
    assert "access revoked, no data lost; not gated" in out
    assert fake.grants[("table", TABLE)] == {"analysts": ["SELECT"], "bi": []}


def test_a_dropped_column_is_refused_like_a_destroy(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, make_contract()))[0] == 0
    marker = fake.tables[TABLE]["marker"]

    path = write_contract(workspace, make_contract(columns=COLUMNS[:2]))
    rc, out = apply_contract(capfd, path)
    assert rc == 1 and "opentofu_data_loss_gate" in out
    assert "drops column(s) region" in out
    assert _columns(fake) == ["id", "amount", "region"]

    rc, out = apply_contract(capfd, path, "--allow-data-loss")
    assert rc == 0, out
    assert _columns(fake) == ["id", "amount"]
    assert fake.tables[TABLE]["marker"] == marker  # dropped in place, not replaced


def test_a_positional_rename_is_refused_like_a_destroy(workspace, fake, capfd):
    # Same column count: the provider renames by position, so amount's values
    # would stay under the new name. The contract cannot say it means a rename.
    assert apply_contract(capfd, write_contract(workspace, make_contract()))[0] == 0
    renamed = [("id", "bigint", True), ("total", "double", False), ("region", "string", False)]
    path = write_contract(workspace, make_contract(columns=renamed))
    rc, out = apply_contract(capfd, path)
    assert rc == 1 and "renames column amount to total by position" in out
    assert _columns(fake) == ["id", "amount", "region"]

    rc, out = apply_contract(capfd, path, "--allow-data-loss")
    assert rc == 0, out
    assert _columns(fake) == ["id", "total", "region"]


def test_an_added_column_applies_in_place(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, make_contract()))[0] == 0
    marker = fake.tables[TABLE]["marker"]

    path = write_contract(
        workspace, make_contract(columns=COLUMNS + [("channel", "string", False)])
    )
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "tofu plan: +0 ~1 -0" in out
    assert _columns(fake) == ["id", "amount", "region", "channel"]
    assert fake.tables[TABLE]["marker"] == marker
    assert "tofu plan: +0 ~0 -0" in apply_contract(capfd, path, "--mode", "dry-run")[1]


def test_an_existing_table_is_adopted_not_replaced(workspace, fake, capfd):
    # In the workspace, not in state: the provider's CREATE OR REPLACE would
    # replace it, with the plan showing a plain +1 (#6140).
    fake.create_table(
        TABLE, [("id", "bigint", False), ("amount", "double", True), ("region", "string", True)]
    )
    marker = fake.tables[TABLE]["marker"]

    path = write_contract(workspace, make_contract())
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "brownfield: adopted 2 pre-existing resource(s)" in out
    assert fake.replaced == []
    assert fake.tables[TABLE]["marker"] == marker
    assert "tofu plan: +0 ~0 -0" in apply_contract(capfd, path, "--mode", "dry-run")[1]


def test_a_type_change_fails_at_plan_naming_the_column(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, make_contract()))[0] == 0

    changed = [
        ("id", "bigint", True),
        ("amount", "decimal(18,2)", False),
        ("region", "string", False),
    ]
    rc, out = apply_contract(
        capfd, write_contract(workspace, make_contract(columns=changed)), "--mode", "dry-run"
    )
    assert rc == 1 and "opentofu_plan_failed" in out
    assert "databricks-column-type-change" in out
    assert "amount: double in the live table, decimal(18,2) in the contract" in out


def test_a_missing_catalog_fails_at_plan(workspace, fake, capfd):
    path = write_contract(workspace, make_contract(catalog="nope"))
    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    assert rc == 1 and "databricks-catalog-missing" in out
    assert "catalog 'nope'" in out
    # Nothing was created on the way to the failure.
    assert fake.schemas == {} and fake.sql_log == []


def test_no_credentials_names_the_variables_to_set(workspace, fake, capfd, monkeypatch):
    monkeypatch.delenv("DATABRICKS_HOST")
    monkeypatch.delenv("DATABRICKS_TOKEN")
    path = write_contract(workspace, make_contract())
    rc, out = apply_contract(capfd, path)
    assert rc == 1 and "databricks-credentials-missing" in out
    assert "DATABRICKS_HOST" in out and "DATABRICKS_TOKEN" in out
    assert fake.schemas == {} and fake.sql_log == []
