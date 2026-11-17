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


def _avro(contract, monkeypatch):
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


MIRROR = "https://mirror.example.internal/maven/proxy/"
USERINFO_URL = "https://u:p@mirror.example.internal/maven/"  # pragma: allowlist secret


def test_the_maven_mirror_replaces_the_confluent_repository(contract, monkeypatch):
    _avro(contract, monkeypatch)
    before = runs_of(contract)[0]
    monkeypatch.setenv(_desired.MAVEN_REPOSITORY_ENV, MIRROR)
    after = runs_of(contract)[0]
    assert after.spark_conf["spark.jars.repositories"] == MIRROR
    assert _desired._CONFLUENT_REPOSITORY not in after.spark_conf.values()
    assert after.spark_conf["spark.jars.packages"] == before.spark_conf["spark.jars.packages"]
    assert after.config_hash != before.config_hash  # a new artifact source restarts the job


def test_the_maven_mirror_serves_a_json_stream_too(contract, monkeypatch):
    monkeypatch.setenv(_desired.MAVEN_REPOSITORY_ENV, MIRROR)
    assert runs_of(contract)[0].spark_conf["spark.jars.repositories"] == MIRROR


def test_without_the_mirror_the_defaults_are_unchanged(contract, monkeypatch):
    monkeypatch.delenv(_desired.MAVEN_REPOSITORY_ENV, raising=False)
    assert _desired.maven_repository({}) is None
    assert _desired.maven_repository({_desired.MAVEN_REPOSITORY_ENV: "  "}) is None
    assert "spark.jars.repositories" not in runs_of(contract)[0].spark_conf


@pytest.mark.parametrize(
    "url, rule",
    [
        ("http://mirror.example.internal/maven/", "https URL"),
        ("mirror.example.internal/maven/", "https URL"),
        ("https:///maven/", "https URL"),
        ("https://mirror.example.internal:99999/", "https URL"),
        (USERINFO_URL, "user or password"),
        ("https://user@mirror.example.internal/maven/", "user or password"),
        ("https://mirror.example.internal/maven/?token=x", "query"),
        ("https://a.example.internal/,https://b.example.internal/", "comma"),
        ("https://169.254.169.254/latest/", "link-local"),
        ("https://[fe80::1]/maven/", "link-local"),
        ("https://[::ffff:169.254.169.254]/", "link-local"),
        ("https://169.254.169.254./latest/", "ending in a dot"),
        ("https://mirror.example.internal:0/", "port 0"),
        ("https://mirror.example.internal/maven/?", "query"),
        ("https://mirror.example.internal/maven/#", "fragment"),
        ("https://a..b/maven/", "not a valid DNS name"),
        ("https://169.254.169.254" + chr(0x3002) + "/", "not a valid DNS name"),
        ("https://169.254.169.254%2e/latest/", "ending in a dot"),
        ("https://mirror.example.internal\\@evil.example/", "user or password"),
    ],
)
def test_a_bad_maven_mirror_is_refused_without_echoing_it(url, rule):
    from fluid_build.spark.policy import SparkPolicyError

    with pytest.raises(SparkPolicyError) as err:
        _desired.maven_repository({_desired.MAVEN_REPOSITORY_ENV: url})
    text = str(err.value) + repr(err.value.__dict__)
    assert rule in text
    assert url not in text


def test_a_bad_maven_mirror_refuses_the_run(contract, monkeypatch):
    from fluid_build.spark.policy import SparkPolicyError

    monkeypatch.setenv(_desired.MAVEN_REPOSITORY_ENV, "http://mirror.example.internal/")
    with pytest.raises(SparkPolicyError):
        runs_of(contract)


def test_a_project_env_file_cannot_set_the_maven_mirror():
    from fluid_build.credentials.protected_env import is_protected_env_key

    assert is_protected_env_key(_desired.MAVEN_REPOSITORY_ENV)


