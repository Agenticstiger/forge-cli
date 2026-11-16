# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The OpenTofu half of a forge Spark build on AWS (``iac/providers/aws_ingest.py``,
``aws_identity.py``): artifacts, log groups, the job's least-privilege role, and what
each of the four runtimes needs. The run itself is the runtime lane's."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess

import pytest

from fluid_build.iac import get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.module import build_module
from fluid_build.iac.providers import aws_identity, aws_ingest
from fluid_build.spark import runner_source
from fluid_build.spark.compiler import CONTRACT_SQL_ALLOWLIST_ENV
from fluid_build.spark.policy import SparkPolicyError
from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV

pytestmark = [pytest.mark.unit, pytest.mark.provider]

MSK_ARN = (
    "arn:aws:kafka:eu-west-1:000000000005:cluster/orders/0a1b2c3d-4e5f-6789-abcd-ef0123456789-2"
)
MASK_KEY = "arn:aws:secretsmanager:eu-west-1:000000000005:secret:commerce/mask-key-AbCdEf"
ROLE = "arn:aws:iam::000000000005:role/data/ingest-orders"
KEY = "commerce_orders_stream_ingest_orders_spark"
VIRTUAL_CLUSTER = "abcdefghij0123456789klmno"  # pragma: allowlist secret - an id

CONTRACT = {
    "fluidVersion": "0.7.7",
    "kind": "DataProduct",
    "id": "commerce.orders-stream",
    "name": "Orders stream",
    "metadata": {"owner": {"team": "commerce-data"}},
    "builds": [
        {
            "id": "ingest-orders",
            "engine": "spark",
            "pattern": "embedded-logic",
            "outputs": ["order_lines"],
            "properties": {
                "stream": {
                    "kind": "kafka",
                    "topic": "commerce.orders",
                    "format": "json",
                    "schema": [
                        {"name": "order_id", "type": "string", "required": True},
                        {"name": "email", "type": "string"},
                    ],
                    "trigger": {"processingTime": "PT1M"},
                },
                "mapping": {
                    "columns": [
                        {"name": "order_id", "from": "order_id"},
                        {"name": "email_hash", "from": "email", "maskAtIngest": "hash"},
                    ]
                },
                "parameters": {
                    "spark": {
                        "runtime": "emr-serverless",
                        "applicationId": "00fabc123def",  # pragma: allowlist secret - an id
                        "source": {"mskClusterArn": MSK_ARN},
                        "maskKeySecretArn": MASK_KEY,
                    }
                },
            },
        }
    ],
    "exposes": [
        {
            "exposeId": "order_lines",
            "kind": "table",
            "binding": {
                "platform": "aws",
                "format": "iceberg",
                "location": {
                    "database": "commerce",
                    "table": "order_lines",
                    "bucket": "commerce-lake",
                    "path": "iceberg/commerce/order_lines/",
                    "region": "eu-west-1",
                },
            },
            "contract": {
                "schema": [
                    {"name": "order_id", "type": "string", "required": True},
                    {"name": "email_hash", "type": "string"},
                ]
            },
        }
    ],
}


