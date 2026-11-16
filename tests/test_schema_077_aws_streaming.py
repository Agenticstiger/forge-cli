# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Schema pins for the 0.7.7-preview fields AWS streaming to Iceberg is built on.

* ``$defs.schemaRegistry`` at ``streamSource.registry``: Glue or Confluent, one definition
  for every engine. The kafka-connect ``schema_registry`` block and managed Flink's
  ``schemaRegistryUrl`` stay valid.
* ``streamSource`` (``builds[].properties.stream``): ``trigger``, ``watermark``,
  ``checkpoint``, ``startingOffsets``.
* ``recordMappingColumn.maskAtIngest``: ``hash`` (HMAC-SHA256 at ingest, keyed by
  ``parameters.spark.maskKeySecretArn``).
* ``$defs.sparkParameters`` at ``builds[].properties.parameters.spark``.
* Six AWS container kinds in ``packaging.containers`` (``mwaaEnvironment`` shared only), and
  the resolver that reads them without changing the plan summary.

Every value set comes from the service's own API reference (Glue CreateSchema
``Compatibility``, the Confluent compatibility levels, EMR Serverless and EMR on EKS
``StartJobRun`` id patterns). Cross-field rules that need more than one object (a registry
per stream format, a checkpoint per warehouse) belong to the lanes that consume the fields.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.iac.packaging import (
    CONTAINER_KINDS,
    LEGACY,
    PREVIEW_CONTAINER_KINDS,
    ContainerDecision,
    PackagingError,
    resolve_packaging,
)
from fluid_build.iac.plan_packaging import build_packaging_summary
from fluid_build.schema_manager import FluidSchemaManager, ValidationResult

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/packaging/aws_streaming_iceberg_077.yaml"
SCHEMAS = ROOT / "fluid_build/schemas"


@pytest.fixture(scope="module")
def manager() -> FluidSchemaManager:
    return FluidSchemaManager()


def _fixture() -> Dict[str, Any]:
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


def _validate(manager: FluidSchemaManager, contract: Dict[str, Any], version: str = "0.7.7"):
    return manager.validate_contract(contract, schema_version=version, offline_only=True)


def _messages(result: ValidationResult) -> List[str]:
    return [getattr(e, "message", str(e)) for e in result.errors]


def _valid(manager, contract) -> bool:
    return _validate(manager, contract).is_valid


def _stream(contract: Dict[str, Any]) -> Dict[str, Any]:
    return contract["builds"][0]["properties"]["stream"]


def _spark(contract: Dict[str, Any], build: int = 0) -> Dict[str, Any]:
    return contract["builds"][build]["properties"]["parameters"]["spark"]


def _with(patch_target, **changes):
    """A copy of the fixture with one object replaced by ``patch_target(contract)``'s result."""
    contract = _fixture()
    target = patch_target(contract)
    target.clear()
    target.update(changes)
    return contract


# ---------------------------------------------------------------------------
# The fixture and what must not move
# ---------------------------------------------------------------------------


class TestFixture:
    def test_every_new_field_validates_on_077(self, manager):
        result = _validate(manager, _fixture())
        assert result.is_valid, _messages(result)

    def test_the_fixture_uses_every_new_field(self):
        contract = _fixture()
        stream = _stream(contract)
        assert {"registry", "trigger", "watermark", "checkpoint", "startingOffsets"} <= set(stream)
        assert contract["builds"][0]["properties"]["mapping"]["columns"][0]["maskAtIngest"]
        assert contract["builds"][0]["engine"] == "spark"
        assert {"mskClusterArn", "auth", "groupIdPrefix"} <= set(_spark(contract)["source"])
        assert _spark(contract)["maskKeySecretArn"]
        # Spark refuses a watermark on TIMESTAMP_NTZ; the fixture's watermark is timestamp_ltz.
        column = {c["name"]: c["type"] for c in stream["schema"]}[stream["watermark"]["column"]]
        assert column == "timestamp_ltz"
        assert set(contract["packaging"]["containers"]) == set(PREVIEW_CONTAINER_KINDS)

    def test_077_stays_a_preview_and_076_the_default(self):
        assert "0.7.7" in FluidSchemaManager.PREVIEW_VERSIONS
        assert FluidSchemaManager.latest_bundled_version() == "0.7.6"

    @pytest.mark.parametrize("version", ["0.7.5", "0.7.6"])
    def test_stable_schemas_carry_none_of_it(self, version):
        text = (SCHEMAS / f"fluid-schema-{version}.json").read_text(encoding="utf-8")
        for name in ("schemaRegistry", "sparkParameters", "maskAtIngest", "mwaaEnvironment"):
            assert name not in text, (version, name)

    def test_a_076_contract_is_told_which_packaging_kinds_need_077(self, manager):
        contract = _fixture()
        contract["fluidVersion"] = "0.7.6"
        del contract["builds"]
        messages = _messages(_validate(manager, contract, "0.7.6"))
        assert any(
            "'emrApplication' (the EMR Serverless application container kind)" in m
            and "set the contract's fluidVersion to 0.7.7" in m
            for m in messages
        ), messages


