# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""runtime: local in the compiler and the runner: a REST-catalog sink the operator
declares, and the statements that create its table on the first run."""

from __future__ import annotations

import copy
import json

import pytest

from fluid_build.spark.compiler import compile_job_config, validation_errors


def local(contract, **location):
    build = contract["builds"][0]
    cfg = build["properties"]["parameters"]["spark"]
    cfg["runtime"] = "local"
    cfg["source"] = {"bootstrapServers": "localhost:9092"}
    cfg.pop("maskKeySecretArn")
    for column in build["properties"]["mapping"]["columns"]:
        column.pop("maskAtIngest", None)
    contract["exposes"][0]["binding"] = {
        "platform": "local",
        "format": "iceberg",
        "location": {
            "catalog": "lakekeeper",
            "database": "commerce",
            "table": "order_lines",
            "bucket": "lake",
            **location,
        },
    }
    return contract


def config_of(contract):
    return compile_job_config(contract, contract["builds"][0]).config


def test_a_local_sink_is_the_submitters_catalog_with_its_create_statements(contract):
    config = config_of(local(contract))
    assert config["sink"] == {
        "catalog": "fluid",
        "type": "rest",
        "kind": "lakekeeper",
        "table": "commerce.order_lines",
        "create": [
            "CREATE NAMESPACE IF NOT EXISTS `fluid`.`commerce`",
            "CREATE TABLE IF NOT EXISTS `fluid`.`commerce`.`order_lines` (\n"
            "  `order_id` STRING NOT NULL,\n"
            "  `customer_id` STRING,\n"
            "  `email_hash` STRING,\n"
            "  `sku` STRING,\n"
            "  `qty` INT,\n"
            "  `event_time` TIMESTAMP\n"
            ") USING iceberg TBLPROPERTIES ('format-version' = '2')",
        ],
    }
    assert config["checkpoint"] == "s3://lake/_checkpoints/commerce.orders-stream/ingest-orders/"


def test_the_binding_s_uri_and_warehouse_go_with_the_sink_for_the_runner_to_check(contract):
    sink = config_of(local(contract, uri="http://lakekeeper:8181/catalog", warehouse="lab"))["sink"]
    assert (sink["uri"], sink["warehouse"]) == ("http://lakekeeper:8181/catalog", "lab")


def test_a_rest_catalog_default_on_platform_local_is_accepted(contract):
    contract = local(contract)
    contract["exposes"][0]["binding"]["location"].pop("catalog")
    assert config_of(contract)["sink"]["kind"] == "rest"


@pytest.mark.parametrize(
    ("change", "says"),
    [
        (lambda c: c["exposes"][0]["binding"].update(platform="aws"), "platform local"),
        (
            lambda c: c["exposes"][0]["binding"]["location"].update(catalog="glue"),
            "Iceberg REST catalog",
        ),
        (
            lambda c: c["builds"][0]["properties"]["parameters"]["spark"].update(
                source={"mskClusterArn": "arn:aws:kafka:eu-west-1:000000000005:cluster/o/1"}
            ),
            "a local run reads bootstrapServers",
        ),
        (
            lambda c: c["exposes"][0]["binding"]["location"].pop("bucket"),
            "keeps its checkpoint in the S3 store its output names",
        ),
    ],
)
def test_a_local_build_the_runner_cannot_run_is_refused(contract, change, says):
    contract = local(contract)
    change(contract)
    with pytest.raises(ValueError, match=says):
        config_of(contract)


def test_a_local_stream_with_its_own_checkpoint_needs_no_bucket(contract):
    contract = local(contract)
    contract["exposes"][0]["binding"]["location"].pop("bucket")
    contract["builds"][0]["properties"]["stream"]["checkpoint"] = "s3://lab-state/c/orders/"
    assert config_of(contract)["checkpoint"] == "s3://lab-state/c/orders/"


def test_an_aws_runtime_still_refuses_a_rest_catalog(contract):
    contract["exposes"][0]["binding"]["location"]["catalog"] = "lakekeeper"
    with pytest.raises(ValueError, match="Glue-catalog Iceberg tables on an AWS runtime"):
        config_of(contract)


def test_an_aws_job_config_is_unchanged(contract):
    sink = config_of(contract)["sink"]
    assert sink == {
        "catalog": "fluid",
        "type": "glue",
        "warehouse": "s3://commerce-lake/iceberg/commerce/order_lines/",
        "region": "eu-west-1",
        "table": "commerce.order_lines",
    }


def test_validate_accepts_a_local_build(contract):
    assert validation_errors(local(contract)) == []


def test_validate_names_a_local_build_on_an_aws_output(contract):
    contract = local(contract)
    contract["exposes"][0]["binding"]["platform"] = "aws"
    (error,) = validation_errors(contract)
    assert "runtime local writes a table in an Iceberg REST catalog" in error


# --- the runner -------------------------------------------------------------------------


class Conf:
    def __init__(self, values):
        self.values = dict(values)

    def get(self, name, default=None):
        return self.values.get(name, default)

    def set(self, name, value):
        self.values[name] = value


class Spark:
    def __init__(self, conf=()):
        self.conf = Conf(conf)
        self.statements = []

    def sql(self, text):
        self.statements.append(text)


def rest_config(runner, contract, **location):
    contract = local(copy.deepcopy(contract), **location)
    return runner.parse_config(compile_job_config(contract, contract["builds"][0]).text)


def test_a_rest_sink_without_a_declared_catalog_is_refused(runner, contract):
    config = rest_config(runner, contract)
    with pytest.raises(runner.JobConfigError, match="declared by the submitter"):
        runner.configure_session(Spark(), config)


def test_a_declared_catalog_is_used_as_declared(runner, contract):
    config = rest_config(runner, contract)
    spark = Spark({"spark.sql.catalog.fluid": "org.apache.iceberg.spark.SparkCatalog"})
    runner.configure_session(spark, config)
    assert spark.conf.get("spark.sql.defaultCatalog") == "fluid"
    assert spark.conf.get("spark.sql.catalog.fluid.catalog-impl") is None  # nothing derived


def test_a_declared_catalog_that_is_not_the_contract_s_is_refused_without_its_uri(runner, contract):
    config = rest_config(runner, contract, uri="http://lakekeeper:8181/catalog")
    spark = Spark(
        {
            "spark.sql.catalog.fluid": "org.apache.iceberg.spark.SparkCatalog",
            "spark.sql.catalog.fluid.uri": "https://user:s3cret@other:8181/c",  # pragma: allowlist secret
        }
    )
    with pytest.raises(runner.JobConfigError) as caught:
        runner.configure_session(spark, config)
    assert "names another" in str(caught.value) and "s3cret" not in str(caught.value)
    same = Spark(
        {
            "spark.sql.catalog.fluid": "org.apache.iceberg.spark.SparkCatalog",
            "spark.sql.catalog.fluid.uri": "http://lakekeeper:8181/catalog/",
        }
    )
    runner.configure_session(same, config)


def test_the_sink_s_statements_run_in_order(runner, contract):
    config = rest_config(runner, contract)
    spark = Spark()
    runner.create_sink_table(spark, config)
    assert [s.split(" (")[0] for s in spark.statements] == [
        "CREATE NAMESPACE IF NOT EXISTS `fluid`.`commerce`",
        "CREATE TABLE IF NOT EXISTS `fluid`.`commerce`.`order_lines`",
    ]
    glue = runner.parse_config(compile_job_config(contract, contract["builds"][0]).text)
    runner.create_sink_table(spark := Spark(), glue)
    assert spark.statements == []


@pytest.mark.parametrize(
    "sink",
    [
        {"type": "hive", "catalog": "fluid", "table": "a.b"},
        {"type": "rest", "catalog": "fluid", "table": "a.b", "create": ["DROP TABLE a.b"]},
        {"type": "rest", "catalog": "fluid", "table": "a.b", "create": "CREATE TABLE x"},
    ],
)
def test_a_sink_the_runner_does_not_write_is_refused(runner, contract, sink):
    config = json.loads(compile_job_config(contract, contract["builds"][0]).text)
    config["sink"] = sink
    with pytest.raises(runner.JobConfigError):
        runner.parse_config(json.dumps(config))


def test_the_compiler_s_contract_is_not_changed_by_local(contract):
    before = copy.deepcopy(contract)
    local(copy.deepcopy(contract))
    assert contract == before