@pytest.fixture(autouse=True)
def operator_policy(monkeypatch):
    """No test inherits an operator's lists from the machine it runs on."""
    for name in (
        CONTRACT_SQL_ALLOWLIST_ENV,
        aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV,
        aws_identity.SPARK_CONF_ALLOWLIST_ENV,
        HOST_ALLOWLIST_ENV,
        "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def contract():
    return copy.deepcopy(CONTRACT)


def spark(contract):
    return contract["builds"][0]["properties"]["parameters"]["spark"]


def emit(contract):
    plugin = get_iac_plugin("aws")
    return plugin.emit(contract), plugin.emit_data(contract)


def statement(document, sid):
    return next(s for s in document["statement"] if s.get("sid") == sid)


# --- no Spark build, no change ---------------------------------------------------


def test_a_contract_without_a_spark_build_gets_nothing(contract):
    contract.pop("builds")
    assert aws_ingest.spark_blocks(contract, "x") == aws_ingest.SparkBlocks({}, {})


def test_a_spark_build_before_0_7_7_gets_nothing(contract):
    contract["fluidVersion"] = "0.7.6"
    assert aws_ingest.spark_blocks(contract, "x") == aws_ingest.SparkBlocks({}, {})


# --- artifacts, logs ---------------------------------------------------------------


def test_the_runner_and_job_config_are_objects_in_the_outputs_bucket(contract):
    resources, data = emit(contract)
    objects = resources["aws_s3_object"]
    runner, config = objects[f"{KEY}_runner"], objects[f"{KEY}_config"]
    prefix = "_fluid/spark/commerce.orders-stream/ingest-orders/"
    assert runner["key"] == prefix + "forge_spark_runner.py"
    assert runner["content"] == runner_source()
    assert config["key"] == prefix + "job.json"
    # The product's own bucket, by reference, so the objects wait for it.
    assert runner["bucket"] == "${aws_s3_bucket.commerce_orders_stream_commerce_lake.id}"
    assert data["aws_msk_bootstrap_brokers"][KEY] == {"cluster_arn": MSK_ARN}


def test_the_job_config_takes_the_iam_brokers_from_the_msk_data_source(contract):
    resources, _ = emit(contract)
    text = resources["aws_s3_object"][f"{KEY}_config"]["content"]
    config = json.loads(text.replace(f"${{data.aws_msk_bootstrap_brokers.{KEY}", "X"))
    assert config["source"]["bootstrapServers"] == "X.bootstrap_brokers_sasl_iam}"
    assert config["source"]["auth"] == "msk-iam"


def test_contract_text_in_the_job_config_stays_a_literal(contract, monkeypatch):
    props = contract["builds"][0]["properties"]
    props.pop("mapping")
    props["sql"] = "SELECT order_id, '${file(\"/etc/passwd\")}' AS email_hash FROM source_events"
    monkeypatch.setenv(CONTRACT_SQL_ALLOWLIST_ENV, "commerce.orders-stream")
    resources, _ = emit(contract)
    rendered = json.loads(
        build_module(get_iac_plugin("aws"), contract)
    )  # the renderer escapes every literal ${
    content = rendered["resource"]["aws_s3_object"][f"{KEY}_config"]["content"]
    assert '$${file(\\"/etc/passwd\\")}' in content
    assert "${data.aws_msk_bootstrap_brokers." in content


def test_a_bucketless_output_uses_the_account_bucket_everywhere(contract):
    del contract["exposes"][0]["binding"]["location"]["bucket"]
    resources, data = emit(contract)
    account = "${data.aws_caller_identity.fluid_lf_caller.account_id}"
    config = resources["aws_s3_object"][f"{KEY}_config"]
    assert config["bucket"] == f"{account}-fluid-data"
    assert f'"s3://{account}-fluid-data/iceberg/commerce/order_lines/"' in config["content"]
    access = data["aws_iam_policy_document"][f"{KEY}_access"]
    assert statement(access, "ReadWrite0")["resources"][0] == (
        f"arn:aws:s3:::{account}-fluid-data/iceberg/commerce/order_lines/*"
    )


def test_each_build_logs_to_its_own_group_under_the_products_key(contract):
    resources, data = emit(contract)
    group = resources["aws_cloudwatch_log_group"][KEY]
    assert group["name"] == "/aws/fluid/spark/commerce.orders-stream/ingest-orders"
    assert group["kms_key_id"] == "${aws_kms_key.commerce_orders_stream_spark_logs.arn}"
    key = resources["aws_kms_key"]["commerce_orders_stream_spark_logs"]
    assert key["enable_key_rotation"] is True
    logs = statement(
        data["aws_iam_policy_document"]["commerce_orders_stream_spark_logs_key"],
        "CloudWatchLogs",
    )
    assert logs["condition"][0]["values"][0].endswith(
        ":log-group:/aws/fluid/spark/commerce.orders-stream/*"
    )


# --- the role ---------------------------------------------------------------------


def test_the_role_is_least_privilege(contract):
    _, data = emit(contract)
    access = data["aws_iam_policy_document"][f"{KEY}_access"]
    data_rw = statement(access, "ReadWrite0")
    assert data_rw["resources"] == [
        "arn:aws:s3:::commerce-lake/iceberg/commerce/order_lines/*",
        "arn:aws:s3:::commerce-lake/_checkpoints/commerce.orders-stream/ingest-orders/*",
    ]
    assert statement(access, "Read1")["resources"] == [
        "arn:aws:s3:::commerce-lake/_fluid/spark/commerce.orders-stream/ingest-orders/*"
    ]
    assert statement(access, "Read1")["actions"] == ["s3:GetObject"]
    listing = statement(access, "List2")
    assert listing["resources"] == ["arn:aws:s3:::commerce-lake"]
    assert listing["condition"][0]["variable"] == "s3:prefix"
    kms = statement(access, "S3Kms")
    assert kms["condition"][0] == {
        "test": "StringEquals",
        "variable": "kms:ViaService",
        "values": ["s3.eu-west-1.amazonaws.com"],
    }
    glue = statement(access, "GlueTable")
    assert glue["resources"][1:] == [
        "arn:aws:glue:eu-west-1:${data.aws_caller_identity.fluid_lf_caller.account_id}"
        ":database/commerce",
        "arn:aws:glue:eu-west-1:${data.aws_caller_identity.fluid_lf_caller.account_id}"
        ":table/commerce/order_lines",
    ]
    assert statement(access, "LakeFormationCredentials")["actions"] == [
        "lakeformation:GetDataAccess"
    ]
    assert statement(access, "Secrets")["resources"] == [MASK_KEY]
    secrets_kms = statement(access, "SecretsKms")["condition"]
    assert secrets_kms[0]["values"] == ["secretsmanager.eu-west-1.amazonaws.com"]
    assert secrets_kms[1] == {
        "test": "StringLike",
        "variable": "kms:EncryptionContext:SecretARN",
        "values": [MASK_KEY],
    }
    assert statement(access, "Logs")["resources"] == [
        f"${{aws_cloudwatch_log_group.{KEY}.arn}}",
        f"${{aws_cloudwatch_log_group.{KEY}.arn}}:*",
    ]
    every_action = {a for s in access["statement"] for a in s["actions"]}
    assert not any(a.endswith(":*") or a == "*" for a in every_action), every_action


def test_msk_iam_grants_the_cluster_topic_and_spark_groups_only(contract):
    _, data = emit(contract)
    access = data["aws_iam_policy_document"][f"{KEY}_access"]
    tail = "orders/0a1b2c3d-4e5f-6789-abcd-ef0123456789-2"
    assert statement(access, "MskConnect")["resources"] == [MSK_ARN]
    assert statement(access, "MskTopic")["resources"] == [
        f"arn:aws:kafka:eu-west-1:000000000005:topic/{tail}/commerce.orders"
    ]
    assert statement(access, "MskGroup")["resources"] == [
        f"arn:aws:kafka:eu-west-1:000000000005:group/{tail}/spark-kafka-source-*"
    ]
    spark(contract)["source"]["groupIdPrefix"] = "orders-ingest"
    _, data = emit(contract)
    group = statement(data["aws_iam_policy_document"][f"{KEY}_access"], "MskGroup")
    assert group["resources"] == [
        f"arn:aws:kafka:eu-west-1:000000000005:group/{tail}/orders-ingest*"
    ]


def test_msk_iam_by_broker_list_needs_the_cluster_arn_for_a_forge_role(contract, monkeypatch):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "msk.example")
    spark(contract)["source"] = {"bootstrapServers": "b-1.msk.example:9098", "auth": "msk-iam"}
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-msk-iam-needs-cluster-arn"


