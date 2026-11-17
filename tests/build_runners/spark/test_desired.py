# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

import copy

import pytest

from fluid_build.build_runners.spark import _desired
from fluid_build.secrets.policy import ALLOWLIST_ENV as SECRET_REF_ALLOWLIST_ENV
from fluid_build.spark import names as _names
from fluid_build.spark.compiler import CONTRACT_SQL_ALLOWLIST_ENV
from tests.build_runners.spark.conftest import ACCOUNT, runs_of, spark_cfg


def _stream(contract):
    return contract["builds"][0]["properties"]["stream"]


def test_plan_action_carries_op_and_action_type_and_the_payload(run):
    action = run.plan_action(3)
    assert action["op"] == action["action_type"] == "spark.reconcile"
    assert (action["step"], action["action_id"], action["id"]) == (
        3,
        "spark_ingest-orders",
        "ingest-orders",
    )
    assert set(action["payload"]) == {
        "product", "build", "runtime", "mode", "scheduled", "region", "target",
        "executionRoleArn", "artifacts", "jobConfigDigest", "runnerSha256", "configHash",
        "sourceId", "checkpointId", "stateImpact",
    }  # fmt: skip
    assert action["payload"]["configHash"] == run.config_hash
    assert set(action["payload"]["artifacts"]) == {"jobConfig", "runner"}


def test_config_hash_is_deterministic(contract):
    assert runs_of(contract)[0].config_hash == runs_of(copy.deepcopy(contract))[0].config_hash


def _spark_conf(c):
    spark_cfg(c)["sparkConf"] = {"spark.executor.memory": "4g"}


def _release(c):
    spark_cfg(c)["releaseLabel"] = "emr-7.5.0"


def _mapping(c):
    c["builds"][0]["properties"]["mapping"]["columns"][0]["from"] = "customer.email"


@pytest.mark.parametrize("change", [_spark_conf, _release, _mapping])
def test_config_hash_changes_with_the_config(contract, change):
    before = runs_of(contract)[0].config_hash
    change(contract)
    assert runs_of(contract)[0].config_hash != before


def test_config_hash_changes_with_the_runner_source(contract, monkeypatch):
    before = runs_of(contract)[0]
    monkeypatch.setattr("fluid_build.spark.runner_source", lambda: "# another runner\n")
    after = runs_of(contract)[0]
    assert after.config_hash != before.config_hash
    assert after.runner_sha256 != before.runner_sha256
    assert after.runner_uri != before.runner_uri


def test_a_new_topic_changes_the_source_but_not_the_checkpoint(contract):
    before = runs_of(contract)[0]
    _stream(contract)["topic"] = "commerce.orders-v2"
    after = runs_of(contract)[0]
    assert after.source_id != before.source_id
    assert after.checkpoint_id == before.checkpoint_id
    assert after.topic == "commerce.orders-v2"


def test_a_declared_checkpoint_changes_the_checkpoint_id(contract):
    before = runs_of(contract)[0]
    _stream(contract)["checkpoint"] = "s3://commerce-lake/_cp/orders-v2/"
    after = runs_of(contract)[0]
    assert after.checkpoint_id != before.checkpoint_id
    assert after.source_id == before.source_id


def test_a_stream_with_a_processing_time_trigger_is_long_running(run):
    assert run.scheduled is False and run.long_running


def test_available_now_is_scheduled(contract):
    _stream(contract)["trigger"] = {"availableNow": True}
    assert runs_of(contract)[0].scheduled is True


def test_a_batch_build_is_scheduled(contract, monkeypatch):
    monkeypatch.setenv(CONTRACT_SQL_ALLOWLIST_ENV, "commerce.orders-stream")
    props = contract["builds"][0]["properties"]
    props.pop("stream"), props.pop("mapping")
    props["sql"] = "SELECT order_id, sku FROM commerce.order_lines"
    spark_cfg(contract).update(runtime="emr-eks", virtualClusterId="vc123", mode="batch")
    spark_cfg(contract).pop("source")
    batch = runs_of(contract)[0]
    assert (batch.mode, batch.scheduled, batch.source_id) == ("batch", True, None)
    assert batch.target == {"virtualClusterId": "vc123"}


def test_emr_ec2_needs_a_cluster_id(contract):
    spark_cfg(contract)["runtime"] = "emr-ec2"
    with pytest.raises(ValueError, match="clusterId"):
        runs_of(contract)
    spark_cfg(contract)["clusterId"] = "j-ABC"
    assert runs_of(contract)[0].target == {"clusterId": "j-ABC"}


