# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A Glue Spark build loads only the jars Glue 5 lacks, each pinned by sha256
(``iac/providers/aws_glue_jars.py``)."""

from __future__ import annotations

import base64
import copy
import shutil
import subprocess

import pytest

from fluid_build.credentials.protected_env import is_protected_env_key
from fluid_build.iac import get_iac_plugin
from fluid_build.iac.base import UnsupportedBindingError
from fluid_build.iac.module import build_module
from fluid_build.iac.providers import aws_glue_jars, aws_identity
from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV
from tests.iac.test_iac_aws_spark_infra import CONTRACT, KEY, VIRTUAL_CLUSTER

pytestmark = [pytest.mark.unit, pytest.mark.provider]

JARS = "s3://platform-jars/spark/glue5/"
REGISTRY_REF = "aws-sm://commerce/registry-secret"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def operator(monkeypatch):
    for name in (
        aws_glue_jars.JARS_URL_ENV,
        aws_identity.EXECUTION_ROLE_ALLOWLIST_ENV,
        aws_identity.SPARK_CONF_ALLOWLIST_ENV,
        "FLUID_SPARK_CONTRACT_SQL_ALLOWLIST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        aws_identity.TARGET_ALLOWLIST_ENV,
        f"emr-serverless:00fabc123def,emr-eks:{VIRTUAL_CLUSTER}",
    )
    monkeypatch.setenv(HOST_ALLOWLIST_ENV, "example.internal")
    monkeypatch.setenv(
        "FLUID_ACQUISITION_SECRET_REF_ALLOWLIST",
        f"{REGISTRY_REF}@registry.example.internal",
    )


def glue(avro: bool = False):
    contract = copy.deepcopy(CONTRACT)
    params = contract["builds"][0]["properties"]["parameters"]["spark"]
    params["runtime"] = "glue"
    del params["applicationId"]
    if avro:
        stream = contract["builds"][0]["properties"]["stream"]
        stream["format"] = "avro-confluent"
        stream["registry"] = {
            "kind": "confluent",
            "url": "https://registry.example.internal",
            "auth": {"secretRef": REGISTRY_REF},
        }
    return contract


def emit(contract):
    plugin = get_iac_plugin("aws")
    return plugin.emit(contract), plugin.emit_data(contract)


def test_the_lock_table_is_consistent():
    files = [j.file for j in aws_glue_jars.AVRO_JARS]
    assert len(set(files)) == len(files)
    for jar in aws_glue_jars.AVRO_JARS:
        assert base64.b64encode(bytes.fromhex(jar.sha256)).decode() == jar.sha256_b64
        group, artifact, version = jar.coordinate.split(":")
        assert jar.file == f"{artifact}-{version}.jar"
        assert not artifact.endswith("_2.13")  # Glue 5 is Scala 2.12
    # What Glue 5 ships is never pinned again (measured: Kafka, MSK IAM, spark-avro).
    coordinates = " ".join(j.coordinate for j in aws_glue_jars.AVRO_JARS)
    for shipped in ("spark-sql-kafka", "aws-msk-iam-auth", "kafka-clients", "spark-avro"):
        assert shipped not in coordinates


def test_the_jar_prefix_is_an_operator_only_setting():
    assert is_protected_env_key(aws_glue_jars.JARS_URL_ENV)


def test_a_json_build_on_glue_adds_no_jars(monkeypatch):
    monkeypatch.setenv(aws_glue_jars.JARS_URL_ENV, JARS)
    resources, data = emit(glue())
    args = resources["aws_glue_job"][KEY]["default_arguments"]
    assert "--extra-jars" not in args and "--user-jars-first" not in args
    assert "aws_s3_object" not in data
    assert "lifecycle" not in resources["aws_glue_job"][KEY]


def test_an_avro_build_on_glue_needs_the_operators_jar_prefix():
    contract = glue(avro=True)
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(contract)
    assert caught.value.kind == "spark-glue-jars-unset"
    assert any(aws_glue_jars.JARS_URL_ENV in w for w in aws_identity.validation_warnings(contract))


def test_a_malformed_prefix_is_refused(monkeypatch):
    monkeypatch.setenv(aws_glue_jars.JARS_URL_ENV, "https://example.com/jars/")
    with pytest.raises(UnsupportedBindingError) as caught:
        emit(glue(avro=True))
    assert caught.value.kind == "spark-glue-jars-url"


def test_an_avro_build_loads_exactly_the_pinned_jars_and_checks_each(monkeypatch):
    monkeypatch.setenv(aws_glue_jars.JARS_URL_ENV, "s3://platform-jars/spark/glue5")
    contract = glue(avro=True)
    assert aws_identity.validation_warnings(contract) == []
    resources, data = emit(contract)
    job = resources["aws_glue_job"][KEY]
    uris = [f"s3://platform-jars/spark/glue5/{j.file}" for j in aws_glue_jars.AVRO_JARS]
    assert job["default_arguments"]["--extra-jars"] == ",".join(uris)
    assert job["default_arguments"]["--user-jars-first"] == "true"
    lookups = data["aws_s3_object"]
    assert len(lookups) == len(aws_glue_jars.AVRO_JARS)
    for index, jar in enumerate(aws_glue_jars.AVRO_JARS):
        assert lookups[f"{KEY}_jar_{index}"] == {
            "bucket": "platform-jars",
            "key": f"spark/glue5/{jar.file}",
            "checksum_mode": "ENABLED",
        }
        check = job["lifecycle"]["precondition"][index]
        assert check["condition"] == (
            f'${{data.aws_s3_object.{KEY}_jar_{index}.checksum_sha256 == "{jar.sha256_b64}"}}'
        )
        assert jar.sha256 in check["error_message"]
    access = data["aws_iam_policy_document"][f"{KEY}_access"]
    read = next(s for s in access["statement"] if s["sid"] == "ReadJars")
    assert read["resources"] == [
        f"arn:aws:s3:::platform-jars/spark/glue5/{j.file}" for j in aws_glue_jars.AVRO_JARS
    ]
    kms = next(s for s in access["statement"] if s["sid"] == "S3Kms")
    assert "arn:aws:s3:::platform-jars" in kms["condition"][1]["values"]


def test_an_avro_build_on_emr_serverless_adds_no_glue_jars(monkeypatch):
    contract = glue(avro=True)
    params = contract["builds"][0]["properties"]["parameters"]["spark"]
    app = "00fabc123def"  # pragma: allowlist secret - an id
    params.update(runtime="emr-serverless", applicationId=app)
    resources, data = emit(contract)
    assert "aws_glue_job" not in resources and "aws_s3_object" not in data


def test_the_upload_commands_keep_a_single_part_sha256():
    commands = aws_glue_jars.upload_commands(
        aws_glue_jars.jars_location({aws_glue_jars.JARS_URL_ENV: JARS})
    )
    assert len(commands) == len(aws_glue_jars.AVRO_JARS)
    assert all(c.endswith("--checksum-algorithm SHA256") for c in commands)


_TOFU = shutil.which("tofu")


@pytest.mark.integration
@pytest.mark.skipif(_TOFU is None, reason="tofu not on PATH")
def test_the_pinned_jar_checks_pass_tofu_validate(monkeypatch, tmp_path):
    from tests.iac.test_iac_tofu_validate import _tofu_init_or_skip

    monkeypatch.setenv(aws_glue_jars.JARS_URL_ENV, JARS)
    (tmp_path / "main.tf.json").write_text(build_module(get_iac_plugin("aws"), glue(avro=True)))
    _tofu_init_or_skip(tmp_path)
    done = subprocess.run(
        [_TOFU, "validate", "-no-color"], cwd=tmp_path, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr or done.stdout
