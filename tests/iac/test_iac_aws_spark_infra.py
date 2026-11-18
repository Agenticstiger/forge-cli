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
from fluid_build.spark import names, runner_source
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
    # The operator lists the runtimes the examples name, and nothing else.
    monkeypatch.setenv(
        aws_identity.TARGET_ALLOWLIST_ENV,
        f"emr-serverless:00fabc123def,emr-ec2:j-1ABCDEF,emr-eks:{VIRTUAL_CLUSTER},msk:{MSK_ARN}",
    )


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
    sha = names.runner_sha256(runner_source())
    assert runner["key"] == f"{prefix}runner/{sha[:16]}/forge_spark_runner.py"
    assert runner["content"] == runner_source()
    assert config["key"] == prefix + "job.json"
    # The product's own bucket, by reference, so the objects wait for it.
    assert runner["bucket"] == "${aws_s3_bucket.commerce_orders_stream_commerce_lake.id}"
    assert data["aws_msk_bootstrap_brokers"][KEY] == {"cluster_arn": MSK_ARN, "region": "eu-west-1"}


def test_the_job_config_takes_the_iam_brokers_from_the_msk_data_source(contract):
    resources, _ = emit(contract)
    text = resources["aws_s3_object"][f"{KEY}_config"]["content"]
    config = json.loads(text.replace(f"${{data.aws_msk_bootstrap_brokers.{KEY}", "X"))
    assert config["source"]["bootstrapServers"] == "X.bootstrap_brokers_sasl_iam}"
    assert config["source"]["auth"] == "msk-iam"


def test_contract_text_holding_an_interpolation_is_refused(contract, monkeypatch):
    """The runner refuses a config that still holds ``${``, and OpenTofu would write the
    contract's own literally (escaped): it is refused before anything is uploaded."""
    props = contract["builds"][0]["properties"]
    props.pop("mapping")
    props["sql"] = "SELECT order_id, '${file(\"/etc/passwd\")}' AS email_hash FROM source_events"
    monkeypatch.setenv(CONTRACT_SQL_ALLOWLIST_ENV, "commerce.orders-stream")
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-config-interpolation"


def test_the_rendered_job_config_interpolates_only_forges_own_references(contract):
    rendered = json.loads(build_module(get_iac_plugin("aws"), contract))
    content = rendered["resource"]["aws_s3_object"][f"{KEY}_config"]["content"]
    assert content.count("${") == 1
    assert "${data.aws_msk_bootstrap_brokers." in content


