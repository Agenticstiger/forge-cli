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

"""LOGIC-1: a derived streaming Iceberg sink writes the exposes its build's outputs pick.

Two CDC builds feeding two Iceberg exposes is the ordinary shape. Every sink
used to write ``exposes[0]``, so the ``refunds`` build's connector carried
``iceberg.tables=sales.orders`` while ``fluid validate``, the preflight and the
run all passed. A sink config forge-cli derives now resolves its exposes from
``build.outputs`` through one resolver that ``fluid validate``, the preflight,
both runners and the late-arrival target read; a join it cannot resolve is a
HARD error that stops the run before any Connect REST call or config write. A
hand-written config, or an override that names the tables itself, behaves as
before.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.api.runner import RunState
from fluid_build.build_runners._acquisition_common import build_acquisition_run_context
from fluid_build.build_runners.debezium.runner import DebeziumRunner
from fluid_build.build_runners.kafka_connect.iceberg_sink_validation import (
    iceberg_sink_preflight,
    validate_iceberg_sink,
)
from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

pytestmark = [pytest.mark.unit]

_SOURCE = {
    "kind": "postgres",
    "connection": {"host": "db", "port": 5432, "database": "app", "user": "u", "password": "p"},
    "mode": "incremental_append",
    "watermark": {"strategy": "high_water_mark", "allowedLateness": "PT5M"},
}

_LAKEKEEPER = {"catalog": "lakekeeper", "uri": "http://lakekeeper:8181/catalog", "warehouse": "wh"}


def _expose(
    eid: str, database: str, table: str, *, fmt: str = "iceberg", **location: Any
) -> Dict[str, Any]:
    return {
        "exposeId": eid,
        "kind": "table",
        "binding": {
            "platform": "aws",
            "format": fmt,
            "location": {
                "database": database,
                "table": table,
                "bucket": "lake",
                "region": "us-east-1",
                **location,
            },
        },
        "contract": {"schema": []},
    }


def _kc_build(bid: str, outputs: Optional[List[str]], stream: str, **kc: Any) -> Dict[str, Any]:
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
                **kc,
            },
        },
    }
    if outputs is not None:
        build["outputs"] = outputs
    return build


def _dbz_build(outputs: Optional[List[str]], **server_sink: Any) -> Dict[str, Any]:
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
            "debezium": {"deployment": {"mode": "embedded"}, "server": {"sink": server_sink}},
        },
    }
    if outputs is not None:
        build["outputs"] = outputs
    return build


def _contract(exposes: List[Dict[str, Any]], builds: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.6",
        "kind": "DataProduct",
        "id": "sales.cdc",
        "name": "Sales CDC",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        # copies: a test edits its contract, and the module-level exposes are shared
        "exposes": copy.deepcopy(exposes),
        "builds": copy.deepcopy(builds),
    }


ORDERS = _expose("orders", "sales", "orders")
REFUNDS = _expose("refunds", "sales", "refunds")
FIN_REFUNDS = _expose("refunds", "finance", "refunds")
RAW = _expose("raw", "sales", "raw", fmt="parquet")


def _two_builds() -> Dict[str, Any]:
    return _contract(
        [ORDERS, FIN_REFUNDS],
        [
            _kc_build("ingest_orders", ["orders"], "public.orders"),
            _kc_build("ingest_refunds", ["refunds"], "public.refunds"),
        ],
    )


def _run_embedded(contract: Dict[str, Any], tmp_path: Path):
    ctx = build_acquisition_run_context(contract["builds"][0], contract, tmp_path)
    return DebeziumRunner().run(ctx)


def _props_path(contract: Dict[str, Any], tmp_path: Path) -> Path:
    return tmp_path / ".fluid" / "debezium" / contract["id"] / "ingest" / "application.properties"


# ── the defect: each derived sink writes ITS build's expose ─────────────────


def test_two_builds_validate_clean():
    assert validate_iceberg_sink(_two_builds()) == ([], [])


@pytest.mark.parametrize(
    "index, table",
    [(0, "sales.orders"), (1, "finance.refunds")],
    ids=["first-build", "second-build"],
)
def test_kafka_connect_sink_and_late_arrival_target_follow_the_builds_expose(
    kafka_connect_mock, tmp_path: Path, index, table
):
    contract = _two_builds()
    build = contract["builds"][index]
    assert execute_kafka_connect_build(build, contract, tmp_path, dry_run=False) == 0
    name = build["properties"]["kafka-connect"]["connector_name"]
    sink = kafka_connect_mock.connectors[f"{name}-sink"]["config"]
    assert sink["iceberg.tables"] == table
    source = kafka_connect_mock.connectors[name]["config"]
    # the late-events side table sits beside the same target
    assert source["fluid.late_arrival.side_output_table"] == f"{table}__late_events"


def test_embedded_debezium_namespace_follows_the_builds_expose(tmp_path: Path):
    contract = _contract([ORDERS, FIN_REFUNDS], [_dbz_build(["refunds"])])
    assert validate_iceberg_sink(contract) == ([], [])
    _run_embedded(contract, tmp_path)  # no server binary: fails after writing the config
    text = _props_path(contract, tmp_path).read_text()
    assert "debezium.sink.iceberg.table-namespace=finance" in text
    assert "debezium.sink.iceberg.warehouse=s3://lake/finance/refunds/" in text
    assert "table-namespace=sales" not in text


def test_validate_checks_the_catalog_of_the_expose_the_build_writes():
    # outputs pick the Lakekeeper expose, which has no uri; the first expose
    # is a complete Glue binding, which is all the validator used to read.
    no_uri = _expose("refunds", "sales", "refunds", catalog="lakekeeper", warehouse="wh")
    contract = _contract([ORDERS, no_uri], [_kc_build("ingest", ["refunds"], "public.refunds")])
    errors, _ = validate_iceberg_sink(contract)
    assert any("lakekeeper catalog requires binding.location.uri" in e for e in errors), errors


# ── Kafka Connect: a derived sink writes one expose; anything else is HARD ──

_KC_REFUSED = [
    ([ORDERS, RAW], ["raw"], "outputs ['raw'] name none of the Iceberg sink exposes"),
    ([ORDERS, REFUNDS], ["orders", "refunds"], "outputs ['orders', 'refunds'] name 2 of"),
    ([ORDERS, REFUNDS], None, "declares no outputs, and the contract has 2 Iceberg sink exposes"),
    ([ORDERS, REFUNDS], [], "declares no outputs, and the contract has 2 Iceberg sink exposes"),
]
_KC_IDS = ["outputs-name-non-iceberg", "outputs-name-two", "no-outputs-two", "empty-outputs-two"]


@pytest.mark.parametrize("exposes, outputs, message", _KC_REFUSED, ids=_KC_IDS)
def test_kafka_connect_validate_and_preflight_refuse(exposes, outputs, message):
    contract = _contract(exposes, [_kc_build("ingest", outputs, "public.orders")])
    errors, warnings = validate_iceberg_sink(contract)
    [error] = [e for e in errors if message in e]
    # names the build, its candidate exposes, and the way out
    assert "build 'ingest'" in error
    assert "orders (sales.orders)" in error
    assert "a derived Kafka Connect sink writes one expose" in error
    assert "split the build" in error and "hand-write the sink config" in error
    assert not any("join is implicit" in w for w in warnings)
    preflight = iceberg_sink_preflight(contract, "ingest")
    assert preflight is not None and message in preflight


@pytest.mark.parametrize("exposes, outputs, message", _KC_REFUSED, ids=_KC_IDS)
def test_kafka_connect_run_refuses_before_any_rest_call(
    kafka_connect_mock, tmp_path: Path, exposes, outputs, message
):
    contract = _contract(exposes, [_kc_build("ingest", outputs, "public.orders")])
    rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc != 0
    assert kafka_connect_mock.connectors == {}


# ── embedded Debezium: one namespace and one catalog ────────────────────────


@pytest.mark.parametrize("outputs", [["orders", "refunds"], None], ids=["both", "no-outputs"])
def test_embedded_debezium_writes_one_namespace_for_several_exposes(tmp_path: Path, outputs):
    # DefaultIcebergTableMapper.mapDestination writes each captured table
    # under table-namespace, so two exposes in one database and one catalog
    # are one derived sink.
    contract = _contract([ORDERS, REFUNDS], [_dbz_build(outputs)])
    assert validate_iceberg_sink(contract) == ([], [])
    _run_embedded(contract, tmp_path)  # no server binary: fails after writing the config
    text = _props_path(contract, tmp_path).read_text()
    assert "debezium.sink.iceberg.table-namespace=sales" in text


_DBZ_REFUSED = [
    ([ORDERS, RAW], ["raw"], "outputs ['raw'] name none of the Iceberg sink exposes"),
    ([ORDERS, FIN_REFUNDS], ["orders", "refunds"], "sit in the databases ['finance', 'sales']"),
    ([ORDERS, FIN_REFUNDS], None, "sit in the databases ['finance', 'sales']"),
    (
        [ORDERS, _expose("refunds", "sales", "refunds", **_LAKEKEEPER)],
        ["orders", "refunds"],
        "resolve to different catalogs (their catalog, warehouse, uri differ)",
    ),
    (
        [ORDERS, _expose("refunds", "sales", "refunds", region="eu-west-1")],
        None,
        "resolve to different catalogs (their region differ)",
    ),
]
_DBZ_IDS = [
    "outputs-name-non-iceberg",
    "two-databases",
    "no-outputs-two-databases",
    "two-catalog-kinds",
    "no-outputs-two-regions",
]


@pytest.mark.parametrize("exposes, outputs, message", _DBZ_REFUSED, ids=_DBZ_IDS)
def test_embedded_debezium_validate_preflight_and_run_refuse(
    tmp_path: Path, exposes, outputs, message
):
    contract = _contract(exposes, [_dbz_build(outputs)])
    errors, _ = validate_iceberg_sink(contract)
    assert any("build 'ingest'" in e and message in e for e in errors), errors
    preflight = iceberg_sink_preflight(contract, "ingest")
    assert preflight is not None and message in preflight
    result = _run_embedded(contract, tmp_path)
    assert result.state == RunState.FAILED
    assert message in (result.error or "")
    assert not _props_path(contract, tmp_path).exists()


# ── unchanged: single-expose contracts and configs that name their tables ───


@pytest.mark.parametrize("outputs", [None, [], ["orders"]], ids=["absent", "empty", "named"])
def test_single_expose_kafka_connect_contract_is_unchanged(
    kafka_connect_mock, tmp_path: Path, outputs
):
    contract = _contract([ORDERS], [_kc_build("ingest", outputs, "public.orders")])
    assert validate_iceberg_sink(contract) == ([], [])
    assert execute_kafka_connect_build(contract["builds"][0], contract, tmp_path) == 0
    sink = kafka_connect_mock.connectors["src-ingest-sink"]["config"]
    assert sink["iceberg.tables"] == "sales.orders"


@pytest.mark.parametrize("outputs", [None, ["orders"]], ids=["absent", "named"])
def test_single_expose_embedded_debezium_contract_is_unchanged(tmp_path: Path, outputs):
    contract = _contract([ORDERS], [_dbz_build(outputs)])
    assert validate_iceberg_sink(contract) == ([], [])
    _run_embedded(contract, tmp_path)
    text = _props_path(contract, tmp_path).read_text()
    assert "debezium.sink.iceberg.table-namespace=sales" in text
    assert "debezium.sink.iceberg.warehouse=s3://lake/sales/orders/" in text


@pytest.mark.parametrize(
    "kc",
    [
        {"iceberg_catalog_overrides": {"iceberg.tables": "sales.refunds"}},
        {
            "iceberg_sink_enabled": True,
            "sink_connector_config": {"iceberg.tables": "sales.refunds"},
        },
        {"sink_connector_config": {"iceberg.tables": "sales.refunds"}},
    ],
    ids=["override", "hand-written-over-derived", "hand-written"],
)
@pytest.mark.parametrize("outputs", [None, ["orders", "refunds"]], ids=["absent", "two"])
def test_kafka_connect_config_naming_its_tables_is_unchanged(
    kafka_connect_mock, tmp_path: Path, kc, outputs
):
    contract = _contract([ORDERS, REFUNDS], [_kc_build("ingest", outputs, "public.refunds", **kc)])
    assert validate_iceberg_sink(contract) == ([], [])
    assert execute_kafka_connect_build(contract["builds"][0], contract, tmp_path) == 0
    sink = kafka_connect_mock.connectors["src-ingest-sink"]["config"]
    assert sink["iceberg.tables"] == "sales.refunds"
    # the late-arrival target is still the first Iceberg expose, as before
    source = kafka_connect_mock.connectors["src-ingest"]["config"]
    assert source["fluid.late_arrival.side_output_table"] == "sales.orders__late_events"


def test_kafka_connect_hand_written_outputs_naming_no_expose_still_warn():
    kc = {"sink_connector_config": {"iceberg.tables": "sales.orders"}}
    contract = _contract([ORDERS, RAW], [_kc_build("ingest", ["raw"], "public.orders", **kc)])
    errors, warnings = validate_iceberg_sink(contract)
    assert errors == []
    assert any("join is implicit" in w for w in warnings)


@pytest.mark.parametrize(
    "server_sink",
    [
        {"iceberg_sink_enabled": True, "config": {"table-namespace": "finance"}},
        {"config": {"catalog-impl": "org.apache.iceberg.aws.glue.GlueCatalog"}},
    ],
    ids=["namespace-override", "hand-written"],
)
def test_embedded_debezium_config_naming_its_namespace_is_unchanged(tmp_path: Path, server_sink):
    # two databases would refuse a derived namespace; these name their own
    contract = _contract([ORDERS, FIN_REFUNDS], [_dbz_build(None, **server_sink)])
    assert validate_iceberg_sink(contract) == ([], [])
    assert iceberg_sink_preflight(contract, "ingest") is None
