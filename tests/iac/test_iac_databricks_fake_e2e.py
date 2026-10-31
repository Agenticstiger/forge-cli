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

Pinned (Stage 1: real ``tofu``, stand-in workspace): validate, plan,
``generate iac --validate`` and ``apply --mode dry-run`` route to OpenTofu; a
second plan is empty; a replace is refused without ``--allow-data-loss``; a
grant revoke is not; a dropped column and a positional rename are refused like a
destroy (each is an in-place ``~``); an added column applies in place; a table
that exists but is not in state is adopted, not replaced; a column type change
and a missing catalog fail at plan with a message that names the column or the
catalog; missing credentials fail naming the variables to set.

Not proven here: what real Databricks accepts (SQL dialect, warehouse state,
privilege names, Unity Catalog storage); that is the manual live tier.

Skipped unless ``tofu`` is on PATH; ``tofu init`` needs the registry, a
provider cache or a filesystem mirror for ``databricks/databricks``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List

import pytest

from fluid_build.iac import IAC_PLUGINS, runner
from fluid_build.iac.module import LOCK_FILE_NAME, dependency_lock

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


def test_a_reorder_is_refused_even_with_allow_data_loss(workspace, fake, capfd):
    # With a column added the provider adds by name and never moves a column, so
    # the next plan would rename by position forever: refused before any change.
    assert apply_contract(capfd, write_contract(workspace, make_contract()))[0] == 0
    reordered = [COLUMNS[1], COLUMNS[0], COLUMNS[2], ("note", "string", False)]
    path = write_contract(workspace, make_contract(columns=reordered))
    rc, out = apply_contract(capfd, path, "--allow-data-loss")
    assert rc == 1 and "databricks-column-reorder" in out
    assert _columns(fake) == ["id", "amount", "region"]


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


LIVE_COLUMNS = [("id", "bigint", False), ("amount", "double", True), ("region", "string", True)]


def test_an_existing_table_is_adopted_not_replaced(workspace, fake, capfd):
    # In the workspace, not in state: the provider's CREATE OR REPLACE would
    # replace it, with the plan showing a plain +1 (#6140).
    fake.create_table(TABLE, LIVE_COLUMNS, properties={"delta.columnMapping.mode": "name"})
    marker = fake.tables[TABLE]["marker"]

    path = write_contract(workspace, make_contract())
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "brownfield: adopted 2 pre-existing resource(s)" in out
    assert fake.replaced == []
    assert fake.tables[TABLE]["marker"] == marker
    assert "tofu plan: +0 ~0 -0" in apply_contract(capfd, path, "--mode", "dry-run")[1]


def _brownfield_with_grants(fake: FakeDatabricks, *table_privileges: str) -> None:
    fake.create_table(TABLE, LIVE_COLUMNS, properties={"delta.columnMapping.mode": "name"})
    for principal in ("analysts", "bi"):
        fake.grant("table", TABLE, principal, *table_privileges)
        fake.grant("schema", "main.sales", principal, "USE_SCHEMA")


def test_existing_grants_are_adopted_with_their_table(workspace, fake, capfd):
    # Each grant is imported by the provider's own id, so the contract's grants
    # plan no change. The table's one update writes back what the provider does
    # not import (its properties and warehouse id): the values it already has.
    _brownfield_with_grants(fake, "SELECT")
    path = write_contract(workspace, make_contract())
    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    assert rc == 0, out
    assert "brownfield: adopted 6 pre-existing resource(s)" in out
    assert "tofu plan: +0 ~1 -0" in out
    assert apply_contract(capfd, path)[0] == 0
    assert "tofu plan: +0 ~0 -0" in apply_contract(capfd, path, "--mode", "dry-run")[1]
    assert fake.grants[("table", TABLE)] == {"analysts": ["SELECT"], "bi": ["SELECT"]}


def test_an_adopted_grant_shows_what_the_contract_takes_away(workspace, fake, capfd):
    # A grant is authoritative for its principal: MODIFY, which the contract does
    # not name, goes. Adopted first, that is an update the plan shows.
    _brownfield_with_grants(fake, "MODIFY", "SELECT")
    path = write_contract(workspace, make_contract())
    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    assert rc == 0 and "tofu plan: +0 ~3 -0" in out, out
    assert apply_contract(capfd, path)[0] == 0
    assert fake.grants[("table", TABLE)] == {"analysts": ["SELECT"], "bi": ["SELECT"]}