def test_a_bucketless_output_uses_the_account_bucket_everywhere(contract):
    location = contract["exposes"][0]["binding"]["location"]
    del location["bucket"]
    # The shared account bucket: the product writes its own prefix only.
    location["path"] = "commerce.orders-stream/order_lines/"
    resources, data = emit(contract)
    account = "${data.aws_caller_identity.fluid_lf_caller.account_id}"
    config = resources["aws_s3_object"][f"{KEY}_config"]
    assert config["bucket"] == f"{account}-fluid-data"
    assert f'"s3://{account}-fluid-data/commerce.orders-stream/order_lines/"' in config["content"]
    access = data["aws_iam_policy_document"][f"{KEY}_access"]
    assert statement(access, "ReadWrite0")["resources"][0] == (
        f"arn:aws:s3:::{account}-fluid-data/commerce.orders-stream/order_lines/*"
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
    glue_arn = "arn:aws:glue:eu-west-1:${data.aws_caller_identity.fluid_lf_caller.account_id}"
    assert glue["resources"][1:] == [
        glue_arn + ":database/commerce",
        glue_arn + ":table/commerce/order_lines",
    ]
    assert statement(access, "LakeFormationCredentials")["actions"] == [
        "lakeformation:GetDataAccess"
    ]
    assert statement(access, "Secrets")["resources"] == [MASK_KEY, f"{MASK_KEY}-??????"]
    secrets_kms = statement(access, "SecretsKms")["condition"]
    assert secrets_kms[0]["values"] == ["secretsmanager.eu-west-1.amazonaws.com"]
    assert secrets_kms[1] == {
        "test": "StringLike",
        "variable": "kms:EncryptionContext:SecretARN",
        "values": [MASK_KEY, f"{MASK_KEY}-??????"],
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
    assert role["name"] == names.execution_role_name("commerce.orders-stream", "ingest-orders")
    trust = data["aws_iam_policy_document"][f"{KEY}_trust"]["statement"][0]
    assert trust["principals"] == [
        {"type": "Service", "identifiers": ["emr-serverless.amazonaws.com"]}
    ]
    source_arn = trust["condition"][1]
    app = (
        "arn:aws:emr-serverless:${data.aws_region.fluid_region.region}"
        ":${data.aws_caller_identity.fluid_lf_caller.account_id}:/applications/00fabc123def"
    )
    assert source_arn["values"] == [app, f"{app}/*"]
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
    assert data["aws_msk_cluster"]["commerce_orders_stream_spark"] == {
        "cluster_name": "orders",
        "region": "eu-west-1",
    }
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
        "/_fluid/spark/commerce.orders-stream/ingest-orders/runner/"
        f"{names.runner_sha256(runner_source())[:16]}/forge_spark_runner.py"
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
    # A batch job reads tables its SQL names: forge cannot derive its role.
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-runtime-field"
    assert any("batch job" in e for e in aws_identity.validation_errors(contract))
    params["executionRoleArn"] = ROLE
    monkeypatch.setenv(aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV, ROLE)
    resources, data = emit(contract)
    assert resources["aws_glue_job"][KEY]["command"]["name"] == "glueetl"
    assert resources["aws_glue_job"][KEY]["role_arn"] == ROLE
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


# --- the role reaches the product's own prefixes only (security review, finding A) ---


@pytest.mark.parametrize(
    "path", ["", "/", "*", "iceberg/*/", "a/../b/", "a/./b/", "${aws:username}/"]
)
def test_a_forge_role_never_gets_a_whole_bucket_or_a_wildcard(contract, path):
    contract["exposes"][0]["binding"]["location"]["path"] = path
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind in ("spark-role-scope", "spark-config-interpolation")


def test_a_prefix_without_its_slash_gets_one(contract):
    contract["exposes"][0]["binding"]["location"]["path"] = "iceberg/commerce/order_lines"
    _, data = emit(contract)
    rw = statement(data["aws_iam_policy_document"][f"{KEY}_access"], "ReadWrite0")
    assert rw["resources"][0] == "arn:aws:s3:::commerce-lake/iceberg/commerce/order_lines/*"


@pytest.mark.parametrize(
    "checkpoint",
    [
        "s3://victim-lake/other-product/",
        "s3://commerce-lake/",
        "s3://commerce-lake/iceberg/other/",
        "s3://commerce-lake/_checkpoints/other.product/x/",
    ],
)
def test_a_declared_checkpoint_must_be_the_products(contract, checkpoint):
    contract["builds"][0]["properties"]["stream"]["checkpoint"] = checkpoint
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind in ("spark-checkpoint-scope", "spark-role-scope")


def test_a_checkpoint_under_the_products_home_is_kept(contract):
    ck = "s3://commerce-lake/_checkpoints/commerce.orders-stream/v2/"
    contract["builds"][0]["properties"]["stream"]["checkpoint"] = ck
    _, data = emit(contract)
    rw = statement(data["aws_iam_policy_document"][f"{KEY}_access"], "ReadWrite0")
    assert "arn:aws:s3:::commerce-lake/_checkpoints/commerce.orders-stream/v2/*" in rw["resources"]


def test_an_operator_scoped_role_takes_any_checkpoint(contract, monkeypatch):
    params = spark(contract)
    params.update(runtime="glue", executionRoleArn=ROLE)
    del params["applicationId"]
    monkeypatch.setenv(aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV, ROLE)
    contract["builds"][0]["properties"]["stream"]["checkpoint"] = "s3://other-lake/ck/"
    resources, _ = emit(contract)
    assert resources["aws_glue_job"][KEY]["role_arn"] == ROLE


@pytest.mark.parametrize("path", ["_fluid/spark/", "_checkpoints/"])
def test_an_output_cannot_live_in_forges_own_roots(contract, path):
    contract["exposes"][0]["binding"]["location"]["path"] = path
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-role-scope"


def test_a_refusal_never_shows_the_internal_account_marker(contract):
    location = contract["exposes"][0]["binding"]["location"]
    del location["bucket"]
    location["path"] = ""
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert "s3://<account>-fluid-data/" in str(caught.value)
    assert "fluidacct" not in str(caught.value)


# --- review round 1 (defect scan, delivery review) ---------------------------------


def test_names_tell_hyphenated_ids_apart():
    """``fluid-a-b-c`` alone is product a-b build c, or product a build b-c."""
    assert names.execution_role_name("a-b", "c") != names.execution_role_name("a", "b-c")
    assert names.glue_job_name("a-b", "c") != names.glue_job_name("a", "b-c")
    assert names.run_name("a-b", "c") != names.run_name("a", "b-c")
    long = names.execution_role_name("p" * 80, "b" * 80)
    assert len(long) <= 64 and long.endswith(f"-{names.build_key('p' * 80, 'b' * 80)}-spark")


def test_two_build_ids_that_meet_at_one_address_are_refused(contract):
    second = copy.deepcopy(contract["builds"][0])
    second["id"] = "ingest_orders"
    contract["builds"].append(second)
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-build-id-clash"


def test_a_contract_mixing_key_types_still_emits(contract):
    contract["metadata"]["labels"] = {2024: "year", "owner": "x"}
    emit(contract)


@pytest.mark.parametrize(
    "field, value",
    [("applicationId", "*"), ("applicationId", ["x"]), ("glueVersion", [1])],
)
def test_runtime_fields_are_checked_before_they_reach_the_module(contract, field, value):
    spark(contract)[field] = value
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-runtime-field"
    assert any(field in e for e in aws_identity.validation_errors(contract))


def test_emr_on_eks_without_its_virtual_cluster_is_refused_not_a_key_error(contract):
    params = spark(contract)
    params.update(runtime="emr-eks")
    del params["applicationId"]
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-runtime-field"


def test_a_cluster_arn_without_its_id_fails_validate_too(contract):
    spark(contract)["source"][
        "mskClusterArn"
    ] = "arn:aws:kafka:eu-west-1:000000000005:cluster/orders"
    assert any("mskClusterArn" in e for e in aws_identity.validation_errors(contract))


@pytest.mark.parametrize("runtime", ["emr-serverless", "glue"])
def test_the_network_follows_the_cluster_whatever_the_auth(contract, runtime):
    params = spark(contract)
    params["source"]["auth"] = "none"
    del params["applicationId"]
    params["runtime"] = runtime
    contract["packaging"] = {"containers": {"emrApplication": "isolated"}}
    resources, data = emit(contract)
    if runtime == "glue":
        assert KEY in resources["aws_glue_connection"]
    else:
        app = resources["aws_emrserverless_application"]["commerce_orders_stream_spark"]
        assert "network_configuration" in app
    assert "MskConnect" not in json.dumps(data)  # no IAM grants without MSK IAM


def test_an_isolated_application_refuses_builds_on_different_networks(contract, monkeypatch):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "msk.example")
    del spark(contract)["applicationId"]
    contract["packaging"] = {"containers": {"emrApplication": "isolated"}}
    second = copy.deepcopy(contract["builds"][0])
    second["id"] = "replay"
    contract["builds"].insert(0, second)
    second_params = second["properties"]["parameters"]["spark"]
    second_params["source"] = {"bootstrapServers": "b-1.msk.example:9098"}
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-emr-application-conflict"


def test_msk_lookups_go_to_the_clusters_region(contract):
    _, data = emit(contract)
    assert data["aws_msk_bootstrap_brokers"][KEY]["region"] == "eu-west-1"


def test_a_name_locator_secret_keeps_its_account_interpolation(contract, monkeypatch):
    from fluid_build.iac.module import render_tofu_json

    params = spark(contract)
    stream = contract["builds"][0]["properties"]["stream"]
    stream.update(format="avro-confluent")
    stream["registry"] = {
        "kind": "confluent",
        "url": "https://registry.example.internal",
        "auth": {"secretRef": "aws-sm://commerce/registry-secret"},  # pragma: allowlist secret
    }
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "example.internal")
    monkeypatch.setenv(
        "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST",
        "aws-sm://commerce/registry-secret@registry.example.internal",
    )
    del params["maskKeySecretArn"]
    contract["builds"][0]["properties"]["mapping"]["columns"][1].pop("maskAtIngest")
    _, data = emit(contract)
    secrets = statement(data["aws_iam_policy_document"][f"{KEY}_access"], "Secrets")
    rendered = render_tofu_json({"data": {"x": {"y": {"r": secrets["resources"]}}}})
    assert "$${data.aws_caller_identity" not in rendered
    assert rendered.count("${data.aws_caller_identity.fluid_lf_caller.account_id}") == 2


