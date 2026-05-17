# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the AWS IaC plugin — contract -> .tf.json translation.

Pure-function tests: no credentials, no network.
"""

from __future__ import annotations

import json

import pytest

from fluid_build.iac import build_module, get_iac_plugin

pytestmark = [pytest.mark.unit, pytest.mark.provider]


def _aws():
    return get_iac_plugin("aws")


def _contract(exposes):
    return {"id": "analytics.lake", "name": "Lake", "exposes": exposes}


def _glue_exposure(**location):
    return {
        "exposeId": "t",
        "binding": {"platform": "aws", "format": "parquet", "location": location},
        "contract": {
            "schema": [
                {"name": "order_id", "type": "string", "required": True},
                {"name": "qty", "type": "integer"},
                {"name": "price", "type": "decimal(10,2)"},
            ]
        },
    }


class TestAwsGlue:
    def test_database_and_table_emitted(self):
        res = _aws().emit(
            _contract([_glue_exposure(database="sales", table="orders", bucket="lake")])
        )
        assert "aws_glue_catalog_database" in res
        assert "aws_glue_catalog_table" in res
        db = next(iter(res["aws_glue_catalog_database"].values()))
        assert db["name"] == "sales"
        tbl = next(iter(res["aws_glue_catalog_table"].values()))
        assert tbl["name"] == "orders"
        assert tbl["table_type"] == "EXTERNAL_TABLE"

    def test_table_references_its_database(self):
        res = _aws().emit(_contract([_glue_exposure(database="d", table="t", bucket="b")]))
        db_name = next(iter(res["aws_glue_catalog_database"]))
        tbl = next(iter(res["aws_glue_catalog_table"].values()))
        assert tbl["database_name"] == f"${{aws_glue_catalog_database.{db_name}.name}}"

    def test_columns_use_hive_types(self):
        res = _aws().emit(_contract([_glue_exposure(database="d", table="t", bucket="b")]))
        tbl = next(iter(res["aws_glue_catalog_table"].values()))
        cols = {c["name"]: c["type"] for c in tbl["storage_descriptor"]["columns"]}
        assert cols["order_id"] == "string"
        assert cols["qty"] == "int"
        assert cols["price"] == "decimal(10,2)"

    def test_storage_descriptor_points_at_s3(self):
        res = _aws().emit(
            _contract(
                [_glue_exposure(database="d", table="t", bucket="my-lake", path="sales/curated/")]
            )
        )
        tbl = next(iter(res["aws_glue_catalog_table"].values()))
        assert tbl["storage_descriptor"]["location"] == "s3://my-lake/sales/curated/"


class TestAwsS3:
    def test_bucket_emitted(self):
        res = _aws().emit(_contract([_glue_exposure(database="d", table="t", bucket="my-lake")]))
        bkt = next(iter(res["aws_s3_bucket"].values()))
        assert bkt["bucket"] == "my-lake"
        assert bkt["force_destroy"] is True

    def test_shared_bucket_is_deduplicated(self):
        # Two exposures sharing one bucket -> a single aws_s3_bucket resource.
        res = _aws().emit(
            _contract(
                [
                    _glue_exposure(database="d", table="t1", bucket="shared"),
                    _glue_exposure(database="d", table="t2", bucket="shared"),
                ]
            )
        )
        assert len(res["aws_s3_bucket"]) == 1
        assert len(res["aws_glue_catalog_table"]) == 2


class TestAwsModuleOutput:
    def test_non_aws_exposures_are_skipped(self):
        c = _contract(
            [
                {
                    "exposeId": "x",
                    "binding": {
                        "platform": "gcp",
                        "format": "bigquery_table",
                        "location": {"dataset": "d", "table": "t"},
                    },
                }
            ]
        )
        assert _aws().emit(c) == {}

    def test_output_is_canonical_and_declares_aws(self):
        c = _contract([_glue_exposure(database="d", table="t", bucket="b")])
        text = build_module(_aws(), c)
        doc = json.loads(text)
        assert text == json.dumps(doc, indent=2, sort_keys=True) + "\n"
        assert doc["terraform"]["required_providers"]["aws"]["source"] == "hashicorp/aws"
