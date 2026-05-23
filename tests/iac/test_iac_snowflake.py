# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the Snowflake IaC plugin — contract -> .tf.json translation.

Pure-function tests: no credentials, no network.
"""

from __future__ import annotations

import json

import pytest

from fluid_build.iac import build_module, get_iac_plugin
from fluid_build.iac.providers.snowflake import (
    _parse_policy_signature,
    _sf_type,
    _task_schedule,
)

pytestmark = [pytest.mark.unit, pytest.mark.provider]


def _sf():
    return get_iac_plugin("snowflake")


def _contract(exposes):
    return {"id": "silver.demo", "name": "Demo", "exposes": exposes}


def _table_exposure(**location):
    return {
        "exposeId": "t",
        "binding": {"platform": "snowflake", "format": "snowflake_table", "location": location},
        "contract": {
            "schema": [
                {"name": "ID", "type": "integer", "required": True},
                {"name": "MSG", "type": "string"},
                {"name": "TS", "type": "timestamp"},
            ]
        },
    }


class TestSnowflakeInfra:
    """The emitter produces the exposure's database, schema, and table —
    OpenTofu owns the whole shape, with no separate native step."""

    def test_emits_database_schema_and_table(self):
        res = _sf().emit(_contract([_table_exposure(database="DB", schema="PUBLIC", table="T")]))
        assert "snowflake_database" in res
        assert "snowflake_schema" in res
        assert "snowflake_table" in res
        assert next(iter(res["snowflake_database"].values()))["name"] == "DB"
        schema_body = next(iter(res["snowflake_schema"].values()))
        assert schema_body["name"] == "PUBLIC"
        # `is_transient` is pinned so re-apply / brownfield import does not
        # plan a destructive replace ("default" != a real schema's "false").
        assert schema_body["is_transient"] == "false"
        # The table carries the contract schema, FLUID types → Snowflake types.
        table_body = next(iter(res["snowflake_table"].values()))
        assert table_body["name"] == "T"
        cols = {c["name"]: c for c in table_body["column"]}
        assert cols["ID"]["type"] == "NUMBER(38,0)"
        assert cols["ID"]["nullable"] is False
        assert cols["MSG"]["type"] == "VARCHAR"
        assert cols["TS"]["type"] == "TIMESTAMP_NTZ"
        # tofu provisions the table but ignores column drift — the build
        # engine (dbt/dlt) owns the live column shape on re-apply.
        assert table_body["lifecycle"]["ignore_changes"] == ["column"]

    def test_view_exposure_emits_snowflake_view(self):
        res = _sf().emit(
            _contract(
                [
                    {
                        "exposeId": "v",
                        "binding": {
                            "platform": "snowflake",
                            "format": "snowflake_view",
                            "location": {
                                "database": "DB",
                                "schema": "SC",
                                "view": "V",
                                "query": "SELECT 1",
                            },
                        },
                    }
                ]
            )
        )
        assert "snowflake_view" in res
        assert "snowflake_table" not in res
        assert next(iter(res["snowflake_view"].values()))["statement"] == "SELECT 1"

    def test_schema_references_its_database(self):
        res = _sf().emit(_contract([_table_exposure(database="DB", schema="SC", table="T")]))
        db_res = next(iter(res["snowflake_database"]))
        sc = next(iter(res["snowflake_schema"].values()))
        assert sc["database"] == f"${{snowflake_database.{db_res}.name}}"


class TestSnowflakeModuleOutput:
    def test_non_snowflake_exposures_are_skipped(self):
        c = _contract(
            [
                {
                    "exposeId": "x",
                    "binding": {
                        "platform": "aws",
                        "format": "parquet",
                        "location": {"database": "d", "table": "t"},
                    },
                }
            ]
        )
        assert _sf().emit(c) == {}

    def test_output_is_canonical_and_declares_snowflake(self):
        c = _contract([_table_exposure(database="DB", schema="SC", table="T")])
        text = build_module(_sf(), c)
        doc = json.loads(text)
        assert text == json.dumps(doc, indent=2, sort_keys=True) + "\n"
        source = doc["terraform"]["required_providers"]["snowflake"]["source"]
        assert source == "snowflakedb/snowflake"


class TestSnowflakeGrants:
    """``security.access_control.grants[]`` → ``snowflake_grant_privileges_to_account_role``."""

    def _with_grants(self, grants):
        return {
            "id": "silver.demo",
            "name": "Demo",
            "exposes": [_table_exposure(database="DB", schema="SC", table="T")],
            "security": {"access_control": {"grants": grants}},
        }

    def test_schema_object_grant(self):
        res = _sf().emit(
            self._with_grants(
                [
                    {
                        "role": "ANALYST",
                        "privilege": "SELECT",
                        "object_type": "TABLE",
                        "object_name": "DB.SC.T",
                    }
                ]
            )
        )
        grant = next(iter(res["snowflake_grant_privileges_to_account_role"].values()))
        assert grant["account_role_name"] == "ANALYST"
        assert grant["privileges"] == ["SELECT"]
        assert grant["on_schema_object"] == {"object_type": "TABLE", "object_name": "DB.SC.T"}

    def test_account_object_grant(self):
        res = _sf().emit(
            self._with_grants(
                [
                    {
                        "role": "LOADER",
                        "privilege": "USAGE",
                        "object_type": "DATABASE",
                        "object_name": "DB",
                    }
                ]
            )
        )
        grant = next(iter(res["snowflake_grant_privileges_to_account_role"].values()))
        assert grant["on_account_object"] == {"object_type": "DATABASE", "object_name": "DB"}

    def test_no_grants_means_no_grant_resources(self):
        res = _sf().emit(_contract([_table_exposure(database="DB", schema="SC", table="T")]))
        assert "snowflake_grant_privileges_to_account_role" not in res


class TestSnowflakePlannedActions:
    """``emit(contract, actions)`` — streams / tasks / views from planner ops."""

    def test_stream_on_table(self):
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.stream.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "ORDERS_STREAM",
                    "source_table": "ORDERS",
                    "append_only": True,
                }
            ],
        )
        stream = next(iter(res["snowflake_stream_on_table"].values()))
        assert stream["name"] == "ORDERS_STREAM"
        assert stream["table"] == '"DB"."SC"."ORDERS"'
        assert stream["append_only"] == "true"

    def test_task_started_when_resumed(self):
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.task.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "ROLLUP",
                    "sql": "INSERT INTO DB.SC.AGG SELECT 1",
                    "schedule": "USING CRON 0 2 * * * UTC",
                    "warehouse": "WH",
                    "after": [],
                },
                {"op": "sf.task.resume", "name": "ROLLUP"},
            ],
        )
        task = next(iter(res["snowflake_task"].values()))
        assert task["sql_statement"] == "INSERT INTO DB.SC.AGG SELECT 1"
        assert task["started"] is True
        # The v2 provider's `using_cron` wants a bare cron — the `USING CRON`
        # keyword is stripped (the provider re-adds it).
        assert task["schedule"] == {"using_cron": "0 2 * * * UTC"}
        assert task["warehouse"] == "WH"

    def test_task_not_started_without_resume(self):
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.task.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "ROLLUP",
                    "sql": "SELECT 1",
                }
            ],
        )
        assert next(iter(res["snowflake_task"].values()))["started"] is False

    def test_view_from_views_section(self):
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.view.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "V_RECENT",
                    "query": "SELECT * FROM DB.SC.ORDERS",
                }
            ],
        )
        view = next(iter(res["snowflake_view"].values()))
        assert view["statement"] == "SELECT * FROM DB.SC.ORDERS"
        assert "is_secure" not in view

    def test_materialized_view_keeps_secure_flag(self):
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.view.materialized.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "V_MAT",
                    "query": "SELECT 1 AS X",
                    "secure": True,
                }
            ],
        )
        assert next(iter(res["snowflake_view"].values()))["is_secure"] == "true"

    def test_raw_sql_ops_are_skipped(self):
        # `sf.sql.execute` (masking/row-access DDL, embedded SQL) has no
        # declarative form — it must not produce a resource.
        res = _sf().emit(
            _contract([]),
            [{"op": "sf.sql.execute", "database": "DB", "sql": "CREATE MASKING POLICY ..."}],
        )
        assert res == {}

    def test_no_actions_emits_no_planned_resources(self):
        res = _sf().emit(_contract([_table_exposure(database="DB", schema="SC", table="T")]), [])
        assert "snowflake_stream_on_table" not in res
        assert "snowflake_task" not in res


class TestSnowflakeProceduresAndPolicies:
    """SQL procedures / functions (from ops) and masking / row-access policies."""

    def test_sql_procedure(self):
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.procedure.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "REFRESH",
                    "language": "SQL",
                    "parameters": [],
                    "body": "BEGIN RETURN 'ok'; END;",
                }
            ],
        )
        proc = next(iter(res["snowflake_procedure_sql"].values()))
        assert proc["name"] == "REFRESH"
        assert proc["procedure_definition"] == "BEGIN RETURN 'ok'; END;"
        assert proc["return_type"] == "VARCHAR"

    def test_non_sql_procedure_skipped(self):
        # Python / Java procedures need runtime config the contract lacks.
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.procedure.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "P",
                    "language": "PYTHON",
                    "body": "def main(): ...",
                }
            ],
        )
        assert res == {}

    def test_sql_function_with_args(self):
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.udf.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "MASK_FN",
                    "language": "SQL",
                    "return_type": "VARCHAR",
                    "parameters": [{"name": "e", "type": "VARCHAR"}],
                    "body": "REGEXP_REPLACE(e, '.+@', '***@')",
                }
            ],
        )
        fn = next(iter(res["snowflake_function_sql"].values()))
        assert fn["arguments"] == [{"arg_name": "e", "arg_data_type": "VARCHAR"}]
        assert fn["return_type"] == "VARCHAR"

    def test_masking_policy(self):
        c = {
            "id": "d",
            "exposes": [_table_exposure(database="DB", schema="SC", table="T")],
            "security": {
                "policies": {
                    "masking": [
                        {
                            "name": "MASK_PII",
                            "body": "'***'",
                            "signature": "(val VARCHAR) RETURNS VARCHAR",
                        }
                    ]
                }
            },
        }
        pol = next(iter(_sf().emit(c)["snowflake_masking_policy"].values()))
        assert pol["name"] == "MASK_PII"
        assert pol["database"] == "DB"
        assert pol["schema"] == "SC"
        assert pol["argument"] == [{"name": "val", "type": "VARCHAR"}]
        assert pol["return_data_type"] == "VARCHAR"

    def test_row_access_policy(self):
        c = {
            "id": "d",
            "exposes": [_table_exposure(database="DB", schema="SC", table="T")],
            "security": {
                "policies": {
                    "row_access": [
                        {
                            "name": "TENANT",
                            "condition": "tid = CURRENT_ACCOUNT()",
                            "signature": "(tid VARCHAR) RETURNS BOOLEAN",
                        }
                    ]
                }
            },
        }
        pol = next(iter(_sf().emit(c)["snowflake_row_access_policy"].values()))
        assert pol["name"] == "TENANT"
        assert pol["body"] == "tid = CURRENT_ACCOUNT()"
        assert pol["argument"] == [{"name": "tid", "type": "VARCHAR"}]

    def test_policy_skipped_without_home_schema(self):
        # No Snowflake exposure → no home schema → policies are skipped.
        c = {
            "id": "d",
            "exposes": [],
            "security": {"policies": {"masking": [{"name": "M", "body": "'x'"}]}},
        }
        assert "snowflake_masking_policy" not in _sf().emit(c)


class TestSnowflakeCredentialEnv:
    """The Snowflake plugin bridges forge-cli's single ``SNOWFLAKE_ACCOUNT``
    to the ``snowflakedb/snowflake`` v2 provider's org/account-name pair."""

    def test_combined_account_is_split(self):
        overlay = _sf().credential_env({"SNOWFLAKE_ACCOUNT": "XGVDOZV-PV74570"})
        assert overlay == {
            "SNOWFLAKE_ORGANIZATION_NAME": "XGVDOZV",
            "SNOWFLAKE_ACCOUNT_NAME": "PV74570",
        }

    def test_explicit_v2_vars_are_not_overridden(self):
        overlay = _sf().credential_env(
            {
                "SNOWFLAKE_ACCOUNT": "XGVDOZV-PV74570",
                "SNOWFLAKE_ORGANIZATION_NAME": "MYORG",
                "SNOWFLAKE_ACCOUNT_NAME": "MYACCT",
            }
        )
        assert overlay == {}

    def test_partial_v2_var_is_completed(self):
        # Org set explicitly, account-name missing → fill only the gap.
        overlay = _sf().credential_env(
            {"SNOWFLAKE_ACCOUNT": "XGVDOZV-PV74570", "SNOWFLAKE_ORGANIZATION_NAME": "MYORG"}
        )
        assert overlay == {"SNOWFLAKE_ACCOUNT_NAME": "PV74570"}

    def test_hostname_suffix_is_stripped(self):
        overlay = _sf().credential_env(
            {"SNOWFLAKE_ACCOUNT": "xgvdozv-pv74570.snowflakecomputing.com"}
        )
        assert overlay == {
            "SNOWFLAKE_ORGANIZATION_NAME": "xgvdozv",
            "SNOWFLAKE_ACCOUNT_NAME": "pv74570",
        }

    def test_legacy_locator_without_org_yields_no_overlay(self):
        # A bare locator (no '<org>-') cannot be split — leave it to the
        # provider to surface its own error.
        assert _sf().credential_env({"SNOWFLAKE_ACCOUNT": "xy12345"}) == {}

    def test_unset_account_yields_no_overlay(self):
        assert _sf().credential_env({}) == {}