def test_a_glue_job_in_a_vpc_can_create_its_network_interfaces(contract):
    params = spark(contract)
    params.update(runtime="glue")
    del params["applicationId"]
    _, data = emit(contract)
    vpc = statement(data["aws_iam_policy_document"][f"{KEY}_access"], "GlueVpc")
    assert "ec2:CreateNetworkInterface" in vpc["actions"]


def test_a_streaming_build_gets_no_caller_policy(contract):
    resources, _ = emit(contract)
    assert "aws_iam_policy" not in resources


def test_a_run_to_completion_build_gets_the_schedulers_policy(contract):
    contract["builds"][0]["properties"]["stream"]["trigger"] = {"availableNow": True}
    resources, data = emit(contract)
    policy = resources["aws_iam_policy"]["commerce_orders_stream_spark_caller"]
    assert policy["name"] == "fluid-commerce.orders-stream-spark-caller"
    document = data["aws_iam_policy_document"]["commerce_orders_stream_spark_caller"]
    sids = {s["sid"] for s in document["statement"]}
    run = next(s for s in document["statement"] if s["sid"].startswith("Run"))
    assert "emr-serverless:StartJobRun" in run["actions"]
    assert run["resources"][0].endswith(":/applications/00fabc123def")
    passrole = next(s for s in document["statement"] if s["sid"].startswith("PassRole"))
    assert passrole["resources"] == [f"${{aws_iam_role.{KEY}.arn}}"]
    assert passrole["condition"][0]["values"] == ["emr-serverless.amazonaws.com"]
    artifacts = next(s for s in document["statement"] if s["sid"].startswith("Artifacts"))
    assert artifacts["resources"][0].endswith("/ingest-orders/job.json")
    assert not any(s.startswith("FindApplication") for s in sids)