# ---------------------------------------------------------------------------
# schemaRegistry
# ---------------------------------------------------------------------------


def _registry(**registry):
    return _with(lambda c: _stream(c)["registry"], **registry)


class TestSchemaRegistry:
    @pytest.mark.parametrize(
        "registry",
        [
            {"kind": "glue"},
            {"kind": "glue", "registryName": "commerce", "compatibility": "BACKWARD_ALL"},
            {"kind": "glue", "subject": "orders", "version": 3, "compatibility": "DISABLED"},
            {"kind": "confluent", "url": "http://schema-registry:8081"},
            {
                "kind": "confluent",
                "url": "https://registry.example/apis/ccompat/v7",
                "version": "latest",
                "compatibility": "FULL_TRANSITIVE",
                "auth": {"secretRef": "env://REGISTRY_AUTH"},  # pragma: allowlist secret
            },
        ],
    )
    def test_accepted(self, manager, registry):
        result = _validate(manager, _registry(**registry))
        assert result.is_valid, _messages(result)

    @pytest.mark.parametrize("mode", ["NONE", "BACKWARD", "FORWARD", "FULL"])
    def test_modes_both_kinds_share(self, manager, mode):
        assert _valid(manager, _registry(kind="glue", compatibility=mode))
        assert _valid(
            manager, _registry(kind="confluent", url="https://r.example", compatibility=mode)
        )

    @pytest.mark.parametrize(
        "registry",
        [
            {},
            {"kind": "apicurio"},
            {"kind": "confluent"},
            {"kind": "confluent", "url": "https://user:pw@r.example"},  # pragma: allowlist secret
            {"kind": "confluent", "url": "https://r.example?token=x"},
            {"kind": "confluent", "url": "https://r.example#frag"},
            {"kind": "confluent", "url": "ftp://r.example"},
            {"kind": "confluent", "url": "https://r.example\n"},
            {"kind": "confluent", "url": "https://r.example", "registryName": "x"},
            {"kind": "confluent", "url": "https://r.example", "compatibility": "BACKWARD_ALL"},
            {"kind": "confluent", "url": "https://r.example", "compatibility": "DISABLED"},
            {"kind": "glue", "url": "https://r.example"},
            {"kind": "glue", "auth": {"secretRef": "env://X"}},  # pragma: allowlist secret
            {"kind": "glue", "compatibility": "BACKWARD_TRANSITIVE"},
            {"kind": "glue", "registryName": "has space"},
            {"kind": "glue", "registryName": "x" * 256},
            {"kind": "glue", "version": 0},
            {"kind": "glue", "version": "3"},
            {"kind": "glue", "version": "earliest"},
            {"kind": "glue", "subject": ""},
            {"kind": "glue", "subject": "a\nb"},
            {"kind": "glue", "compatibility": "backward"},
            {"kind": "glue", "region": "eu-west-1"},
        ],
    )
    def test_rejected(self, manager, registry):
        assert not _valid(manager, _registry(**registry)), registry

    def test_a_glue_subject_is_a_glue_schema_name(self, manager):
        assert _valid(manager, _registry(kind="glue", subject="orders.value-v1"))
        assert not _valid(manager, _registry(kind="glue", subject="orders value"))
        assert _valid(
            manager, _registry(kind="confluent", url="https://r.example", subject="orders value")
        )

    @pytest.mark.parametrize(
        ("registry", "says"),
        [
            ({"kind": "glue", "url": "https://r.example"}, "url is for kind: confluent"),
            (
                {"kind": "glue", "auth": {"secretRef": "env://A"}},  # pragma: allowlist secret
                "auth is for kind: confluent",
            ),
            (
                {"kind": "confluent", "url": "https://r.example", "registryName": "r"},
                "registryName is for kind: glue",
            ),
        ],
    )
    def test_a_key_of_the_other_kind_is_named(self, manager, registry, says):
        messages = _messages(_validate(manager, _registry(**registry)))
        assert any(says in m for m in messages), messages
        assert not any("should not be valid" in m for m in messages), messages

    @pytest.mark.parametrize(
        "registry",
        [
            {
                "kind": "confluent",
                "url": "https://user:hunter2@r.example",  # pragma: allowlist secret
            },
            {"kind": "confluent", "url": "https://r.example", "auth": "hunter2"},
        ],
    )
    def test_a_credential_pasted_into_the_registry_is_not_quoted(self, manager, registry):
        messages = _messages(_validate(manager, _registry(**registry)))
        assert messages
        assert not any("hunter2" in m for m in messages), messages

    def test_auth_is_the_kafka_connect_shape(self, manager):
        schema = manager._bundled_schemas["0.7.7"]
        registry = schema["$defs"]["schemaRegistry"]
        assert registry["properties"]["auth"]["$ref"] == (
            "#/$defs/acquisitionPattern/properties/kafka-connect/properties/schema_registry"
            "/properties/auth"
        )
        assert registry["required"] == ["kind"]
        assert registry["properties"]["version"]["default"] == "latest"
        assert "<topic>-value" in registry["properties"]["subject"]["description"]
        assert set(registry["properties"]["compatibility"]["enum"]) == {
            "NONE", "DISABLED", "BACKWARD", "BACKWARD_ALL", "FORWARD", "FORWARD_ALL", "FULL",
            "FULL_ALL", "BACKWARD_TRANSITIVE", "FORWARD_TRANSITIVE", "FULL_TRANSITIVE",
        }  # fmt: skip
        assert _stream_def(manager)["properties"]["registry"]["$ref"] == "#/$defs/schemaRegistry"

    def test_the_older_spellings_are_documented_and_still_valid(self, manager):
        description = manager._bundled_schemas["0.7.7"]["$defs"]["schemaRegistry"]["description"]
        assert "kafka-connect schema_registry" in description
        assert "schemaRegistryUrl" in description
        flink = yaml.safe_load((ROOT / "examples/flink-iceberg/contract.fluid.yaml").read_text())
        flink["builds"][0]["properties"]["parameters"]["flink"]["source"][
            "schemaRegistryUrl"
        ] = "https://registry.example"
        flink["builds"][0]["properties"]["stream"]["format"] = "avro-confluent"
        assert _valid(manager, flink), _messages(_validate(manager, flink))
        assert _valid(manager, _kafka_connect_contract())