class TestSnowflakeDiscoverImports:
    """`discover_imports` produces brownfield `tofu import` candidates whose
    addresses line up with what `emit` produced."""

    def test_discovers_database_schema_and_table(self):
        c = _contract([_table_exposure(database="DB", schema="SC", table="T")])
        by_addr = {b.to: b.id for b in _sf().discover_imports(c)}
        # cid = safe_ident("silver.demo") = "silver_demo"
        assert by_addr["snowflake_database.silver_demo_DB"] == "DB"
        assert by_addr["snowflake_schema.silver_demo_DB_SC"] == '"DB"."SC"'
        # The table is an import candidate too — v2's pipe-delimited import id.
        assert by_addr["snowflake_table.silver_demo_DB_SC_T"] == "DB|SC|T"

    def test_addresses_match_emit_resource_keys(self):
        # Every import address must be a real resource key in emit() — this
        # is the invariant that keeps brownfield adoption correct.
        c = _contract(
            [
                _table_exposure(database="DB", schema="SC", table="T1"),
                _table_exposure(database="DB", schema="SC", table="T2"),
            ]
        )
        resources = _sf().emit(c)
        emitted = {f"{rtype}.{rname}" for rtype, items in resources.items() for rname in items}
        blocks = _sf().discover_imports(c)
        assert blocks
        for block in blocks:
            assert block.to in emitted, f"{block.to} has no matching emitted resource"

    def test_database_and_schema_are_deduped(self):
        c = _contract(
            [
                _table_exposure(database="DB", schema="SC", table="T1"),
                _table_exposure(database="DB", schema="SC", table="T2"),
            ]
        )
        blocks = _sf().discover_imports(c)
        dbs = [b for b in blocks if b.to.startswith("snowflake_database.")]
        schemas = [b for b in blocks if b.to.startswith("snowflake_schema.")]
        # One database + one schema despite two exposures sharing them.
        assert len(dbs) == 1
        assert len(schemas) == 1

    def test_view_exposure_also_yields_the_view(self):
        c = _contract(
            [
                {
                    "exposeId": "v",
                    "binding": {
                        "platform": "snowflake",
                        "format": "snowflake_view",
                        "location": {"database": "DB", "schema": "SC", "view": "V"},
                    },
                }
            ]
        )
        by_addr = {b.to: b.id for b in _sf().discover_imports(c)}
        assert by_addr == {
            "snowflake_database.silver_demo_DB": "DB",
            "snowflake_schema.silver_demo_DB_SC": '"DB"."SC"',
            "snowflake_view.silver_demo_DB_SC_V": '"DB"."SC"."V"',
        }

    def test_non_snowflake_exposure_is_skipped(self):
        c = _contract([{"exposeId": "g", "binding": {"platform": "gcp"}}])
        assert _sf().discover_imports(c) == []


