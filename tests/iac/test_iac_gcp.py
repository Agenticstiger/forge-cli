# Copyright 2024-2026 Agentics Transformation Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the GCP IaC plugin — contract -> .tf.json translation.

Pure-function tests: no credentials, no network.
"""

from __future__ import annotations

import json

import pytest

from fluid_build.iac import build_module, get_iac_plugin

pytestmark = [pytest.mark.unit, pytest.mark.provider]


def _gcp():
    return get_iac_plugin("gcp")


def _contract(exposes):
    return {"id": "analytics.demo", "name": "Demo", "exposes": exposes}


class TestGcpBigQuery:
    def test_table_emits_dataset_and_table(self):
        res = _gcp().emit(
            _contract(
                [
                    {
                        "exposeId": "orders",
                        "binding": {
                            "platform": "gcp",
                            "format": "bigquery_table",
                            "location": {"dataset": "sales", "table": "orders"},
                        },
                        "contract": {
                            "schema": [
                                {"name": "id", "type": "integer", "required": True},
                                {"name": "note", "type": "string"},
                            ]
                        },
                    }
                ]
            )
        )
        assert "google_bigquery_dataset" in res
        assert "google_bigquery_table" in res
        ds = next(iter(res["google_bigquery_dataset"].values()))
        assert ds["dataset_id"] == "sales"
        tbl = next(iter(res["google_bigquery_table"].values()))
        assert tbl["table_id"] == "orders"
        assert tbl["deletion_protection"] is False
        by_name = {f["name"]: f for f in json.loads(tbl["schema"])}
        assert by_name["id"]["type"] == "INT64"
        assert by_name["id"]["mode"] == "REQUIRED"
        assert by_name["note"]["type"] == "STRING"
        assert by_name["note"]["mode"] == "NULLABLE"

    def test_table_cross_references_its_dataset(self):
        res = _gcp().emit(
            _contract(
                [
                    {
                        "exposeId": "t",
                        "binding": {
                            "format": "bigquery_table",
                            "location": {"dataset": "d", "table": "t"},
                        },
                    }
                ]
            )
        )
        ds_name = next(iter(res["google_bigquery_dataset"]))
        tbl = next(iter(res["google_bigquery_table"].values()))
        assert tbl["dataset_id"] == f"${{google_bigquery_dataset.{ds_name}.dataset_id}}"

    def test_view_emits_view_block_not_schema(self):
        res = _gcp().emit(
            _contract(
                [
                    {
                        "exposeId": "v",
                        "binding": {
                            "format": "bigquery_view",
                            "location": {"dataset": "d", "view": "v", "query": "SELECT 1"},
                        },
                    }
                ]
            )
        )
        tbl = next(iter(res["google_bigquery_table"].values()))
        assert tbl["view"]["query"] == "SELECT 1"
        assert tbl["view"]["use_legacy_sql"] is False
        assert "schema" not in tbl


class TestGcpStorageAndPubsub:
    def test_gcs_bucket(self):
        res = _gcp().emit(
            _contract(
                [
                    {
                        "exposeId": "b",
                        "binding": {
                            "format": "gcs_bucket",
                            "location": {"bucket": "my-bucket", "region": "EU"},
                        },
                    }
                ]
            )
        )
        bkt = next(iter(res["google_storage_bucket"].values()))
        assert bkt["name"] == "my-bucket"
        assert bkt["location"] == "EU"
        assert bkt["uniform_bucket_level_access"] is True

    def test_pubsub_topic(self):
        res = _gcp().emit(
            _contract(
                [
                    {
                        "exposeId": "e",
                        "binding": {"format": "pubsub_topic", "location": {"topic": "events"}},
                    }
                ]
            )
        )
        topic = next(iter(res["google_pubsub_topic"].values()))
        assert topic["name"] == "events"


class TestGcpModuleOutput:
    def test_empty_contract_yields_empty_resources(self):
        doc = json.loads(build_module(_gcp(), _contract([])))
        assert doc["resource"] == {}

    def test_non_gcp_formats_are_skipped(self):
        c = _contract(
            [{"exposeId": "x", "binding": {"format": "parquet", "location": {"table": "t"}}}]
        )
        assert _gcp().emit(c) == {}

    def test_output_is_canonical(self):
        c = _contract(
            [
                {
                    "exposeId": "t",
                    "binding": {
                        "format": "bigquery_table",
                        "location": {"dataset": "d", "table": "t"},
                    },
                }
            ]
        )
        text = build_module(_gcp(), c)
        assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"

    def test_required_providers_declares_google(self):
        doc = json.loads(build_module(_gcp(), _contract([])))
        assert doc["terraform"]["required_providers"]["google"]["source"] == "hashicorp/google"
