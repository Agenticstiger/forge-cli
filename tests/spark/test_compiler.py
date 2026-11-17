# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The job config a Spark build compiles to: deterministic, checked, credential-free."""

import json

import pytest
import sqlglot
from sqlglot.errors import ErrorLevel

from fluid_build._errors import FluidUserError
from fluid_build.providers.aws.util.warehouse import get_iceberg_warehouse
from fluid_build.secrets.policy import ALLOWLIST_ENV as SECRET_REF_ALLOWLIST_ENV
from fluid_build.spark.compiler import (
    CONFIG_VERSION,
    CONTRACT_SQL_ALLOWLIST_ENV,
    MSK_BROKERS_PLACEHOLDER,
    compile_job_config,
    spark_builds,
    validation_errors,
    validation_warnings,
)
from fluid_build.spark.policy import SparkPolicyError
from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV

from .conftest import BROKERS, MASK_KEY_ARN


def job(contract, build=None, **kw):
    return compile_job_config(contract, build or contract["builds"][0], **kw)


def source_of(contract):
    return contract["builds"][0]["properties"]["parameters"]["spark"]["source"]


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
    assert "fluid_mask(CAST(`src`.`customer`.`email` AS BINARY)) AS `email_hash`" in (
        config["sql"]["select"]
    )
    assert config["maskKey"] == {"secretArn": MASK_KEY_ARN}


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
            "auth": {
                "secretRef": "aws-sm://commerce/registry#user_info"  # pragma: allowlist secret
            },
        },
    )
    ref = "aws-sm://commerce/registry#user_info"
    with pytest.raises(FluidUserError):
        job(contract)  # the operator has not let this secret go to that host
    environ = {
        SECRET_REF_ALLOWLIST_ENV: f"{ref}@registry.example.internal",
        HOST_ALLOWLIST_ENV: "msk.example,example.internal",
    }
    text = job(contract, environ=environ).text
    assert ref in text
    assert "password" not in text.lower()


@pytest.mark.parametrize(
    ("url", "ref", "message"),
    [
        ("http://registry.example.internal", "env://REGISTRY_USER_INFO", "https only"),
        ("https://localhost:8081", "env://REGISTRY_USER_INFO", "named registry host"),
        ("https://collector.attacker.example", "aws-sm://other-product/db#password", ""),
    ],
)
def test_a_registry_credential_goes_only_where_the_operator_lets_it(contract, url, ref, message):
    """The contract names both the secret and the host: without the secrets policy the
    job's role would read any secret it can and send it to any host."""
    contract["builds"][0]["properties"]["stream"].update(
        format="avro-confluent",
        registry={"kind": "confluent", "url": url, "auth": {"secretRef": ref}},
    )
    with pytest.raises((ValueError, FluidUserError), match=message):
        job(contract)


def test_avro_reads_the_registry_subject_and_declares_the_schema_to_check(contract):
    stream = contract["builds"][0]["properties"]["stream"]
    stream.update(
        format="avro-confluent",
        key={"format": "avro-confluent", "schema": [{"name": "order_id", "type": "string"}]},
        registry={
            "kind": "confluent",
            "url": "http://registry.example.internal:8081",
            "version": 3,
        },
    )
    stream["key"]["prefix"] = "k_"
    source = job(contract).config["source"]
    assert source["registry"] == {"url": "http://registry.example.internal:8081", "auth": None}
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
                    "auth": {"secretRef": "vault://kv/registry#pw"},  # pragma: allowlist secret
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
    ("source", "message"),
    [
        (None, "names its Kafka cluster"),
        ({}, "bootstrapServers or mskClusterArn"),
        (
            {"bootstrapServers": BROKERS, "mskClusterArn": "arn:aws:kafka:eu-west-1:1:x"},
            "bootstrapServers or mskClusterArn",
        ),
        ({"bootstrapServers": "b-1"}, "host:port"),
        ({"bootstrapServers": "b-1:9098;rm -rf /"}, "host:port"),
        ({"bootstrapServers": "${a}"}, "host:port"),
        ({"mskClusterArn": "arn:aws:s3:::bucket"}, "MSK cluster ARN"),
        ({"bootstrapServers": BROKERS, "auth": "scram"}, "auth"),
        ({"bootstrapServers": BROKERS, "groupIdPrefix": "a b"}, "groupIdPrefix"),
        ({"bootstrapServers": BROKERS, "jaas": "x"}, "unsupported field"),
    ],
)
def test_the_kafka_cluster_comes_from_parameters_spark_source(contract, source, message):
    contract["builds"][0]["properties"]["parameters"]["spark"]["source"] = source
    with pytest.raises(ValueError, match=message):
        job(contract)


