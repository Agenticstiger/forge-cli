# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The job config a Spark build compiles to: deterministic, checked, credential-free."""

import json

import pytest
import sqlglot
from sqlglot.errors import ErrorLevel

from fluid_build.providers.aws.util.warehouse import get_iceberg_warehouse
from fluid_build.spark.compiler import (
    CONFIG_VERSION,
    CONTRACT_SQL_ALLOWLIST_ENV,
    compile_job_config,
    spark_builds,
)
from fluid_build.spark.policy import SparkPolicyError

from .conftest import BROKERS


def job(contract, build=None, **kw):
    kw.setdefault("bootstrap_servers", BROKERS)
    return compile_job_config(contract, build or contract["builds"][0], **kw)


def test_the_same_contract_gives_the_same_bytes(contract):
    first, second = job(contract), job(contract)
    assert first.text == second.text
    assert first.digest == second.digest
    assert first.text.endswith("}\n")
    assert list(json.loads(first.text)) == sorted(json.loads(first.text))


def test_the_config_says_what_the_job_does(contract):
    config = job(contract).config
    assert config["configVersion"] == CONFIG_VERSION == 1
    assert (config["product"], config["build"], config["mode"]) == (
        "commerce.orders-stream",
        "ingest-orders",
        "streaming",
    )
    source = config["source"]
    assert source["bootstrapServers"] == BROKERS
    assert source["auth"] == "msk-iam"
    assert source["topic"] == "commerce.orders"
    assert source["startingOffsets"] == "earliest"
    assert source["value"]["format"] == "json"
    assert source["value"]["schema"].startswith("`order_id` STRING, `customer` STRUCT<")
    assert config["trigger"] == {"processingTime": "60000 milliseconds"}
    assert config["watermark"] == {"column": "event_time", "delay": "600000 milliseconds"}
    assert config["maskedColumns"] == ["email_hash"]
    assert "sha2(CAST(`src`.`customer`.`email` AS STRING), 256) AS `email_hash`" in (
        config["sql"]["select"]
    )


def test_both_queries_are_spark_sql(contract):
    for sql in job(contract).config["sql"].values():
        (tree,) = sqlglot.parse(sql, read="spark", error_level=ErrorLevel.RAISE)
        assert tree is not None


def test_the_sink_is_the_glue_table_at_the_canonical_warehouse(contract):
    sink = job(contract).config["sink"]
    location = contract["exposes"][0]["binding"]["location"]
    assert sink == {
        "catalog": "fluid",
        "type": "glue",
        "warehouse": get_iceberg_warehouse(location, account_ref=""),
        "region": "eu-west-1",
        "table": "commerce.order_lines",
        "write": "append",
    }


def test_the_checkpoint_defaults_to_one_place_per_build_beside_the_tables(contract):
    # At the bucket root, not under the table: Iceberg's orphan-file cleanup deletes
    # whatever under a table's location no snapshot references.
    assert job(contract).config["checkpoint"] == (
        "s3://commerce-lake/_checkpoints/commerce.orders-stream/ingest-orders/"
    )


def test_a_declared_checkpoint_is_kept(contract):
    contract["builds"][0]["properties"]["stream"]["checkpoint"] = "s3://state/orders"
    assert job(contract).config["checkpoint"] == "s3://state/orders/"


def test_a_bucketless_binding_needs_the_account(contract):
    del contract["exposes"][0]["binding"]["location"]["bucket"]
    with pytest.raises(ValueError, match="pass the account"):
        job(contract)
    config = job(contract, account_ref="000000000005").config
    assert config["checkpoint"].startswith("s3://000000000005-fluid-data/_checkpoints/")


def test_the_config_holds_no_credential(contract):
    stream = contract["builds"][0]["properties"]["stream"]
    stream.update(
        format="avro-confluent",
        registry={
            "kind": "confluent",
            "url": "https://registry.example.internal",
            "auth": {"secretRef": "aws-sm://commerce/registry#user_info"},
        },
    )
    text = job(contract).text
    assert "aws-sm://commerce/registry#user_info" in text
    assert "password" not in text.lower()