# --- Lake Formation writes (item 4) ---------------------------------------------------


def registration_role_key(resources):
    (registration,) = resources["aws_lakeformation_resource"].values()
    return registration["role_arn"].removeprefix("${aws_iam_role.").removesuffix(".arn}")


def _with_lake_formation(contract, **gov):
    contract["exposes"][0]["binding"]["governance"] = {
        "lakeFormation": {"registerLocation": True, **gov}
    }
    return contract


def test_a_location_a_spark_job_writes_is_registered_with_a_role_that_can_write(contract):
    resources, data = emit(_with_lake_formation(contract))
    (registration,) = resources["aws_lakeformation_resource"].values()
    role = registration_role_key(resources)
    assert registration["use_service_linked_role"] is False
    assert registration["role_arn"] == f"${{aws_iam_role.{role}.arn}}"
    # The role and its policy exist before Lake Formation (re-)registers the location.
    assert registration["depends_on"] == [f"aws_iam_role.{role}", f"aws_iam_role_policy.{role}"]
    trust = data["aws_iam_policy_document"][f"{role}_trust"]["statement"][0]
    assert trust["principals"] == [
        {"type": "Service", "identifiers": ["lakeformation.amazonaws.com"]}
    ]
    access = data["aws_iam_policy_document"][f"{role}_access"]
    rw = statement(access, "ReadWrite")
    assert "s3:PutObject" in rw["actions"]
    assert rw["resources"] == ["arn:aws:s3:::commerce-lake/iceberg/commerce/order_lines/*"]
    name = resources["aws_iam_role"][role]["name"]
    assert len(name) <= 64 and name.endswith("-lf")


