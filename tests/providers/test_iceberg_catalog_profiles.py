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

"""PR7 — REST + GCP (and Azure) catalog profiles in the resolver: the FileIO
follows the warehouse scheme, and the catalog type follows the catalog kind."""

from __future__ import annotations

import pytest

from fluid_build.build_runners.kafka_connect.iceberg_sink import emit_iceberg_sink_config
from fluid_build.providers._iceberg_catalog import (
    ADLS_FILE_IO,
    GCS_FILE_IO,
    S3_FILE_IO,
    _io_impl_for_warehouse,
    resolve_iceberg_catalog,
)

pytestmark = [pytest.mark.unit]


class _Sink:
    def __init__(self, catalog=None, partition_by=None):
        self.catalog = catalog
        self.partition_by = partition_by or []


# ── warehouse scheme -> FileIO ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "warehouse, expected",
    [
        ("s3://b/wh/", S3_FILE_IO),
        ("s3a://b/wh/", S3_FILE_IO),
        ("gs://b/wh/", GCS_FILE_IO),
        ("gcs://b/wh/", GCS_FILE_IO),
        ("abfss://c@acct.dfs.core.windows.net/wh", ADLS_FILE_IO),
        ("", None),
        ("my-catalog-name", None),  # a REST catalog NAME, not an object-store URI
    ],
)
def test_io_impl_for_warehouse(warehouse, expected):
    assert _io_impl_for_warehouse(warehouse) == expected


# ── GCP profile: gs:// warehouse -> GCSFileIO ───────────────────────────────


def test_resolve_gcp_rest_catalog_uses_gcs_fileio():
    binding = {
        "platform": "gcp",
        "location": {
            "database": "analytics",
            "table": "events",
            "catalog": "rest",
            "uri": "https://catalog.example/api",
            "warehouse": "gs://lake/warehouse/",
        },
    }
    r = resolve_iceberg_catalog(binding)
    assert r.catalog_type == "rest"
    assert r.io_impl == GCS_FILE_IO
    assert r.warehouse == "gs://lake/warehouse/"
    assert r.uri == "https://catalog.example/api"
    assert r.fq_table == "analytics.events"


def test_resolve_azure_warehouse_uses_adls_fileio():
    binding = {
        "platform": "azure",
        "location": {
            "database": "d",
            "table": "t",
            "catalog": "rest",
            "uri": "https://x/api",
            "warehouse": "abfss://c@a.dfs.core.windows.net/wh",
        },
    }
    assert resolve_iceberg_catalog(binding).io_impl == ADLS_FILE_IO


# ── catalog kinds map to the runtime catalog_type ───────────────────────────


@pytest.mark.parametrize(
    "catalog, expected_type",
    [
        ("rest", "rest"),
        ("nessie", "nessie"),
        ("hive", "hive"),
        ("polaris", "rest"),  # REST-fronted -> rest
        ("unity", "rest"),
        ("snowflake-managed", "rest"),
    ],
)
def test_catalog_kind_maps_to_catalog_type(catalog, expected_type):
    binding = {
        "platform": "local",
        "location": {"database": "d", "table": "t", "catalog": catalog, "warehouse": "gs://b/wh/"},
    }
    assert (
        resolve_iceberg_catalog(binding, sink=_Sink(catalog=catalog)).catalog_type == expected_type
    )


# ── glue is unchanged (regression) ──────────────────────────────────────────


def test_glue_still_resolves_s3_fileio():
    binding = {
        "platform": "aws",
        "location": {"database": "s", "table": "o", "bucket": "lake", "region": "us-east-1"},
    }
    r = resolve_iceberg_catalog(binding)
    assert r.catalog_type == "glue"
    assert r.io_impl == S3_FILE_IO
    assert r.catalog_impl == "org.apache.iceberg.aws.glue.GlueCatalog"


# ── end-to-end: the deriver emits the GCP catalog config ────────────────────


def test_deriver_emits_gcp_catalog_config():
    binding = {
        "platform": "gcp",
        "location": {
            "database": "analytics",
            "table": "events",
            "catalog": "rest",
            "uri": "https://catalog.example/api",
            "warehouse": "gs://lake/warehouse/",
        },
    }
    resolved = resolve_iceberg_catalog(binding)
    cfg = emit_iceberg_sink_config(resolved, product_id="analytics.events", topics=["events"])
    assert cfg["iceberg.catalog.type"] == "rest"
    assert cfg["iceberg.catalog.io-impl"] == GCS_FILE_IO
    assert cfg["iceberg.catalog.uri"] == "https://catalog.example/api"
    assert cfg["iceberg.catalog.warehouse"] == "gs://lake/warehouse/"
    assert cfg["iceberg.tables"] == "analytics.events"