def test_serverless_targets_the_application_by_name_unless_given_an_id(contract):
    assert runs_of(contract)[0].target == {"applicationName": "fluid-commerce.orders-stream-spark"}
    spark_cfg(contract)["applicationId"] = "00abc"
    assert runs_of(contract)[0].target == {"applicationId": "00abc"}


def test_the_default_role_is_in_the_given_account(contract):
    run = runs_of(contract)[0]
    assert run.role_arn == (
        f"arn:aws:iam::{ACCOUNT}:role/"
        + _names.execution_role_name("commerce.orders-stream", "ingest-orders")
    )


def test_the_default_role_uses_the_china_partition(contract):
    contract["exposes"][0]["binding"]["location"]["region"] = "cn-north-1"
    assert runs_of(contract)[0].role_arn.startswith(f"arn:aws-cn:iam::{ACCOUNT}:role/")


def test_streaming_serverless_retries_default_to_five_and_cap_at_ten(contract):
    assert runs_of(contract)[0].retry_attempts == 5
    contract["builds"][0]["execution"] = {"retries": {"maxAttempts": 50}}
    assert runs_of(contract)[0].retry_attempts == 10
    contract["builds"][0]["execution"] = {"retries": {"maxAttempts": 3}}
    assert runs_of(contract)[0].retry_attempts == 3


def test_avro_confluent_adds_the_abris_package_and_the_confluent_repository(contract, monkeypatch):
    ref = "aws-sm://commerce/registry#user_info"  # pragma: allowlist secret
    monkeypatch.setenv(SECRET_REF_ALLOWLIST_ENV, f"{ref}@registry.example.internal")
    _stream(contract).update(
        format="avro-confluent",
        registry={
            "kind": "confluent",
            "url": "https://registry.example.internal",
            "auth": {"secretRef": ref},
        },
    )
    conf = runs_of(contract)[0].spark_conf
    packages = conf["spark.jars.packages"].split(",")
    assert any("abris" in p for p in packages) and any("spark-sql-kafka" in p for p in packages)
    assert conf["spark.jars.repositories"] == _desired._CONFLUENT_REPOSITORY


def test_a_json_stream_adds_no_avro_package(run):
    assert "abris" not in run.spark_conf["spark.jars.packages"]
    assert "aws-msk-iam-auth" in run.spark_conf["spark.jars.packages"]
    assert "spark.jars.repositories" not in run.spark_conf


def test_an_operator_listed_spark_conf_overrides_the_defaults(contract, monkeypatch):
    monkeypatch.setenv(_desired.CONF_ALLOWLIST_ENV, "spark.jars")
    spark_cfg(contract)["sparkConf"] = {"spark.jars": "/mine.jar", "spark.executor.memory": 4}
    conf = runs_of(contract)[0].spark_conf
    assert conf["spark.jars"] == "/mine.jar"
    assert conf["spark.executor.memory"] == "4"
    assert "spark.sql.extensions" in conf


def test_duplicate_build_ids_raise(contract):
    contract["builds"].append(copy.deepcopy(contract["builds"][0]))
    with pytest.raises(ValueError, match="unique"):
        runs_of(contract)


def test_a_build_filter_selects_one(contract):
    assert runs_of(contract, build_id="nope") == []
    assert [r.build for r in runs_of(contract, build_id="ingest-orders")] == ["ingest-orders"]


def test_identity_args_and_tags_carry_the_hash_source_and_checkpoint(run):
    assert run.entry_args()[:2] == ["--config", run.job_uri]
    tags = run.tags()
    assert tags["fluid_config_hash"] == run.config_hash
    assert set(tags) >= {"fluid_product", "fluid_build", "fluid_source"}


@pytest.mark.parametrize(
    "key",
    [
        "spark.jars",
        "spark.jars.packages",
        "spark.jars.repositories",
        "spark.sql.extensions",
        "spark.driver.extraJavaOptions",
        "spark.executor.extraClassPath",
        "spark.plugins",
        "spark.submit.pyFiles",
        "spark.sql.catalog.fluid.catalog-impl",
        "spark.hadoop.fs.s3a.endpoint",
        "spark.executorEnv.AWS_ROLE_ARN",
        "spark.emr-serverless.driverEnv.X",
        "spark.made.up",
        "spark.sql.adaptive.customCostEvaluatorClass",
    ],
)
def test_spark_conf_that_could_load_code_is_refused_unless_the_operator_lists_it(
    contract, monkeypatch, key
):
    from fluid_build.spark.policy import SparkPolicyError

    contract["builds"][0]["properties"]["parameters"]["spark"]["sparkConf"] = {key: "x"}
    with pytest.raises(SparkPolicyError) as err:
        _desired.desired_runs(contract, account=ACCOUNT)
    assert key in err.value.what and _desired.CONF_ALLOWLIST_ENV in err.value.fix
    monkeypatch.setenv(_desired.CONF_ALLOWLIST_ENV, key)
    assert _desired.desired_runs(contract, account=ACCOUNT)[0].spark_conf[key] == "x"


