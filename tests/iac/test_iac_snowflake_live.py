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

"""Live ``tofu apply`` round-trips for the Snowflake IaC plugin.

Each test compiles a FLUID contract through the plugin into ``.tf.json``,
runs a real ``tofu`` init/plan/apply cycle against a Snowflake account,
then independently verifies the object via ``snowflake.connector`` —
proving ``tofu apply`` did the work, not the test. Teardown destroys.

Triple-gated (``tofu`` + credentials + ``FLUID_IAC_LIVE_SNOWFLAKE=1``)
and isolated into a throwaway ``FLUID_IACTEST_*`` database — see
``conftest.py``. Data-plane resources (database / schema / table / view)
have a real OpenTofu reference chain so ``tofu`` orders them itself.
Orchestration / governance resources reference their container by literal
name, but the plugin attaches an explicit ``depends_on`` when the same
module also emits the container — so single-apply tests live alongside
container-pre-create ones (which prove the external-container path still
works for resources whose database / schema is supplied out of band).
"""

from __future__ import annotations

import contextlib
import os

import pytest

from fluid_build.iac import runner

from .conftest import (
    create_container,
    function_action,
    grant_contract,
    masking_policy_contract,
    planned_view_action,
    procedure_action,
    row_access_policy_contract,
    sf_exists,
    sf_rows,
    sf_table_columns,
    snowflake_plugin,
    stream_action,
    table_contract,
    task_action,
    view_contract,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.provider,
    pytest.mark.snowflake,
    pytest.mark.slow,
]

# A non-PUBLIC schema name — PUBLIC is auto-created with every database, so
# a ``snowflake_schema`` resource named PUBLIC would collide on apply.
_SCHEMA = "S1"

# A no-exposes contract — orchestration actions emit their resource without
# also emitting a database / schema (those are pre-created as a container).
_NO_EXPOSES = {"id": "iac.livetest", "name": "IaC Live Test", "exposes": []}


# ---------------------------------------------------------------------------
# Data plane — database / schema / table / view (real reference chain)
# ---------------------------------------------------------------------------


def test_live_database(tofu_project, live_db, sf_connection):
    """``snowflake_database`` — ``tofu apply`` creates the database."""
    tofu_project.apply_ok(table_contract(live_db, _SCHEMA, "EVENTS"))
    assert sf_exists(sf_connection, "DATABASES", live_db)


def test_live_schema(tofu_project, live_db, sf_connection):
    """``snowflake_schema`` — created inside the contract's database."""
    tofu_project.apply_ok(table_contract(live_db, _SCHEMA, "EVENTS"))
    assert sf_exists(sf_connection, "SCHEMAS", _SCHEMA, in_clause=f'IN DATABASE "{live_db}"')


def test_live_table_with_columns(tofu_project, live_db, sf_connection):
    """``snowflake_table`` (a v2 preview resource) — created with the
    contract's column schema; FLUID types map to Snowflake types and the
    ``required`` flag becomes ``NOT NULL``."""
    columns = [
        {"name": "ID", "type": "integer", "required": True},
        {"name": "LABEL", "type": "string"},
        {"name": "AMOUNT", "type": "decimal(12,2)"},
        {"name": "TS", "type": "timestamp"},
    ]
    tofu_project.apply_ok(table_contract(live_db, _SCHEMA, "EVENTS", columns=columns))

    assert sf_exists(
        sf_connection, "TABLES", "EVENTS", in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"'
    )
    cols = sf_table_columns(sf_connection, live_db, _SCHEMA, "EVENTS")
    assert set(cols) == {"ID", "LABEL", "AMOUNT", "TS"}
    assert cols["ID"]["type"].startswith("NUMBER")
    assert cols["ID"]["nullable"] is False  # required → NOT NULL
    assert cols["LABEL"]["type"].startswith("VARCHAR")
    assert cols["LABEL"]["nullable"] is True
    assert cols["AMOUNT"]["type"] == "NUMBER(12,2)"  # decimal(12,2) preserved
    assert cols["TS"]["type"].startswith("TIMESTAMP_NTZ")


def test_live_view_from_exposes(tofu_project, live_db, sf_connection):
    """``snowflake_view`` — a ``snowflake_view``-format exposure."""
    tofu_project.apply_ok(view_contract(live_db, _SCHEMA, "EVENTS_V", query="SELECT 1 AS ONE"))
    assert sf_exists(
        sf_connection, "VIEWS", "EVENTS_V", in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"'
    )