class TestSnowflakeProviderBlock:
    """The emitter enables the v2 provider's preview resources it relies on —
    ``snowflake_table`` and the SQL procedure / function resources."""

    def test_provider_block_enables_table_preview(self):
        feats = _sf().provider_block()["preview_features_enabled"]
        assert "snowflake_table_resource" in feats

    def test_emitted_module_carries_the_preview_block(self):
        doc = json.loads(
            build_module(_sf(), _contract([_table_exposure(database="DB", schema="SC", table="T")]))
        )
        # The `.tf.json` provider block is keyed by provider local name.
        feats = doc["provider"]["snowflake"]["preview_features_enabled"]
        assert "snowflake_table_resource" in feats

    def test_provider_block_is_credential_free(self):
        # Feature flags only — never a secret-shaped key.
        flat = json.dumps(_sf().provider_block()).lower()
        for secret in ("password", "token", "private_key", "secret"):
            assert secret not in flat


class TestSnowflakeTypeMapping:
    """``_sf_type`` — FLUID column type → Snowflake SQL type."""

    @pytest.mark.parametrize(
        ("fluid_type", "sf_type"),
        [
            ("string", "VARCHAR"),
            ("text", "VARCHAR"),
            ("char", "VARCHAR"),
            ("integer", "NUMBER(38,0)"),
            ("int", "NUMBER(38,0)"),
            ("bigint", "NUMBER(38,0)"),
            ("long", "NUMBER(38,0)"),
            ("float", "FLOAT"),
            ("double", "FLOAT"),
            ("real", "FLOAT"),
            ("boolean", "BOOLEAN"),
            ("bool", "BOOLEAN"),
            ("date", "DATE"),
            ("time", "TIME"),
            ("timestamp", "TIMESTAMP_NTZ"),
            ("datetime", "TIMESTAMP_NTZ"),
            ("variant", "VARIANT"),
            ("object", "OBJECT"),
            ("array", "ARRAY"),
            ("binary", "BINARY"),
            ("bytes", "BINARY"),
        ],
    )
    def test_known_types_map(self, fluid_type, sf_type):
        assert _sf_type(fluid_type) == sf_type

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("decimal(10,2)", "NUMBER(10,2)"),
            ("numeric(5,0)", "NUMBER(5,0)"),
            ("number(38,0)", "NUMBER(38,0)"),
            ("decimal", "NUMBER(38,0)"),  # bare decimal widens to a safe default
            ("numeric", "NUMBER(38,0)"),
        ],
    )
    def test_decimal_family_is_parsed(self, raw, expected):
        assert _sf_type(raw) == expected

    def test_type_match_is_case_insensitive(self):
        assert _sf_type("STRING") == "VARCHAR"
        assert _sf_type("Integer") == "NUMBER(38,0)"
        assert _sf_type("DECIMAL(8,4)") == "NUMBER(8,4)"

    def test_unknown_and_empty_types_fall_back_to_varchar(self):
        assert _sf_type("blob") == "VARCHAR"
        assert _sf_type(None) == "VARCHAR"
        assert _sf_type("") == "VARCHAR"