def test_the_job_role_may_commit_to_the_table_through_lake_formation(contract):
    resources, _ = emit(_with_lake_formation(contract))
    grant = resources["aws_lakeformation_permissions"][
        "commerce_orders_stream_lf_spark_writer_order_lines_0"
    ]
    assert grant["principal"] == f"${{aws_iam_role.{KEY}.arn}}"
    # A stream appends: no DELETE, and no DATA_LOCATION_ACCESS (the table exists).
    assert grant["permissions"] == ["ALTER", "DESCRIBE", "INSERT", "SELECT"]


def test_a_location_no_spark_job_writes_keeps_the_service_linked_role(contract):
    contract.pop("builds")
    resources, _ = emit(_with_lake_formation(contract))
    (registration,) = resources["aws_lakeformation_resource"].values()
    assert registration == {
        "arn": "arn:aws:s3:::commerce-lake/iceberg/commerce/order_lines/",
        "use_service_linked_role": True,
    }
    assert "aws_iam_role" not in resources


def test_lake_formation_without_registration_still_grants_the_writer(contract):
    contract["exposes"][0]["binding"]["governance"] = {"lakeFormation": {"grants": []}}
    resources, data = emit(contract)
    assert "aws_lakeformation_resource" not in resources
    assert not any("_lf_writer_" in k for k in resources.get("aws_iam_role", {}))
    assert resources["aws_lakeformation_permissions"]


def test_emr_on_ec2_gets_no_grant_for_a_role_forge_does_not_know(contract):
    params = spark(contract)
    params.update(runtime="emr-ec2", clusterId="j-1ABCDEF")
    del params["applicationId"]
    resources, _ = emit(_with_lake_formation(contract))
    assert not any("spark_writer" in k for k in resources.get("aws_lakeformation_permissions", {}))


@pytest.mark.integration
@pytest.mark.skipif(_TOFU is None, reason="tofu not on PATH")
def test_the_lake_formation_write_path_passes_tofu_validate(contract, tmp_path):
    from tests.iac.test_iac_tofu_validate import _tofu_init_or_skip

    (tmp_path / "main.tf.json").write_text(
        build_module(get_iac_plugin("aws"), _with_lake_formation(contract))
    )
    _tofu_init_or_skip(tmp_path)
    done = subprocess.run(
        [_TOFU, "validate", "-no-color"], cwd=tmp_path, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr or done.stdout


# --- review round 2 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "runtime, field, value",
    [
        ("emr-serverless", "applicationId", "00fvictim"),
        ("emr-ec2", "clusterId", "j-VICTIM1234"),
        ("emr-eks", "virtualClusterId", "victimvc"),
    ],
)
def test_an_existing_runtime_is_named_only_once_the_operator_lists_it(
    contract, runtime, field, value
):
    params = spark(contract)
    params.pop("applicationId")
    params.update({"runtime": runtime, field: value})
    (warning,) = aws_identity.validation_warnings(contract)
    assert f"{runtime}:{value}" in warning
    with pytest.raises(SparkPolicyError) as caught:
        emit(contract)
    assert aws_identity.TARGET_ALLOWLIST_ENV in caught.value.why


