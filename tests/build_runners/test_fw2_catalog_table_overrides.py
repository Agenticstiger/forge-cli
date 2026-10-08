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

"""An override or a hand-written config can supply what a catalog needs to start.

The DynamoDB / JDBC warehouse and the BigQuery ``gcp.bigquery.project-id`` are
hard requirements of the sink, but the runner merges operator maps into the
config it pushes, so a key set there reaches the worker. Validate and the
preflight used to read only ``binding.location`` for them and refused configs
whose pushed form carried the key. The prefix is the runtime's: Kafka Connect
reads ``iceberg.catalog.<prop>``, Debezium Server takes the bare ``<prop>``.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from fluid_build.build_runners.kafka_connect.iceberg_sink import emit_iceberg_sink_config
from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
    validate_iceberg_sink,
)
from fluid_build.providers._iceberg_catalog import DYNAMODB_CATALOG_IMPL, resolve_iceberg_catalog

pytestmark = [pytest.mark.unit]

_JDBC_URI = "jdbc:postgresql://pg:5432/iceberg"


def _binding(catalog: str, platform: str = "aws", **location: Any) -> Dict[str, Any]:
    loc = {"catalog": catalog, "database": "streaming", "table": "orders", **location}
    return {"platform": platform, "format": "iceberg", "location": loc}


def _kc_contract(binding: Dict[str, Any], kc: Dict[str, Any]) -> Dict[str, Any]:
    return _contract(binding, "kafka-connect", kc)


def _dbz_contract(binding: Dict[str, Any], server_sink: Dict[str, Any]) -> Dict[str, Any]:
    return _contract(
        binding, "debezium", {"deployment": {"mode": "embedded"}, "server": {"sink": server_sink}}
    )


def _contract(binding: Dict[str, Any], engine: str, engine_props: Dict[str, Any]):
    return {
        "id": "bronze.orders_stream",
        "builds": [
            {
                "id": "ingest",
                "engine": engine,
                "properties": {
                    "source": {"kind": "postgres", "mode": "incremental_append"},
                    "sink": {"format": "iceberg"},
                    engine: engine_props,
                },
            }
        ],
        "exposes": [{"exposeId": "orders", "kind": "table", "binding": binding}],
    }


def _accepted(contract: Dict[str, Any]) -> None:
    errors = validate_iceberg_sink(contract)[0]
    assert errors == [], errors
    assert iceberg_sink_preflight(contract, "ingest") is None


def _refused(contract: Dict[str, Any], needle: str) -> None:
    errors = validate_iceberg_sink(contract)[0]
    assert any(needle in e for e in errors), errors
    refusal = iceberg_sink_preflight(contract, "ingest")
    assert refusal is not None and needle in refusal


_NO_WH = {
    "dynamodb": _binding("dynamodb", region="eu-west-1"),
    "jdbc": _binding("jdbc", region="eu-west-1", uri=_JDBC_URI),
}


# ── warehouse: DynamoDB / JDBC ──────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["dynamodb", "jdbc"])
def test_kafka_connect_catalog_override_warehouse_is_accepted(kind):
    kc = {"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "s3://ops/wh"}}
    _accepted(_kc_contract(_NO_WH[kind], kc))
    # ...and it is what the runner pushes.
    resolved = resolve_iceberg_catalog(_NO_WH[kind], account_ref="")
    cfg = emit_iceberg_sink_config(resolved, product_id="p", topics=["t"], kc_props=kc)
    assert cfg["iceberg.catalog.warehouse"] == "s3://ops/wh"


def test_kafka_connect_hand_written_config_with_a_warehouse_is_accepted():
    kc = {
        "sink_connector_config": {
            "connector.class": "x",
            "iceberg.catalog.catalog-impl": DYNAMODB_CATALOG_IMPL,
            "iceberg.catalog.warehouse": "s3://ops/wh",
        }
    }
    _accepted(_kc_contract(_NO_WH["dynamodb"], kc))


@pytest.mark.parametrize("kind", ["dynamodb", "jdbc"])
@pytest.mark.parametrize("enabled", [True, False], ids=["derived", "hand-written"])
def test_debezium_server_sink_config_warehouse_is_accepted(kind, enabled):
    server_sink = {"iceberg_sink_enabled": enabled, "config": {"warehouse": "s3://ops/wh"}}
    if not enabled and kind == "dynamodb":
        server_sink["config"]["catalog-impl"] = DYNAMODB_CATALOG_IMPL
    if not enabled and kind == "jdbc":
        server_sink["config"]["type"] = "jdbc"
    _accepted(_dbz_contract(_NO_WH[kind], server_sink))


@pytest.mark.parametrize(
    "kc",
    [
        {"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": ""}},
        # Kafka Connect does not read a bare ``warehouse`` as a catalog property
        {"iceberg_catalog_overrides": {"warehouse": "s3://ops/wh"}},
        {"iceberg_catalog_overrides": {"iceberg.catalog.client.region": "eu-west-1"}},
    ],
    ids=["empty", "unprefixed", "other-key"],
)
def test_an_override_that_sets_no_catalog_warehouse_is_still_refused(kc):
    _refused(
        _kc_contract(_NO_WH["dynamodb"], kc), "dynamodb catalog requires binding.location.warehouse"
    )


def test_debezium_prefixed_warehouse_does_not_count():
    server_sink = {"iceberg_sink_enabled": True, "config": {"iceberg.catalog.warehouse": "s3://o"}}
    _refused(
        _dbz_contract(_NO_WH["dynamodb"], server_sink),
        "dynamodb catalog requires binding.location.warehouse",
    )


def test_an_override_warehouse_does_not_satisfy_the_jdbc_uri():
    binding = _binding("jdbc", region="eu-west-1")
    kc = {"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "s3://ops/wh"}}
    errors = validate_iceberg_sink(_kc_contract(binding, kc))[0]
    assert [e for e in errors if "requires binding.location" in e] == [
        "iceberg sink (build 'ingest'): jdbc catalog requires binding.location.uri"
    ]


def test_the_refusal_names_the_override_remedy():
    errors = validate_iceberg_sink(_kc_contract(_NO_WH["dynamodb"], {}))[0]
    hit = [e for e in errors if "requires binding.location.warehouse" in e]
    assert len(hit) == 1 and "in an override" in hit[0]


# ── project: BigQuery ───────────────────────────────────────────────────────

_BQ_NO_PROJECT = _binding("bigquery", "gcp", bucket="lake")


def test_kafka_connect_catalog_override_project_is_accepted():
    kc = {"iceberg_catalog_overrides": {"iceberg.catalog.gcp.bigquery.project-id": "p"}}
    _accepted(_kc_contract(_BQ_NO_PROJECT, kc))


def test_kafka_connect_hand_written_project_is_accepted():
    kc = {
        "sink_connector_config": {
            "iceberg.catalog.type": "bigquery",
            "iceberg.catalog.gcp.bigquery.project-id": "p",
        }
    }
    _accepted(_kc_contract(_BQ_NO_PROJECT, kc))


def test_debezium_server_sink_config_project_is_accepted():
    server_sink = {"iceberg_sink_enabled": True, "config": {"gcp.bigquery.project-id": "p"}}
    _accepted(_dbz_contract(_BQ_NO_PROJECT, server_sink))


@pytest.mark.parametrize(
    "overrides",
    [{"iceberg.catalog.gcp.bigquery.project-id": ""}, {"gcp.bigquery.project-id": "p"}],
    ids=["empty", "unprefixed"],
)
def test_an_override_that_sets_no_project_is_still_refused(overrides):
    _refused(
        _kc_contract(_BQ_NO_PROJECT, {"iceberg_catalog_overrides": overrides}),
        "bigquery catalog requires binding.location.project",
    )


# ── the BigQuery no-warehouse warning ───────────────────────────────────────


def _warehouse_warning(contract: Dict[str, Any]) -> str:
    hit = [w for w in validate_iceberg_sink(contract)[1] if "no gs:// warehouse" in w]
    assert len(hit) == 1, hit
    return hit[0]


def test_kafka_connect_warning_says_auto_create_fails_even_in_an_existing_dataset():
    # apache-iceberg-1.10.0: IcebergWriterFactory.autoCreateTable calls
    # createNamespace before createTable, and BigQueryMetastoreCatalog's
    # createNamespace refuses without a warehouse.
    binding = _binding("bigquery", "gcp", project="acme-proj", region="EU")
    warning = _warehouse_warning(_kc_contract(binding, {"streamingSink": {"autoCreate": True}}))
    assert "createNamespace" in warning
    assert "table auto-creation fails even in a dataset that exists" in warning
    assert "default storage location URI" not in warning


def test_debezium_warning_says_the_server_refuses_to_boot():
    # memiiso/debezium-server-iceberg 1.2.0.Final IcebergConfig.java:44-45
    # declares debezium.sink.iceberg.warehouse as a String with no default, so
    # the server does not start without one, whether or not the tables exist.
    binding = _binding("bigquery", "gcp", project="acme-proj")
    warning = _warehouse_warning(_dbz_contract(binding, {}))
    assert "refuses to boot" in warning
    assert "debezium.sink.iceberg.warehouse" in warning
    assert "default storage location URI" not in warning
    assert "createNamespace" not in warning