class TestSnowflakeGrantEdgeCases:
    """``_emit_grants`` — account/schema scoping, account-wide, malformed entries."""

    def _emit_grant(self, grant):
        contract = {
            "id": "silver.demo",
            "exposes": [_table_exposure(database="DB", schema="SC", table="T")],
            "security": {"access_control": {"grants": [grant]}},
        }
        return _sf().emit(contract).get("snowflake_grant_privileges_to_account_role", {})

    def test_grant_without_object_targets_the_account(self):
        # No object_type/object_name → an account-wide grant (`on_account`).
        body = next(
            iter(self._emit_grant({"role": "SYSADMIN", "privilege": "CREATE DATABASE"}).values())
        )
        assert body["on_account"] is True
        assert "on_schema_object" not in body and "on_account_object" not in body

    @pytest.mark.parametrize("object_type", ["WAREHOUSE", "INTEGRATION"])
    def test_warehouse_and_integration_are_account_level(self, object_type):
        body = next(
            iter(
                self._emit_grant(
                    {
                        "role": "LOADER",
                        "privilege": "USAGE",
                        "object_type": object_type,
                        "object_name": "OBJ",
                    }
                ).values()
            )
        )
        assert body["on_account_object"]["object_type"] == object_type

    def test_grant_missing_role_is_skipped(self):
        assert self._emit_grant({"privilege": "SELECT"}) == {}

    def test_grant_missing_privilege_is_skipped(self):
        assert self._emit_grant({"role": "ANALYST"}) == {}

    def test_non_mapping_grant_entry_is_skipped(self):
        contract = {
            "id": "d",
            "exposes": [_table_exposure(database="DB", schema="SC", table="T")],
            "security": {"access_control": {"grants": ["NOT_A_DICT", None]}},
        }
        assert "snowflake_grant_privileges_to_account_role" not in _sf().emit(contract)


