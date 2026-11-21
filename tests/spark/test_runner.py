# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The runner's own logic that runs before Spark does: no pyspark needed here."""

import ast
import hashlib
import json
import sys
import types

import pytest

from fluid_build.spark import runner_source
from fluid_build.spark.compiler import compile_job_config

from .conftest import BROKERS, MASK_KEY_ARN, RUNNER_PATH


def config_text(contract, **kw):
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


def test_an_unresolved_interpolation_is_refused(runner, contract, monkeypatch):
    from fluid_build.spark.compiler import TARGET_ALLOWLIST_ENV

    arn = "arn:aws:kafka:eu-west-1:000000000005:cluster/o/1"
    monkeypatch.setenv(TARGET_ALLOWLIST_ENV, f"msk:{arn}")
    spark = contract["builds"][0]["properties"]["parameters"]["spark"]
    spark["source"] = {"mskClusterArn": arn}
    text = config_text(contract)  # the uploader has not filled in the MSK brokers
    with pytest.raises(runner.JobConfigError, match="unresolved"):
        runner.parse_config(text)


def test_msk_iam_sets_the_sasl_options(runner, contract):
    source = runner.parse_config(config_text(contract))["source"]
    options = runner.kafka_options(source)
    assert options["kafka.bootstrap.servers"] == BROKERS
    assert options["subscribe"] == "commerce.orders"
    assert options["kafka.sasl.mechanism"] == "AWS_MSK_IAM"
    assert options["kafka.security.protocol"] == "SASL_SSL"
    assert "groupIdPrefix" not in options
    plain = runner.kafka_options({**source, "auth": "none", "groupIdPrefix": "forge-o"})
    assert plain["groupIdPrefix"] == "forge-o"
    assert "kafka.sasl.mechanism" not in plain
    with pytest.raises(runner.JobConfigError, match="unknown Kafka auth"):
        runner.kafka_options({**source, "auth": "scram"})


def test_tls_without_iam_speaks_ssl_and_iam_stays_sasl_ssl(runner, contract):
    source = runner.parse_config(config_text(contract))["source"]
    tls = runner.kafka_options({**source, "auth": "none", "tls": True})
    assert tls["kafka.security.protocol"] == "SSL"
    assert "kafka.sasl.mechanism" not in tls
    plain = runner.kafka_options({**source, "auth": "none", "tls": False})
    assert "kafka.security.protocol" not in plain
    iam = runner.kafka_options({**source, "auth": "msk-iam", "tls": True})
    assert iam["kafka.security.protocol"] == "SASL_SSL"


def test_a_registry_credential_comes_from_the_environment(runner, monkeypatch):
    monkeypatch.setenv("REGISTRY_USER_INFO", "user:secret")  # pragma: allowlist secret
    assert runner.resolve_secret("env://REGISTRY_USER_INFO") == "user:secret"
    registry = {
        "url": "http://r:8081",
        "auth": {"secretRef": "env://REGISTRY_USER_INFO"},  # pragma: allowlist secret
    }
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

    class Opener:
        def open(self, request, timeout):
            seen["url"], seen["auth"] = request.full_url, request.get_header("Authorization")
            return Response()

    def build_opener(*handlers):
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(runner.urllib.request, "build_opener", build_opener)
    monkeypatch.setenv("REGISTRY_USER_INFO", "u:p")  # pragma: allowlist secret
    ref = "env://REGISTRY_USER_INFO"  # pragma: allowlist secret
    registry = {"url": "http://r:8081/", "auth": {"secretRef": ref}}
    assert runner.fetch_reader_schema(registry, "a/b-value", 3) == '{"type":"record"}'
    assert seen["url"] == "http://r:8081/subjects/a%2Fb-value/versions/3"
    assert seen["auth"] == "Basic dTpw"
    assert seen["handlers"] == (runner._NoRedirect,)


def test_a_registry_redirect_is_refused_not_followed(runner):
    """urllib would send the Authorization header on to the redirect's host."""
    with pytest.raises(runner.JobConfigError, match="redirect"):
        runner._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil/")


def test_an_aws_sm_reference_with_a_field_and_a_version(runner, monkeypatch):
    client = _boto3(monkeypatch, {"SecretString": '{"user_info": "u:p"}'})
    calls = []
    client.get_secret_value = lambda **kw: calls.append(kw) or {
        "SecretString": '{"user_info": "u:p"}'
    }
    assert runner.resolve_secret("aws-sm://commerce/registry#user_info?version=v1") == "u:p"
    assert calls == [
        {"SecretId": "commerce/registry", "VersionId": "v1"}  # pragma: allowlist secret
    ]


def test_a_batch_config_that_reads_a_topic_is_refused(runner, contract):
    config = json.loads(config_text(contract))
    config["mode"] = "batch"
    with pytest.raises(runner.JobConfigError, match="reads no topic"):
        runner.parse_config(json.dumps(config))


def test_the_runner_imports_nothing_but_the_standard_library_at_module_level():
    """It runs on clusters without forge: pyspark and boto3 are imported where used."""
    tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module.split(".")[0])
    assert names <= {
        "__future__", "argparse", "base64", "functools", "hashlib", "hmac", "importlib",
        "inspect", "json", "os", "re", "shutil", "sys", "tempfile", "time", "types", "unicodedata",
        "urllib", "zipfile",
    }  # fmt: skip


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
        # the dead letters' check, compiled by forge-cli like the other two
        or c.endswith('spark.sql(queries["check"])')
        # the sink's own CREATE ... IF NOT EXISTS statements, compiled by forge-cli
        or c == "spark.sql(statement)"
        for c in calls
    )