def test_spark_conf_tuning_is_allowed_and_a_value_with_whitespace_is_not(contract):
    from fluid_build.spark.policy import SparkPolicyError

    spark = contract["builds"][0]["properties"]["parameters"]["spark"]
    spark["sparkConf"] = {"spark.executor.memory": "4g", "spark.dynamicAllocation.enabled": "true"}
    conf = _desired.desired_runs(contract, account=ACCOUNT)[0].spark_conf
    assert (
        conf["spark.executor.memory"] == "4g" and conf["spark.dynamicAllocation.enabled"] == "true"
    )
    spark["sparkConf"] = {"spark.executor.memory": "4g --jars s3://x/y.jar"}
    with pytest.raises(SparkPolicyError):
        _desired.desired_runs(contract, account=ACCOUNT)


def test_an_operator_prefix_allows_a_family(contract, monkeypatch):
    monkeypatch.setenv(_desired.CONF_ALLOWLIST_ENV, "spark.sql.streaming.*")
    spark = contract["builds"][0]["properties"]["parameters"]["spark"]
    spark["sparkConf"] = {"spark.sql.streaming.minBatchesToRetain": "50"}
    assert _desired.desired_runs(contract, account=ACCOUNT)[0].spark_conf


def test_a_spark_conf_key_outside_the_schema_pattern_is_refused_even_when_listed(
    contract, monkeypatch
):
    from fluid_build.spark.policy import SparkPolicyError

    key = "spark.executor.memory --conf spark.jars"
    monkeypatch.setenv(_desired.CONF_ALLOWLIST_ENV, key)
    contract["builds"][0]["properties"]["parameters"]["spark"]["sparkConf"] = {key: "x"}
    with pytest.raises(SparkPolicyError):
        _desired.desired_runs(contract, account=ACCOUNT)


def test_a_plan_needs_no_aws_account_and_binding_keeps_the_hash(contract, monkeypatch):
    def no_sts(*_a, **_k):
        raise AssertionError("planning must not resolve the account")

    monkeypatch.setattr("fluid_build.providers.aws.util.config.resolve_account_and_region", no_sts)
    del contract["exposes"][0]["binding"]["location"]["bucket"]  # the <account>-fluid-data bucket
    run = _desired.desired_runs(contract)[0]
    assert run.needs_account and _desired.ACCOUNT_TOKEN in run.role_arn
    assert _desired.ACCOUNT_TOKEN in run.plan_action(1)["payload"]["artifacts"]["jobConfig"]
    bound = run.bind(ACCOUNT)
    assert not bound.needs_account and bound.config_hash == run.config_hash
    assert bound.job_uri.startswith(f"s3://{ACCOUNT}-fluid-data/")
    assert _desired.desired_runs(contract, account=ACCOUNT)[0] == bound


def test_names_of_different_builds_never_collide_on_hyphens():
    assert _names.run_name("a-b", "c") != _names.run_name("a", "b-c")
    assert _names.glue_job_name("a-b", "c") != _names.glue_job_name("a", "b-c")
    long = _names.execution_role_name("p" * 100, "b" * 100)
    assert len(long) <= 64 and long.endswith("-spark")


def test_the_runner_key_is_per_build_and_content_addressed(contract):
    (run,) = _desired.desired_runs(contract, account=ACCOUNT)
    prefix = "s3://commerce-lake/_fluid/spark/commerce.orders-stream/ingest-orders/runner/"
    assert run.runner_uri == f"{prefix}{run.runner_sha256[:16]}/forge_spark_runner.py"


def test_without_an_application_id_the_products_own_application_is_used(contract):
    contract["builds"][0]["properties"]["parameters"]["spark"].pop("applicationId", None)
    (run,) = _desired.desired_runs(contract, account=ACCOUNT)
    assert run.target == {"applicationName": "fluid-commerce.orders-stream-spark"}