def test_an_isolated_application_needs_no_operator_entry(contract, monkeypatch):
    # Only the cluster it reads is listed: the product's own application needs no entry.
    monkeypatch.setenv(aws_identity.TARGET_ALLOWLIST_ENV, f"msk:{MSK_ARN}")
    del spark(contract)["applicationId"]
    contract["packaging"] = {"containers": {"emrApplication": "isolated"}}
    assert aws_identity.validation_warnings(contract) == []
    emit(contract)


def test_a_contract_with_date_keys_still_emits(contract):
    import datetime

    contract["metadata"]["retention"] = {datetime.date(2024, 1, 1): "x"}
    emit(contract)


@pytest.mark.parametrize(
    "location",
    [
        {"path": None},
        {
            "bucket": "{{ env.FLUID_TEST_UNSET_LAKE }}",
            "path": "commerce.orders-stream/order_lines/",
        },
    ],
    ids=["bucket-root", "unresolved-template"],
)
def test_a_spark_written_registration_is_one_named_prefix(contract, location, monkeypatch):
    monkeypatch.delenv("FLUID_TEST_UNSET_LAKE", raising=False)
    loc = contract["exposes"][0]["binding"]["location"]
    for k, v in location.items():
        if v is None:
            loc.pop(k)
        else:
            loc[k] = v
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(_with_lake_formation(contract))
    assert caught.value.kind == "spark-lf-registration-scope"


def test_locations_that_fold_alike_get_their_own_roles(contract):
    second = copy.deepcopy(contract["exposes"][0])
    second["exposeId"] = "order_lines_v2"
    second["binding"]["location"].update(table="order_lines_v2", path="iceberg_commerce/v2/")
    contract["exposes"][0]["binding"]["location"]["path"] = "iceberg/commerce/v2/"
    contract["exposes"].append(second)
    build = copy.deepcopy(contract["builds"][0])
    build["id"] = "ingest-v2"
    build["outputs"] = ["order_lines_v2"]
    contract["builds"].append(build)
    for expose in contract["exposes"]:
        expose["binding"]["governance"] = {"lakeFormation": {"registerLocation": True}}
    resources, _ = emit(contract)
    assert len([k for k in resources["aws_iam_role"] if "_lf_writer_" in k]) == 2


def test_two_exposes_on_one_location_share_its_registration_role(contract):
    second = copy.deepcopy(contract["exposes"][0])
    second["exposeId"] = "order_lines_v2"
    second["binding"]["location"]["table"] = "order_lines_v2"
    contract["exposes"].append(second)
    build = copy.deepcopy(contract["builds"][0])
    build["id"] = "ingest-v2"
    build["outputs"] = ["order_lines_v2"]
    contract["builds"].append(build)
    for expose in contract["exposes"]:
        expose["binding"]["governance"] = {"lakeFormation": {"registerLocation": True}}
    resources, _ = emit(contract)
    (registration,) = resources["aws_lakeformation_resource"].values()
    writers = [k for k in resources["aws_iam_role"] if "_lf_writer_" in k]
    assert len(writers) == 1
    assert registration["role_arn"] == f"${{aws_iam_role.{writers[0]}.arn}}"


def test_the_registration_role_is_scoped_even_with_an_operator_role(contract, monkeypatch):
    params = spark(contract)
    params.update(runtime="glue", executionRoleArn=ROLE)
    del params["applicationId"]
    monkeypatch.setenv(aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV, ROLE)
    contract["exposes"][0]["binding"]["location"]["path"] = "*"
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(_with_lake_formation(contract))
    assert caught.value.kind == "spark-role-scope"


def test_a_batch_build_on_emr_on_ec2_runs_as_the_clusters_role(contract):
    params = spark(contract)
    params.update(runtime="emr-ec2", clusterId="j-1ABCDEF", mode="batch")
    del params["applicationId"], params["source"], params["maskKeySecretArn"]
    props = contract["builds"][0]["properties"]
    props.pop("stream"), props.pop("mapping")
    props["sql"] = "SELECT order_id, order_id AS email_hash FROM commerce.raw_orders"
    assert not any("batch job" in e for e in aws_identity.validation_errors(contract))