def test_the_runner_ships_with_the_package():
    source = runner_source()
    assert "CONFIG_VERSION = 1" in source
    assert "def main(" in source


# ── maskAtIngest: keyed HMAC-SHA256 ─────────────────────────────────────


def test_the_mask_is_hmac_sha256_under_the_key(runner):
    # RFC 4231, test case 2.
    assert runner.mask_digest(b"Jefe", b"what do ya want for nothing?") == (
        "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"  # pragma: allowlist secret - a reference or test vector
    )


def test_the_same_value_and_key_give_the_same_digest_and_another_key_another(runner):
    key, other = b"k" * 32, b"o" * 32
    value = bytearray(b"a@example.com")  # Spark hands a binary over as bytearray
    assert runner.mask_digest(key, value) == runner.mask_digest(key, b"a@example.com")
    assert runner.mask_digest(key, value) != runner.mask_digest(other, value)
    assert runner.mask_digest(key, value) != hashlib.sha256(b"a@example.com").hexdigest()


def test_null_stays_null(runner):
    assert runner.mask_digest(b"k", None) is None


class _SecretsManager:
    def __init__(self, secret):
        self.secret = secret
        self.asked = []

    def get_secret_value(self, SecretId):
        self.asked.append(SecretId)
        return self.secret


def _boto3(monkeypatch, secret):
    client = _SecretsManager(secret)
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda service: client))
    return client


def test_the_key_comes_from_secrets_manager_and_is_never_printed(runner, monkeypatch, capsys):
    key = "a-very-secret-masking-key"  # pragma: allowlist secret
    client = _boto3(monkeypatch, {"SecretString": key})
    assert runner.load_mask_key(MASK_KEY_ARN) == key.encode()
    assert client.asked == [MASK_KEY_ARN]
    out = capsys.readouterr()
    assert key not in out.out + out.err
    _boto3(monkeypatch, {"SecretBinary": b"\x01\x02"})
    assert runner.load_mask_key(MASK_KEY_ARN) == b"\x01\x02"
    _boto3(monkeypatch, {"SecretString": ""})
    with pytest.raises(runner.JobConfigError, match="empty") as refused:
        runner.load_mask_key(MASK_KEY_ARN)
    assert MASK_KEY_ARN not in str(refused.value)


def test_the_key_never_reaches_the_job_config(contract):
    """The config names the key's secret by ARN, once, and holds nothing else of it."""
    text = config_text(contract)
    assert json.loads(text)["maskKey"] == {"secretArn": MASK_KEY_ARN}
    assert text.count("secretsmanager") == 1


def test_a_batch_run_replaces_the_partitions_it_computes(runner, contract, monkeypatch):
    calls = []

    class Writer:
        def option(self, name, value):
            calls.append((name, value))
            return self

        def overwritePartitions(self):  # noqa: N802 - PySpark's name
            calls.append("overwritePartitions")

    class Frame:
        def writeTo(self, table):  # noqa: N802 - PySpark's name
            calls.append(table)
            return Writer()

    config = runner.parse_config(config_text(contract))
    config.update(mode="batch", source=None, maskedColumns=[], maskKey=None)
    monkeypatch.setattr(runner, "build_output", lambda spark, cfg: Frame())
    runner.run(_NoSpark(), config)
    assert calls == [
        "fluid.commerce.order_lines",
        ("check-nullability", "false"),
        "overwritePartitions",
    ]


def test_a_masked_config_without_its_key_arn_is_refused(runner, contract):
    config = runner.parse_config(config_text(contract))
    config["maskKey"] = None
    with pytest.raises(runner.JobConfigError, match="maskKey.secretArn"):
        runner.run(_NoSpark(), config)


class _NoSpark:
    class conf:  # noqa: N801 - a stand-in for SparkSession.conf
        @staticmethod
        def set(name, value):
            pass

        @staticmethod
        def get(name, default=None):
            return default


@pytest.mark.parametrize(
    "argv",
    [
        # Glue's shape: its own arguments around the job's, --conf from the build's sparkConf
        ["--JOB_ID", "j_1", "--JOB_RUN_ID", "jr_1", "--job-bookmark-option",
         "job-bookmark-disable", "--TempDir", "s3://tmp/", "--enable-glue-datacatalog", "true",
         "--config", "s3://b/_fluid/job.json", "--conf", "spark.sql.shuffle.partitions=8",
         "--JOB_NAME", "fluid-x-spark", "--fluid-config-hash", "abc"],
        ["--conf", "spark.a=b", "--config", "s3://b/_fluid/job.json", "--JOB_NAME", "x"],
        ["--config", "s3://b/_fluid/job.json", "--conf", "spark.a=b", "--co", "x"],
    ],
)  # fmt: skip
def test_glue_arguments_never_stand_in_for_the_config(runner, monkeypatch, argv):
    """Found by the spark-infra lane: with abbreviations on, Glue's --conf was --config."""
    read = []
    session = types.SimpleNamespace(builder=types.SimpleNamespace(getOrCreate=lambda: "spark"))
    monkeypatch.setitem(sys.modules, "pyspark", types.ModuleType("pyspark"))
    monkeypatch.setitem(sys.modules, "pyspark.sql", types.SimpleNamespace(SparkSession=session))
    monkeypatch.setattr(runner, "read_text", lambda spark, path: read.append(path) or "{}")
    monkeypatch.setattr(runner, "parse_config", lambda text: {})
    monkeypatch.setattr(runner, "run", lambda spark, config: None)
    assert runner.main(argv) == 0
    assert read == ["s3://b/_fluid/job.json"]
