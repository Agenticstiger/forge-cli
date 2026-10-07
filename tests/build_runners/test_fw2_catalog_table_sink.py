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

"""The derived sink configs carry what DynamoDB, JDBC and BigQuery need to start.

CON-3: the derived dynamodb and jdbc configs had no warehouse, which both
catalogs refuse at initialise, yet validate and the preflight passed. CON-4:
the derived bigquery config had no ``gcp.bigquery.project-id`` (required at
initialise), no warehouse, and an AWS-only ``client.region``. Property names
are from apache/iceberg at apache-iceberg-1.10.0.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from fluid_build.build_runners.debezium.iceberg_sink import emit_debezium_iceberg_sink_config
from fluid_build.build_runners.kafka_connect.iceberg_sink import emit_iceberg_sink_config
from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
    validate_iceberg_sink,
)
from fluid_build.providers._iceberg_catalog import resolve_iceberg_catalog

pytestmark = [pytest.mark.unit]

_JDBC_URI = "jdbc:postgresql://pg:5432/iceberg"


def _binding(catalog: str, platform: str = "aws", **location: Any) -> Dict[str, Any]:
    loc = {"catalog": catalog, "database": "streaming", "table": "orders", **location}
    return {"platform": platform, "format": "iceberg", "location": loc}


def _contract(
    binding: Dict[str, Any], *, engine: str = "kafka-connect", props: Optional[Dict] = None
) -> Dict[str, Any]:
    build_props: Dict[str, Any] = {
        "source": {"kind": "postgres", "mode": "incremental_append"},
        "sink": {"format": "iceberg"},
    }
    if engine == "debezium":
        build_props["debezium"] = props or {
            "deployment": {"mode": "embedded"},
            "server": {"sink": {}},
        }
    else:
        build_props["kafka-connect"] = props or {}
    return {
        "id": "bronze.orders_stream",
        "builds": [{"id": "ingest", "engine": engine, "properties": build_props}],
        "exposes": [{"exposeId": "orders", "kind": "table", "binding": binding}],
    }


def _kc(binding: Dict[str, Any]) -> Dict[str, str]:
    resolved = resolve_iceberg_catalog(binding, account_ref="123456789012")
    return emit_iceberg_sink_config(resolved, product_id="bronze.orders_stream", topics=["t"])


def _dbz(binding: Dict[str, Any]) -> Dict[str, str]:
    return emit_debezium_iceberg_sink_config(
        resolve_iceberg_catalog(binding, account_ref="123456789012")
    )


# ── CON-3: dynamodb / jdbc ──────────────────────────────────────────────────

_DERIVABLE = [
    _binding("dynamodb", bucket="acme-lake", region="eu-west-1"),
    _binding("jdbc", bucket="acme-lake", region="eu-west-1", uri=_JDBC_URI),
]


@pytest.mark.parametrize("binding", _DERIVABLE, ids=["dynamodb", "jdbc"])
def test_both_runtimes_emit_the_bucket_derived_warehouse(binding):
    assert _kc(binding)["iceberg.catalog.warehouse"] == "s3://acme-lake/streaming/orders/"
    assert _dbz(binding)["warehouse"] == "s3://acme-lake/streaming/orders/"


@pytest.mark.parametrize("binding", _DERIVABLE, ids=["dynamodb", "jdbc"])
@pytest.mark.parametrize("engine", ["kafka-connect", "debezium"])
def test_a_derivable_warehouse_validates_clean(binding, engine):
    contract = _contract(binding, engine=engine)
    assert validate_iceberg_sink(contract)[0] == []
    assert iceberg_sink_preflight(contract, "ingest") is None


@pytest.mark.parametrize(
    "binding",
    [
        _binding("dynamodb", region="eu-west-1"),
        _binding("jdbc", region="eu-west-1", uri=_JDBC_URI),
        # no scheme to derive with off aws / gcp
        _binding("dynamodb", "local", bucket="acme-lake"),
    ],
    ids=["dynamodb", "jdbc", "dynamodb-local"],
)
@pytest.mark.parametrize("engine", ["kafka-connect", "debezium"])
def test_no_derivable_warehouse_is_a_hard_error_and_the_preflight_refuses(binding, engine):
    kind = binding["location"]["catalog"]
    contract = _contract(binding, engine=engine)
    errors = validate_iceberg_sink(contract)[0]
    hit = [e for e in errors if f"{kind} catalog requires binding.location.warehouse" in e]
    assert len(hit) == 1, errors
    assert "binding.location.bucket" in hit[0]
    refusal = iceberg_sink_preflight(contract, "ingest")
    assert refusal is not None and "binding.location.warehouse" in refusal


def test_jdbc_still_requires_its_uri():
    errors = validate_iceberg_sink(_contract(_binding("jdbc", bucket="acme-lake")))[0]
    assert any("jdbc catalog requires binding.location.uri" in e for e in errors), errors


def test_an_explicit_warehouse_satisfies_dynamodb_off_aws():
    binding = _binding("dynamodb", "local", warehouse="s3://wh/root")
    assert validate_iceberg_sink(_contract(binding))[0] == []
    assert _kc(binding)["iceberg.catalog.warehouse"] == "s3://wh/root"


# ── CON-4: bigquery ─────────────────────────────────────────────────────────


def _bq(**location: Any) -> Dict[str, Any]:
    return _binding("bigquery", "gcp", **location)


_FULL_BQ = _bq(project="acme-proj", region="eu", bucket="acme-lake")


def test_kafka_connect_bigquery_config_carries_project_location_and_warehouse():
    cfg = _kc(_FULL_BQ)
    assert cfg["iceberg.catalog.type"] == "bigquery"
    assert cfg["iceberg.catalog.gcp.bigquery.project-id"] == "acme-proj"
    assert cfg["iceberg.catalog.gcp.bigquery.location"] == "eu"
    assert cfg["iceberg.catalog.warehouse"] == "gs://acme-lake"
    assert "iceberg.catalog.client.region" not in cfg


def test_debezium_bigquery_config_carries_project_location_and_warehouse():
    cfg = _dbz(_FULL_BQ)
    assert cfg["type"] == "bigquery"
    assert cfg["gcp.bigquery.project-id"] == "acme-proj"
    assert cfg["gcp.bigquery.location"] == "eu"
    assert cfg["warehouse"] == "gs://acme-lake"
    assert "client.region" not in cfg


def test_an_operator_override_still_wins_over_a_derived_bigquery_key():
    resolved = resolve_iceberg_catalog(_FULL_BQ)
    cfg = emit_iceberg_sink_config(
        resolved,
        product_id="p",
        topics=["t"],
        kc_props={"iceberg_catalog_overrides": {"iceberg.catalog.gcp.bigquery.location": "us"}},
    )
    assert cfg["iceberg.catalog.gcp.bigquery.location"] == "us"


@pytest.mark.parametrize("engine", ["kafka-connect", "debezium"])
def test_bigquery_without_project_is_a_hard_error_and_the_preflight_refuses(engine):
    contract = _contract(_bq(bucket="acme-lake"), engine=engine)
    errors = validate_iceberg_sink(contract)[0]
    hit = [e for e in errors if "bigquery catalog requires binding.location.project" in e]
    assert len(hit) == 1, errors
    assert "gcp.bigquery.project-id" in hit[0]
    refusal = iceberg_sink_preflight(contract, "ingest")
    assert refusal is not None and "binding.location.project" in refusal


def _warehouse_warnings(contract: Dict[str, Any]):
    return [w for w in validate_iceberg_sink(contract)[1] if "no gs:// warehouse" in w]


@pytest.mark.parametrize("engine", ["kafka-connect", "debezium"])
def test_bigquery_with_no_derivable_warehouse_warns(engine):
    contract = _contract(_bq(project="acme-proj"), engine=engine)
    errors = validate_iceberg_sink(contract)[0]
    assert errors == []
    hit = _warehouse_warnings(contract)
    assert len(hit) == 1
    assert "default storage location URI" in hit[0]
    assert ("debezium.sink.iceberg.warehouse" in hit[0]) == (engine == "debezium")


def test_bigquery_with_a_bucket_does_not_warn_about_the_warehouse():
    assert _warehouse_warnings(_contract(_FULL_BQ)) == []


@pytest.mark.parametrize(
    "engine, props",
    [
        (
            "kafka-connect",
            {"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "gs://ops/wh"}},
        ),
        (
            "debezium",
            {
                "deployment": {"mode": "embedded"},
                "server": {
                    "sink": {"iceberg_sink_enabled": True, "config": {"warehouse": "gs://o"}}
                },
            },
        ),
    ],
)
def test_an_override_warehouse_silences_the_bigquery_warning(engine, props):
    contract = _contract(_bq(project="acme-proj"), engine=engine, props=props)
    assert _warehouse_warnings(contract) == []


def test_runtime_version_warning_names_the_failure_and_the_remedy():
    warnings = validate_iceberg_sink(_contract(_FULL_BQ))[1]
    hit = [w for w in warnings if "iceberg.catalog.type=bigquery" in w]
    assert len(hit) == 1
    assert "on that sink the connector fails at start" in hit[0]
    assert "Run a sink built from Iceberg >= 1.10" in hit[0]