def _stream_def(manager):
    return manager._bundled_schemas["0.7.7"]["$defs"]["streamSource"]


def _kafka_connect_contract() -> Dict[str, Any]:
    contract = _fixture()
    contract["builds"] = [
        {
            "id": "orders-cdc",
            "engine": "kafka-connect",
            "pattern": "acquisition",
            "outputs": ["orders"],
            "properties": {
                "source": {"kind": "kafka", "mode": "streaming"},
                "kafka-connect": {
                    "connector_class": "io.example.Source",
                    "schema_registry": {
                        "url": "http://schema-registry:8081",
                        "auth": {"secretRef": "env://REGISTRY_AUTH"},  # pragma: allowlist secret
                    },
                },
            },
        }
    ]
    contract["exposes"] = contract["exposes"][:1]
    return contract


# ---------------------------------------------------------------------------
# streamSource: trigger, watermark, checkpoint, startingOffsets
# ---------------------------------------------------------------------------


def _stream_field(name, value):
    contract = _fixture()
    _stream(contract)[name] = value
    return contract


class TestStreamFields:
    @pytest.mark.parametrize(
        "name,value",
        [
            ("trigger", {"processingTime": "PT30S"}),
            ("trigger", {"processingTime": "PT1H"}),
            ("trigger", {"availableNow": True}),
            ("watermark", {"column": "event_time", "delay": "PT5M"}),
            ("watermark", {"column": "payload.event_time", "delay": "PT1H"}),
            ("checkpoint", "s3://bucket.with.dots/a/b/"),
            ("checkpoint", "s3://my-bucket"),
            ("startingOffsets", "earliest"),
            ("startingOffsets", "latest"),
        ],
    )
    def test_accepted(self, manager, name, value):
        result = _validate(manager, _stream_field(name, value))
        assert result.is_valid, _messages(result)

    @pytest.mark.parametrize(
        "name,value",
        [
            ("trigger", {}),
            ("trigger", {"availableNow": False}),
            ("trigger", {"processingTime": "1 minute"}),
            ("trigger", {"processingTime": "PT1M", "availableNow": True}),
            ("trigger", {"once": True}),
            ("trigger", "PT1M"),
            ("watermark", {"column": "event_time"}),
            ("watermark", {"delay": "PT1M"}),
            ("watermark", {"column": "event time", "delay": "PT1M"}),
            ("watermark", {"column": "event_time", "delay": "10 minutes"}),
            ("watermark", {"column": "event_time", "delay": "PT1M", "x": 1}),
            ("checkpoint", "https://bucket/path"),
            ("checkpoint", "s3://Upper/path"),
            ("checkpoint", "s3a://bucket/path"),
            ("checkpoint", "s3://bucket/path\n"),
            ("checkpoint", "/local/path"),
            ("startingOffsets", "none"),
            ("startingOffsets", {"orders": {"0": 23}}),
        ],
    )
    def test_rejected(self, manager, name, value):
        assert not _valid(manager, _stream_field(name, value)), (name, value)

    def test_the_watermark_column_says_spark_needs_timestamp_ltz(self, manager):
        watermark = manager._bundled_schemas["0.7.7"]["$defs"]["streamSource"]["properties"][
            "watermark"
        ]
        assert "must be timestamp_ltz" in watermark["properties"]["column"]["description"]

    def test_new_fields_are_optional(self, manager):
        contract = _fixture()
        for name in ("registry", "trigger", "watermark", "checkpoint", "startingOffsets"):
            del _stream(contract)[name]
        contract["builds"][0]["properties"]["stream"]["format"] = "json"
        assert _valid(manager, contract), _messages(_validate(manager, contract))


