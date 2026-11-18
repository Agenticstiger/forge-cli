# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An expose's Airflow asset URI is the AIP-60 URI its provider's ``create_asset`` builds."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.providers.aws.util.warehouse import get_iceberg_warehouse
from fluid_build.schedulers.airflow._assets import (
    MAX_URI,
    NotAddressable,
    expose_asset_uri,
    normalize_dataset_uri,
)


def _expose(platform: str, **location: Any) -> Dict[str, Any]:
    return {"exposeId": "e", "binding": {"platform": platform, "location": location}}


@pytest.mark.parametrize(
    "expose, uri",
    [
        (_expose("aws", bucket="lake", path="iceberg/curated/t/"), "s3://lake/iceberg/curated/t"),
        (_expose("aws", bucket="lake", path="/a/dt=2026-01-01/"), "s3://lake/a/dt=2026-01-01"),
        (_expose("aws", bucket="lake", database="raw", table="events"), "s3://lake/raw/events"),
        (
            _expose("gcp", project="prj-1", dataset="sales", table="orders"),
            "bigquery://prj-1/sales/orders",
        ),
        (_expose("gcp", bucket="b", path="landing/x"), "gs://b/landing/x"),
        (
            _expose(
                "snowflake", account="xy12345.eu-central-1", database="DB", schema="S", table="T"
            ),
            "snowflake://xy12345.eu-central-1/DB/S/T",
        ),
        (
            _expose(
                "databricks",
                host="dbc-1.cloud.databricks.com",
                database="main",
                schema="s",
                table="t",
            ),
            "databricks://dbc-1.cloud.databricks.com/main/s/t",
        ),
        (
            _expose("postgres", host="db", database="d", schema="s", table="t"),
            "postgres://db:5432/d/s/t",
        ),
        (
            _expose("pgvector", host="db:6543", database="d", schema="s", table="t"),
            "postgres://db:6543/d/s/t",
        ),
        (_expose("kafka", host="b1:9092,b2:9092", topic="orders.v1"), "kafka://b1:9092/orders.v1"),
        (_expose("local", path="/data/out/x.parquet"), "file:///data/out/x.parquet"),
    ],
)
def test_each_platform_is_named_as_its_airflow_provider_names_it(expose, uri):
    assert expose_asset_uri(expose) == (uri, "")
    # Already in the form Airflow normalises a URI to.
    assert normalize_dataset_uri(uri) == uri


def test_an_aws_table_lives_where_the_warehouse_writer_puts_it():
    location = {"bucket": "lake", "database": "raw", "table": "events"}
    uri, _ = expose_asset_uri({"binding": {"platform": "aws", "location": location}})
    assert get_iceberg_warehouse(location, account_ref="unused").rstrip("/") == uri


@pytest.mark.parametrize(
    "expose, why",
    [
        (_expose("aws", database="raw", table="events"), "has no location.bucket"),
        (
            _expose("aws", bucket="{{ env.BUCKET }}", path="x"),
            "filled in on the worker at run time",
        ),
        (_expose("aws", bucket="lake", path="a/../b"), "'.' or '..' segment"),
        (_expose("aws", bucket="lake", path="a b"), "an asset URI would have to escape"),
        (_expose("gcp", project="p", table="t"), "it has no location.dataset"),
        (_expose("snowflake", database="D", schema="S", table="T"), "it has no location.account"),
        (_expose("databricks", database="main", schema="s", table="t"), "it has no location.host"),
        (_expose("kafka", host="b1:9092", topic="bad topic"), "is not a Kafka topic name"),
        (_expose("local", path="./out/x.parquet"), "relative to the project directory"),
        (_expose("local", path="/"), "names no file"),
        (_expose("azure", path="x"), "no Airflow provider names a 'azure' binding"),
        ({"binding": {}}, "its binding names no platform"),
    ],
)
def test_an_expose_no_uri_can_name_says_why(expose, why):
    uri, reason = expose_asset_uri(expose)
    assert uri is None and why in reason


def test_a_uri_longer_than_airflow_takes_names_nothing():
    uri, reason = expose_asset_uri(_expose("aws", bucket="lake", path="/".join(["a" * 200] * 8)))
    assert uri is None and str(MAX_URI) in reason


@pytest.mark.parametrize(
    "raw, normalized",
    [
        ("S3://lake/x/", "s3://lake/x"),
        ("postgresql://db/d/s/t", "postgres://db:5432/d/s/t"),
        ("dataset://gcp/project/dataset/table", "dataset://gcp/project/dataset/table"),
        ("x-team://anything/goes?here#too", "x-team://anything/goes?here#too"),
        ("orders_ready", "orders_ready"),
        ("kafka://b:9092/topic", "kafka://b:9092/topic"),
        ("postgres://db:/d/s/t", "postgres://db:5432/d/s/t"),
    ],
)
def test_a_dataset_uri_is_normalised_as_airflow_does(raw, normalized):
    assert normalize_dataset_uri(raw) == normalized


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "s3://",
        "s3:///key",
        "gs://",
        "snowflake://acct/db/schema",
        "databricks://h/c/s/t/extra",
        "postgres://db/d//t",
        "postgres://db:abc/d/s/t",
        "kafka://b:9092",
        "file://",
        "airflow://x",
        "AIRFLOW://x",
        "s3://u@b/k",
        "s3://b/k#frag",
        "s3://b/k\\x",
        "s3://b/k é",
        "s3://b/{{ env.X }}",
        "s3://b/${X}",
        "x" * (MAX_URI + 1),
    ],
)
def test_a_dataset_uri_airflow_would_refuse_or_misread_is_refused(raw):
    with pytest.raises(NotAddressable):
        normalize_dataset_uri(raw)


def test_a_non_string_dataset_is_refused():
    with pytest.raises(NotAddressable, match="non-empty string"):
        normalize_dataset_uri(None)
