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

"""PR2 — the Iceberg sink-config deriver + its catalog resolver."""

from __future__ import annotations

import pytest

from fluid_build.build_runners.kafka_connect.iceberg_sink import (
    ICEBERG_SINK_CLASS,
    control_topic,
    emit_iceberg_sink_config,
    sanitize_topic_segment,
)
from fluid_build.providers._iceberg_catalog import (
    ResolvedIcebergCatalog,
    resolve_iceberg_catalog,
)

pytestmark = [pytest.mark.unit]


class _Sink:
    def __init__(self, catalog=None, partition_by=None):
        self.catalog = catalog
        self.partition_by = partition_by or []


# ── resolver ────────────────────────────────────────────────────────────────


def test_resolve_glue_catalog():
    binding = {
        "platform": "aws",
        "location": {
            "database": "sales",
            "table": "orders",
            "bucket": "lake",
            "region": "us-east-1",
        },
    }
    r = resolve_iceberg_catalog(binding, contract={"metadata": {"primaryKey": ["id"]}})
    assert r.catalog_type == "glue"
    assert r.warehouse == "s3://lake/sales/orders/"
    assert r.catalog_impl == "org.apache.iceberg.aws.glue.GlueCatalog"
    assert r.io_impl == "org.apache.iceberg.aws.s3.S3FileIO"
    assert r.region == "us-east-1"
    assert r.fq_table == "sales.orders"
    assert r.id_columns == ("id",)


def test_resolve_rest_catalog():
    binding = {
        "platform": "local",
        "location": {
            "database": "default",
            "table": "events",
            "catalog": "rest",
            "uri": "http://iceberg:8181",
            "warehouse": "s3://bucket/warehouse/",
        },
    }
    r = resolve_iceberg_catalog(binding, sink=_Sink(catalog="rest"))
    assert r.catalog_type == "rest"
    assert r.uri == "http://iceberg:8181"
    assert r.warehouse == "s3://bucket/warehouse/"
    assert r.fq_table == "default.events"


def test_resolve_primary_key_string_and_partition_from_sink():
    binding = {"platform": "aws", "location": {"database": "d", "table": "t", "bucket": "b"}}
    r = resolve_iceberg_catalog(
        binding, contract={"metadata": {"primaryKey": "pk"}}, sink=_Sink(partition_by=["region"])
    )
    assert r.id_columns == ("pk",)
    assert r.partition_by == ("region",)


# ── deriver ─────────────────────────────────────────────────────────────────

GLUE = ResolvedIcebergCatalog(
    catalog_type="glue",
    warehouse="s3://lake/sales/orders/",
    fq_table="sales.orders",
    catalog_impl="org.apache.iceberg.aws.glue.GlueCatalog",
    io_impl="org.apache.iceberg.aws.s3.S3FileIO",
    region="us-east-1",
    id_columns=("id",),
)


def test_deriver_core_keys_and_class():
    cfg = emit_iceberg_sink_config(GLUE, product_id="analytics.orders", topics=["orders"])
    # current Apache class, never the retired io.tabular one
    assert cfg["connector.class"] == ICEBERG_SINK_CLASS
    assert "io.tabular" not in cfg["connector.class"]
    assert cfg["iceberg.tables"] == "sales.orders"
    assert cfg["topics"] == "orders"
    # catalog-impl XOR type: Iceberg's CatalogUtil refuses a config carrying
    # both, so a Glue sink that sent both never started.
    assert cfg["iceberg.catalog.catalog-impl"] == "org.apache.iceberg.aws.glue.GlueCatalog"
    assert "iceberg.catalog.type" not in cfg
    assert cfg["iceberg.catalog.warehouse"] == "s3://lake/sales/orders/"
    assert cfg["iceberg.catalog.io-impl"] == "org.apache.iceberg.aws.s3.S3FileIO"
    assert cfg["iceberg.catalog.client.region"] == "us-east-1"
    assert cfg["iceberg.tables.default-id-columns"] == "id"


def test_deriver_unique_control_topic_not_shared_default():
    cfg = emit_iceberg_sink_config(GLUE, product_id="analytics.orders", topics=["t"])
    assert cfg["iceberg.control.topic"] == "_iceberg-control-analytics.orders"
    assert cfg["iceberg.control.topic"] != "control-iceberg"


def test_deriver_default_json_converters_match_spike():
    cfg = emit_iceberg_sink_config(GLUE, product_id="p", topics=["t"])
    assert cfg["value.converter"] == "org.apache.kafka.connect.json.JsonConverter"
    assert cfg["value.converter.schemas.enable"] == "false"
    assert cfg["key.converter.schemas.enable"] == "false"


def test_deriver_avro_converters_when_schema_registry():
    cfg = emit_iceberg_sink_config(
        GLUE, product_id="p", topics=["t"], schema_registry_url="http://sr:8081"
    )
    assert "avro" in cfg["value.converter"].lower()