# ---------------------------------------------------------------------------
# recordMappingColumn.maskAtIngest
# ---------------------------------------------------------------------------


class TestMaskAtIngest:
    def _column(self, contract):
        return contract["builds"][0]["properties"]["mapping"]["columns"][0]

    def test_hash_is_accepted_and_optional(self, manager):
        contract = _fixture()
        assert self._column(contract)["maskAtIngest"] == "hash"
        assert _valid(manager, contract)
        del self._column(contract)["maskAtIngest"]
        assert _valid(manager, contract)

    @pytest.mark.parametrize("value", ["sha256", "redact", "HASH", True, ["hash"]])
    def test_other_values_are_rejected(self, manager, value):
        contract = _fixture()
        self._column(contract)["maskAtIngest"] = value
        assert not _valid(manager, contract)

    def test_the_hash_is_a_keyed_hmac(self, manager):
        defs = manager._bundled_schemas["0.7.7"]["$defs"]
        mask = defs["recordMappingColumn"]["properties"]["maskAtIngest"]
        assert mask["enum"] == ["hash"]
        assert "HMAC-SHA256 keyed by parameters.spark.maskKeySecretArn" in mask["description"]
        assert "lowercase hex; null stays null" in mask["description"]
        key = defs["sparkParameters"]["properties"]["maskKeySecretArn"]
        assert "Spark compiler" in key["description"]

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws:secretsmanager:eu-west-1:123456789012:secret:mask-key-AbCdEf",
            "arn:aws-us-gov:secretsmanager:us-gov-west-1:123456789012:secret:team/mask-AbCdEf",
            "arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:k",
        ],
    )
    def test_a_secrets_manager_arn_is_accepted(self, manager, arn):
        assert _valid(manager, _spark_params(runtime="glue", maskKeySecretArn=arn))

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws:secretsmanager:eu-west-1:123456789012:secret:",
            "arn:aws:secretsmanager:eu-west-1:1234:secret:k",
            "arn:aws:kms:eu-west-1:123456789012:key/abc",
            "arn:aws:ssm:eu-west-1:123456789012:parameter/k",
            "arn:aws:secretsmanager:eu-west-1:123456789012:secret:k\n",
            "mask-key",
            "",
        ],
    )
    def test_anything_else_is_refused(self, manager, arn):
        assert not _valid(manager, _spark_params(runtime="glue", maskKeySecretArn=arn))


# ---------------------------------------------------------------------------
# sparkParameters
# ---------------------------------------------------------------------------


def _spark_params(**spark):
    return _with(_spark, **spark)


