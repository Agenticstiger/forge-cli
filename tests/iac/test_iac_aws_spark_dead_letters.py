# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A Spark stream's dead letters on AWS (``properties.stream.deadLetter``): the table
OpenTofu emits beside the output and the job's grants to write it, or the job's grants to
write a dead-letter topic on the cluster it reads. Nothing changes for a stream without
dead letters."""

from __future__ import annotations

import copy
import os
import subprocess

import pytest

from fluid_build.iac import get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.module import build_module
from fluid_build.spark import dead_letter
from fluid_build.spark.compiler import compile_job_config
from tests.iac.test_iac_aws_spark_infra import (  # noqa: F401 - operator_policy is a fixture
    _TOFU,
    CONTRACT,
    KEY,
    MSK_ARN,
    emit,
    operator_policy,
    statement,
)

pytestmark = [pytest.mark.unit, pytest.mark.provider]

TABLE_KEY = "commerce_orders_stream_commerce_order_lines__dead_letter"
GRANT_KEY = "commerce_orders_stream_lf_spark_dead_letters_ingest_orders"
CLUSTER_TAIL = "orders/0a1b2c3d-4e5f-6789-abcd-ef0123456789-2"


@pytest.fixture
def contract():
    return copy.deepcopy(CONTRACT)


def with_dead_letters(contract, **spec):
    contract["builds"][0]["properties"]["stream"]["deadLetter"] = spec
    return contract


def governed(contract, **gov):
    contract["exposes"][0]["binding"]["governance"] = {"lakeFormation": {"grants": [], **gov}}
    return contract


def test_a_stream_without_dead_letters_emits_what_it_did(contract):
    """Only the contract's own text in the catalog (``fluid_contract``) differs."""
    before = emit(contract)
    after = emit(with_dead_letters(copy.deepcopy(contract), enabled=False))
    (resources, data), (resources_now, data_now) = before, after
    assert {k: set(v) for k, v in resources.items()} == {
        k: set(v) for k, v in resources_now.items()
    }
    assert data == data_now  # the job's role and every other document
    assert TABLE_KEY not in after[0]["aws_glue_catalog_table"]


def test_the_dead_letters_table_sits_beside_the_output_never_inside_it(contract):
    resources, data = emit(with_dead_letters(contract))
    table = resources["aws_glue_catalog_table"][TABLE_KEY]
    assert table["name"] == "order_lines__dead_letter"
    storage = table["storage_descriptor"]
    assert storage["location"] == "s3://commerce-lake/iceberg/commerce/order_lines__dead_letter/"
    assert {c["name"]: c["type"] for c in storage["columns"]} == {
        "error": "string",
        "raw_value": "binary",
        "raw_key": "binary",
        "kafka_topic": "string",
        "kafka_partition": "int",
        "kafka_offset": "bigint",
        "kafka_timestamp": "timestamp",
        "dead_lettered_at": "timestamp",
    }
    # The columns a local run creates, in the same order.
    assert [c["name"] for c in storage["columns"]] == [n for n, _, _ in dead_letter.COLUMNS]
    assert table["open_table_format_input"] == {
        "iceberg_input": {"metadata_operation": "CREATE", "version": "2"}
    }
    assert "forge Spark build ingest-orders" in table["description"]
    # The table the job config names is the one OpenTofu creates.
    compiled = with_dead_letters(copy.deepcopy(CONTRACT))
    config = compile_job_config(compiled, compiled["builds"][0]).config
    assert config["deadLetter"]["table"] == "commerce.order_lines__dead_letter"
    access = data["aws_iam_policy_document"][f"{KEY}_access"]
    assert statement(access, "ReadWrite0")["resources"] == [
        "arn:aws:s3:::commerce-lake/iceberg/commerce/order_lines/*",
        "arn:aws:s3:::commerce-lake/iceberg/commerce/order_lines__dead_letter/*",
        "arn:aws:s3:::commerce-lake/_checkpoints/commerce.orders-stream/ingest-orders/*",
    ]
    assert statement(access, "GlueTable")["resources"][-2:] == [
        "arn:aws:glue:eu-west-1:${data.aws_caller_identity.fluid_lf_caller.account_id}"
        ":table/commerce/order_lines",
        "arn:aws:glue:eu-west-1:${data.aws_caller_identity.fluid_lf_caller.account_id}"
        ":table/commerce/order_lines__dead_letter",
    ]
    # A table sink writes no topic.
    assert "MskDeadLetterTopic" not in {s.get("sid") for s in access["statement"]}


def test_a_named_dead_letters_table_is_the_one_emitted_and_granted(contract):
    resources, data = emit(with_dead_letters(contract, table="order_rejects"))
    table = resources["aws_glue_catalog_table"]["commerce_orders_stream_commerce_order_rejects"]
    assert table["storage_descriptor"]["location"].endswith("/iceberg/commerce/order_rejects/")
    glue = statement(data["aws_iam_policy_document"][f"{KEY}_access"], "GlueTable")
    assert glue["resources"][-1].endswith(":table/commerce/order_rejects")