def test_deriver_is_deterministic():
    a = emit_iceberg_sink_config(GLUE, product_id="p", topics=["t1", "t2"])
    b = emit_iceberg_sink_config(GLUE, product_id="p", topics=["t1", "t2"])
    assert a == b


def test_deriver_exactly_once_emits_transactional_prefix():
    cfg = emit_iceberg_sink_config(
        GLUE, product_id="analytics.orders", topics=["t"], delivery_guarantee="exactly_once"
    )
    assert cfg["iceberg.coordinator.transactional.prefix"] == "iceberg-coord-analytics.orders"


def test_deriver_overrides_always_win():
    cfg = emit_iceberg_sink_config(
        GLUE,
        product_id="p",
        topics=["t"],
        kc_props={"iceberg_catalog_overrides": {"iceberg.catalog.warehouse": "s3://override/"}},
    )
    assert cfg["iceberg.catalog.warehouse"] == "s3://override/"


def test_deriver_streaming_tuning_block():
    cfg = emit_iceberg_sink_config(
        GLUE,
        product_id="p",
        topics=["t"],
        kc_props={"streamingSink": {"autoCreate": True, "commitIntervalMs": 1000}},
    )
    assert cfg["iceberg.tables.auto-create-enabled"] == "true"
    assert cfg["iceberg.control.commit.interval-ms"] == "1000"


# ── product_id sanitization (collision-safe, RFC §6.6) ──────────────────────


def test_sanitize_clean_id_unchanged():
    assert sanitize_topic_segment("analytics.orders") == "analytics.orders"


def test_sanitize_illegal_chars_collision_safe():
    a = sanitize_topic_segment("team/orders")  # illegal '/'
    b = sanitize_topic_segment("team:orders")  # illegal ':' — same slug, different id
    assert a.startswith("team-orders-")
    assert b.startswith("team-orders-")
    assert a != b  # stable-hash suffix keeps them distinct (no control-topic collision)


def test_control_topic_bounds_kafka_249():
    assert len(control_topic("x" * 500)) <= 249


# ── runner wiring (merge-precedence + default-off) ──────────────────────────


def _iceberg_contract(*, sink_connector_config=None):
    kc = {"deployment": {"server_url": "http://kafka-connect.test:8083"}, "connector_name": "src"}
    if sink_connector_config is not None:
        kc["sink_connector_name"] = "snk"
        kc["sink_connector_config"] = sink_connector_config
    return {
        "fluidVersion": "0.7.3",
        "kind": "DataProduct",
        "id": "bronze.kc_iceberg",
        "name": "X",
        "metadata": {"layer": "Bronze", "owner": {"team": "dp", "email": "x@y.z"}},
        "builds": [
            {
                "id": "ingest",
                "pattern": "acquisition",
                "engine": "kafka-connect",
                "capabilities": ["streaming", "at_least_once"],
                "properties": {
                    "source": {
                        "kind": "postgres",
                        "connection": {
                            "host": "db",
                            "port": 5432,
                            "database": "x",
                            "user": "u",
                            "password": "p",
                        },
                        "mode": "incremental_append",
                        "streams": ["public.orders"],
                    },
                    "sink": {"format": "iceberg"},
                    "kafka-connect": kc,
                },
                "outputs": ["data"],
            }
        ],
        "exposes": [
            {
                "exposeId": "data",
                "kind": "table",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {
                        "database": "sales",
                        "table": "orders",
                        "bucket": "lake",
                        "region": "us-east-1",
                    },
                },
                "contract": {"schema": [], "schemaPolicy": "discover_and_freeze"},
            }
        ],
    }


def test_runner_derives_iceberg_sink_when_no_handwritten(kafka_connect_mock, tmp_path):
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    contract = _iceberg_contract()
    rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc == 0
    sink = kafka_connect_mock.connectors.get("src-sink")
    assert sink is not None, "iceberg sink connector was not derived/created"
    cfg = sink["config"]
    assert cfg["connector.class"] == "org.apache.iceberg.connect.IcebergSinkConnector"
    assert cfg["iceberg.tables"] == "sales.orders"
    assert cfg["iceberg.catalog.warehouse"] == "s3://lake/sales/orders/"
    assert cfg["iceberg.control.topic"] == "_iceberg-control-bronze.kc_iceberg"


def test_runner_default_off_when_handwritten_sink(kafka_connect_mock, tmp_path):
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    # A hand-written sink_connector_config is present -> the deriver must NOT run
    # (existing contracts stay byte-for-byte unaffected).
    contract = _iceberg_contract(
        sink_connector_config={
            "connector.class": "io.confluent.connect.s3.S3SinkConnector",
            "topics": "public.orders",
            "s3.bucket.name": "b",
        }
    )
    rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path, dry_run=False)
    assert rc == 0
    cfg = kafka_connect_mock.connectors["snk"]["config"]
    assert cfg["connector.class"] == "io.confluent.connect.s3.S3SinkConnector"
    assert "iceberg.tables" not in cfg