class TestSparkParameters:
    @pytest.mark.parametrize(
        "spark",
        [
            {"runtime": "emr-serverless"},
            {"runtime": "emr-serverless", "applicationId": "00f1abcdexample", "mode": "batch"},
            {"runtime": "emr-ec2"},
            {"runtime": "emr-ec2", "clusterId": "j-3TS0OIYO4NFN", "releaseLabel": "emr-7.14.0"},
            {
                "runtime": "emr-eks",
                "virtualClusterId": "abcdefghij0123456789klmno",  # pragma: allowlist secret
            },
            {"runtime": "glue"},
            {"runtime": "glue", "glueVersion": "5.0", "mode": "streaming"},
            {
                "runtime": "emr-serverless",
                "executionRoleArn": "arn:aws-cn:iam::123456789012:role/team/spark-job",
                "sparkConf": {
                    "spark.executor.memory": "4g",
                    "spark.dynamicAllocation.enabled": "true",
                },
            },
            {"runtime": "emr-serverless", "source": {"bootstrapServers": "b-1.kafka:9092"}},
            {
                "runtime": "emr-serverless",
                "source": {"bootstrapServers": "b-1:9092,b-2:9092", "auth": "none"},
            },
            {
                "runtime": "emr-eks",
                "virtualClusterId": "abcdefghij0123456789klmno",  # pragma: allowlist secret
                "source": {
                    "mskClusterArn": "arn:aws-cn:kafka:cn-north-1:123456789012:cluster/c/uuid-1",
                    "auth": "msk-iam",
                    "groupIdPrefix": "orders",
                },
            },
        ],
    )
    def test_accepted(self, manager, spark):
        result = _validate(manager, _spark_params(**spark))
        assert result.is_valid, _messages(result)

    @pytest.mark.parametrize(
        "spark",
        [
            {},
            {"runtime": "databricks"},
            {"runtime": "emr-eks"},
            {"runtime": "emr-serverless", "mode": "continuous"},
            {"runtime": "emr-ec2", "applicationId": "00f1abcdexample"},
            {"runtime": "emr-serverless", "clusterId": "j-ABC"},
            {"runtime": "emr-serverless", "virtualClusterId": "abc"},
            {"runtime": "glue", "releaseLabel": "emr-7.12.0"},
            {"runtime": "emr-serverless", "glueVersion": "5.0"},
            {"runtime": "emr-serverless", "applicationId": "UPPER"},
            {"runtime": "emr-ec2", "clusterId": "cluster-1"},
            {"runtime": "emr-serverless", "releaseLabel": "7.12.0"},
            {"runtime": "glue", "glueVersion": "5"},
            {"runtime": "emr-serverless", "executionRoleArn": "arn:aws:iam::1234:role/x"},
            {"runtime": "emr-serverless", "executionRoleArn": "arn:aws:iam::123456789012:user/x"},
            {"runtime": "emr-serverless", "sparkConf": {"spark.executor.instances": 4}},
            {"runtime": "emr-serverless", "sparkConf": {"bad key": "x"}},
            {"runtime": "emr-serverless", "entryPoint": "s3://bucket/job.py"},
            # parameters.spark.source: exactly one of bootstrapServers / mskClusterArn.
            {"runtime": "emr-serverless", "source": {}},
            {"runtime": "emr-serverless", "source": {"auth": "msk-iam"}},
            {
                "runtime": "emr-serverless",
                "source": {
                    "bootstrapServers": "b-1:9092",
                    "mskClusterArn": "arn:aws:kafka:eu-west-1:123456789012:cluster/c/u",
                },
            },
            {"runtime": "emr-serverless", "source": {"mskClusterArn": "arn:aws:kafka:eu-west-1"}},
            {
                "runtime": "emr-serverless",
                "source": {"mskClusterArn": "arn:aws:kafka:eu-west-1:1234:cluster/c/u"},
            },
            {
                "runtime": "emr-serverless",
                "source": {"mskClusterArn": "arn:aws:iam::123456789012:role/not-a-cluster"},
            },
            {
                "runtime": "emr-serverless",
                "source": {"mskClusterArn": "arn:aws:kafka:eu-west-1:123456789012:cluster/c\n"},
            },
            {"runtime": "emr-serverless", "source": {"bootstrapServers": ""}},
            {
                "runtime": "emr-serverless",
                "source": {"bootstrapServers": "b:9092", "auth": "scram"},
            },
            {
                "runtime": "emr-serverless",
                "source": {"bootstrapServers": "b:9092", "schemaRegistryUrl": "https://r"},
            },
        ],
    )
    def test_rejected(self, manager, spark):
        assert not _valid(manager, _spark_params(**spark)), spark

    @pytest.mark.parametrize(
        ("spark", "says"),
        [
            (
                {"runtime": "emr-ec2", "applicationId": "abc"},
                "applicationId is only valid with runtime: emr-serverless",
            ),
            (
                {"runtime": "emr-serverless", "clusterId": "j-ABC"},
                "clusterId is only valid with runtime: emr-ec2",
            ),
            (
                {"runtime": "glue", "virtualClusterId": "abc"},
                "virtualClusterId is only valid with runtime: emr-eks",
            ),
            ({"runtime": "glue", "releaseLabel": "emr-7.12.0"}, "glue runtime takes glueVersion"),
            ({"runtime": "emr-ec2", "glueVersion": "5.0"}, "glueVersion is only valid with"),
        ],
    )
    def test_a_field_of_another_runtime_is_named(self, manager, spark, says):
        messages = _messages(_validate(manager, _spark_params(**spark)))
        assert any(says in m for m in messages), messages
        assert not any("should not be valid" in m for m in messages), messages

    def test_defaults_and_the_split_with_execution_are_documented(self, manager):
        spark = manager._bundled_schemas["0.7.7"]["$defs"]["sparkParameters"]
        props = spark["properties"]
        assert spark["required"] == ["runtime"]
        assert props["runtime"]["enum"] == ["emr-serverless", "emr-ec2", "emr-eks", "glue"]
        assert props["mode"]["enum"] == ["streaming", "batch"]
        assert props["glueVersion"]["default"] == "5.0"
        assert props["releaseLabel"]["default"] == "emr-7.12.0"
        assert "builds[].execution" in spark["description"]
        assert "execution.runtime" in spark["description"]

    @pytest.mark.parametrize(
        ("source", "says"),
        [
            ({}, "set bootstrapServers (a host:port list) or mskClusterArn"),
            ({"auth": "msk-iam"}, "set bootstrapServers (a host:port list) or mskClusterArn"),
            (
                {
                    "bootstrapServers": "b-1:9092",
                    "mskClusterArn": "arn:aws:kafka:eu-west-1:123456789012:cluster/c/u",
                },
                "set one of bootstrapServers or mskClusterArn, not both",
            ),
        ],
    )
    def test_the_source_rule_names_its_keys(self, manager, source, says):
        messages = _messages(_validate(manager, _spark_params(runtime="glue", source=source)))
        assert any(says in m for m in messages), messages

    @pytest.mark.parametrize("field", ["bootstrapServers", "groupIdPrefix"])
    @pytest.mark.parametrize("value", ["   ", " b:9092", "b:9092 ", "b:9092\nx", "b:9092\n"])
    def test_source_text_takes_no_blank_or_line_break(self, manager, field, value):
        source = {"bootstrapServers": "b-1:9092", field: value}
        assert not _valid(manager, _spark_params(runtime="glue", source=source))

    def test_the_kafka_source_mirrors_flink_and_defers_the_registry(self, manager):
        source = manager._bundled_schemas["0.7.7"]["$defs"]["sparkParameters"]["properties"][
            "source"
        ]
        assert source["allOf"][0]["anyOf"] == [
            {"required": ["bootstrapServers"]},
            {"required": ["mskClusterArn"]},
        ]
        assert source["properties"]["auth"]["enum"] == ["msk-iam", "none"]
        assert "msk-iam with mskClusterArn" in source["properties"]["auth"]["description"]
        assert "parameters.flink.source.bootstrapServers" in (
            source["properties"]["bootstrapServers"]["description"]
        )
        assert "properties.stream.registry" in source["description"]

    def test_other_parameters_stay_free_form(self, manager):
        contract = _fixture()
        contract["builds"][0]["properties"]["parameters"]["custom"] = {"anything": [1, 2]}
        assert _valid(manager, contract)

    def test_a_spark_stream_build_needs_no_sql(self, manager):
        contract = _fixture()
        assert "sql" not in contract["builds"][0]["properties"]
        assert _valid(manager, contract)

    def test_a_spark_stream_build_without_a_mapping_still_needs_sql(self, manager):
        # Neither sql nor mapping: the build would compute nothing.
        contract = _fixture()
        del contract["builds"][0]["properties"]["mapping"]
        assert not _valid(manager, contract)
        contract["builds"][0]["properties"]["sql"] = "SELECT * FROM orders"
        assert _valid(manager, contract)

    def test_a_spark_build_without_a_stream_still_needs_sql(self, manager):
        contract = _fixture()
        del contract["builds"][1]["properties"]["sql"]
        result = _validate(manager, contract)
        assert not result.is_valid
        assert any("'sql' is a required property" in m for m in _messages(result))

    def test_a_stream_build_without_spark_or_managed_flink_still_needs_sql(self, manager):
        contract = _fixture()
        del contract["builds"][0]["properties"]["parameters"]["spark"]
        assert not _valid(manager, contract)

    def test_the_managed_flink_exemption_is_unchanged(self, manager):
        flink = yaml.safe_load((ROOT / "examples/flink-iceberg/contract.fluid.yaml").read_text())
        assert "sql" not in flink["builds"][0]["properties"]
        assert _valid(manager, flink)
        del flink["builds"][0]["properties"]["parameters"]["flink"]["management"]
        assert not _valid(manager, flink)