def test_live_full_contract_single_apply(tofu_project, live_db, sf_connection):
    """A table + view exposure provision in one ``tofu apply`` — the
    database → schema → table/view reference chain orders itself."""
    contract = {
        "id": "iac.livetest.full",
        "name": "Full",
        "exposes": [
            table_contract(live_db, _SCHEMA, "EVENTS")["exposes"][0],
            view_contract(live_db, _SCHEMA, "EVENTS_V", query="SELECT 1 AS ONE")["exposes"][0],
        ],
    }
    tofu_project.apply_ok(contract)
    assert sf_exists(sf_connection, "DATABASES", live_db)
    assert sf_exists(
        sf_connection, "TABLES", "EVENTS", in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"'
    )
    assert sf_exists(
        sf_connection, "VIEWS", "EVENTS_V", in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"'
    )


# ---------------------------------------------------------------------------
# Orchestration — streams / tasks / planned views / procedures / functions
# (emitted resources reference their container by literal name)
# ---------------------------------------------------------------------------


def test_live_stream(tofu_project, live_db, sf_connection):
    """``snowflake_stream_on_table`` — a CDC stream on a base table."""
    create_container(sf_connection, live_db, _SCHEMA, base_table="ORDERS")
    action = stream_action(live_db, _SCHEMA, "ORDERS", name="ORDERS_STREAM")
    tofu_project.apply_ok(_NO_EXPOSES, actions=[action])
    assert sf_exists(
        sf_connection,
        "STREAMS",
        "ORDERS_STREAM",
        in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"',
    )


def test_live_task(tofu_project, live_db, sf_connection):
    """``snowflake_task`` — a scheduled SQL task."""
    create_container(sf_connection, live_db, _SCHEMA)
    action = task_action(live_db, _SCHEMA, os.environ["SNOWFLAKE_WAREHOUSE"], name="ROLLUP_TASK")
    tofu_project.apply_ok(_NO_EXPOSES, actions=[action])
    assert sf_exists(
        sf_connection, "TASKS", "ROLLUP_TASK", in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"'
    )


def test_live_planned_view(tofu_project, live_db, sf_connection):
    """``snowflake_view`` from a planner ``sf.view.ensure`` op."""
    create_container(sf_connection, live_db, _SCHEMA)
    action = planned_view_action(live_db, _SCHEMA, "RECENT_V")
    tofu_project.apply_ok(_NO_EXPOSES, actions=[action])
    assert sf_exists(
        sf_connection, "VIEWS", "RECENT_V", in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"'
    )


def test_live_procedure(tofu_project, live_db, sf_connection):
    """``snowflake_procedure_sql`` (v2 preview) — a SQL stored procedure."""
    create_container(sf_connection, live_db, _SCHEMA)
    action = procedure_action(live_db, _SCHEMA, "PING_PROC")
    tofu_project.apply_ok(_NO_EXPOSES, actions=[action])
    assert sf_exists(
        sf_connection, "PROCEDURES", "PING_PROC", in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"'
    )


def test_live_function(tofu_project, live_db, sf_connection):
    """``snowflake_function_sql`` (v2 preview) — a SQL UDF."""
    create_container(sf_connection, live_db, _SCHEMA)
    action = function_action(live_db, _SCHEMA, "DOUBLE_FN")
    tofu_project.apply_ok(_NO_EXPOSES, actions=[action])
    assert sf_exists(
        sf_connection,
        "USER FUNCTIONS",
        "DOUBLE_FN",
        in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"',
    )


# ---------------------------------------------------------------------------
# Access control + governance
# ---------------------------------------------------------------------------


def test_live_grant(tofu_project, live_db, sf_connection):
    """``snowflake_grant_privileges_to_account_role`` — a table-scoped grant
    really shows up in ``SHOW GRANTS``."""
    role = os.environ["SNOWFLAKE_ROLE"]
    create_container(sf_connection, live_db, _SCHEMA, base_table="EVENTS")
    contract = grant_contract(live_db, _SCHEMA, "EVENTS", role=role, privilege="SELECT")
    resources = snowflake_plugin().emit(contract)
    grant_only = {
        "snowflake_grant_privileges_to_account_role": resources[
            "snowflake_grant_privileges_to_account_role"
        ]
    }
    tofu_project.apply_resources_ok(grant_only)

    rows = sf_rows(sf_connection, f'SHOW GRANTS ON TABLE "{live_db}"."{_SCHEMA}"."EVENTS"')
    # SHOW GRANTS columns: created_on, privilege, granted_on, name,
    # granted_to, grantee_name, grant_option, granted_by.
    assert any(
        row[1] == "SELECT" and str(row[5]).upper() == role.upper() for row in rows
    ), f"SELECT grant to {role} not found in {rows}"