def test_an_operator_listed_spark_conf_overrides_the_defaults(contract, monkeypatch):
    monkeypatch.setenv(_desired.CONF_ALLOWLIST_ENV, "spark.sql.shuffle.service.*")
    spark_cfg(contract)["sparkConf"] = {
        "spark.sql.shuffle.service.x": "y",
        "spark.executor.memory": 4,
    }
    conf = runs_of(contract)[0].spark_conf
    assert conf["spark.sql.shuffle.service.x"] == "y"
    assert conf["spark.executor.memory"] == "4"
    assert "spark.sql.extensions" in conf and "spark.jars" in conf  # forge's own stay


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
        "spark.sql.adaptive.customCostEvaluatorClass",
    ],
)
@pytest.mark.parametrize("listed", [None, "{key}", "spark.*", "spark.sql.*,{key},*"])
def test_spark_conf_that_could_load_code_is_refused_whatever_the_operator_lists(
    contract, monkeypatch, key, listed
):
    from fluid_build.spark.policy import SparkPolicyError

    if listed:
        monkeypatch.setenv(_desired.CONF_ALLOWLIST_ENV, listed.format(key=key))
    contract["builds"][0]["properties"]["parameters"]["spark"]["sparkConf"] = {key: "x"}
    with pytest.raises(SparkPolicyError) as err:
        _desired.desired_runs(contract, account=ACCOUNT)
    assert key in err.value.what and "never passes" in err.value.fix
    assert "export" not in err.value.fix  # listing it would not help


def test_an_unknown_key_is_refused_until_the_operator_lists_it(contract, monkeypatch):
    from fluid_build.spark.policy import SparkPolicyError

    key = "spark.made.up"
    contract["builds"][0]["properties"]["parameters"]["spark"]["sparkConf"] = {key: "x"}
    with pytest.raises(SparkPolicyError) as err:
        _desired.desired_runs(contract, account=ACCOUNT)
    assert f'export {_desired.CONF_ALLOWLIST_ENV}="{key}"' in err.value.fix
    assert "never passes" not in err.value.fix
    monkeypatch.setenv(_desired.CONF_ALLOWLIST_ENV, key)
    assert _desired.desired_runs(contract, account=ACCOUNT)[0].spark_conf[key] == "x"


def test_a_mixed_refusal_names_both_fixes(contract):
    from fluid_build.spark.policy import SparkPolicyError

    spark = contract["builds"][0]["properties"]["parameters"]["spark"]
    spark["sparkConf"] = {"spark.made.up": "x", "spark.jars": "/a.jar"}
    with pytest.raises(SparkPolicyError) as err:
        _desired.desired_runs(contract, account=ACCOUNT)
    assert "Drop spark.jars: forge never passes it" in err.value.fix
    assert f'{_desired.CONF_ALLOWLIST_ENV}="spark.made.up"' in err.value.fix


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


def test_every_key_the_infrastructure_emit_denies_is_denied_here():
    """A run is no looser than what fluid apply provisions: the IaC emit's deny list is a
    subset."""
    identity = pytest.importorskip("fluid_build.iac.providers.aws_identity")
    entries = getattr(identity, "DENIED_SPARK_CONF", None)
    if entries is None:
        pytest.skip("the IaC emit's DENIED_SPARK_CONF has not landed")
    keys = [e[:-1] + "probe" if e.endswith("*") else e for e in entries]
    assert [k for k in keys if not _desired._conf_denied(k)] == []


@pytest.mark.parametrize(
    "key",
    [
        "spark.driver.defaultJavaOptions",
        "spark.executor.userClassPathFirst",
        "spark.emr-serverless.executorEnv.X",
        "spark.pyspark.python",
        "spark.sql.warehouse.dir",
        "spark.sql.streaming.checkpointLocation",
        "spark.ssl.enabled",
        "spark.master",
    ],
)
def test_the_wider_deny_list_holds_under_a_star(key):
    assert not _desired._conf_allowed(key, ["*"])


@pytest.mark.parametrize(
    "conf, hint, shown",
    [
        ({"spark.jars ": "x"}, "Fix the key 'spark.jars '", "'spark.jars '"),
        ({"spark.executor.memory": "4g --jars x"}, "a value without spaces", None),
        ({"spark.executor.memory": ""}, "a value without spaces", None),
    ],
)
def test_a_refusal_the_allowlist_cannot_fix_gives_no_export_hint(contract, conf, hint, shown):
    from fluid_build.spark.policy import SparkPolicyError

    spark_cfg(contract)["sparkConf"] = conf
    with pytest.raises(SparkPolicyError) as err:
        runs_of(contract)
    assert hint in err.value.fix and "export" not in err.value.fix
    if shown:
        assert shown in err.value.what  # a trailing space is visible