# ---------------------------------------------------------------------------
# packaging: the six AWS container kinds
# ---------------------------------------------------------------------------


def _packaging(**block) -> Dict[str, Any]:
    contract = _fixture()
    contract["packaging"] = block
    return contract


class TestPackagingSchema:
    @pytest.mark.parametrize("kind", PREVIEW_CONTAINER_KINDS)
    def test_each_kind_takes_shared(self, manager, kind):
        contract = _packaging(mode="isolated", pool="p", containers={kind: "shared"})
        assert _valid(manager, contract), _messages(_validate(manager, contract))

    @pytest.mark.parametrize("kind", [k for k in PREVIEW_CONTAINER_KINDS if k != "mwaaEnvironment"])
    def test_each_kind_but_mwaa_takes_isolated(self, manager, kind):
        assert _valid(manager, _packaging(containers={kind: "isolated"}))

    def test_mwaa_isolated_is_refused_by_the_schema(self, manager):
        assert not _valid(manager, _packaging(containers={"mwaaEnvironment": "isolated"}))

    def test_an_unknown_kind_is_still_refused(self, manager):
        assert not _valid(manager, _packaging(containers={"emrCluster": "shared"}))


class TestPackagingResolver:
    def test_the_fixture_resolves(self):
        resolution = resolve_packaging(_fixture())
        assert resolution.decision_for("lfTagVocabulary") is ContainerDecision.REFERENCED
        assert resolution.decision_for("emrApplication") is ContainerDecision.REFERENCED
        assert resolution.decision_for("kmsKey") is ContainerDecision.OWNED
        assert resolution.decision_for("mwaaEnvironment") is ContainerDecision.REFERENCED
        assert resolution.decision_for("bucket") is ContainerDecision.OWNED
        assert resolution.decision_for("emrApplication", "orders") is ContainerDecision.REFERENCED

    def test_the_blanket_mode_reaches_the_new_kinds(self):
        shared = resolve_packaging({"packaging": {"mode": "shared", "pool": "p"}})
        isolated = resolve_packaging({"packaging": {"mode": "isolated"}})
        for kind in PREVIEW_CONTAINER_KINDS:
            assert shared.decision_for(kind) is ContainerDecision.REFERENCED
        for kind in set(PREVIEW_CONTAINER_KINDS) - {"mwaaEnvironment"}:
            assert isolated.decision_for(kind) is ContainerDecision.OWNED

    def test_mwaa_is_referenced_under_isolated_and_needs_no_pool(self):
        resolution = resolve_packaging({"packaging": {"mode": "isolated"}})
        assert resolution.decision_for("mwaaEnvironment") is ContainerDecision.REFERENCED
        assert resolution.pool is None
        explicit = resolve_packaging({"packaging": {"containers": {"mwaaEnvironment": "shared"}}})
        assert explicit.pool is None

    def test_mwaa_isolated_is_refused(self):
        with pytest.raises(PackagingError) as raised:
            resolve_packaging({"packaging": {"containers": {"mwaaEnvironment": "isolated"}}})
        assert raised.value.kind == "shared-only-container"

    @pytest.mark.parametrize("kind", [k for k in PREVIEW_CONTAINER_KINDS if k != "mwaaEnvironment"])
    def test_a_shared_new_kind_needs_a_pool(self, kind):
        with pytest.raises(PackagingError) as raised:
            resolve_packaging({"packaging": {"containers": {kind: "shared"}}})
        assert raised.value.kind == "pool-required"

    def test_a_block_written_before_077_still_resolves(self):
        # mode: shared with each of the six kinds overridden to isolated shares
        # nothing, so it never needed a pool; the preview kinds must not change that.
        block = {"mode": "shared", "containers": {k: "isolated" for k in CONTAINER_KINDS}}
        resolution = resolve_packaging({"packaging": block, "exposes": []})
        assert resolution.pool is None
        assert resolution.decision_for("emrApplication") is ContainerDecision.REFERENCED

    def test_a_binding_override_wins_for_a_new_kind(self):
        contract = {
            "packaging": {"mode": "isolated"},
            "exposes": [
                {
                    "exposeId": "orders",
                    "binding": {
                        "platform": "aws",
                        "packaging": {"pool": "p", "containers": {"emrApplication": "shared"}},
                    },
                }
            ],
        }
        resolution = resolve_packaging(contract)
        assert resolution.decision_for("emrApplication") is ContainerDecision.OWNED
        assert resolution.decision_for("emrApplication", "orders") is ContainerDecision.REFERENCED

    def test_no_block_is_legacy_for_the_new_kinds(self):
        resolution = resolve_packaging({"exposes": [{"exposeId": "o", "binding": {}}]})
        assert resolution is LEGACY
        for kind in PREVIEW_CONTAINER_KINDS:
            assert resolution.decision_for(kind) is ContainerDecision.LEGACY
            assert resolution.decision_for(kind, "o") is ContainerDecision.LEGACY

    def test_an_unknown_kind_lists_every_kind(self):
        with pytest.raises(PackagingError) as raised:
            resolve_packaging({"packaging": {"containers": {"emrCluster": "shared"}}})
        assert raised.value.kind == "invalid-container-kind"
        assert "mwaaEnvironment" in str(raised.value) and "bucket" in str(raised.value)

    def test_the_plan_summary_keeps_its_six_kinds(self):
        """``plan.json`` serializes this summary, so a new key would move every plan digest."""
        summary = build_packaging_summary(_fixture())
        assert set(summary["containers"]) == set(CONTAINER_KINDS)
        assert all(
            set(e["containers"]) == set(CONTAINER_KINDS) for e in summary.get("exposures", [])
        )
        stable = _fixture()
        stable["packaging"] = {"mode": "shared", "pool": "p"}
        assert set(build_packaging_summary(stable)["containers"]) == set(CONTAINER_KINDS)