def test_live_masking_policy(tofu_project, live_db, sf_connection):
    """``snowflake_masking_policy`` — created in its home schema."""
    create_container(sf_connection, live_db, _SCHEMA)
    contract = masking_policy_contract(live_db, _SCHEMA, "EVENTS", name="MASK_PII")
    resources = snowflake_plugin().emit(contract)
    tofu_project.apply_resources_ok(
        {"snowflake_masking_policy": resources["snowflake_masking_policy"]}
    )
    assert sf_exists(
        sf_connection,
        "MASKING POLICIES",
        "MASK_PII",
        in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"',
    )


def test_live_row_access_policy(tofu_project, live_db, sf_connection):
    """``snowflake_row_access_policy`` — created in its home schema."""
    create_container(sf_connection, live_db, _SCHEMA)
    contract = row_access_policy_contract(live_db, _SCHEMA, "EVENTS", name="TENANT_RAP")
    resources = snowflake_plugin().emit(contract)
    tofu_project.apply_resources_ok(
        {"snowflake_row_access_policy": resources["snowflake_row_access_policy"]}
    )
    assert sf_exists(
        sf_connection,
        "ROW ACCESS POLICIES",
        "TENANT_RAP",
        in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"',
    )


# ---------------------------------------------------------------------------
# Re-apply behaviour — idempotency, drift tolerance, the credential bridge
# ---------------------------------------------------------------------------


def test_live_idempotency(tofu_project, live_db, sf_connection):
    """A second ``tofu plan`` after apply reports zero changes — the emitted
    config is stable (notably the ``is_transient`` pin on the schema and the
    resource cross-references)."""
    tofu_project.apply_ok(table_contract(live_db, _SCHEMA, "EVENTS"))
    replan = tofu_project.plan()
    assert replan.ok, replan.stderr or replan.stdout
    assert runner.change_summary(replan) == {"add": 0, "change": 0, "remove": 0}


def test_live_drift_ignore_columns(tofu_project, live_db, sf_connection):
    """An out-of-band column change does not make ``tofu`` plan a revert —
    ``lifecycle.ignore_changes=["column"]`` lets the build engine (dbt/dlt)
    own the live column shape without ``tofu`` fighting it."""
    tofu_project.apply_ok(table_contract(live_db, _SCHEMA, "EVENTS"))
    # Simulate a dbt CREATE OR REPLACE / ALTER widening the table.
    sf_rows(
        sf_connection,
        f'ALTER TABLE "{live_db}"."{_SCHEMA}"."EVENTS" ADD COLUMN "EXTRA" VARCHAR',
    )
    replan = tofu_project.plan()
    assert replan.ok, replan.stderr or replan.stdout
    assert runner.change_summary(replan) == {"add": 0, "change": 0, "remove": 0}


def test_live_credential_bridge(tofu_project, live_db, tofu_env):
    """The ``SNOWFLAKE_ACCOUNT`` → org/account split lets the v2 provider
    self-configure — a real ``tofu plan`` reaches the account (the
    "260000: account is empty" regression guard)."""
    assert tofu_env.get("SNOWFLAKE_ORGANIZATION_NAME"), "org name not bridged"
    assert tofu_env.get("SNOWFLAKE_ACCOUNT_NAME"), "account name not bridged"

    tofu_project.emit(table_contract(live_db, _SCHEMA, "EVENTS"))
    init = tofu_project.init()
    assert init.ok, init.stderr or init.stdout
    plan = tofu_project.plan()
    assert plan.ok, plan.stderr or plan.stdout


def test_live_destroy_removes_objects(tofu_project, live_db, sf_connection):
    """``tofu destroy`` tears the provisioned objects back down — the path
    the rollback / cleanup flow depends on."""
    tofu_project.apply_ok(table_contract(live_db, _SCHEMA, "EVENTS"))
    assert sf_exists(sf_connection, "DATABASES", live_db)

    destroyed = tofu_project.destroy()
    assert destroyed.ok, destroyed.stderr or destroyed.stdout
    assert not sf_exists(sf_connection, "DATABASES", live_db)