def test_emr_serverless_trusts_only_its_application(contract):
    resources, data = emit(contract)
    role = resources["aws_iam_role"][KEY]
    assert role["name"] == "fluid-commerce.orders-stream-ingest-orders-spark"
    trust = data["aws_iam_policy_document"][f"{KEY}_trust"]["statement"][0]
    assert trust["principals"] == [
        {"type": "Service", "identifiers": ["emr-serverless.amazonaws.com"]}
    ]
    source_arn = trust["condition"][1]
    assert source_arn["values"] == [
        "arn:aws:emr-serverless:eu-west-1:${data.aws_caller_identity.fluid_lf_caller.account_id}"
        ":/applications/00fabc123def"
    ]
    assert resources["aws_iam_role_policy"][KEY]["role"] == f"${{aws_iam_role.{KEY}.id}}"
    # The domain's application is referenced, never created.
    assert "aws_emrserverless_application" not in resources


def test_a_role_name_stays_within_iams_limit(contract):
    contract["id"] = "commerce." + "x" * 80
    resources, _ = emit(contract)
    (role,) = resources["aws_iam_role"].values()
    assert len(role["name"]) <= 64


# --- runtimes ---------------------------------------------------------------------


def test_emr_serverless_without_an_application_is_the_domains(contract):
    del spark(contract)["applicationId"]
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-emr-application-shared"