class TestSnowflakeTaskEdgeCases:
    """``_emit_planned_task`` — DAG deps, optional warehouse/schedule, guards."""

    def test_after_dependencies_are_carried(self):
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.task.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "CHILD",
                    "sql": "SELECT 1",
                    "after": ["PARENT_A", "PARENT_B"],
                }
            ],
        )
        assert next(iter(res["snowflake_task"].values()))["after"] == ["PARENT_A", "PARENT_B"]

    def test_task_without_warehouse_or_schedule_omits_them(self):
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.task.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "T",
                    "sql": "SELECT 1",
                }
            ],
        )
        body = next(iter(res["snowflake_task"].values()))
        assert "warehouse" not in body
        assert "schedule" not in body

    def test_incomplete_task_is_skipped(self):
        # Missing the SQL body → no resource.
        res = _sf().emit(
            _contract([]),
            [{"op": "sf.task.ensure", "database": "DB", "schema": "SC", "name": "T"}],
        )
        assert "snowflake_task" not in res


class TestSnowflakeStreamEdgeCases:
    """``_emit_planned_stream`` — optional append-only, guard."""

    def test_stream_without_append_only_omits_the_flag(self):
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.stream.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "S",
                    "source_table": "SRC",
                }
            ],
        )
        assert "append_only" not in next(iter(res["snowflake_stream_on_table"].values()))

    def test_incomplete_stream_is_skipped(self):
        # Missing the source table → no resource.
        res = _sf().emit(
            _contract([]),
            [{"op": "sf.stream.ensure", "database": "DB", "schema": "SC", "name": "S"}],
        )
        assert "snowflake_stream_on_table" not in res


class TestSnowflakeProcedureFunctionEdgeCases:
    """``_emit_planned_procedure`` / ``_emit_planned_function`` — guards, defaults."""

    def test_incomplete_procedure_is_skipped(self):
        res = _sf().emit(
            _contract([]),
            [{"op": "sf.procedure.ensure", "database": "DB", "schema": "SC", "name": "P"}],
        )
        assert "snowflake_procedure_sql" not in res

    def test_function_default_return_type_is_varchar(self):
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.udf.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "F",
                    "language": "SQL",
                    "body": "1",
                }
            ],
        )
        assert next(iter(res["snowflake_function_sql"].values()))["return_type"] == "VARCHAR"

    def test_non_sql_function_is_skipped(self):
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.udf.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "F",
                    "language": "PYTHON",
                    "body": "def x(): ...",
                }
            ],
        )
        assert "snowflake_function_sql" not in res

    def test_argument_name_and_type_aliases_are_accepted(self):
        # The contract's `parameters` shape is informal — name/type carry
        # under several keys; a parameter missing either is dropped.
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.udf.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "F",
                    "language": "SQL",
                    "body": "1",
                    "parameters": [
                        {"arg_name": "A", "arg_data_type": "NUMBER"},
                        {"name": "B", "data_type": "VARCHAR"},
                        {"name": "NO_TYPE"},  # dropped — no type
                    ],
                }
            ],
        )
        args = next(iter(res["snowflake_function_sql"].values()))["arguments"]
        assert args == [
            {"arg_name": "A", "arg_data_type": "NUMBER"},
            {"arg_name": "B", "arg_data_type": "VARCHAR"},
        ]


class TestSnowflakeTableEdgeCases:
    """``_emit_snowflake`` — column-less tables, partial locations."""

    def test_table_with_no_columns_emits_empty_column_list(self):
        c = _contract(
            [
                {
                    "exposeId": "t",
                    "binding": {
                        "platform": "snowflake",
                        "format": "snowflake_table",
                        "location": {"database": "DB", "schema": "SC", "table": "T"},
                    },
                    "contract": {"schema": []},
                }
            ]
        )
        assert next(iter(_sf().emit(c)["snowflake_table"].values()))["column"] == []

    def test_location_missing_schema_emits_nothing(self):
        c = _contract([_table_exposure(database="DB", table="T")])
        assert _sf().emit(c) == {}

    def test_location_missing_database_emits_nothing(self):
        c = _contract([_table_exposure(schema="SC", table="T")])
        assert _sf().emit(c) == {}