def test_avro_reads_the_registry_subject_and_declares_the_schema_to_check(contract):
    stream = contract["builds"][0]["properties"]["stream"]
    stream.update(
        format="avro-confluent",
        key={"format": "avro-confluent", "schema": [{"name": "order_id", "type": "string"}]},
        registry={"kind": "confluent", "url": "http://registry:8081", "version": 3},
    )
    stream["key"]["prefix"] = "k_"
    source = job(contract).config["source"]
    assert source["registry"] == {"url": "http://registry:8081", "auth": None}
    assert source["value"] == {
        "format": "avro-confluent",
        "subject": "commerce.orders-value",
        "version": 3,
        "schema": source["value"]["schema"],
    }
    assert source["value"]["schema"].startswith("`order_id` STRING")
    assert source["key"] == {
        "format": "avro-confluent",
        "subject": "commerce.orders-key",
        "version": "latest",
        "schema": "`order_id` STRING",
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"registry": {"kind": "glue"}}, "only to avro-confluent"),
        ({"format": "avro-confluent"}, "need their registry"),
        (
            {"format": "avro-confluent", "registry": {"kind": "glue"}},
            "Glue Schema Registry is not supported yet",
        ),
        (
            {"format": "avro-confluent", "registry": {"kind": "confluent", "url": "ftp://x"}},
            "http",
        ),
        (
            {
                "format": "avro-confluent",
                "registry": {
                    "kind": "confluent",
                    "url": "http://r:8081",
                    "auth": {"secretRef": "vault://kv/registry#pw"},
                },
            },
            "not vault",
        ),
        ({"trigger": {"processingTime": "P1M"}}, "a month or a year"),
        ({"trigger": {"availableNow": False}}, "processingTime or availableNow"),
        ({"watermark": {"column": "order_id", "delay": "PT1M"}}, "top-level timestamp"),
        ({"watermark": {"column": "customer.id", "delay": "PT1M"}}, "top-level timestamp"),
        (
            {
                "schema": [{"name": "t", "type": "timestamp"}],
                "watermark": {"column": "t", "delay": "PT1M"},
            },
            "declare it timestamp_ltz",
        ),
        ({"startingOffsets": "newest"}, "earliest or latest"),
        ({"checkpoint": "s3://commerce-lake"}, "with a prefix"),
        ({"metadata": [{"name": "epoch", "from": "leader-epoch"}]}, "leader-epoch"),
    ],
)
def test_a_stream_the_runner_cannot_read_is_refused(contract, change, message):
    contract["builds"][0]["properties"]["stream"].update(change)
    with pytest.raises(ValueError, match=message):
        job(contract)


def test_available_now_is_a_catch_up_run(contract):
    contract["builds"][0]["properties"]["stream"]["trigger"] = {"availableNow": True}
    assert job(contract).config["trigger"] == {"availableNow": True}


def test_an_absent_trigger_follows_the_freshness_slo(contract):
    del contract["builds"][0]["properties"]["stream"]["trigger"]
    assert job(contract).config["trigger"] is None
    contract["exposes"][0]["qos"] = {"freshnessSLO": "PT10M"}
    assert job(contract).config["trigger"] == {"processingTime": "300000 milliseconds"}


@pytest.mark.parametrize(
    "brokers",
    [None, "", "b-1:9098,", "b-1", "b-1:9098;rm -rf /", "${a} ${b}", "a:1 b:2"],
)
def test_bootstrap_servers_are_a_broker_list(contract, brokers):
    with pytest.raises(ValueError, match="bootstrap servers"):
        job(contract, bootstrap_servers=brokers)


def test_bootstrap_servers_may_be_one_interpolation_the_uploader_resolves(contract):
    token = "${aws_msk_cluster.orders.bootstrap_brokers_sasl_iam}"
    assert job(contract, bootstrap_servers=token).config["source"]["bootstrapServers"] == token


