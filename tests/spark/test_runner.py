# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The runner's own logic that runs before Spark does: no pyspark needed here."""

import ast
import json

import pytest

from fluid_build.spark import runner_source
from fluid_build.spark.compiler import compile_job_config

from .conftest import BROKERS, RUNNER_PATH


def config_text(contract, **kw):
    kw.setdefault("bootstrap_servers", BROKERS)
    return compile_job_config(contract, contract["builds"][0], **kw).text


def test_the_runner_reads_what_the_compiler_writes(runner, contract):
    config = runner.parse_config(config_text(contract))
    assert config["configVersion"] == runner.CONFIG_VERSION


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"configVersion": 2}, "configVersion 1 only"),
        ({"sink": None}, "lacks"),
        ({"mode": "micro"}, "streaming or batch"),
        ({"checkpoint": None}, "keeps a checkpoint"),
    ],
)
def test_a_config_the_runner_does_not_run_is_refused(runner, contract, change, message):
    config = {**json.loads(config_text(contract)), **change}
    with pytest.raises(runner.JobConfigError, match=message):
        runner.parse_config(json.dumps(config))


def test_an_unresolved_interpolation_is_refused(runner, contract):
    text = config_text(contract, bootstrap_servers="${aws_msk_cluster.x.bootstrap_brokers}")
    with pytest.raises(runner.JobConfigError, match="unresolved"):
        runner.parse_config(text)


def test_msk_iam_sets_the_sasl_options(runner, contract):
    source = runner.parse_config(config_text(contract))["source"]
    options = runner.kafka_options(source, "streaming")
    assert options["kafka.bootstrap.servers"] == BROKERS
    assert options["subscribe"] == "commerce.orders"
    assert options["kafka.sasl.mechanism"] == "AWS_MSK_IAM"
    assert options["kafka.security.protocol"] == "SASL_SSL"
    assert "endingOffsets" not in options
    plain = runner.kafka_options({**source, "auth": "none"}, "batch")
    assert "kafka.sasl.mechanism" not in plain
    assert plain["endingOffsets"] == "latest"
    with pytest.raises(runner.JobConfigError, match="unknown Kafka auth"):
        runner.kafka_options({**source, "auth": "scram"}, "streaming")


def test_a_registry_credential_comes_from_the_environment(runner, monkeypatch):
    monkeypatch.setenv("REGISTRY_USER_INFO", "user:secret")  # pragma: allowlist secret
    assert runner.resolve_secret("env://REGISTRY_USER_INFO") == "user:secret"
    registry = {"url": "http://r:8081", "auth": {"secretRef": "env://REGISTRY_USER_INFO"}}
    assert runner.registry_conf(registry) == {
        "schema.registry.url": "http://r:8081",
        "basic.auth.credentials.source": "USER_INFO",
        "basic.auth.user.info": "user:secret",  # pragma: allowlist secret
    }
    monkeypatch.delenv("REGISTRY_USER_INFO")
    with pytest.raises(runner.JobConfigError, match="is not set"):
        runner.resolve_secret("env://REGISTRY_USER_INFO")
    with pytest.raises(runner.JobConfigError, match="does not read vault"):
        runner.resolve_secret("vault://kv/x#y")


def test_the_reader_schema_is_fetched_by_subject_and_version(runner, monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"schema": '{"type":"record"}'}).encode()

    def urlopen(request, timeout):
        seen["url"], seen["auth"] = request.full_url, request.get_header("Authorization")
        return Response()

    monkeypatch.setattr(runner.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("REGISTRY_USER_INFO", "u:p")  # pragma: allowlist secret
    registry = {"url": "http://r:8081/", "auth": {"secretRef": "env://REGISTRY_USER_INFO"}}
    assert runner.fetch_reader_schema(registry, "a/b-value", 3) == '{"type":"record"}'
    assert seen["url"] == "http://r:8081/subjects/a%2Fb-value/versions/3"
    assert seen["auth"] == "Basic dTpw"


def test_the_runner_imports_nothing_but_the_standard_library_at_module_level():
    """It runs on clusters without forge: pyspark and boto3 are imported where used."""
    tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module.split(".")[0])
    assert names <= {"__future__", "argparse", "base64", "json", "os", "sys", "urllib"}


def test_the_runner_never_formats_the_query_text():
    """spark.sql(text, **kwargs) runs str.format on the text, and map keys may hold { }."""
    source = RUNNER_PATH.read_text(encoding="utf-8")
    calls = [
        line.strip()
        for line in source.splitlines()
        if "spark.sql(" in line and not line.strip().startswith("#")
    ]
    assert calls and all(
        c.endswith('spark.sql(queries["source"])') or c.endswith('spark.sql(queries["select"])')
        for c in calls
    )


def test_the_runner_ships_with_the_package():
    source = runner_source()
    assert "CONFIG_VERSION = 1" in source
    assert "def main(" in source
