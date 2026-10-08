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

"""The catalog-kind table against what each Iceberg catalog needs to start.

CON-3: Iceberg's DynamoDbCatalog and JdbcCatalog refuse to initialise without
a warehouse (apache-iceberg-1.10.0 DynamoDbCatalog.java:132-134,
JdbcCatalog.java:117-119), so the resolver derives one from an explicit
``location.bucket``. CON-4: BigQueryMetastoreCatalog refuses to initialise
without ``gcp.bigquery.project-id`` (BigQueryMetastoreCatalog.java:84-87).
CON-5: a ``platform: confluent`` expose with no catalog is the Glue table the
Tableflow IaC publishes.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from fluid_build.providers._iceberg_catalog import (
    BIGQUERY_LOCATION,
    BIGQUERY_PROJECT_ID,
    binding_catalog_kind,
    catalog_kind_info,
    default_catalog_kind,
    is_glue_cataloged,
    resolve_iceberg_catalog,
)

pytestmark = [pytest.mark.unit]


def _binding(catalog: Optional[str], platform: str = "aws", **location: Any) -> Dict[str, Any]:
    loc: Dict[str, Any] = {"database": "streaming", "table": "orders", **location}
    if catalog is not None:
        loc["catalog"] = catalog
    return {"platform": platform, "format": "iceberg", "location": loc}


# ── the rows: an independent pin of what each sink needs ────────────────────


@pytest.mark.parametrize(
    "kind, requires",
    [
        ("dynamodb", ("warehouse",)),
        ("jdbc", ("uri", "warehouse")),
        ("bigquery", ("project",)),
    ],
)
def test_sink_requires_matches_the_catalog_initialize_checks(kind, requires):
    assert catalog_kind_info(kind).sink_requires == requires


# ── CON-3: dynamodb / jdbc derive a warehouse from an explicit bucket ───────


@pytest.mark.parametrize(
    "kind, extra", [("dynamodb", {}), ("jdbc", {"uri": "jdbc:postgresql://pg:5432/iceberg"})]
)
@pytest.mark.parametrize(
    "platform, expected",
    [
        ("aws", "s3://acme-lake/streaming/orders/"),
        ("s3", "s3://acme-lake/streaming/orders/"),  # an aws alias
        ("gcp", "gs://acme-lake/streaming/orders/"),
    ],
)
def test_bucket_derives_the_warehouse_with_the_platform_scheme(kind, extra, platform, expected):
    r = resolve_iceberg_catalog(_binding(kind, platform, bucket="acme-lake", **extra))
    assert r.warehouse == expected


def test_derived_warehouse_honours_location_path():
    r = resolve_iceberg_catalog(_binding("dynamodb", bucket="acme-lake", path="/lake/orders"))
    assert r.warehouse == "s3://acme-lake/lake/orders"


def test_explicit_warehouse_beats_the_bucket():
    r = resolve_iceberg_catalog(_binding("dynamodb", bucket="acme-lake", warehouse="s3://other/wh"))
    assert r.warehouse == "s3://other/wh"


@pytest.mark.parametrize(
    "binding",
    [
        # no bucket: never the account-derived bucket the Glue row falls back to
        _binding("dynamodb"),
        _binding("jdbc", uri="jdbc:postgresql://pg:5432/iceberg"),
        # an unresolved template is not a bucket either
        _binding("dynamodb", bucket="{{ env.FW2_NO_SUCH_BUCKET_VAR }}"),
        # a platform with no scheme to derive with
        _binding("jdbc", "local", bucket="acme-lake", uri="jdbc:sqlite::memory:"),
    ],
)
def test_no_explicit_bucket_derives_no_warehouse(binding, monkeypatch):
    monkeypatch.delenv("FW2_NO_SUCH_BUCKET_VAR", raising=False)
    assert resolve_iceberg_catalog(binding, account_ref="123456789012").warehouse == ""


def test_glue_keeps_its_account_fallback():
    r = resolve_iceberg_catalog(_binding("glue"), account_ref="123456789012")
    assert r.warehouse == "s3://123456789012-fluid-data/streaming/orders/"


@pytest.mark.parametrize("kind", ["hadoop", "hive", "nessie", "rest", "lakekeeper"])
def test_other_kinds_do_not_derive_from_the_bucket(kind):
    assert resolve_iceberg_catalog(_binding(kind, bucket="acme-lake")).warehouse == ""


# ── CON-4: bigquery gets its project, location and gs:// warehouse ──────────


def _bq(**location: Any) -> Dict[str, Any]:
    return _binding("bigquery", "gcp", **location)


def test_bigquery_maps_project_and_region_to_its_own_properties():
    r = resolve_iceberg_catalog(_bq(project="acme-proj", region="eu", bucket="acme-lake"))
    assert dict(r.extra_catalog_props) == {
        BIGQUERY_PROJECT_ID: "acme-proj",
        BIGQUERY_LOCATION: "eu",
    }
    assert BIGQUERY_PROJECT_ID == "gcp.bigquery.project-id"
    assert BIGQUERY_LOCATION == "gcp.bigquery.location"
    # client.region is an AWS client property
    assert r.region is None


def test_bigquery_without_region_sets_no_location():
    r = resolve_iceberg_catalog(_bq(project="acme-proj"))
    assert dict(r.extra_catalog_props) == {BIGQUERY_PROJECT_ID: "acme-proj"}


@pytest.mark.parametrize(
    "location, warehouse",
    [
        ({"bucket": "acme-lake"}, "gs://acme-lake"),
        ({"bucket": "acme-lake", "path": "iceberg"}, "gs://acme-lake/iceberg"),
        ({"warehouse": "gs://wh-bucket/root", "bucket": "acme-lake"}, "gs://wh-bucket/root"),
        # a foreign scheme is storage the GCP IaC never creates
        ({"warehouse": "s3://elsewhere"}, ""),
        ({}, ""),
    ],
)
def test_bigquery_warehouse_is_the_gs_storage_uri(location, warehouse):
    r = resolve_iceberg_catalog(_bq(project="acme-proj", **location))
    assert r.warehouse == warehouse
    assert (r.io_impl is not None) == bool(warehouse)


# ── CON-5: confluent's default catalog is Glue, as Tableflow publishes ──────


def test_confluent_default_catalog_is_glue():
    binding = {"platform": "confluent", "format": "iceberg", "location": {"database": "d"}}
    assert default_catalog_kind(binding) == "glue"
    assert binding_catalog_kind(binding) == "glue"
    assert is_glue_cataloged(binding)


@pytest.mark.parametrize(
    "platform, expected",
    [("aws", "glue"), ("snowflake", "snowflake-managed"), ("gcp", "rest"), ("local", "rest")],
)
def test_other_platform_defaults_are_unchanged(platform, expected):
    assert default_catalog_kind({"platform": platform}) == expected


def test_confluent_explicit_catalog_still_wins():
    binding = {"platform": "confluent", "location": {"catalog": "lakekeeper"}}
    assert binding_catalog_kind(binding) == "lakekeeper"