class TestSnowflakePolicySignature:
    """``_parse_policy_signature`` — argument + return-type extraction."""

    def test_multi_argument_signature(self):
        args, return_type = _parse_policy_signature(
            "(EMAIL VARCHAR, TIER NUMBER) RETURNS VARCHAR", "VARCHAR"
        )
        assert args == [
            {"name": "EMAIL", "type": "VARCHAR"},
            {"name": "TIER", "type": "NUMBER"},
        ]
        assert return_type == "VARCHAR"

    def test_missing_signature_yields_a_default_argument(self):
        args, return_type = _parse_policy_signature(None, "BOOLEAN")
        assert args == [{"name": "val", "type": "VARCHAR"}]
        assert return_type == "BOOLEAN"

    def test_return_type_is_read_from_the_signature(self):
        _args, return_type = _parse_policy_signature("(x VARCHAR) RETURNS BOOLEAN", "VARCHAR")
        assert return_type == "BOOLEAN"

    def test_masking_policy_carries_a_multi_arg_signature(self):
        c = {
            "id": "d",
            "exposes": [_table_exposure(database="DB", schema="SC", table="T")],
            "security": {
                "policies": {
                    "masking": [
                        {
                            "name": "M",
                            "body": "'***'",
                            "signature": "(EMAIL VARCHAR, ROLE VARCHAR) RETURNS VARCHAR",
                        }
                    ]
                }
            },
        }
        pol = next(iter(_sf().emit(c)["snowflake_masking_policy"].values()))
        assert pol["argument"] == [
            {"name": "EMAIL", "type": "VARCHAR"},
            {"name": "ROLE", "type": "VARCHAR"},
        ]


class TestSnowflakeEmitInvariants:
    """Cross-cutting guarantees: no data sub-tree, injection-safe, secret-free."""

    def test_emit_data_is_empty(self):
        # Snowflake emits only `resource` blocks — never a `data` sub-tree.
        c = _contract([_table_exposure(database="DB", schema="SC", table="T")])
        assert _sf().emit_data(c) == {}

    def test_contract_strings_cannot_inject_tofu_interpolation(self):
        # A `${...}` smuggled into a contract value must be escaped to a
        # literal in the emitted `.tf.json`; the emitter's own resource
        # cross-references (TofuExpr) stay live.
        c = _contract(
            [
                {
                    "exposeId": "v",
                    "binding": {
                        "platform": "snowflake",
                        "format": "snowflake_view",
                        "location": {
                            "database": "DB",
                            "schema": "SC",
                            "view": "V",
                            "query": "SELECT '${file(\"/etc/passwd\")}'",
                        },
                    },
                }
            ]
        )
        text = build_module(_sf(), c)
        assert "$${file(" in text  # contract-derived → escaped
        assert "${file(" not in text.replace("$${file(", "")
        # The emitter's own database cross-reference stays a live expression.
        assert "${snowflake_database." in text

    def test_emitted_module_never_carries_credentials(self, monkeypatch):
        # Even with secrets in the environment, emit is a pure function of
        # the contract — the `.tf.json` stays secret-free.
        monkeypatch.setenv("SNOWFLAKE_PASSWORD", "hunter2-should-not-leak")
        monkeypatch.setenv("SNOWFLAKE_PRIVATE_KEY", "PRIVKEYMATERIAL")
        text = build_module(
            _sf(), _contract([_table_exposure(database="DB", schema="SC", table="T")])
        )
        assert "hunter2-should-not-leak" not in text
        assert "PRIVKEYMATERIAL" not in text

    def test_every_planner_op_is_handled(self):
        # Coverage pin: each `sf.*` op the planner can emit must map to a
        # resource (or be deliberately skipped). A dispatch refactor that
        # drops an op fails here instead of silently no-op'ing on apply.
        cases = {
            "sf.stream.ensure": (
                {"database": "D", "schema": "S", "name": "N", "source_table": "SRC"},
                "snowflake_stream_on_table",
            ),
            "sf.task.ensure": (
                {"database": "D", "schema": "S", "name": "N", "sql": "SELECT 1"},
                "snowflake_task",
            ),
            "sf.view.ensure": (
                {"database": "D", "schema": "S", "name": "N", "query": "SELECT 1"},
                "snowflake_view",
            ),
            "sf.view.materialized.ensure": (
                {"database": "D", "schema": "S", "name": "N", "query": "SELECT 1"},
                "snowflake_view",
            ),
            "sf.procedure.ensure": (
                {"database": "D", "schema": "S", "name": "N", "language": "SQL", "body": "B"},
                "snowflake_procedure_sql",
            ),
            "sf.udf.ensure": (
                {"database": "D", "schema": "S", "name": "N", "language": "SQL", "body": "B"},
                "snowflake_function_sql",
            ),
            # No declarative form — must produce nothing (the R8 boundary).
            "sf.sql.execute": ({"database": "D", "sql": "CREATE ..."}, None),
        }
        for op, (payload, expected) in cases.items():
            res = _sf().emit(_contract([]), [{"op": op, **payload}])
            if expected is None:
                assert res == {}, f"{op} should emit nothing, got {sorted(res)}"
            else:
                assert expected in res, f"{op} should emit {expected}, got {sorted(res)}"