def test_lake_formation_lets_the_job_commit_to_its_dead_letters(contract):
    resources, _ = emit(with_dead_letters(governed(contract)))
    grant = resources["aws_lakeformation_permissions"][GRANT_KEY]
    assert grant["principal"] == f"${{aws_iam_role.{KEY}.arn}}"
    # Appends only: no DELETE, as for the stream's own output.
    assert grant["permissions"] == ["ALTER", "DESCRIBE", "INSERT", "SELECT"]
    assert grant["table"] == [
        {
            "database_name": f"${{aws_glue_catalog_table.{TABLE_KEY}.database_name}}",
            "name": f"${{aws_glue_catalog_table.{TABLE_KEY}.name}}",
        }
    ]
    # Without Lake Formation, the role's own grants are all it needs.
    resources, _ = emit(with_dead_letters(copy.deepcopy(CONTRACT)))
    assert GRANT_KEY not in resources.get("aws_lakeformation_permissions", {})


def test_a_location_forge_registers_refuses_a_dead_letters_table(contract, listed_topic):
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(with_dead_letters(governed(contract, registerLocation=True)))
    assert caught.value.kind == "spark-dead-letters-table-unsupported"
    # A topic needs no location.
    topic = with_dead_letters(
        governed(copy.deepcopy(CONTRACT), registerLocation=True),
        sink="topic",
        topic="commerce.orders.dlq",
    )
    resources, _ = emit(topic)
    assert TABLE_KEY not in resources["aws_glue_catalog_table"]


@pytest.fixture
def listed_topic(monkeypatch):
    """The operator lists the dead letters' topic for writing, beside the examples' entries."""
    from fluid_build.iac.providers.aws_identity import TARGET_ALLOWLIST_ENV

    entries = os.environ[TARGET_ALLOWLIST_ENV]
    monkeypatch.setenv(TARGET_ALLOWLIST_ENV, f"{entries},msk-write:{MSK_ARN}/commerce.orders.dlq")


def test_a_topic_the_operator_has_not_listed_for_writing_is_refused(contract):
    from fluid_build.spark.policy import SparkPolicyError

    with pytest.raises(SparkPolicyError) as caught:
        emit(with_dead_letters(contract, sink="topic", topic="payments.commands"))
    assert f"msk-write:{MSK_ARN}/payments.commands" in caught.value.why


def test_the_streams_own_topic_is_never_its_dead_letters(contract):
    with pytest.raises(ValueError, match="read its dead letters again"):
        compile_job_config(
            with_dead_letters(contract, sink="topic", topic="commerce.orders"),
            contract["builds"][0],
        )


def test_a_dead_letters_topic_may_be_written_on_the_cluster_and_nothing_else(
    contract, listed_topic
):
    resources, data = emit(with_dead_letters(contract, sink="topic", topic="commerce.orders.dlq"))
    access = data["aws_iam_policy_document"][f"{KEY}_access"]
    write = statement(access, "MskDeadLetterTopic")
    assert write["actions"] == ["kafka-cluster:DescribeTopic", "kafka-cluster:WriteData"]
    assert write["resources"] == [
        f"arn:aws:kafka:eu-west-1:000000000005:topic/{CLUSTER_TAIL}/commerce.orders.dlq"
    ]
    # A Kafka producer is idempotent by default, which MSK authorizes on the cluster.
    assert statement(access, "MskIdempotentWrite") == {
        "sid": "MskIdempotentWrite",
        "effect": "Allow",
        "actions": ["kafka-cluster:WriteDataIdempotently"],
        "resources": [MSK_ARN],
    }
    # The source topic stays read-only, and no table is emitted for a topic sink.
    assert "kafka-cluster:WriteData" not in statement(access, "MskTopic")["actions"]
    assert TABLE_KEY not in resources["aws_glue_catalog_table"]


@pytest.mark.integration
@pytest.mark.skipif(_TOFU is None, reason="tofu not on PATH")
def test_the_dead_letters_module_passes_tofu_validate(contract, tmp_path):
    from tests.iac.test_iac_tofu_validate import _tofu_init_or_skip

    module = build_module(get_iac_plugin("aws"), with_dead_letters(governed(contract)))
    (tmp_path / "main.tf.json").write_text(module)
    _tofu_init_or_skip(tmp_path)
    done = subprocess.run(
        [_TOFU, "validate", "-no-color"], cwd=tmp_path, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr or done.stdout


def test_a_bucketless_output_forge_does_not_register_keeps_its_dead_letters_table(contract):
    loc = contract["exposes"][0]["binding"]["location"]
    del loc["bucket"]  # the account's shared bucket, under the product's own prefix
    loc["path"] = "commerce.orders-stream/order_lines/"
    resources, _ = emit(with_dead_letters(governed(contract, registerLocation=True)))
    assert TABLE_KEY in resources["aws_glue_catalog_table"]


@pytest.mark.parametrize(
    ("change", "says"),
    [
        (
            lambda c: c["exposes"][0].update(lifecycle={"retention": "P30D", "expire": True}),
            "lifecycle expires",
        ),
        (lambda c: c["exposes"][0]["binding"]["location"].update(path=""), "bucket's root"),
    ],
)
def test_a_dead_letters_table_forge_cannot_place_or_expire_is_refused(contract, change, says):
    change(contract)
    with pytest.raises(UnsupportedBindingError, match=says) as caught:
        emit(with_dead_letters(contract))
    assert caught.value.kind == "spark-dead-letters-table-unsupported"


def test_two_builds_never_share_a_dead_letters_table(contract):
    second = copy.deepcopy(contract["builds"][0])
    second["id"] = "ingest-orders-again"
    contract["builds"].append(second)
    for build in contract["builds"]:
        build["properties"]["stream"]["deadLetter"] = {"table": "rejects"}
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-address-conflict"