def test_an_msk_cluster_arn_leaves_one_placeholder_for_the_uploader(contract):
    arn = "arn:aws:kafka:eu-west-1:000000000005:cluster/orders/abc-1"
    contract["builds"][0]["properties"]["parameters"]["spark"]["source"] = {"mskClusterArn": arn}
    source = job(contract).config["source"]
    assert source["bootstrapServers"] == MSK_BROKERS_PLACEHOLDER == "${fluid_msk_bootstrap_brokers}"
    assert (source["mskClusterArn"], source["auth"]) == (arn, "msk-iam")


def test_kafka_auth_defaults_to_none_for_brokers_and_takes_group_id_prefix(contract):
    source_of(contract).pop("auth")
    source_of(contract)["groupIdPrefix"] = "forge-orders"
    source = job(contract).config["source"]
    assert (source["auth"], source["groupIdPrefix"]) == (
        "none",
        "forge-orders",
    )  # pragma: allowlist secret


def test_a_broker_the_operator_has_not_listed_is_refused(contract, monkeypatch):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "example.internal")
    with pytest.raises(FluidUserError, match="Kafka bootstrap server"):
        job(contract)


def test_masking_needs_the_products_key(contract):
    contract["builds"][0]["properties"]["parameters"]["spark"].pop("maskKeySecretArn")
    with pytest.raises(ValueError, match="maskKeySecretArn"):
        job(contract)
    spark = contract["builds"][0]["properties"]["parameters"]["spark"]
    spark["maskKeySecretArn"] = "not-an-arn"  # pragma: allowlist secret
    with pytest.raises(ValueError, match="expected a secret ARN"):
        job(contract)


def test_an_unmasked_build_carries_no_key(contract):
    columns = contract["builds"][0]["properties"]["mapping"]["columns"]
    columns[1].pop("maskAtIngest")
    contract["exposes"][0]["contract"]["schema"][2]["name"] = "email_hash"
    config = job(contract).config
    assert (config["maskedColumns"], config["maskKey"]) == ([], None)


def test_a_batch_job_reads_no_topic(contract):
    """A batch read sees only what Kafka still retains: overwriting the table with it
    would drop every older row, so a topic is landed by a stream (availableNow)."""
    contract["builds"][0]["properties"]["parameters"]["spark"]["mode"] = "batch"
    with pytest.raises(ValueError, match="availableNow"):
        job(contract)


def test_a_new_stream_starts_at_the_latest_offset_unless_told(contract):
    contract["builds"][0]["properties"]["stream"].pop("startingOffsets")
    assert job(contract).config["source"]["startingOffsets"] == "latest"


def test_the_default_checkpoint_takes_the_iac_account_token(contract):
    del contract["exposes"][0]["binding"]["location"]["bucket"]
    token = "${data.aws_caller_identity.current.account_id}"
    assert job(contract, account_ref=token).config["checkpoint"] == (
        f"s3://{token}-fluid-data/_checkpoints/commerce.orders-stream/ingest-orders/"
    )