class TestSnowflakeTaskSchedule:
    """``_task_schedule`` — map a contract schedule string to the v2
    provider's ``schedule`` block (``minutes`` or ``using_cron``)."""

    @pytest.mark.parametrize(
        ("raw", "expected_minutes"),
        [
            ("5 MINUTE", 5),
            ("1 MINUTES", 1),
            ("30 minute", 30),
            ("  60   MINUTES  ", 60),
        ],
    )
    def test_minute_interval_form_maps_to_minutes(self, raw, expected_minutes):
        # Snowflake task schedules of the form ``<n> MINUTE[S]`` must NOT
        # be emitted as ``using_cron`` — the v2 provider models minute
        # intervals as a dedicated ``minutes`` integer field.
        assert _task_schedule(raw) == {"minutes": expected_minutes}

    def test_using_cron_keyword_is_stripped(self):
        # The v2 provider's ``using_cron`` wants a bare cron — it prepends
        # ``USING CRON`` itself. A contract carrying the Snowflake SQL form
        # is normalised to the bare expression.
        assert _task_schedule("USING CRON 0 9 * * * UTC") == {"using_cron": "0 9 * * * UTC"}
        assert _task_schedule("  using   cron   0 9 * * * UTC  ") == {"using_cron": "0 9 * * * UTC"}

    def test_bare_cron_expression_passes_through(self):
        assert _task_schedule("0 9 * * * UTC") == {"using_cron": "0 9 * * * UTC"}

    def test_emitted_task_carries_minutes_schedule(self):
        # End-to-end through emit: a minute-interval schedule lands as the
        # provider's ``schedule.minutes`` field, not a bogus ``using_cron``.
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.task.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "T",
                    "sql": "SELECT 1",
                    "schedule": "5 MINUTE",
                }
            ],
        )
        assert next(iter(res["snowflake_task"].values()))["schedule"] == {"minutes": 5}


