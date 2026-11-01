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

A contract's governance labels become ``databricks_entity_tag_assignment`` resources on the
schema and the table: apply creates them, a second plan is empty, removing a label destroys
only its assignment without ``--allow-data-loss``, and an assignment already in the workspace
(state lost, or set by hand) is adopted, not a failed create.

A contract's column restrictions and row filters become Unity Catalog SQL functions bound on
the table, run on the warehouse outside OpenTofu (``databricks_enforce.py``): apply binds the
filter and masks, a second apply executes no statement, a table that already exists is bound
before ``tofu apply`` can grant on it, a changed predicate binds a new function, a removed
restriction is dropped, a binding that fails revokes SELECT from the restricted principals and
fails the run (and the next apply binds first and grants again), and a failure before the apply
stops it with nothing applied.

Not proven here: what real Databricks accepts (SQL dialect, warehouse state,
privilege names, Unity Catalog storage, that CREATE FUNCTION IF NOT EXISTS and SET MASK / SET
ROW FILTER behave as the stand-in models them); that is the manual live tier.

Skipped unless ``tofu`` is on PATH; ``tofu init`` needs the registry, a
provider cache or a filesystem mirror for ``databricks/databricks``.
"""

from __future__ import annotations

import importlib.util
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
    assert "no data lost; not gated" in out
    assert fake.grants[("table", TABLE)] == {"analysts": ["SELECT"], "bi": []}


LABELS = {"data-classification": "confidential", "cost-center": "cx-001"}
SCHEMA_TAGS = {"data_classification": "confidential", "cost_center": "cx-001"}
TABLE_TAGS = {**SCHEMA_TAGS, "sensitivity": "high"}


def _governed(**labels: str) -> dict:
    return make_contract(labels=labels or LABELS, expose_labels={"sensitivity": "high"})


def test_apply_writes_the_governance_labels_as_tags_and_a_second_plan_is_empty(
    workspace, fake, capfd
):
    path = write_contract(workspace, _governed())

    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert fake.tags_on("schemas", "main.sales") == SCHEMA_TAGS
    assert fake.tags_on("tables", TABLE) == TABLE_TAGS
    assert fake.unknown == []

    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    assert rc == 0, out
    assert "tofu plan: +0 ~0 -0" in out
    assert fake.unknown == []


def test_a_contract_without_labels_makes_no_tag_assignment(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, make_contract()))[0] == 0
    assert fake.tags == {}
    assert not any("entity-tag-assignments" in path for _method, path in fake.requests)


def test_a_changed_label_value_updates_the_tag_in_place(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, _governed()))[0] == 0

    rc, out = apply_contract(
        capfd, write_contract(workspace, _governed(**{**LABELS, "cost-center": "cx-002"}))
    )
    assert rc == 0, out
    assert fake.tags_on("tables", TABLE)["cost_center"] == "cx-002"
    assert fake.tags_on("schemas", "main.sales")["cost_center"] == "cx-002"
    assert (
        "PATCH",
        f"/api/2.1/unity-catalog/entity-tag-assignments/tables/{TABLE}/tags/cost_center",
    ) in (fake.requests)


def test_removing_a_label_destroys_only_its_assignment_without_allow_data_loss(
    workspace, fake, capfd
):
    assert apply_contract(capfd, write_contract(workspace, _governed()))[0] == 0
    marker = fake.tables[TABLE]["marker"]

    path = write_contract(workspace, _governed(**{"data-classification": "confidential"}))
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "tag assignment(s) removed" in out and "not gated" in out
    assert fake.tags_on("schemas", "main.sales") == {"data_classification": "confidential"}
    assert fake.tags_on("tables", TABLE) == {
        "data_classification": "confidential",
        "sensitivity": "high",
    }
    assert fake.tables[TABLE]["marker"] == marker and fake.replaced == []
    assert "tofu plan: +0 ~0 -0" in apply_contract(capfd, path, "--mode", "dry-run")[1]


def _lose_state(workspace: Path) -> None:
    (workdir,) = (workspace / ".fluid" / "iac" / "databricks").iterdir()
    states = list(workdir.glob("terraform.tfstate*"))
    assert states, list(workdir.iterdir())
    for state in states:
        state.unlink()


def test_a_rerun_after_state_loss_adopts_the_existing_assignments(workspace, fake, capfd):
    path = write_contract(workspace, _governed())
    assert apply_contract(capfd, path)[0] == 0
    marker = fake.tables[TABLE]["marker"]
    _lose_state(workspace)

    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "brownfield: adopted" in out
    assert "already exists" not in out
    assert fake.tags_on("tables", TABLE) == TABLE_TAGS
    assert fake.tables[TABLE]["marker"] == marker
    assert "tofu plan: +0 ~0 -0" in apply_contract(capfd, path, "--mode", "dry-run")[1]


def test_an_assignment_set_by_hand_is_adopted_and_its_value_corrected(workspace, fake, capfd):
    fake.create_table(TABLE, LIVE_COLUMNS, properties={"delta.columnMapping.mode": "name"})
    fake.tag("tables", TABLE, "sensitivity", "low")
    fake.tag("tables", TABLE, "owned_by_someone_else", "keep")

    rc, out = apply_contract(capfd, write_contract(workspace, _governed()))
    assert rc == 0, out
    assert fake.tags_on("tables", TABLE) == {**TABLE_TAGS, "owned_by_someone_else": "keep"}
    assert fake.replaced == []


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


def test_a_workspace_in_the_declared_region_plans_and_applies(workspace, fake, capfd):
    # The stand-in's metastore is in eu-central-1: the precondition reads it through the
    # real provider's data.databricks_current_metastore and holds.
    path = write_contract(workspace, make_contract(region="eu-central-1"))

    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "main.sales" in fake.schemas
    assert ("GET", "/api/2.1/unity-catalog/metastore_summary") in fake.requests
    assert fake.unknown == []

    rc, out = apply_contract(capfd, path, "--mode", "dry-run")
    assert rc == 0 and "tofu plan: +0 ~0 -0" in out, out


def test_a_workspace_in_another_region_fails_the_plan_naming_both_regions(workspace, fake, capfd):
    path = write_contract(workspace, make_contract(region="us-west-2"))

    rc, out = apply_contract(capfd, path, "--mode", "dry-run")

    assert rc == 1 and "opentofu_plan_failed" in out, out
    assert "us-west-2" in out and "eu-central-1" in out
    assert "DATABRICKS_HOST" in out
    # The plan failed before anything was written.
    assert fake.schemas == {} and fake.tables == {} and fake.sql_log == []
    assert fake.unknown == []


def test_a_region_in_another_case_is_refused_before_tofu(workspace, fake, capfd):
    # The sovereignty policy compares the region verbatim, so a spelling the
    # module normalised could pass deniedRegions and land in that region.
    path = write_contract(workspace, make_contract(region="EU-Central-1"))

    rc, out = apply_contract(capfd, path, "--mode", "dry-run")

    assert rc != 0 and "databricks-region-invalid" in out, out
    assert "opentofu_plan_failed" not in out
    assert fake.schemas == {} and fake.tables == {} and fake.sql_log == []


def test_apply_refuses_a_placement_the_sovereignty_policy_denies(workspace, fake, capfd):
    # fluid apply runs only the schema gate, not fluid validate's policy check,
    # so the emitter holds the policy, as the GCP emitter does.
    contract = make_contract(region="us-west-2")
    contract["sovereignty"] = {"deniedRegions": ["us-west-2"], "enforcementMode": "strict"}
    path = write_contract(workspace, contract)

    rc, out = apply_contract(capfd, path, "--mode", "dry-run")

    assert rc != 0 and "sovereignty" in out.lower() and "us-west-2" in out, out
    assert "opentofu_plan_failed" not in out
    assert fake.schemas == {} and fake.tables == {} and fake.sql_log == []


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


# ── column masks and row filters ────────────────────────────────────────────
def _sdk_missing() -> bool:
    try:
        return importlib.util.find_spec("databricks.sdk") is None
    except ModuleNotFoundError:
        return True


requires_sdk = pytest.mark.skipif(
    _sdk_missing(), reason="databricks-sdk (the [databricks] extra) is not installed"
)

EU = "region = 'eu'"


def _restricted(
    *,
    where: str = EU,
    deny: bool = True,
    mask: bool = True,
    row_filter: bool = True,
    readers=("group:analysts", "group:bi"),
) -> dict:
    """``orders`` with ``amount`` denied and ``region`` masked to the analysts, who also read
    only some rows. ``bi`` is not restricted."""
    contract = make_contract(readers=readers)
    authz = contract["exposes"][0]["policy"]["authz"]
    restrictions = []
    if deny:
        restrictions.append(
            {"principal": "group:analysts", "columns": ["amount"], "access": "deny"}
        )
    if mask:
        restrictions.append(
            {
                "principal": "group:analysts",
                "columns": ["region"],
                "access": "mask",
                "mask": "last_four",
            }
        )
    if restrictions:
        authz["columnRestrictions"] = restrictions
    if row_filter:
        authz["rowFilters"] = [{"principal": "group:analysts", "where": where}]
    return contract


def _live(fake: FakeDatabricks) -> dict:
    return fake.tables[TABLE]


def _masks(fake: FakeDatabricks) -> dict:
    return {c["name"]: c["mask"]["function_name"] for c in _live(fake)["columns"] if c.get("mask")}


def _brownfield(fake: FakeDatabricks) -> None:
    fake.create_table(TABLE, LIVE_COLUMNS, properties={"delta.columnMapping.mode": "name"})


@requires_sdk
def test_apply_binds_the_filter_and_masks_and_a_second_apply_executes_no_statement(
    workspace, fake, capfd
):
    path = write_contract(workspace, _restricted())

    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    row_filter = _live(fake)["row_filter"]
    assert row_filter["function_name"].startswith("main.sales.fluid_rf_orders_")
    assert row_filter["input_column_names"] == ["region"]
    assert sorted(_masks(fake)) == ["amount", "region"]
    assert all(f in fake.functions for f in [row_filter["function_name"], *_masks(fake).values()])
    # The table was created by this apply, so everything was bound after it, empty.
    assert fake.grants[("table", TABLE)] == {"analysts": ["SELECT"], "bi": ["SELECT"]}
    assert "masks and row filter" in out and fake.unknown == []

    statements = len(fake.sql_log)
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "tofu plan: +0 ~0 -0" in out
    assert len(fake.sql_log) == statements, "a second apply executes no statement"
    assert fake.unknown == []


@requires_sdk
def test_a_table_that_exists_is_bound_before_the_apply_can_grant_on_it(workspace, fake, capfd):
    _brownfield(fake)
    rc, out = apply_contract(capfd, write_contract(workspace, _restricted()))
    assert rc == 0, out
    assert "row_filter" in _live(fake) and len(_masks(fake)) == 2
    assert fake.replaced == []

    timeline = fake.timeline
    last_bind = max(
        i for i, (kind, what) in enumerate(timeline) if kind == "sql" and "SET " in what
    )
    first_grant = min(
        i for i, (kind, what) in enumerate(timeline) if kind == "perm" and what.endswith("orders")
    )
    assert last_bind < first_grant, "every binding was made before the first grant on the table"


@requires_sdk
def test_a_dry_run_prints_the_statements_and_executes_none(workspace, fake, capfd):
    _brownfield(fake)
    rc, out = apply_contract(capfd, write_contract(workspace, _restricted()), "--mode", "dry-run")
    assert rc == 0, out
    assert "before apply: bind row filter" in out and "SET ROW FILTER" in out
    assert "dry-run: none of these statements ran." in out and "dry-run: plan only" in out
    assert fake.sql_log == [] and fake.functions == {} and "row_filter" not in _live(fake)


@requires_sdk
def test_a_changed_predicate_binds_a_new_function_and_keeps_the_old_one(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, _restricted()))[0] == 0
    old = _live(fake)["row_filter"]["function_name"]
    masks = _masks(fake)

    rc, out = apply_contract(capfd, write_contract(workspace, _restricted(where="region = 'uk'")))
    assert rc == 0, out
    new = _live(fake)["row_filter"]["function_name"]
    assert new != old and old in fake.functions and new in fake.functions
    assert _masks(fake) == masks
    assert not any(s.startswith("DROP") for s in fake.sql_log)
    assert not any("REPLACE FUNCTION" in s for s in fake.sql_log)


@requires_sdk
def test_a_removed_restriction_is_dropped_and_said_to_widen_access(workspace, fake, capfd):
    assert apply_contract(capfd, write_contract(workspace, _restricted()))[0] == 0

    rc, out = apply_contract(
        capfd, write_contract(workspace, _restricted(mask=False, row_filter=False))
    )
    assert rc == 0, out
    assert "access widened (no data lost, not gated): drop row filter" in out
    assert "access widened (no data lost, not gated): drop mask" in out
    assert "row_filter" not in _live(fake)
    assert sorted(_masks(fake)) == ["amount"]

    rc, out = apply_contract(
        capfd, write_contract(workspace, _restricted(deny=False, mask=False, row_filter=False))
    )
    assert rc == 0, out
    assert _masks(fake) == {} and "row_filter" not in _live(fake)
    statements = len(fake.sql_log)
    assert apply_contract(capfd, write_contract(workspace, make_contract()))[0] == 0
    assert len(fake.sql_log) == statements


@requires_sdk
def test_a_failed_binding_revokes_select_fails_the_run_and_the_next_apply_fixes_it(
    workspace, fake, capfd
):
    path = write_contract(workspace, _restricted())
    fake.fail_on("SET ROW FILTER", "[INTERNAL_ERROR] warehouse stopped")

    rc, out = apply_contract(capfd, path)
    assert rc == 1, out
    assert "opentofu_access_enforcement_failed" in out
    assert "warehouse stopped" in out and "SELECT was revoked" in out
    assert "revoked: REVOKE SELECT ON TABLE `main`.`sales`.`orders` FROM `analysts`" in out
    assert fake.grants[("table", TABLE)]["analysts"] == []
    assert fake.grants[("table", TABLE)]["bi"] == ["SELECT"], "an unrestricted reader keeps it"
    assert "row_filter" not in _live(fake)

    fake.failing.clear()
    mark = len(fake.timeline)
    rc, out = apply_contract(capfd, path)
    assert rc == 0, out
    assert "row_filter" in _live(fake) and len(_masks(fake)) == 2
    assert fake.grants[("table", TABLE)]["analysts"] == ["SELECT"], "granted again by the apply"
    again = fake.timeline[mark:]
    bound = next(i for i, (k, w) in enumerate(again) if k == "sql" and "SET ROW FILTER" in w)
    granted = next(i for i, (k, w) in enumerate(again) if k == "perm" and w.endswith("orders"))
    assert bound < granted, "the table is bound before the grant is put back"


@requires_sdk
def test_a_failure_before_the_apply_stops_it_with_nothing_applied(workspace, fake, capfd):
    _brownfield(fake)
    assert apply_contract(capfd, write_contract(workspace, _restricted()))[0] == 0
    # A new reader, and a changed predicate whose function cannot be made.
    fake.fail_on("CREATE FUNCTION", "[INSUFFICIENT_PERMISSIONS] cannot create a function")
    contract = _restricted(
        where="region = 'uk'", readers=("group:analysts", "group:bi", "group:new")
    )

    rc, out = apply_contract(capfd, write_contract(workspace, contract))
    assert rc == 1, out
    assert "opentofu_access_enforcement_failed" in out and "the apply did not run" in out
    assert "new" not in fake.grants[("table", TABLE)], "tofu apply never ran"
    assert not any(s.startswith("REVOKE") for s in fake.sql_log)
    assert fake.grants[("table", TABLE)]["analysts"] == ["SELECT"]


@requires_sdk
def test_a_contract_with_an_invalid_predicate_is_refused_before_anything_runs(
    workspace, fake, capfd
):
    contract = _restricted(where="region = 'eu'; DROP TABLE main.sales.orders")
    rc, out = apply_contract(capfd, write_contract(workspace, contract))
    assert rc == 1 and "databricks-row-filter-invalid" in out
    assert fake.sql_log == [] and fake.tables == {}


@requires_sdk
def test_an_apply_that_fails_after_creating_the_table_still_binds_it_and_fails_with_the_apply_error(
    workspace, fake, capfd
):
    # The table is created, then the grant on it fails: a later run of a job that fills the
    # table would read it with no mask or filter if the failed apply stopped there.
    fake.fail_route("PATCH", "/permissions/table/", message="grant refused")
    rc, out = apply_contract(capfd, write_contract(workspace, _restricted()))

    assert rc == 1, out
    assert "opentofu_apply_failed" in out, "the run fails with the apply's own error"
    assert "opentofu_access_enforcement_failed" not in out
    assert TABLE in fake.tables, "the failed apply did create the table"
    assert "row_filter" in _live(fake) and sorted(_masks(fake)) == ["amount", "region"]
    assert "binding the masks and row filter on the tables that exist" in out
    assert not any(s.startswith(("DROP", "REVOKE")) for s in fake.sql_log)


@requires_sdk
def test_an_apply_that_fails_before_creating_the_table_binds_nothing_and_says_nothing_alarming(
    workspace, fake, capfd
):
    fake.fail_route("POST", "/unity-catalog/schemas", message="no schema for you")
    rc, out = apply_contract(capfd, write_contract(workspace, _restricted()))

    assert rc == 1 and "opentofu_apply_failed" in out, out
    assert TABLE not in fake.tables and fake.sql_log == []
    assert "not created by the failed apply" in out
    assert "NOT REVOKED" not in out


@requires_sdk
def test_a_failed_apply_never_drops_a_binding_the_contract_no_longer_asks_for(
    workspace, fake, capfd
):
    assert apply_contract(capfd, write_contract(workspace, _restricted()))[0] == 0
    before = _live(fake)["row_filter"]["function_name"]
    mark = len(fake.sql_log)

    # No restriction now, and a new reader whose grant fails: dropping the filter would widen
    # access, and waits for an apply that succeeds.
    fake.fail_route("PATCH", "/permissions/table/", message="grant refused")
    contract = _restricted(
        mask=False,
        deny=False,
        row_filter=False,
        readers=("group:analysts", "group:bi", "group:new"),
    )
    rc, out = apply_contract(capfd, write_contract(workspace, contract))
    assert rc == 1 and "opentofu_apply_failed" in out, out
    assert _live(fake)["row_filter"]["function_name"] == before
    assert sorted(_masks(fake)) == ["amount", "region"]
    assert not any(s.startswith("DROP") for s in fake.sql_log[mark:])
    assert "access widened" not in out

    fake.failing_routes.clear()
    rc, out = apply_contract(capfd, write_contract(workspace, contract))
    assert rc == 0, out
    assert "row_filter" not in _live(fake) and _masks(fake) == {}, "dropped once it succeeded"