def test_every_reason_gets_its_own_hint(contract):
    from fluid_build.spark.policy import SparkPolicyError

    spark_cfg(contract)["sparkConf"] = {
        "spark.jars ": "x",
        "spark.plugins": "p",
        "spark.made.up": "y",
        "spark.executor.memory": "",
    }
    with pytest.raises(SparkPolicyError) as err:
        runs_of(contract)
    fix = err.value.fix
    assert "Fix the key 'spark.jars '" in fix
    assert "Drop spark.plugins: forge never passes it" in fix
    assert "Give spark.executor.memory a value without spaces" in fix
    assert f'export {_desired.CONF_ALLOWLIST_ENV}="spark.made.up"' in fix


# ── Glue: the pinned jars the job loads are part of the run hash ─────────────────────────


def _glue(contract, avro, monkeypatch):
    spark_cfg(contract)["runtime"] = "glue"
    spark_cfg(contract).pop("applicationId", None)
    if avro:
        _avro(contract, monkeypatch)
    return contract


def _bump_one_pin(monkeypatch):
    import dataclasses

    from fluid_build.iac.providers import aws_glue_jars

    first, *rest = aws_glue_jars.AVRO_JARS
    bumped = dataclasses.replace(first, sha256="0" * 64)
    monkeypatch.setattr(aws_glue_jars, "AVRO_JARS", (bumped, *rest))


JARS_URL = "s3://platform-jars/glue/avro/"


def test_a_new_glue_jar_pin_changes_the_glue_run_hash(contract, monkeypatch):
    run = runs_of(_glue(contract, True, monkeypatch))[0]
    _bump_one_pin(monkeypatch)
    assert runs_of(contract)[0].config_hash != run.config_hash


def test_a_new_glue_jars_prefix_changes_the_glue_run_hash(contract, monkeypatch):
    from fluid_build.iac.providers.aws_glue_jars import JARS_URL_ENV

    monkeypatch.delenv(JARS_URL_ENV, raising=False)
    run = runs_of(_glue(contract, True, monkeypatch))[0]
    monkeypatch.setenv(JARS_URL_ENV, JARS_URL)
    assert runs_of(contract)[0].config_hash != run.config_hash


def test_the_glue_run_hash_holds_the_emits_own_fingerprint(contract, monkeypatch):
    """The same function and inputs as the IaC emit, never a re-derivation."""
    from fluid_build.iac.providers import aws_glue_jars

    seen = []
    real = aws_glue_jars.glue_jars_fingerprint

    def spy(config, environ=None):
        seen.append(real(config, environ))
        return seen[-1]

    monkeypatch.setattr(aws_glue_jars, "glue_jars_fingerprint", spy)
    monkeypatch.setenv(aws_glue_jars.JARS_URL_ENV, JARS_URL)
    runs_of(_glue(contract, True, monkeypatch))
    # What the emit computes from its own compile (aws_ingest: its account marker, os.environ)
    from fluid_build.spark.compiler import compile_job_config

    emitted = compile_job_config(contract, contract["builds"][0], account_ref="${emit_account}")
    assert seen == [real(emitted.config)]
    assert seen[0].startswith("sha256:")


@pytest.mark.parametrize(
    "runtime, extra",
    [
        ("emr-serverless", {}),
        ("emr-eks", {"virtualClusterId": "vc1"}),
        ("emr-ec2", {"clusterId": "j-1"}),
        ("glue", {}),  # a JSON stream: Glue adds no jars
    ],
)
@pytest.mark.parametrize("avro", [False, True])
def test_a_build_that_loads_no_glue_jars_keeps_its_hash(
    contract, monkeypatch, runtime, extra, avro
):
    if runtime == "glue" and avro:
        pytest.skip("a Glue Avro build loads the pinned jars")
    from fluid_build.iac.providers import aws_glue_jars

    spark_cfg(contract).update(runtime=runtime, **extra)
    if avro:
        _avro(contract, monkeypatch)
    before = runs_of(contract)[0].config_hash

    def never(*_a, **_k):
        raise AssertionError("not a Glue build that adds jars")

    if runtime != "glue":
        monkeypatch.setattr(aws_glue_jars, "glue_jars_fingerprint", never)
    _bump_one_pin(monkeypatch)
    monkeypatch.setenv(aws_glue_jars.JARS_URL_ENV, JARS_URL)
    assert runs_of(contract)[0].config_hash == before


def test_a_malformed_glue_jars_prefix_is_invalid(contract, monkeypatch):
    from fluid_build.iac.providers.aws_glue_jars import JARS_URL_ENV

    monkeypatch.setenv(JARS_URL_ENV, "https://not-s3/")
    with pytest.raises(ValueError, match=JARS_URL_ENV):
        runs_of(_glue(contract, True, monkeypatch))