def test_the_mask_key_is_under_the_operators_secret_prefixes(contract):
    """The contract names the key's secret; without the operator's prefixes a product
    could hash with another product's key and join against that product's table."""
    environ = {
        "FLUID_SECRET_PREFIXES": "aws-sm://arn:aws:secretsmanager:eu-west-1:000000000005:secret:other/"  # pragma: allowlist secret - a reference or test vector
    }
    environ[HOST_ALLOWLIST_ENV] = "msk.example,example.internal"
    with pytest.raises(FluidUserError, match="outside the prefixes"):
        job(contract, environ=environ)
    environ[
        "FLUID_SECRET_PREFIXES"
    ] += ",aws-sm://arn:aws:secretsmanager:eu-west-1:000000000005:secret:commerce/"
    assert job(contract, environ=environ).config["maskKey"] == {"secretArn": MASK_KEY_ARN}


def test_validate_survives_a_malformed_build(contract):
    contract["builds"][0]["properties"]["stream"].update(
        format="avro-confluent", registry={"kind": "confluent", "url": "http://[x"}
    )
    assert validation_warnings(contract) == []
    assert validation_errors(contract)
    contract["builds"][0]["properties"]["parameters"]["spark"] = True
    assert validation_warnings(contract) == []
    assert validation_errors(contract) == ["Spark: parameters.spark must be an object"]


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


def test_validate_reports_the_operators_policy_and_warns_about_contract_sql(contract, monkeypatch):
    assert validation_errors(contract) == validation_warnings(contract) == []
    # Policy #818: a broker missing from the allowlist warns at validate, refuses at run time.
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "example.internal")
    assert validation_errors(contract) == []
    (warning,) = validation_warnings(contract)
    assert f"not in {HOST_ALLOWLIST_ENV}: b-1.msk.example, b-2.msk.example;" in warning
    with pytest.raises(SparkPolicyError, match="Kafka bootstrap server"):
        job(contract)
    # A link-local broker is refused at every stage.
    source_of(contract)["bootstrapServers"] = "169.254.169.254:9092"
    (error,) = validation_errors(contract)
    assert error.startswith("Spark build ingest-orders:") and "Kafka bootstrap server" in error
    source_of(contract)["bootstrapServers"] = BROKERS
    _sql_build(contract)
    assert validation_errors(contract) == []  # the allowlist is apply's, here a warning
    (warning,) = validation_warnings(contract)
    assert CONTRACT_SQL_ALLOWLIST_ENV in warning


def test_validate_reports_masking_without_a_key(contract):
    contract["builds"][0]["properties"]["parameters"]["spark"].pop("maskKeySecretArn")
    (error,) = validation_errors(contract)
    assert "maskKeySecretArn" in error


def test_the_flink_lane_no_longer_claims_a_spark_builds_mapping(contract):
    from fluid_build.streaming.compiler import validation_warnings as flink_warnings

    assert not any("managed Flink lane" in w for w in flink_warnings(contract))


def test_a_missing_credential_grant_warns_at_validate_and_refuses_at_compile(contract):
    """Policy #818: a missing operator entry warns at validate, refuses at compile and run."""
    ref = "env://SCHEMA_REGISTRY_AUTH"  # pragma: allowlist secret
    contract["builds"][0]["properties"]["stream"].update(
        format="avro-confluent",
        registry={
            "kind": "confluent",
            "url": "https://schema-registry.example.internal",
            "auth": {"secretRef": ref},
        },
    )
    assert validation_errors(contract) == []
    # The contract-wide secret check names the grant and the registry host
    # (test_validate_prints_each_missing_entry_once); the Spark warnings do not repeat them.
    assert validation_warnings(contract) == []
    with pytest.raises(FluidUserError):
        job(contract)
    environ = {
        SECRET_REF_ALLOWLIST_ENV: f"{ref}@schema-registry.example.internal",
        HOST_ALLOWLIST_ENV: "msk.example,example.internal",
    }
    assert job(contract, environ=environ).config["source"]["registry"]["auth"] == {"secretRef": ref}
    # https stays an error at validate too.
    contract["builds"][0]["properties"]["stream"]["registry"][
        "url"
    ] = "http://schema-registry.example.internal"
    (error,) = validation_errors(contract)
    assert "https only" in error