def test_an_isolated_application_auto_stops_is_capped_and_sits_by_the_brokers(contract):
    del spark(contract)["applicationId"]
    contract["packaging"] = {"containers": {"emrApplication": "isolated"}}
    resources, data = emit(contract)
    app = resources["aws_emrserverless_application"]["commerce_orders_stream_spark"]
    assert app["release_label"] == "emr-7.12.0" and app["type"] == "spark"
    assert app["auto_stop_configuration"] == {"enabled": True, "idle_timeout_minutes": 15}
    assert app["maximum_capacity"] == {"cpu": "16 vCPU", "memory": "64 GB"}
    cluster = "data.aws_msk_cluster.commerce_orders_stream_spark"
    assert app["network_configuration"]["subnet_ids"] == (
        f"${{{cluster}.broker_node_group_info[0].client_subnets}}"
    )
    assert data["aws_msk_cluster"]["commerce_orders_stream_spark"] == {"cluster_name": "orders"}
    trust = data["aws_iam_policy_document"][f"{KEY}_trust"]["statement"][0]
    assert trust["condition"][1]["values"][0].endswith(
        ":/applications/${aws_emrserverless_application.commerce_orders_stream_spark.id}"
    )


def test_glue_runs_the_runner_as_a_streaming_job_with_iceberg(contract):
    params = spark(contract)
    params.update(runtime="glue")
    del params["applicationId"]
    resources, data = emit(contract)
    job = resources["aws_glue_job"][KEY]
    assert job["command"]["name"] == "gluestreaming"
    assert job["command"]["script_location"] == (
        f"s3://${{aws_s3_object.{KEY}_runner.bucket}}"
        "/_fluid/spark/commerce.orders-stream/ingest-orders/forge_spark_runner.py"
    )
    args = job["default_arguments"]
    assert args["--datalake-formats"] == "iceberg"
    assert args["--config"].endswith("/ingest-orders/job.json")
    assert args["--continuous-log-logGroup"] == (
        "/aws/fluid/spark/commerce.orders-stream/ingest-orders"
    )
    assert job["glue_version"] == "5.0"
    assert job["role_arn"] == f"${{aws_iam_role.{KEY}.arn}}"
    assert job["execution_property"] == {"max_concurrent_runs": 1}
    assert job["connections"] == [f"${{aws_glue_connection.{KEY}.name}}"]
    assert resources["aws_glue_connection"][KEY]["connection_type"] == "NETWORK"
    trust = data["aws_iam_policy_document"][f"{KEY}_trust"]["statement"][0]
    assert trust["principals"] == [{"type": "Service", "identifiers": ["glue.amazonaws.com"]}]
    # Glue's --TempDir is the one more prefix it writes.
    rw = statement(data["aws_iam_policy_document"][f"{KEY}_access"], "ReadWrite0")
    assert rw["resources"][-1].endswith("/ingest-orders/tmp/*")