# ---------------------------------------------------------------------------
# Single-apply container ordering — the emitter's ``depends_on`` wiring lets
# orchestration + governance resources live in the same module as their
# container (database / schema / table) without a cold-apply race.
# ---------------------------------------------------------------------------


def test_live_stream_with_emitted_container_single_apply(tofu_project, live_db, sf_connection):
    """A contract with a table exposure AND a stream on that table applies in
    one shot — the stream's ``depends_on`` orders it after the table.

    Without the wiring, ``tofu`` would create the stream and the table in
    parallel and the stream's ``CREATE STREAM`` would race the table.
    """
    contract = table_contract(live_db, _SCHEMA, "EVENTS")
    action = stream_action(live_db, _SCHEMA, "EVENTS", name="EVENTS_STREAM")

    tofu_project.apply_ok(contract, actions=[action])

    assert sf_exists(
        sf_connection, "TABLES", "EVENTS", in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"'
    )
    assert sf_exists(
        sf_connection,
        "STREAMS",
        "EVENTS_STREAM",
        in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"',
    )


def test_live_masking_policy_with_emitted_container_single_apply(
    tofu_project, live_db, sf_connection
):
    """A contract that emits both a schema/table AND a masking policy on the
    same schema applies in one shot — the policy's ``depends_on`` orders it
    after the schema."""
    contract = masking_policy_contract(live_db, _SCHEMA, "EVENTS", name="MASK_PII")

    tofu_project.apply_ok(contract)

    assert sf_exists(
        sf_connection, "TABLES", "EVENTS", in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"'
    )
    assert sf_exists(
        sf_connection,
        "MASKING POLICIES",
        "MASK_PII",
        in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"',
    )


def test_live_grant_with_emitted_target_single_apply(tofu_project, live_db, sf_connection):
    """A contract with both a table exposure AND a grant on that table
    applies in one shot — the grant's ``depends_on`` orders it after the
    table, so ``GRANT SELECT ON TABLE`` does not race ``CREATE TABLE``."""
    role = os.environ["SNOWFLAKE_ROLE"]
    contract = grant_contract(live_db, _SCHEMA, "EVENTS", role=role, privilege="SELECT")

    tofu_project.apply_ok(contract)

    assert sf_exists(
        sf_connection, "TABLES", "EVENTS", in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"'
    )
    rows = sf_rows(sf_connection, f'SHOW GRANTS ON TABLE "{live_db}"."{_SCHEMA}"."EVENTS"')
    assert any(
        row[1] == "SELECT" and str(row[5]).upper() == role.upper() for row in rows
    ), f"SELECT grant to {role} not found in {rows}"


def test_live_task_with_minute_interval_schedule(tofu_project, live_db, sf_connection):
    """A task with a ``<n> MINUTE`` schedule applies cleanly — the emitter
    maps it to the v2 provider's ``schedule.minutes``, not a bogus
    ``using_cron`` value that Snowflake would reject."""
    create_container(sf_connection, live_db, _SCHEMA)
    action = {
        "op": "sf.task.ensure",
        "database": live_db,
        "schema": _SCHEMA,
        "name": "EVERY_5_MIN",
        "sql": "SELECT CURRENT_TIMESTAMP()",
        "schedule": "5 MINUTE",
        "warehouse": os.environ["SNOWFLAKE_WAREHOUSE"],
        "after": [],
    }
    tofu_project.apply_ok(_NO_EXPOSES, actions=[action])
    assert sf_exists(
        sf_connection,
        "TASKS",
        "EVERY_5_MIN",
        in_clause=f'IN SCHEMA "{live_db}"."{_SCHEMA}"',
    )


# ---------------------------------------------------------------------------
# Catalog enrichment — table COMMENT + per-column comments
#
# Absorbed from the retired SnowflakeHorizonRegistrar. Reads ONLY existing
# v0.7.3 schema fields (``description`` / ``metadata.description`` /
# ``metadata.layer`` / ``metadata.productType`` / ``domain`` /
# ``fluidVersion`` / ``column.description``) — zero new schema surface.
# ---------------------------------------------------------------------------