# --- independent security review of #918 (HIGH, MEDIUM) --------------------------------


def test_an_msk_cluster_is_read_only_once_the_operator_lists_it(contract, monkeypatch):
    """A contract naming another team's cluster would get Connect/ReadData on its topics."""
    other = "arn:aws:kafka:eu-west-1:000000000005:cluster/payments-team/1111-2222"
    spark(contract)["source"]["mskClusterArn"] = other
    (warning,) = [w for w in aws_identity.validation_warnings(contract) if f"msk:{other}" in w]
    assert aws_identity.TARGET_ALLOWLIST_ENV in warning
    with pytest.raises(SparkPolicyError) as caught:
        emit(contract)
    assert f"msk:{other}" in caught.value.why
    # The compiler refuses it too (a run compiles without the IaC).
    from fluid_build.spark.compiler import compile_job_config

    with pytest.raises(SparkPolicyError):
        compile_job_config(contract, contract["builds"][0], account_ref="000000000005")


@pytest.mark.parametrize(
    "location, packaging",
    [
        ({"bucket": None, "path": "payments/cards/"}, None),
        ({"bucket": "shared-pool", "path": "payments/cards/"}, {"mode": "shared", "pool": "p"}),
    ],
    ids=["fallback-bucket", "pool-bucket"],
)
def test_a_shared_bucket_prefix_must_be_the_products(contract, location, packaging):
    loc = contract["exposes"][0]["binding"]["location"]
    for k, v in location.items():
        if v is None:
            loc.pop(k)
        else:
            loc[k] = v
    if packaging:
        contract["packaging"] = packaging
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-role-shared-prefix"
    loc["path"] = "commerce.orders-stream/order_lines/"
    emit(contract)


def test_conf_prefixes_share_the_runs_lanes_star_grammar(contract, monkeypatch):
    spark(contract)["sparkConf"] = {"spark.sql.streaming.metricsEnabled": "true"}
    monkeypatch.setenv(aws_identity.SPARK_CONF_ALLOWLIST_ENV, "spark.sql.streaming.*")
    emit(contract)
    for key in (
        "spark.sql.adaptive.customCostEvaluatorClass",
        "spark.sql.streaming.stateStore.providerClass",
    ):
        spark(contract)["sparkConf"] = {key: "x"}
        assert any(key in e for e in aws_identity.validation_errors(contract))


def test_the_glue_role_trusts_only_its_job_and_tags_only_glues_interfaces(contract):
    params = spark(contract)
    params.update(runtime="glue")
    del params["applicationId"]
    _, data = emit(contract)
    trust = data["aws_iam_policy_document"][f"{KEY}_trust"]["statement"][0]
    job = names.glue_job_name("commerce.orders-stream", "ingest-orders")
    assert trust["condition"][1]["values"][0].endswith(f":job/{job}")
    access = data["aws_iam_policy_document"][f"{KEY}_access"]
    tags = statement(access, "GlueVpcTags")
    assert tags["condition"][0]["values"] == ["aws-glue-service-resource"]
    delete = statement(access, "GlueVpcDelete")
    assert delete["condition"][0]["variable"] == "aws:ResourceTag/aws-glue-service-resource"
    assert "ec2:DeleteNetworkInterface" not in statement(access, "GlueVpc")["actions"]


def test_the_account_bucket_named_literally_is_still_shared(contract):
    loc = contract["exposes"][0]["binding"]["location"]
    loc.update(bucket="000000000005-fluid-data", path="payments/cards/")
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-role-shared-prefix"