def test_a_glue_batch_job_is_glueetl(contract, monkeypatch):
    params = spark(contract)
    params.update(runtime="glue", mode="batch")
    del params["applicationId"], params["source"], params["maskKeySecretArn"]
    props = contract["builds"][0]["properties"]
    props.pop("stream"), props.pop("mapping")
    props["sql"] = "SELECT order_id, order_id AS email_hash FROM commerce.raw_orders"
    monkeypatch.setenv(CONTRACT_SQL_ALLOWLIST_ENV, "commerce.orders-stream")
    resources, data = emit(contract)
    assert resources["aws_glue_job"][KEY]["command"]["name"] == "glueetl"
    assert "aws_glue_connection" not in resources
    assert "MskConnect" not in json.dumps(data)


def test_emr_on_eks_trusts_the_virtual_clusters_service_accounts(contract):
    params = spark(contract)
    params.update(runtime="emr-eks", virtualClusterId=VIRTUAL_CLUSTER)
    del params["applicationId"]
    resources, data = emit(contract)
    assert data["aws_emrcontainers_virtual_cluster"][KEY] == {"virtual_cluster_id": VIRTUAL_CLUSTER}
    trust = data["aws_iam_policy_document"][f"{KEY}_trust"]["statement"][0]
    assert trust["actions"] == ["sts:AssumeRoleWithWebIdentity"]
    assert trust["principals"][0]["type"] == "Federated"
    role_name = resources["aws_iam_role"][KEY]["name"]
    sub = trust["condition"][0]["values"][0]
    assert sub.endswith(
        ":emr-containers-sa-*-*-${data.aws_caller_identity.fluid_lf_caller.account_id}-"
        + aws_identity.base36(role_name)
    )
    assert sub.startswith("system:serviceaccount:${data.aws_emrcontainers_virtual_cluster.")


def test_base36_is_awsclis_encoding():
    assert aws_identity.base36("a") == "2p"  # 97 = 2 * 36 + 25
    assert aws_identity.base36("") == "0"
    assert int(aws_identity.base36("role"), 36) == int.from_bytes(b"role", "big")


def test_emr_on_ec2_needs_its_cluster(contract):
    params = spark(contract)
    params.update(runtime="emr-ec2")
    del params["applicationId"]
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-emr-ec2-needs-cluster"


def test_emr_on_ec2_gets_a_policy_for_the_clusters_role_and_no_role(contract):
    params = spark(contract)
    params.update(runtime="emr-ec2", clusterId="j-1ABCDEF")
    del params["applicationId"]
    resources, data = emit(contract)
    assert "aws_iam_role" not in resources
    policy = resources["aws_iam_policy"][KEY]
    assert policy["policy"] == f"${{data.aws_iam_policy_document.{KEY}_access.json}}"
    assert f"{KEY}_trust" not in data["aws_iam_policy_document"]


# --- the operator's policy ----------------------------------------------------------


def test_a_named_role_is_referenced_only_once_the_operator_lists_it(contract, monkeypatch):
    params = spark(contract)
    params.update(runtime="glue", executionRoleArn=ROLE)
    del params["applicationId"]
    with pytest.raises(SparkPolicyError) as caught:
        emit(contract)
    assert aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV in caught.value.why
    assert any(
        aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV in w
        for w in aws_identity.validation_warnings(contract)
    )
    assert aws_identity.validation_errors(contract) == []
    monkeypatch.setenv(
        aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV, "arn:aws:iam::000000000005:role/data/"
    )
    resources, data = emit(contract)
    assert resources["aws_glue_job"][KEY]["role_arn"] == ROLE
    assert "aws_iam_role" not in resources
    assert f"{KEY}_access" not in data.get("aws_iam_policy_document", {})


def test_a_path_prefix_entry_must_end_in_a_slash(monkeypatch):
    monkeypatch.setenv(
        aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV, "arn:aws:iam::000000000005:role/da"
    )
    assert not aws_identity.role_listed(ROLE)