# ---------------------------------------------------------------------------
# The commands an operator runs
# ---------------------------------------------------------------------------


def _fluid(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"}
    }
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["COLUMNS"] = "200"
    return subprocess.run(
        [sys.executable, "-m", "fluid_build.cli", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


class TestCommands:
    def test_fluid_validate_accepts_the_fixture(self, tmp_path, capsys):
        from fluid_build.cli import build_parser
        from fluid_build.cli.validate import run

        args = build_parser().parse_args(["validate", str(FIXTURE), "--offline"])
        rc = run(args, logging.getLogger("test"))
        output = "".join(capsys.readouterr())
        assert rc == 0, output

    def test_generate_iac_aws_runs(self, tmp_path):
        out = tmp_path / "iac"
        done = _fluid(
            "generate", "iac", str(FIXTURE), "--provider", "aws", "--out", str(out), cwd=tmp_path
        )
        assert done.returncode == 0, done.stdout + done.stderr
        assert json.loads((out / "main.tf.json").read_text())["resource"]

    def test_diff_against_a_changed_copy_runs(self, tmp_path):
        changed = _fixture()
        stream = _stream(changed)
        stream["trigger"] = {"availableNow": True}
        stream["registry"] = {"kind": "glue", "registryName": "commerce"}
        stream["format"] = "json"
        del changed["builds"][0]["properties"]["mapping"]["columns"][0]["maskAtIngest"]
        _spark(changed).clear()
        _spark(changed).update(runtime="glue", glueVersion="5.0")
        path = tmp_path / "new.fluid.yaml"
        path.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")
        done = _fluid("diff", str(path), "--baseline", str(FIXTURE), cwd=tmp_path)
        assert done.returncode in (0, 1), done.stdout + done.stderr
        assert "Traceback" not in done.stdout + done.stderr
        assert "stream_format_changed" in done.stdout

    def test_odcs_export_runs(self, tmp_path):
        out = tmp_path / "odcs.yaml"
        done = _fluid("odcs", "export", str(FIXTURE), "--output", str(out), cwd=tmp_path)
        assert done.returncode == 0, done.stdout + done.stderr
        exported = yaml.safe_load(out.read_text())
        assert exported["kind"] == "DataContract"
        assert {s["name"] for s in exported["schema"]} == {"orders", "orders_daily"}