class TestSnowflakeDependencyWiring:
    """Orchestration / governance resources name their container by literal
    string, which carries no OpenTofu edge — so each emitter attaches an
    explicit ``depends_on`` for every container resource the *same* module
    also emits. A cold ``tofu apply`` then orders the container first."""

    # cid for `_contract(...)` is safe_ident("silver.demo") == "silver_demo".
    _CID = "silver_demo"

    # ----- streams / tasks / planned views / procedures / functions -------

    def test_stream_depends_on_emitted_schema_and_source_table(self):
        c = _contract([_table_exposure(database="DB", schema="SC", table="EVENTS")])
        res = _sf().emit(
            c,
            [
                {
                    "op": "sf.stream.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "S",
                    "source_table": "EVENTS",
                }
            ],
        )
        deps = next(iter(res["snowflake_stream_on_table"].values()))["depends_on"]
        assert f"snowflake_schema.{self._CID}_DB_SC" in deps
        assert f"snowflake_table.{self._CID}_DB_SC_EVENTS" in deps

    def test_stream_without_emitted_container_has_no_depends_on(self):
        # External (pre-existing) container — no resources to order against,
        # so no ``depends_on`` is emitted: the resource applies as before.
        res = _sf().emit(
            _contract([]),
            [
                {
                    "op": "sf.stream.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "S",
                    "source_table": "SRC",
                }
            ],
        )
        assert "depends_on" not in next(iter(res["snowflake_stream_on_table"].values()))

    def test_task_depends_on_emitted_schema(self):
        c = _contract([_table_exposure(database="DB", schema="SC", table="T")])
        res = _sf().emit(
            c,
            [
                {
                    "op": "sf.task.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "T",
                    "sql": "SELECT 1",
                }
            ],
        )
        deps = next(iter(res["snowflake_task"].values()))["depends_on"]
        assert f"snowflake_schema.{self._CID}_DB_SC" in deps

    def test_planned_view_depends_on_emitted_schema(self):
        c = _contract([_table_exposure(database="DB", schema="SC", table="T")])
        res = _sf().emit(
            c,
            [
                {
                    "op": "sf.view.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "V",
                    "query": "SELECT 1",
                }
            ],
        )
        deps = next(iter(res["snowflake_view"].values()))["depends_on"]
        assert f"snowflake_schema.{self._CID}_DB_SC" in deps

    def test_procedure_depends_on_emitted_schema(self):
        c = _contract([_table_exposure(database="DB", schema="SC", table="T")])
        res = _sf().emit(
            c,
            [
                {
                    "op": "sf.procedure.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "P",
                    "language": "SQL",
                    "body": "BEGIN RETURN 'ok'; END;",
                }
            ],
        )
        deps = next(iter(res["snowflake_procedure_sql"].values()))["depends_on"]
        assert f"snowflake_schema.{self._CID}_DB_SC" in deps

    def test_function_depends_on_emitted_schema(self):
        c = _contract([_table_exposure(database="DB", schema="SC", table="T")])
        res = _sf().emit(
            c,
            [
                {
                    "op": "sf.udf.ensure",
                    "database": "DB",
                    "schema": "SC",
                    "name": "F",
                    "language": "SQL",
                    "body": "1",
                }
            ],
        )
        deps = next(iter(res["snowflake_function_sql"].values()))["depends_on"]
        assert f"snowflake_schema.{self._CID}_DB_SC" in deps

    # ----- governance policies --------------------------------------------

    def test_masking_policy_depends_on_emitted_schema(self):
        c = {
            "id": "silver.demo",
            "exposes": [_table_exposure(database="DB", schema="SC", table="T")],
            "security": {
                "policies": {
                    "masking": [
                        {
                            "name": "M",
                            "body": "'***'",
                            "signature": "(v VARCHAR) RETURNS VARCHAR",
                        }
                    ]
                }
            },
        }
        deps = next(iter(_sf().emit(c)["snowflake_masking_policy"].values()))["depends_on"]
        assert f"snowflake_schema.{self._CID}_DB_SC" in deps

    def test_row_access_policy_depends_on_emitted_schema(self):
        c = {
            "id": "silver.demo",
            "exposes": [_table_exposure(database="DB", schema="SC", table="T")],
            "security": {
                "policies": {
                    "row_access": [
                        {
                            "name": "R",
                            "condition": "TRUE",
                            "signature": "(v VARCHAR) RETURNS BOOLEAN",
                        }
                    ]
                }
            },
        }
        deps = next(iter(_sf().emit(c)["snowflake_row_access_policy"].values()))["depends_on"]
        assert f"snowflake_schema.{self._CID}_DB_SC" in deps

    # ----- grants ---------------------------------------------------------

    def test_grant_on_table_depends_on_emitted_table(self):
        c = {
            "id": "silver.demo",
            "exposes": [_table_exposure(database="DB", schema="SC", table="T")],
            "security": {
                "access_control": {
                    "grants": [
                        {
                            "role": "ANALYST",
                            "privilege": "SELECT",
                            "object_type": "TABLE",
                            "object_name": "DB.SC.T",
                        }
                    ]
                }
            },
        }
        grant = next(iter(_sf().emit(c)["snowflake_grant_privileges_to_account_role"].values()))
        assert f"snowflake_table.{self._CID}_DB_SC_T" in grant["depends_on"]

    def test_grant_on_database_depends_on_emitted_database(self):
        c = {
            "id": "silver.demo",
            "exposes": [_table_exposure(database="DB", schema="SC", table="T")],
            "security": {
                "access_control": {
                    "grants": [
                        {
                            "role": "LOADER",
                            "privilege": "USAGE",
                            "object_type": "DATABASE",
                            "object_name": "DB",
                        }
                    ]
                }
            },
        }
        grant = next(iter(_sf().emit(c)["snowflake_grant_privileges_to_account_role"].values()))
        assert grant["depends_on"] == [f"snowflake_database.{self._CID}_DB"]

    def test_account_wide_grant_has_no_depends_on(self):
        # An ``on_account`` grant has no specific object to wait for.
        c = {
            "id": "silver.demo",
            "exposes": [],
            "security": {
                "access_control": {"grants": [{"role": "SYSADMIN", "privilege": "CREATE DATABASE"}]}
            },
        }
        grant = next(iter(_sf().emit(c)["snowflake_grant_privileges_to_account_role"].values()))
        assert "depends_on" not in grant

    # ----- invariant ------------------------------------------------------

    def test_every_depends_on_address_points_at_a_real_resource(self):
        # Whatever the emitter wires, every ``depends_on`` entry must
        # resolve to a resource that actually exists in the same module
        # — no dangling references can slip past ``tofu validate``.
        c = {
            "id": "silver.demo",
            "exposes": [_table_exposure(database="DB", schema="SC", table="EVENTS")],
            "security": {
                "access_control": {
                    "grants": [
                        {
                            "role": "ANALYST",
                            "privilege": "SELECT",
                            "object_type": "TABLE",
                            "object_name": "DB.SC.EVENTS",
                        }
                    ]
                },
                "policies": {
                    "masking": [
                        {
                            "name": "M",
                            "body": "'x'",
                            "signature": "(v VARCHAR) RETURNS VARCHAR",
                        }
                    ],
                    "row_access": [
                        {
                            "name": "R",
                            "condition": "TRUE",
                            "signature": "(v VARCHAR) RETURNS BOOLEAN",
                        }
                    ],
                },
            },
        }
        actions = [
            {
                "op": "sf.stream.ensure",
                "database": "DB",
                "schema": "SC",
                "name": "S",
                "source_table": "EVENTS",
            },
            {
                "op": "sf.task.ensure",
                "database": "DB",
                "schema": "SC",
                "name": "T",
                "sql": "SELECT 1",
            },
            {
                "op": "sf.procedure.ensure",
                "database": "DB",
                "schema": "SC",
                "name": "P",
                "language": "SQL",
                "body": "BEGIN RETURN 'ok'; END;",
            },
        ]
        resources = _sf().emit(c, actions)
        addresses = {f"{rtype}.{rname}" for rtype, items in resources.items() for rname in items}
        any_dep_seen = False
        for rtype, items in resources.items():
            for rname, body in items.items():
                deps = body.get("depends_on") or []
                if deps:
                    any_dep_seen = True
                for dep in deps:
                    assert dep in addresses, (
                        f"{rtype}.{rname} depends_on dangling address {dep!r} "
                        f"(known: {sorted(addresses)})"
                    )
        # And the wiring is not vacuous — at least one resource picked up a dep.
        assert any_dep_seen, "dependency wiring produced no depends_on at all"
