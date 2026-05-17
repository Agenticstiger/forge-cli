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


class TestSnowflakeTable:
    def test_emits_database_schema_table(self):
        res = _sf().emit(_contract([_table_exposure(database="DB", schema="PUBLIC", table="T")]))
        assert "snowflake_database" in res
        assert "snowflake_schema" in res
        assert "snowflake_table" in res
        assert next(iter(res["snowflake_database"].values()))["name"] == "DB"
        assert next(iter(res["snowflake_schema"].values()))["name"] == "PUBLIC"
        assert next(iter(res["snowflake_table"].values()))["name"] == "T"

    def test_schema_and_table_reference_parents(self):
        res = _sf().emit(_contract([_table_exposure(database="DB", schema="SC", table="T")]))
        db_res = next(iter(res["snowflake_database"]))
        sc_res = next(iter(res["snowflake_schema"]))
        sc = next(iter(res["snowflake_schema"].values()))
        tbl = next(iter(res["snowflake_table"].values()))
        assert sc["database"] == f"${{snowflake_database.{db_res}.name}}"
        assert tbl["database"] == f"${{snowflake_database.{db_res}.name}}"
        assert tbl["schema"] == f"${{snowflake_schema.{sc_res}.name}}"

    def test_columns_use_snowflake_types(self):
        res = _sf().emit(_contract([_table_exposure(database="DB", schema="SC", table="T")]))
        cols = {c["name"]: c for c in next(iter(res["snowflake_table"].values()))["column"]}
        assert cols["ID"]["type"] == "NUMBER(38,0)"
        assert cols["ID"]["nullable"] is False
        assert cols["MSG"]["type"] == "VARCHAR"
        assert cols["MSG"]["nullable"] is True
        assert cols["TS"]["type"] == "TIMESTAMP_NTZ"


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
