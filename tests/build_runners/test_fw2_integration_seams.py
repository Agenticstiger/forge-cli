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

"""The sink-expose resolver and the catalog-kind derivation, composed.

The resolver picks the exposes a derived streaming sink writes from the
build's ``outputs``; the catalog table derives what each kind needs to start
(a DynamoDB / JDBC warehouse from the bucket, the BigQuery project and
location). Each was tested on its own. Here a build writes a DynamoDB expose
and another a BigQuery expose in one contract, an embedded Debezium build
writes two exposes of one JDBC or DynamoDB database, and a Confluent Tableflow
expose with no catalog sits beside a self-managed sink with access grants.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

from fluid_build.api.runner import RunState
from fluid_build.build_runners._acquisition_common import build_acquisition_run_context
from fluid_build.build_runners.debezium.runner import DebeziumRunner
from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
    validate_iceberg_sink,
)
from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build
from fluid_build.cli import policy_compile
from fluid_build.policy.compiler import compile_policy

pytestmark = [pytest.mark.unit]

_SOURCE = {
    "kind": "postgres",
    "connection": {"host": "db", "port": 5432, "database": "app", "user": "u", "password": "p"},
    "mode": "incremental_append",
    "watermark": {"strategy": "high_water_mark", "allowedLateness": "PT5M"},
}

_JDBC_URI = "jdbc:postgresql://catalog-db:5432/iceberg"


def _expose(eid: str, platform: str, **location: Any) -> Dict[str, Any]:
    return {
        "exposeId": eid,
        "kind": "table",
        "binding": {"platform": platform, "format": "iceberg", "location": location},
        "contract": {"schema": [{"name": "id", "type": "integer"}]},
    }


def _kc_build(bid: str, outputs: Optional[List[str]], stream: str) -> Dict[str, Any]:
    build: Dict[str, Any] = {
        "id": bid,
        "pattern": "acquisition",
        "engine": "kafka-connect",
        "capabilities": ["streaming"],
        "properties": {
            "source": {**copy.deepcopy(_SOURCE), "streams": [stream]},
            "sink": {"format": "iceberg"},
            "kafka-connect": {
                "deployment": {"server_url": "http://kafka-connect.test:8083"},
                "connector_name": f"src-{bid}",
            },
        },
    }
    if outputs is not None:
        build["outputs"] = outputs
    return build


def _dbz_build(outputs: Optional[List[str]]) -> Dict[str, Any]:
    build: Dict[str, Any] = {
        "id": "ingest",
        "pattern": "acquisition",
        "engine": "debezium",
        "capabilities": ["cdc", "streaming"],
        "properties": {
            "source": {
                **copy.deepcopy(_SOURCE),
                "mode": "cdc",
                "streams": ["public.orders", "public.refunds"],
            },
            "sink": {"format": "iceberg"},
            "debezium": {"deployment": {"mode": "embedded"}, "server": {"sink": {}}},
        },
    }
    if outputs is not None:
        build["outputs"] = outputs
    return build


def _contract(
    exposes: List[Dict[str, Any]],
    builds: List[Dict[str, Any]],
    *,
    grants: bool = False,
) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "sales.cdc",
        "name": "Sales CDC",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "exposes": copy.deepcopy(exposes),
        "builds": copy.deepcopy(builds),
    }
    if grants:
        doc["accessPolicy"] = {
            "grants": [{"principal": "group:analysts@example.com", "permissions": ["read"]}]
        }
    return doc


# ── a DynamoDB expose and a BigQuery expose, one Kafka Connect build each ────

DDB = _expose(
    "orders",
    "aws",
    database="sales",
    table="orders",
    bucket="lake",
    region="us-east-1",
    catalog="dynamodb",
)
BQ = _expose(
    "events",
    "gcp",
    database="analytics",
    table="events",
    bucket="gbkt",
    region="europe-west1",
    project="acme-proj",
    catalog="bigquery",
)


def _ddb_and_bq() -> Dict[str, Any]:
    return _contract(
        [DDB, BQ],
        [
            _kc_build("ingest_orders", ["orders"], "public.orders"),
            _kc_build("ingest_events", ["events"], "public.events"),
        ],
    )


def test_ddb_and_bq_builds_validate_against_their_own_catalogs():
    errors, warnings = validate_iceberg_sink(_ddb_and_bq())
    assert errors == []
    # the only warning is the BigQuery build's published-sink advisory
    assert len(warnings) == 1, warnings
    assert "build 'ingest_events'" in warnings[0]
    assert "iceberg.catalog.type=bigquery" in warnings[0]
    for bid in ("ingest_orders", "ingest_events"):
        assert iceberg_sink_preflight(_ddb_and_bq(), bid) is None


def test_ddb_build_pushes_the_dynamodb_catalog_with_its_derived_warehouse(
    kafka_connect_mock, tmp_path: Path
):
    contract = _ddb_and_bq()
    assert execute_kafka_connect_build(contract["builds"][0], contract, tmp_path) == 0
    sink = kafka_connect_mock.connectors["src-ingest_orders-sink"]["config"]
    assert sink["iceberg.tables"] == "sales.orders"
    assert sink["iceberg.catalog.catalog-impl"] == "org.apache.iceberg.aws.dynamodb.DynamoDbCatalog"
    assert sink["iceberg.catalog.warehouse"] == "s3://lake/sales/orders/"
    assert sink["iceberg.catalog.client.region"] == "us-east-1"
    assert not any(k.startswith("iceberg.catalog.gcp.") for k in sink)


def test_bq_build_pushes_the_bigquery_catalog_with_its_project_and_location(
    kafka_connect_mock, tmp_path: Path
):
    contract = _ddb_and_bq()
    assert execute_kafka_connect_build(contract["builds"][1], contract, tmp_path) == 0
    sink = kafka_connect_mock.connectors["src-ingest_events-sink"]["config"]
    assert sink["iceberg.tables"] == "analytics.events"
    assert sink["iceberg.catalog.type"] == "bigquery"
    assert sink["iceberg.catalog.warehouse"] == "gs://gbkt"
    assert sink["iceberg.catalog.gcp.bigquery.project-id"] == "acme-proj"
    assert sink["iceberg.catalog.gcp.bigquery.location"] == "europe-west1"
    assert "iceberg.catalog.client.region" not in sink
    assert "iceberg.catalog.catalog-impl" not in sink


def test_a_build_naming_both_kinds_is_refused_before_any_rest_call(
    kafka_connect_mock, tmp_path: Path
):
    contract = _contract([DDB, BQ], [_kc_build("ingest", ["orders", "events"], "public.orders")])
    errors, _ = validate_iceberg_sink(contract)
    assert any("name 2 of the Iceberg sink exposes" in e for e in errors), errors
    assert execute_kafka_connect_build(contract["builds"][0], contract, tmp_path) != 0
    assert kafka_connect_mock.connectors == {}


def test_bq_build_without_project_is_refused_even_when_the_first_expose_is_complete():
    no_project = copy.deepcopy(BQ)
    del no_project["binding"]["location"]["project"]
    contract = _contract(
        [DDB, no_project], [_kc_build("ingest_events", ["events"], "public.events")]
    )
    errors, _ = validate_iceberg_sink(contract)
    assert any("bigquery catalog requires binding.location.project" in e for e in errors), errors


# ── embedded Debezium over two exposes of one JDBC / DynamoDB database ──────


def _two_tables(kind: str, **extra: Any) -> List[Dict[str, Any]]:
    common: Dict[str, Any] = {
        "database": "sales",
        "bucket": "lake",
        "region": "us-east-1",
        "catalog": kind,
    }
    if kind == "jdbc":
        common["uri"] = _JDBC_URI
    common.update(extra)
    return [
        _expose("orders", "aws", table="orders", **common),
        _expose("refunds", "aws", table="refunds", **common),
    ]


def _props_path(contract: Dict[str, Any], tmp_path: Path) -> Path:
    return tmp_path / ".fluid" / "debezium" / contract["id"] / "ingest" / "application.properties"


@pytest.mark.parametrize("kind", ["jdbc", "dynamodb"])
@pytest.mark.parametrize("outputs", [["orders", "refunds"], None], ids=["both", "no-outputs"])
def test_debezium_writes_two_bucket_derived_exposes_of_one_database(tmp_path: Path, kind, outputs):
    # Each expose derives ``s3://lake/sales/<table>/``: a per-table prefix of
    # one bucket, as Glue's is, not two catalogs.
    contract = _contract(_two_tables(kind), [_dbz_build(outputs)])
    assert validate_iceberg_sink(contract) == ([], [])
    assert iceberg_sink_preflight(contract, "ingest") is None
    ctx = build_acquisition_run_context(contract["builds"][0], contract, tmp_path)
    DebeziumRunner().run(ctx)  # no server binary: fails after writing the config
    text = _props_path(contract, tmp_path).read_text()
    assert "debezium.sink.iceberg.table-namespace=sales" in text
    assert "debezium.sink.iceberg.warehouse=s3://lake/sales/orders/" in text


@pytest.mark.parametrize("kind", ["jdbc", "dynamodb"])
def test_debezium_refuses_bucket_derived_exposes_in_two_buckets(tmp_path: Path, kind):
    exposes = _two_tables(kind)
    exposes[1]["binding"]["location"]["bucket"] = "other-lake"
    contract = _contract(exposes, [_dbz_build(None)])
    message = "resolve to different catalogs (their warehouse differ)"
    errors, _ = validate_iceberg_sink(contract)
    assert any(message in e for e in errors), errors
    result = DebeziumRunner().run(
        build_acquisition_run_context(contract["builds"][0], contract, tmp_path)
    )
    assert result.state == RunState.FAILED
    assert message in (result.error or "")
    assert not _props_path(contract, tmp_path).exists()


def test_debezium_compares_a_warehouse_the_binding_sets_whole():
    exposes = _two_tables("jdbc")
    exposes[0]["binding"]["location"]["warehouse"] = "s3://lake/a/"
    exposes[1]["binding"]["location"]["warehouse"] = "s3://lake/b/"
    errors, _ = validate_iceberg_sink(_contract(exposes, [_dbz_build(None)]))
    assert any("their warehouse differ" in e for e in errors), errors
    exposes[1]["binding"]["location"]["warehouse"] = "s3://lake/a/"
    assert validate_iceberg_sink(_contract(exposes, [_dbz_build(None)])) == ([], [])


@pytest.mark.parametrize(
    "key, value, named",
    [("project", "other-proj", "project"), ("region", "us-central1", "region")],
)
def test_debezium_names_the_bigquery_setting_that_differs(key, value, named):
    # ``location.project`` / ``location.region`` reach the catalog as
    # gcp.bigquery.project-id / gcp.bigquery.location; the refusal names the
    # binding setting.
    first = copy.deepcopy(BQ)
    first["exposeId"] = "orders"
    second = copy.deepcopy(BQ)
    second["exposeId"] = "refunds"
    second["binding"]["location"]["table"] = "refunds"
    second["binding"]["location"][key] = value
    errors, _ = validate_iceberg_sink(_contract([first, second], [_dbz_build(None)]))
    assert any(f"resolve to different catalogs (their {named} differ)" in e for e in errors), errors


# ── a Confluent Tableflow expose with no catalog, beside a sink, with grants ─

TABLEFLOW = _expose(
    "tableflow_orders",
    "confluent",
    environment_id="env-123",
    kafka_cluster_id="lkc-123",
    topic="orders",
    database="sales_glue",
    bucket="acme-tableflow",
    confluent_role_arn="arn:aws:iam::123456789012:role/tableflow",
    region="eu-west-1",
)
GLUE = _expose("orders", "aws", database="sales", table="orders", bucket="lake", region="us-east-1")


def test_a_tableflow_expose_is_not_a_sink_candidate():
    # one self-managed Iceberg expose: a build with no outputs writes it
    contract = _contract([TABLEFLOW, GLUE], [_kc_build("ingest", None, "public.orders")])
    assert validate_iceberg_sink(contract) == ([], [])


def test_a_derived_sink_naming_only_the_tableflow_expose_is_refused(
    kafka_connect_mock, tmp_path: Path
):
    contract = _contract(
        [TABLEFLOW, GLUE], [_kc_build("ingest", ["tableflow_orders"], "public.orders")]
    )
    errors, _ = validate_iceberg_sink(contract)
    assert any(
        "outputs ['tableflow_orders'] name none of the Iceberg sink exposes" in e for e in errors
    ), errors
    assert execute_kafka_connect_build(contract["builds"][0], contract, tmp_path) != 0
    assert kafka_connect_mock.connectors == {}


def test_policy_compile_grants_both_glue_tables_and_prints_no_warning(tmp_path: Path, caplog):
    contract = _contract(
        [TABLEFLOW, GLUE], [_kc_build("ingest", ["orders"], "public.orders")], grants=True
    )
    bindings, warnings = compile_policy(contract)
    assert warnings == []
    assert [(b["provider"], b["resource_type"], b["resource_id"]) for b in bindings] == [
        ("aws", "s3.bucket", "acme-tableflow"),
        ("aws", "glue.table", "sales_glue"),
        ("aws", "s3.bucket", "lake"),
        ("aws", "glue.table", "sales.orders"),
    ]

    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract, sort_keys=False), encoding="utf-8")
    out = tmp_path / "bindings.json"
    logger = logging.getLogger("test.fw2.seams")
    caplog.set_level(logging.DEBUG, logger="test.fw2.seams")
    assert policy_compile.run(argparse.Namespace(contract=str(path), out=str(out)), logger) == 0
    assert json.loads(out.read_text())["bindings"] == bindings
    shown = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert shown == [], [r.getMessage() for r in shown]