def test_live_table_carries_horizon_markdown_comment(tofu_project, live_db, sf_connection):
    """The IaC plugin's catalog-style enrichment lands on the real
    Snowflake table:

    * Table COMMENT — visible via ``SHOW TABLES`` — carries the markdown
      block: description + FLUID classification + the contract YAML,
      mirroring what the retired ``SnowflakeHorizonRegistrar`` used to
      push via raw HTTP.
    * Per-column comments — visible via ``DESC TABLE`` — carry each
      column's ``description``.

    Pins the "zero new schema fields" claim: every contract field
    below already existed in v0.7.3 before this branch.
    """
    contract = {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "iac.livetest.horizon",
        "name": "Horizon catalog enrichment",
        "domain": "commerce",
        "description": "Silver orders with PII columns",
        "metadata": {
            "layer": "Silver",
            "productType": "ADP",
            "description": "Customer orders — silver layer (live test)",
            "owner": {"team": "data-eng", "email": "x@x.co"},
        },
        "exposes": [
            {
                "exposeId": "t",
                "binding": {
                    "platform": "snowflake",
                    "format": "snowflake_table",
                    "location": {
                        "database": live_db,
                        "schema": _SCHEMA,
                        "table": "ORDERS",
                    },
                },
                "contract": {
                    "schema": [
                        {
                            "name": "ID",
                            "type": "string",
                            "required": True,
                            "description": "Order id",
                        },
                        {
                            "name": "EMAIL",
                            "type": "string",
                            "description": "Customer email",
                        },
                    ]
                },
            }
        ],
    }
    tofu_project.apply_ok(contract)

    # Table COMMENT — SHOW TABLES exposes it as a named column. Look it
    # up by header so this is robust to Snowflake adding new columns.
    with contextlib.closing(sf_connection.cursor()) as cur:
        cur.execute(f'SHOW TABLES LIKE \'ORDERS\' IN SCHEMA "{live_db}"."{_SCHEMA}"')
        rows = cur.fetchall()
        headers = [c[0].lower() for c in cur.description]
    assert rows, "ORDERS table not created"
    comment = rows[0][headers.index("comment")] or ""
    assert (
        "Customer orders — silver layer (live test)" in comment
    ), f"product description not in table comment: {comment[:200]!r}"
    assert "fluid_layer: Silver" in comment
    assert "fluid_product_type: ADP" in comment
    assert "fluid_domain: commerce" in comment
    # YAML fence + contract id round-trip through the comment.
    assert "```yaml" in comment
    assert "iac.livetest.horizon" in comment

    # Per-column comments — DESC TABLE has a ``comment`` column too.
    with contextlib.closing(sf_connection.cursor()) as cur:
        cur.execute(f'DESC TABLE "{live_db}"."{_SCHEMA}"."ORDERS"')
        col_rows = cur.fetchall()
        col_headers = [c[0].lower() for c in cur.description]
    col_comment_idx = col_headers.index("comment")
    col_name_idx = col_headers.index("name")
    by_name = {r[col_name_idx]: r[col_comment_idx] for r in col_rows}
    assert by_name["ID"] == "Order id"
    assert by_name["EMAIL"] == "Customer email"


# ---------------------------------------------------------------------------
# Iceberg prerequisites — EXTERNAL VOLUME (account-level object)
# ---------------------------------------------------------------------------


def test_live_iceberg_external_volume(tofu_project, live_db, sf_connection):
    """``snowflake_external_volume`` — the dbt Iceberg prerequisite applies.

    Proves the emitted resource shape against the real provider: the
    ``storage_location`` block, ``allow_writes`` as the string "true", and
    the derived ``FLUID_*_VOL`` name (the cross-emitter contract with the
    dbt ``catalogs.yml`` emitter). The role ARN is a placeholder: Snowflake
    creates the volume without contacting AWS (verification is deferred to
    SYSTEM$VERIFY_EXTERNAL_VOLUME), so no real bucket is touched. The
    volume is account-level; teardown's ``tofu destroy`` drops it.
    """
    from fluid_build.providers._iceberg_catalog import iceberg_external_volume_name

    contract = table_contract(live_db, _SCHEMA, "EVENTS", cid="iac.livetest.iceberg")
    binding = contract["exposes"][0]["binding"]
    binding["format"] = "iceberg"
    binding["location"]["warehouse"] = "s3://fluid-iactest-iceberg-lab/products/livetest/"
    binding["location"]["iam_role_arn"] = "arn:aws:iam::123456789012:role/fluid-iactest-placeholder"

    tofu_project.apply_ok(contract)

    expected = iceberg_external_volume_name(contract, binding)
    assert sf_exists(sf_connection, "EXTERNAL VOLUMES", expected)