@pytest.mark.parametrize("part", ["binding", "location", "contract"])
def test_validate_reports_an_output_part_that_is_not_an_object(contract, part):
    """A schema-invalid output is an error line, never an unhandled exception."""
    expose = contract["exposes"][0]
    if part == "location":
        expose["binding"]["location"] = "x"
    else:
        expose[part] = "x"
    (error,) = validation_errors(contract)
    assert "expected an object" in error


def test_contract_sql_holding_an_interpolation_is_refused(contract):
    """The runner refuses any ${...}: compiling one would deploy a job that never starts."""
    build = _sql_build(contract, "SELECT '${env:HOME}' AS order_id FROM commerce.order_lines")
    environ = {CONTRACT_SQL_ALLOWLIST_ENV: "commerce.orders-stream"}
    with pytest.raises(ValueError, match="unresolved interpolation"):
        job(contract, build, environ=environ)
    assert any("unresolved interpolation" in e for e in validation_errors(contract))


def test_a_public_broker_address_warns_at_validate_and_refuses_at_compile(contract, monkeypatch):
    """Policy #818 for an address as for a name; a private address needs no entry."""
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "example.internal")
    source_of(contract)["bootstrapServers"] = "8.8.8.8:9092"
    assert validation_errors(contract) == []
    (warning,) = validation_warnings(contract)
    assert f"not in {HOST_ALLOWLIST_ENV}: 8.8.8.8;" in warning
    with pytest.raises(SparkPolicyError, match="Kafka bootstrap server"):
        job(contract)
    source_of(contract)["bootstrapServers"] = "10.0.0.5:9092"
    assert validation_warnings(contract) == []
    assert job(contract).config["source"]["bootstrapServers"] == "10.0.0.5:9092"


def test_a_host_refusal_is_the_spark_jobs_not_the_flink_lanes(contract, monkeypatch):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "example.internal")
    with pytest.raises(SparkPolicyError) as refused:
        job(contract)
    assert refused.value.code == "SparkPolicyError"
    assert refused.value.what.startswith("a forge Spark job may not use")
    assert "Flink" not in refused.value.what


def test_a_registry_without_a_credential_is_named_by_the_spark_warning(contract, monkeypatch):
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "msk.example")
    contract["builds"][0]["properties"]["stream"].update(
        format="avro-confluent",
        registry={"kind": "confluent", "url": "https://schema-registry.example.internal"},
    )
    (warning,) = validation_warnings(contract)
    assert f"not in {HOST_ALLOWLIST_ENV}: schema-registry.example.internal;" in warning


def test_validate_prints_each_missing_entry_once(contract, tmp_path, monkeypatch, capsys):
    """fluid validate: one warning for the brokers, one for the registry host and one for
    the credential's grant, each once (the secret check owns the registry's two)."""
    import yaml

    from fluid_build.cli import main

    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "example.org")
    monkeypatch.chdir(tmp_path)
    ref = "env://SCHEMA_REGISTRY_AUTH"  # pragma: allowlist secret
    contract["builds"][0]["properties"]["stream"].update(
        format="avro-confluent",
        registry={
            "kind": "confluent",
            "url": "https://schema-registry.example.internal",
            "auth": {"secretRef": ref},
        },
    )
    (tmp_path / "c.fluid.yaml").write_text(yaml.safe_dump(contract), encoding="utf-8")
    capsys.readouterr()
    assert main(["validate", "c.fluid.yaml", "--offline"]) == 0
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line[:4].strip()[:1].isdigit()
    ]
    assert len(lines) == 3, lines
    assert sum("b-1.msk.example" in line for line in lines) == 1
    assert sum("schema-registry.example.internal" in line for line in lines) == 2