def test_a_glue_role_tags_and_deletes_only_interfaces_in_its_subnet(contract):
    params = spark(contract)
    params.update(runtime="glue")
    del params["applicationId"]
    _, data = emit(contract)
    access = data["aws_iam_policy_document"][f"{KEY}_access"]
    subnet = {
        "test": "ArnEquals",
        "variable": "ec2:Subnet",
        "values": [f"${{data.aws_subnet.{KEY}.arn}}"],
    }
    tags = statement(access, "GlueVpcTags")
    assert tags["actions"] == ["ec2:CreateTags"] and subnet in tags["condition"]
    assert {"test": "Null", "variable": "aws:TagKeys", "values": ["false"]} in tags["condition"]
    assert subnet in statement(access, "GlueVpcDelete")["condition"]


def test_the_lf_role_writes_only_the_products_prefix_of_the_account_bucket(contract, monkeypatch):
    """Re-check of #918: the registration role followed the pool rule only, so the account
    bucket named literally (no packaging block) let it write another product's prefix."""
    params = spark(contract)
    params.update(runtime="glue", executionRoleArn=ROLE)
    del params["applicationId"]
    monkeypatch.setenv(aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV, ROLE)
    loc = contract["exposes"][0]["binding"]["location"]
    loc.update(bucket="000000000005-fluid-data", path="payments/cards/")
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(_with_lake_formation(contract))
    assert caught.value.kind == "spark-role-shared-prefix"
    loc["path"] = "commerce.orders-stream/order_lines/"
    resources, _ = emit(_with_lake_formation(contract))
    assert any("_lf_writer_" in k for k in resources["aws_iam_role"])


@pytest.mark.parametrize(
    "auth, listener", [("none", "bootstrap_brokers_tls"), ("msk-iam", "bootstrap_brokers_sasl_iam")]
)
def test_the_job_config_is_the_compilers_with_only_the_brokers_filled(contract, auth, listener):
    """One writer for job.json: the compiler. The IaC fills the MSK broker placeholder and
    nothing else; without IAM it reads the TLS listener, as MSK encrypts client traffic by
    default and its plaintext list is then empty (#922)."""
    from fluid_build.spark.compiler import MSK_BROKERS_PLACEHOLDER, compile_job_config

    spark(contract)["source"]["auth"] = auth
    resources, _ = emit(contract)
    text = resources["aws_s3_object"][f"{KEY}_config"]["content"]
    filled = f"${{data.aws_msk_bootstrap_brokers.{KEY}.{listener}}}"
    assert filled in text
    compiled = compile_job_config(
        contract, contract["builds"][0], account_ref="000000000005"
    ).config
    rendered = json.loads(text.replace(filled, MSK_BROKERS_PLACEHOLDER))
    assert rendered == compiled


@pytest.mark.parametrize(
    ("runtime", "label", "says"),
    [
        ("emr-eks", "emr-7.12.0", "emr-X.Y.Z-latest or emr-X.Y.Z-YYYYMMDD"),
        ("emr-serverless", "emr-7.12.0-latest", "takes emr-X.Y.Z (e.g. emr-7.12.0)"),
    ],
)
def test_a_release_label_in_another_runtime_s_form_is_refused(contract, runtime, label, says):
    params = spark(contract)
    params.update(runtime=runtime, releaseLabel=label)
    if runtime == "emr-eks":
        del params["applicationId"]
        params["virtualClusterId"] = VIRTUAL_CLUSTER
    (error,) = [e for e in aws_identity.runtime_errors(contract["builds"][0]) if "release" in e]
    assert says in error
    with pytest.raises(UnsupportedBindingError, match="releaseLabel"):
        emit(contract)


def test_emr_on_eks_emits_with_its_image_label(contract):
    params = spark(contract)
    del params["applicationId"]
    params.update(runtime="emr-eks", virtualClusterId=VIRTUAL_CLUSTER)
    assert aws_identity.runtime_errors(contract["builds"][0]) == []
    params["releaseLabel"] = "emr-7.12.0-20251111"
    assert aws_identity.runtime_errors(contract["builds"][0]) == []
    emit(contract)