def test_kafka_auth_is_msk_iam_or_none(contract):
    assert job(contract, kafka_auth="none").config["source"]["auth"] == "none"
    with pytest.raises(ValueError, match="kafka auth"):
        job(contract, kafka_auth="sasl-plain")


def test_a_batch_job_reads_the_topic_once(contract):
    props = contract["builds"][0]["properties"]
    props["parameters"]["spark"]["mode"] = "batch"
    with pytest.raises(ValueError, match="keeps no position"):
        job(contract)
    for key in ("trigger", "watermark"):
        del props["stream"][key]
    del props["stream"]["startingOffsets"]
    config = job(contract).config
    assert config["source"]["startingOffsets"] == "earliest"
    assert (config["trigger"], config["watermark"], config["checkpoint"]) == (None, None, None)
    assert config["sink"]["write"] == "overwrite"


def _sql_build(contract, sql="SELECT order_id, sku FROM commerce.order_lines"):
    build = contract["builds"][0]
    props = build["properties"]
    props.pop("stream", None)
    props.pop("mapping", None)
    props["sql"] = sql
    props["parameters"]["spark"] = {"runtime": "emr-eks", "virtualClusterId": "abc"}
    return build


def test_contract_sql_runs_only_for_a_listed_build(contract):
    build = _sql_build(contract)
    with pytest.raises(SparkPolicyError) as refused:
        job(contract, build)
    assert CONTRACT_SQL_ALLOWLIST_ENV in refused.value.fix
    for entry in ("commerce.orders-stream", "commerce.orders-stream/ingest-orders"):
        config = job(contract, build, environ={CONTRACT_SQL_ALLOWLIST_ENV: entry}).config
        assert config["mode"] == "batch" and config["source"] is None
        assert config["sql"]["source"] is None
        assert config["sql"]["select"].startswith("SELECT `order_id`, `customer_id`")


def test_contract_sql_is_one_select(contract):
    environ = {CONTRACT_SQL_ALLOWLIST_ENV: "commerce.orders-stream"}
    for sql in ("DROP TABLE commerce.order_lines", "SELECT 1; SELECT 2"):
        with pytest.raises(ValueError, match="single SELECT"):
            job(contract, _sql_build(contract, sql), environ=environ)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: c["builds"][0].update(engine="flink"), "engine: spark"),
        (lambda c: c["builds"][0].update(id="../x"), "an id of letters"),
        (lambda c: c.update(id="a/../b"), "a product id"),
        (
            lambda c: c["builds"][0]["properties"]["parameters"]["spark"].update(runtime="x"),
            "runtime",
        ),
        (lambda c: c["builds"][0]["properties"]["parameters"]["spark"].update(mode="x"), "mode"),
        (lambda c: c["builds"][0].update(outputs=["a", "b"]), "exactly one Iceberg output"),
        (lambda c: c["builds"][0].update(outputs=["missing"]), "one expose"),
        (lambda c: c["exposes"][0]["binding"].update(format="parquet"), "must be iceberg"),
        (
            lambda c: c["exposes"][0]["binding"]["location"].update(
                catalog="rest", uri="http://x", warehouse="w"
            ),
            "Glue-catalog Iceberg",
        ),
        (lambda c: c["builds"][0]["properties"].update(sql="SELECT 1"), "not both"),
        (lambda c: c["builds"][0]["properties"].pop("stream"), "reads the records"),
    ],
)
def test_a_build_the_runner_cannot_run_is_refused(contract, mutate, message):
    mutate(contract)
    with pytest.raises(ValueError, match=message):
        job(contract)


def test_spark_builds_are_the_engine_spark_builds_with_parameters(contract):
    assert spark_builds(contract) == [contract["builds"][0]]
    contract["builds"][0]["engine"] = "dbt"
    assert spark_builds(contract) == []