@pytest.mark.parametrize(
    "key",
    [
        "spark.jars.packages",
        "spark.jars",
        "spark.hadoop.fs.s3a.aws.credentials.provider",
        "spark.sql.catalog.fluid.warehouse",
        "spark.sql.extensions",
        "spark.driver.extraJavaOptions",
        "spark.executorEnv.AWS_ACCESS_KEY_ID",
        "spark.kubernetes.authenticate.driver.serviceAccountName",
        "spark.sql.streaming.checkpointLocation",
        "spark.emr-serverless.driverEnv.AWS_PROFILE",
    ],
)
def test_a_spark_conf_key_that_changes_code_identity_or_paths_is_always_refused(
    contract, monkeypatch, key
):
    spark(contract)["sparkConf"] = {key: "x"}
    monkeypatch.setenv(aws_identity.SPARK_CONF_ALLOWLIST_ENV, f"{key},spark.")
    (error,) = aws_identity.validation_errors(contract)
    assert key in error
    with pytest.raises(SparkPolicyError):
        emit(contract)


def test_tuning_keys_pass_and_other_keys_need_the_operator(contract, monkeypatch):
    spark(contract)["sparkConf"] = {
        "spark.sql.shuffle.partitions": "64",
        "spark.dynamicAllocation.maxExecutors": "8",
        "spark.speculation": "true",
    }
    assert aws_identity.validation_errors(contract) == []
    (warning,) = aws_identity.validation_warnings(contract)
    assert "spark.speculation" in warning and "shuffle" not in warning
    with pytest.raises(SparkPolicyError) as caught:
        emit(contract)
    assert "spark.speculation" in caught.value.why
    monkeypatch.setenv(aws_identity.SPARK_CONF_ALLOWLIST_ENV, "spark.speculation")
    emit(contract)


def test_validate_reports_the_identity_findings(contract, tmp_path, monkeypatch):
    from fluid_build.cli import build_parser
    from fluid_build.cli.validate import run as validate_run

    spark(contract)["sparkConf"] = {"spark.jars.packages": "evil:evil:1"}
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(json.dumps(contract))
    args = build_parser().parse_args(["validate", str(path), "--offline"])
    import logging

    assert validate_run(args, logging.getLogger("t")) != 0


# --- OpenTofu accepts it ------------------------------------------------------------

_TOFU = shutil.which("tofu")


def _runtime(contract, runtime):
    params = spark(contract)
    del params["applicationId"]
    if runtime == "emr-serverless":
        contract["packaging"] = {"containers": {"emrApplication": "isolated"}}
    elif runtime == "emr-eks":
        params["virtualClusterId"] = VIRTUAL_CLUSTER
    elif runtime == "emr-ec2":
        params["clusterId"] = "j-1ABCDEF"
    params["runtime"] = runtime
    return contract


@pytest.mark.integration
@pytest.mark.skipif(_TOFU is None, reason="tofu not on PATH")
@pytest.mark.parametrize("runtime", ["emr-serverless", "glue", "emr-eks", "emr-ec2"])
def test_every_runtime_passes_tofu_validate(contract, tmp_path, runtime):
    from tests.iac.test_iac_tofu_validate import _tofu_init_or_skip

    (tmp_path / "main.tf.json").write_text(
        build_module(get_iac_plugin("aws"), _runtime(contract, runtime))
    )
    _tofu_init_or_skip(tmp_path)
    done = subprocess.run(
        [_TOFU, "validate", "-no-color"], cwd=tmp_path, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr or done.stdout


def test_a_glue_connection_is_named_for_the_build_and_carries_the_emulators_catalog(
    contract, monkeypatch
):
    params = spark(contract)
    params.update(runtime="glue")
    del params["applicationId"]
    resources, _ = emit(contract)
    connection = resources["aws_glue_connection"][KEY]
    assert connection["name"] == "fluid-commerce.orders-stream-ingest-orders-msk"
    assert "catalog_id" not in connection
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")
    resources, _ = emit(contract)
    assert resources["aws_glue_connection"][KEY]["catalog_id"] == (
        "${data.aws_caller_identity.fluid_lf_caller.account_id}"
    )