def test_an_existing_table_without_column_mapping_is_refused_not_upgraded(workspace, fake, capfd):
    # Turning column mapping on upgrades the table's Delta protocol, which older
    # readers cannot read: the owner's call, so no flag lets the apply make it.
    fake.create_table(TABLE, LIVE_COLUMNS)
    path = write_contract(workspace, make_contract())
    for extra in ((), ("--allow-data-loss",)):
        rc, out = apply_contract(capfd, path, *extra)
        assert rc == 1 and "databricks-adopt-protocol-upgrade" in out, out
        assert "ALTER TABLE `main`.`sales`.`orders` SET TBLPROPERTIES" in out
        assert "released the" in out, out
    assert not [sql for sql in fake.sql_log if "TBLPROPERTIES" in sql]
    assert fake.tables[TABLE]["properties"] == {}
    # The refused runs applied nothing, so they leave nothing they imported in
    # state: leaving the table out of the contract, as the refusal offers, then
    # plans no destroy of a table the product never created.
    assert _tables_in_state(workspace) == []

    # The owner runs the ALTER in Databricks; the next apply adopts the table.
    fake.tables[TABLE]["properties"]["delta.columnMapping.mode"] = "name"
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert fake.replaced == []


def test_a_dry_run_leaves_the_state_as_it_found_it(workspace, fake, capfd):
    # The plan adopts the existing table to show its update, not its replacement;
    # a dry-run then forgets it again, and the apply adopts it for good.
    fake.create_table(TABLE, LIVE_COLUMNS, properties={"delta.columnMapping.mode": "name"})
    path = write_contract(workspace, make_contract())
    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    assert rc == 0 and "brownfield: adopted" in out and "released the" in out, out
    assert _tables_in_state(workspace) == []
    rc, out = apply_contract(capfd, path)
    assert rc == 0 and "released the" not in out, out
    assert len(_tables_in_state(workspace)) == 1
    assert fake.replaced == []


def _tables_in_state(workspace: Path) -> List[str]:
    workdirs = list((workspace / ".fluid" / "iac" / "databricks").iterdir())
    assert len(workdirs) == 1, workdirs
    listed = runner.tofu_state_list(str(workdirs[0]))
    return [address for address in listed if address.startswith("databricks_sql_table.")]


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


def test_no_host_names_the_variable_to_set(workspace, fake, capfd, monkeypatch):
    # Without it the provider would take the DEFAULT profile's workspace.
    monkeypatch.delenv("DATABRICKS_HOST")
    path = write_contract(workspace, make_contract())
    rc, out = apply_contract(capfd, path)
    assert rc == 1 and "databricks-host-unset" in out and "DATABRICKS_HOST" in out
    assert fake.schemas == {} and fake.sql_log == []


def test_no_credentials_names_the_variables_to_set(workspace, fake, capfd, monkeypatch):
    monkeypatch.delenv("DATABRICKS_TOKEN")
    path = write_contract(workspace, make_contract())
    rc, out = apply_contract(capfd, path)
    assert rc == 1 and "databricks-credentials-missing" in out
    assert "DATABRICKS_TOKEN" in out and "DATABRICKS_CLIENT_SECRET" in out
    assert "DATABRICKS_CONFIG_PROFILE" in out
    assert fake.schemas == {} and fake.sql_log == []


def test_diff_initialises_with_the_committed_lock(workspace, fake, capfd):
    path = write_contract(workspace, make_contract())
    assert apply_contract(capfd, path)[0] == 0
    [lock] = workspace.glob(f".fluid/iac/databricks/*/{LOCK_FILE_NAME}")
    lock.unlink()  # written by the apply; the diff must write its own
    rc, out = run_fluid(capfd, "diff", path)
    assert rc == 0, out
    assert lock.read_text() == dependency_lock(IAC_PLUGINS["databricks"])