# ── runner preflight: fail closed BEFORE any Connect REST call ──────────────
#
# The runner runs the same checks as `fluid validate` right before deriving,
# so a sink the validator rejects never reaches the cluster: before this a
# Lakekeeper binding with no uri was POSTed as a connector that failed at its
# first record, and a Glue sink with an overridden `type` never started.

_LAKEKEEPER_NO_URI = {
    "platform": "local",
    "format": "iceberg",
    "location": {
        "database": "sales",
        "table": "orders",
        "catalog": "lakekeeper",
        "warehouse": "analytics",
    },
}


def _run(contract, tmp_path):
    from fluid_build.build_runners._acquisition_common import build_acquisition_run_context
    from fluid_build.build_runners.kafka_connect.runner import KafkaConnectRunner

    ctx = build_acquisition_run_context(contract["builds"][0], contract, tmp_path)
    return KafkaConnectRunner().run(ctx)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (
            lambda c: c["exposes"][0].__setitem__("binding", _LAKEKEEPER_NO_URI),
            "lakekeeper catalog requires binding.location.uri",
        ),
        (
            lambda c: c["builds"][0]["properties"]["kafka-connect"].__setitem__(
                "iceberg_catalog_overrides", {"iceberg.catalog.type": "rest"}
            ),
            "would carry both iceberg.catalog.type",
        ),
        (
            lambda c: c["builds"][0]["properties"]["sink"].__setitem__("catalog", "lakekeeper"),
            "disagrees with the expose's catalog 'glue'",
        ),
        (lambda c: c.__setitem__("exposes", []), "has no expose with binding.format=iceberg"),
    ],
    ids=["missing-uri", "type-and-impl", "sink-vs-expose", "no-expose"],
)
def test_runner_preflight_fails_before_any_rest_call(
    kafka_connect_mock, tmp_path, mutate, expected
):
    from fluid_build.api.runner import RunState
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    contract = _iceberg_contract()
    mutate(contract)

    result = _run(contract, tmp_path)
    assert result.state == RunState.FAILED
    assert expected in (result.error or ""), result.error
    assert "iceberg sink preflight failed" in result.error
    # Nothing reached the cluster: not the sink, and not the SOURCE connector
    # either (it would stream into a topic no sink drains).
    assert kafka_connect_mock.calls == []
    assert kafka_connect_mock.connectors == {}

    assert execute_kafka_connect_build(contract["builds"][0], contract, tmp_path) == 1
    assert kafka_connect_mock.calls == []


def test_runner_preflight_warning_logs_and_still_deploys(kafka_connect_mock, tmp_path, caplog):
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    contract = _iceberg_contract()
    contract["exposes"][0]["binding"] = {
        **_LAKEKEEPER_NO_URI,
        "location": {
            **_LAKEKEEPER_NO_URI["location"],
            "catalog": "nessie",
            "uri": "http://nessie:19120/api/v2",
            "warehouse": "s3://lake/warehouse",
        },
    }
    with caplog.at_level("WARNING", logger="fluid.acquire.kafka_connect"):
        rc = execute_kafka_connect_build(contract["builds"][0], contract, tmp_path)
    assert rc == 0
    assert any("iceberg-nessie" in r.getMessage() for r in caplog.records)
    cfg = kafka_connect_mock.connectors["src-sink"]["config"]
    assert cfg["iceberg.catalog.type"] == "nessie"
    assert "iceberg.catalog.catalog-impl" not in cfg
    assert cfg["iceberg.catalog.uri"] == "http://nessie:19120/api/v2"


def test_complete_lakekeeper_streams_over_rest(kafka_connect_mock, tmp_path):
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    contract = _iceberg_contract()
    contract["exposes"][0]["binding"] = {
        **_LAKEKEEPER_NO_URI,
        "location": {**_LAKEKEEPER_NO_URI["location"], "uri": "http://lakekeeper:8181/catalog"},
    }
    assert execute_kafka_connect_build(contract["builds"][0], contract, tmp_path) == 0
    cfg = kafka_connect_mock.connectors["src-sink"]["config"]
    assert cfg["iceberg.catalog.type"] == "rest"
    assert cfg["iceberg.catalog.uri"] == "http://lakekeeper:8181/catalog"
    # A warehouse NAME: the catalog vends FileIO, so none is forced.
    assert "iceberg.catalog.io-impl" not in cfg


def test_runner_handwritten_sink_skips_the_preflight(kafka_connect_mock, tmp_path):
    from fluid_build.build_runners.kafka_connect.runner import execute_kafka_connect_build

    # Derivation is OFF for a hand-written sink, so nothing is derived from the
    # binding and the run stays byte-for-byte what it was (validate still flags it).
    contract = _iceberg_contract(sink_connector_config={"connector.class": "x.Y", "topics": "t"})
    contract["exposes"][0]["binding"] = _LAKEKEEPER_NO_URI
    assert execute_kafka_connect_build(contract["builds"][0], contract, tmp_path) == 0
    assert kafka_connect_mock.connectors["snk"]["config"] == {
        "connector.class": "x.Y",
        "topics": "t",
    }
