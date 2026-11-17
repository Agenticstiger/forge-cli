# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The Spark lane on LocalStack: ``fluid spark run`` starts the runner on Glue 5, EMR
Serverless and EMR on EC2, against MSK, the Glue catalog, S3 and Secrets Manager.

Opt-in: ``FLUID_SPARK_LOCALSTACK=1`` and a LocalStack Pro at ``FLUID_LOCALSTACK_ENDPOINT``
(default ``http://localhost:4566``) with glue, emr-serverless, emr, kafka, s3 and
secretsmanager. It never resets LocalStack, which other tests may share: every resource it
makes is named ``fluid-iactest-spark-<run>`` (Glue databases ``fluid_iactest_spark_<run>``)
and deleted by name at the end.

What LocalStack runs (measured on LocalStack Pro 2026.8.2):

- Glue 5.0 runs the real ``amazon/aws-glue-libs:5.0.8`` image (Spark 3.5.4-amzn, Iceberg
  1.7.1, boto3, pandas, pyarrow) with ``AWS_ENDPOINT_URL`` set to LocalStack: the whole
  streaming build runs there, the mask key included.
- EMR Serverless and EMR on EC2 run LocalStack's own Spark 3.3.0 on Java 8, inside the
  LocalStack container and without ``AWS_ENDPOINT_URL``, whatever the release label says.
  They run a batch build (no Kafka, no mask key), with Spark 3.3 packages and the catalog's
  LocalStack endpoints added to the submitted Spark configuration by the test
  (:func:`._localstack_emr.with_spark_conf`), not by the contract: a contract's ``sparkConf``
  can never set those keys, and forge's own EMR defaults (EMR's Iceberg jar path, Spark 3.5
  packages) are EMR's.
- MSK is a plaintext Kafka. ``get-bootstrap-brokers`` gives only ``BootstrapBrokerStringTls``,
  whose port speaks no TLS, and there is no SASL/IAM listener: ``auth: msk-iam`` is not tested
  here, and the uploaded config of an ARN read without IAM has ``tls`` set to false.
- EMR on EKS is not emulated.

Nothing in forge yet provisions a Spark build's infrastructure, so the test does it in that
lane's place: the Glue job, the EMR Serverless application, the EMR cluster and the Iceberg
tables, the upload of ``job.json`` and the runner, and ``${fluid_msk_bootstrap_brokers}``
filled in from ``get-bootstrap-brokers``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
import uuid
from types import SimpleNamespace

import pytest
import yaml

from fluid_build.build_runners.spark._desired import desired_runs
from fluid_build.spark import runner_source
from fluid_build.spark.compiler import (
    CONTRACT_SQL_ALLOWLIST_ENV,
    MSK_BROKERS_PLACEHOLDER,
    TARGET_ALLOWLIST_ENV,
)

from .conftest import CONTRACT

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.aws,
    pytest.mark.emulated_heavy,
]

ENDPOINT = os.environ.get("FLUID_LOCALSTACK_ENDPOINT", "http://localhost:4566")
ACCOUNT = "000000000000"
REGION = "us-east-1"
SERVICES = ("glue", "emr-serverless", "emr", "kafka", "s3", "secretsmanager", "sts", "logs")
MASK_KEY = "fluid-iactest-spark-localstack-key"  # pragma: allowlist secret
#: LocalStack's EMR runs Spark 3.3: the Iceberg runtime and AWS bundle for it.
SPARK33_PACKAGES = (
    "org.apache.iceberg:iceberg-spark-runtime-3.3_2.12:1.6.1,"
    "org.apache.iceberg:iceberg-aws-bundle:1.6.1"
)
RUN_TIMEOUT_S = 1500

ORDERS = [
    {
        "order_id": "o1",
        "customer": {"id": "c1", "email": "a@example.com"},
        "items": [{"sku": "s1", "qty": 2}, {"sku": "s2", "qty": 1}],
        "event_time": "2026-11-16T10:00:00.123Z",
    },
    {"order_id": "o2", "customer": {"id": "c2"}, "items": [], "event_time": None},
    {"order_id": "o3", "customer": None, "items": [{"sku": "s3", "qty": 5}]},
]
DIGEST = hmac.new(MASK_KEY.encode(), b"a@example.com", hashlib.sha256).hexdigest()
LINES = [
    {
        "order_id": "o1",
        "customer_id": "c1",
        "email_hash": DIGEST,
        "sku": "s1",
        "qty": 2,
        "event_time": "2026-11-16T10:00:00.123000",
    },
    {
        "order_id": "o1",
        "customer_id": "c1",
        "email_hash": DIGEST,
        "sku": "s2",
        "qty": 1,
        "event_time": "2026-11-16T10:00:00.123000",
    },
    {
        "order_id": "o3",
        "customer_id": None,
        "email_hash": None,
        "sku": "s3",
        "qty": 5,
        "event_time": None,
    },
]
LINES_DDL = (
    "order_id STRING NOT NULL, customer_id STRING, email_hash STRING, sku STRING, qty INT, "
    "event_time TIMESTAMP"
)

#: A Glue job the test runs for itself: SQL over the Iceberg catalog, records to a topic,
#: then each listed table's rows printed as one ``FLUID_ROWS`` line.
HELPER = r"""
import base64, json, sys
from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
uri = sys.argv[sys.argv.index("--payload") + 1]
payload = json.loads(spark.read.text(uri, wholetext=True).first()[0])
spark.conf.set("spark.sql.session.timeZone", "UTC")
for name, value in payload["catalog"].items():
    spark.conf.set(name, value)
for statement in payload.get("sql", []):
    spark.sql(statement)
if payload.get("records"):
    rows = [(bytearray(base64.b64decode(v)),) for v in payload["records"]]
    (spark.createDataFrame(rows, "value BINARY").write.format("kafka")
        .option("kafka.bootstrap.servers", payload["bootstrap"])
        .option("topic", payload["topic"]).save())
for table in payload.get("dump", []):
    rows = [r.asDict() for r in spark.table(table).collect()]
    for r in rows:
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                r[k] = v.isoformat()
    print("FLUID_ROWS " + json.dumps({"table": table, "rows": rows}, sort_keys=True))
"""


def _localstack_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/health", timeout=5) as r:  # nosec B310
            services = json.load(r).get("services") or {}
        with urllib.request.urlopen(f"{ENDPOINT}/_localstack/info", timeout=5) as r:  # nosec B310
            edition = json.load(r).get("edition")
    except OSError:
        return False
    return edition == "pro" and all(services.get(s) in ("available", "running") for s in SERVICES)


@pytest.fixture(scope="module")
def ls():
    """LocalStack's clients and this run's names; deletes what the run made."""
    if os.environ.get("FLUID_SPARK_LOCALSTACK") != "1":
        pytest.skip("set FLUID_SPARK_LOCALSTACK=1 to run the Spark lane on LocalStack")
    if not _localstack_ready():
        pytest.skip(f"no LocalStack Pro with {', '.join(SERVICES)} at {ENDPOINT}")
    boto3 = pytest.importorskip("boto3")
    run = uuid.uuid4().hex[:8]
    mp = pytest.MonkeyPatch()
    for name, value in {
        "AWS_ENDPOINT_URL": ENDPOINT,
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",  # pragma: allowlist secret
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCOUNT_ID": ACCOUNT,
    }.items():
        mp.setenv(name, value)
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):
        mp.delenv(name, raising=False)

    def client(service):
        return boto3.client(service, region_name=REGION)

    prefix = f"fluid-iactest-spark-{run}"
    stack = SimpleNamespace(
        run=run,
        prefix=prefix,
        bucket=prefix,
        database=prefix.replace("-", "_"),
        client=client,
        made=[],  # (kind, name) to delete, newest last
    )
    try:
        # First, so it goes last: the tests' Glue jobs create it.
        stack.made.append(("glue-db", stack.database))
        client("s3").create_bucket(Bucket=stack.bucket)
        stack.made.append(("bucket", stack.bucket))
        secret = client("secretsmanager").create_secret(
            Name=f"{prefix}/mask-key", SecretString=MASK_KEY
        )
        stack.secret_arn = secret["ARN"]
        stack.made.append(("secret", stack.secret_arn))
        stack.msk_arn, stack.brokers = _msk(stack)
        yield stack
    finally:
        _clean(stack)
        mp.undo()


def _msk(stack):
    kafka, ec2 = stack.client("kafka"), stack.client("ec2")
    subnets = [s["SubnetId"] for s in ec2.describe_subnets()["Subnets"][:2]]
    arn = kafka.create_cluster_v2(
        ClusterName=f"{stack.prefix}-msk",
        Provisioned={
            "KafkaVersion": "3.6.0",
            "NumberOfBrokerNodes": 2,
            "BrokerNodeGroupInfo": {"InstanceType": "kafka.t3.small", "ClientSubnets": subnets},
        },
    )["ClusterArn"]
    stack.made.append(("msk", arn))
    for _ in range(120):
        if kafka.describe_cluster_v2(ClusterArn=arn)["ClusterInfo"]["State"] == "ACTIVE":
            break
        time.sleep(5)
    else:
        pytest.fail("the MSK cluster did not become ACTIVE")
    brokers = kafka.get_bootstrap_brokers(ClusterArn=arn)
    # LocalStack names its plaintext listener's brokers BootstrapBrokerStringTls.
    found = brokers.get("BootstrapBrokerString") or brokers.get("BootstrapBrokerStringTls")
    assert found, brokers
    return arn, found


def _clean(stack):
    for kind, name in reversed(stack.made):
        try:
            if kind == "bucket":
                s3 = stack.client("s3")
                for page in s3.get_paginator("list_objects_v2").paginate(Bucket=name):
                    keys = [{"Key": o["Key"]} for o in page.get("Contents") or []]
                    if keys:
                        s3.delete_objects(Bucket=name, Delete={"Objects": keys})
                s3.delete_bucket(Bucket=name)
            elif kind == "secret":
                stack.client("secretsmanager").delete_secret(
                    SecretId=name, ForceDeleteWithoutRecovery=True
                )
            elif kind == "msk":
                stack.client("kafka").delete_cluster(ClusterArn=name)
            elif kind == "glue-job":
                stack.client("glue").delete_job(JobName=name)
            elif kind == "glue-db":
                glue = stack.client("glue")
                if name not in [d["Name"] for d in glue.get_databases()["DatabaseList"]]:
                    continue
                for table in glue.get_tables(DatabaseName=name).get("TableList") or []:
                    glue.delete_table(DatabaseName=name, Name=table["Name"])
                glue.delete_database(Name=name)
            elif kind == "emr-app":
                emr = stack.client("emr-serverless")
                emr.stop_application(applicationId=name)
                emr.delete_application(applicationId=name)
            elif kind == "emr-cluster":
                stack.client("emr").terminate_job_flows(JobFlowIds=[name])
        except Exception as exc:  # noqa: BLE001 - report every leftover, keep deleting
            print(f"cleanup of {kind} {name} failed: {exc!r}")


def _put(stack, uri, text):
    bucket, key = uri.removeprefix("s3://").split("/", 1)
    stack.client("s3").put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"))


#: The Kafka source a Glue streaming job has on its class path. LocalStack runs every job
#: type with the glueetl one (``Failed to find data source: kafka``), so the test's jobs add
#: them, as Glue's gluestreaming job type does.
GLUE_KAFKA_JARS = ",".join(
    f"local://{path}"
    for path in (
        "/usr/lib/spark/connector/lib/spark-sql-kafka-0-10_2.12-3.5.4-amzn-0.jar",
        "/usr/lib/spark/connector/lib/spark-token-provider-kafka-0-10_2.12-3.5.4-amzn-0.jar",
        "/usr/share/aws/glue-streaming/lib/kafka-clients-3.4.1.jar",
        "/usr/share/aws/glue-streaming/lib/commons-pool2-2.11.1.jar",
    )
)


def _glue_job(stack, name, script_uri, role):
    stack.client("glue").create_job(
        Name=name,
        Role=role,
        GlueVersion="5.0",
        Command={"Name": "gluestreaming", "ScriptLocation": script_uri, "PythonVersion": "3"},
        DefaultArguments={
            "--datalake-formats": "iceberg",
            "--conf": f"spark.jars={GLUE_KAFKA_JARS}",
        },
    )
    stack.made.append(("glue-job", name))


def _glue_logs(stack, run_id):
    logs = stack.client("logs")
    try:
        events = logs.filter_log_events(
            logGroupName="/aws-glue/jobs/logs-v2", logStreamNames=[run_id]
        )["events"]
    except logs.exceptions.ResourceNotFoundException:
        return ""
    return "\n".join(e["message"] for e in events)


def _failure(text):
    """A failed run's Python traceback, not the tail of Spark's INFO log."""
    start = text.find("Traceback")
    return text[start : start + 6000] if start >= 0 else text[-4000:]


def _helper(stack, catalog, *, sql=(), records=(), topic=None, dump=()):
    """Run HELPER as a Glue 5 job; the dumped tables' rows, by table."""
    glue = stack.client("glue")
    name = f"{stack.prefix}-helper"
    if ("glue-job", name) not in stack.made:
        script = f"s3://{stack.bucket}/test/helper.py"
        _put(stack, script, HELPER)
        _glue_job(stack, name, script, f"arn:aws:iam::{ACCOUNT}:role/{stack.prefix}")
    payload = f"s3://{stack.bucket}/test/payload-{uuid.uuid4().hex[:8]}.json"
    body = {
        "catalog": catalog,
        "sql": list(sql),
        "records": [base64.b64encode(r).decode() for r in records],
        "bootstrap": stack.brokers,
        "topic": topic,
        "dump": list(dump),
    }
    _put(stack, payload, json.dumps(body))
    run_id = glue.start_job_run(JobName=name, Arguments={"--payload": payload})["JobRunId"]
    state = _wait(lambda: glue.get_job_run(JobName=name, RunId=run_id)["JobRun"])
    text = _glue_logs(stack, run_id)
    if state["JobRunState"] != "SUCCEEDED":
        pytest.fail(
            f"helper run {state['JobRunState']}: {state.get('ErrorMessage')}\n{_failure(text)}"
        )
    found = {}
    for line in text.splitlines():
        if line.startswith("FLUID_ROWS "):
            item = json.loads(line.removeprefix("FLUID_ROWS "))
            found[item["table"]] = sorted(
                item["rows"], key=lambda r: (r["order_id"], r.get("sku") or "")
            )
    return found


def _wait(read, done=("SUCCEEDED", "FAILED", "ERROR", "STOPPED", "TIMEOUT")):
    deadline = time.monotonic() + RUN_TIMEOUT_S
    while time.monotonic() < deadline:
        state = read()
        if state["JobRunState"] in done:
            return state
        time.sleep(5)
    raise TimeoutError(f"the Glue run did not end in {RUN_TIMEOUT_S} s")


def _catalog(config):
    """The catalog settings the runner derives, for the test's own Glue jobs."""
    sink, prefix = config["sink"], f"spark.sql.catalog.{config['sink']['catalog']}"
    return {
        prefix: "org.apache.iceberg.spark.SparkCatalog",
        f"{prefix}.catalog-impl": "org.apache.iceberg.aws.glue.GlueCatalog",
        f"{prefix}.io-impl": "org.apache.iceberg.aws.s3.S3FileIO",
        f"{prefix}.warehouse": sink["warehouse"],
        f"{prefix}.client.region": REGION,
    }


def _contract(stack, build_id, **spark):
    contract = json.loads(json.dumps(CONTRACT))
    contract["id"] = f"iactest.spark-{stack.run}"
    build = contract["builds"][0]
    build["id"] = build_id
    build["properties"]["parameters"]["spark"] = {
        "executionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/{stack.prefix}",
        **spark,
    }
    location = contract["exposes"][0]["binding"]["location"]
    location.update(
        database=stack.database, bucket=stack.bucket, region=REGION, path="iceberg/order_lines/"
    )
    return contract


def _batch(contract, source_table, target_table):
    """The build as a batch: the contract's SELECT over ``source_table``, into ``target_table``."""
    props = contract["builds"][0]["properties"]
    props.pop("stream")
    props.pop("mapping")
    database = contract["exposes"][0]["binding"]["location"]["database"]
    props["sql"] = f"SELECT * FROM {database}.{source_table} WHERE qty > 1"
    props["parameters"]["spark"]["mode"] = "batch"
    contract["exposes"][0]["binding"]["location"]["table"] = target_table
    return contract


def _provision(stack, contract, tmp_path, *, fill_brokers=True):
    """What the spark-infra lane will do: upload the job config and the runner."""
    (run,) = desired_runs(contract, account=ACCOUNT)
    text = run.job_text
    if fill_brokers:
        text = text.replace(MSK_BROKERS_PLACEHOLDER, stack.brokers)
        # LocalStack's "Tls" listener is plaintext (a TLS handshake meets EOF), where MSK's
        # is TLS: the runner would speak SSL to it.
        text = text.replace('"tls": true', '"tls": false')
    _put(stack, run.job_uri, text)
    _put(stack, run.runner_uri, runner_source())
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    return run, json.loads(run.job_text), str(path)


def _fluid(capsys, *argv):
    from fluid_build.cli import main

    capsys.readouterr()
    code = main(list(argv))
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith(("{", "["))]
    return code, json.loads(lines[-1]) if lines else out


@pytest.fixture
def lane(ls, monkeypatch, tmp_path):
    """One test's policy and cwd: the operator lists nothing it does not name. forge's EMR
    Serverless clients fill the ``id`` LocalStack leaves out of list summaries
    (:mod:`._localstack_emr`): the product reads AWS's shape."""
    from fluid_build.build_runners.spark import _adapters

    from ._localstack_emr import with_summary_ids

    monkeypatch.setattr(_adapters, "default_clients", with_summary_ids(_adapters.default_clients))
    monkeypatch.chdir(tmp_path)  # fluid reads the cwd's .env
    monkeypatch.setenv(TARGET_ALLOWLIST_ENV, f"msk:{ls.msk_arn}")  # the run's own cluster
    for name in ("AWS_ENDPOINT_URL", "AWS_ACCESS_KEY_ID", "AWS_REGION", "AWS_ACCOUNT_ID"):
        assert os.environ.get(name), name
    return ls


def test_the_runner_reads_secrets_manager_by_arn(lane, runner):
    """``aws-sm://`` references (``#field``, ``#field?version=``) and the mask key, by ARN."""
    sm = lane.client("secretsmanager")
    created = sm.create_secret(
        Name=f"{lane.prefix}/registry",
        SecretString=json.dumps({"username": "u1", "password": "p1"}),  # pragma: allowlist secret
    )
    lane.made.append(("secret", created["ARN"]))
    first = created["VersionId"]
    sm.put_secret_value(
        SecretId=created["ARN"],
        SecretString=json.dumps({"username": "u2", "password": "p2"}),  # pragma: allowlist secret
    )
    arn = created["ARN"]
    assert runner.resolve_secret(f"aws-sm://{arn}#username") == "u2"
    # forge's grammar: the field before the version (secrets/ref.py).
    assert runner.resolve_secret(f"aws-sm://{arn}#password?version={first}") == "p1"
    assert json.loads(runner.resolve_secret(f"aws-sm://{arn}"))["username"] == "u2"
    assert runner.load_mask_key(lane.secret_arn) == MASK_KEY.encode()


def test_glue_streams_a_topic_into_an_iceberg_table(lane, capsys, tmp_path, monkeypatch):
    """Glue 5: MSK by ARN, the Glue catalog, the default S3 checkpoint and the keyed mask."""
    topic = f"{lane.prefix}.orders"
    contract = _contract(
        lane,
        "ingest-orders",
        runtime="glue",
        mode="streaming",
        source={"mskClusterArn": lane.msk_arn, "auth": "none"},
        maskKeySecretArn=lane.secret_arn,
    )
    contract["builds"][0]["properties"]["stream"].update(
        topic=topic, trigger={"availableNow": True}
    )
    run, config, path = _provision(lane, contract, tmp_path)
    assert config["source"]["bootstrapServers"] == MSK_BROKERS_PLACEHOLDER
    assert config["checkpoint"] == (
        f"s3://{lane.bucket}/_checkpoints/{contract['id']}/ingest-orders/"
    )
    _glue_job(lane, run.target["glueJobName"], run.runner_uri, run.role_arn)
    table = f"fluid.{lane.database}.order_lines"
    _helper(
        lane,
        _catalog(config),
        sql=[
            f"CREATE NAMESPACE IF NOT EXISTS fluid.{lane.database}",
            f"CREATE TABLE {table} ({LINES_DDL}) USING iceberg",
        ],
        records=[json.dumps(o).encode() for o in ORDERS],
        topic=topic,
    )

    code, outcome = _fluid(capsys, "spark", "run", path, "--build-id", "ingest-orders", "--json")
    assert code == 0, outcome
    assert outcome["state"] == "SUCCEEDED", outcome

    rows = _helper(lane, _catalog(config), dump=[table])[table]
    assert rows == LINES
    glue_table = lane.client("glue").get_table(DatabaseName=lane.database, Name="order_lines")
    assert glue_table["Table"]["Parameters"]["table_type"].upper() == "ICEBERG"
    checkpoint = lane.client("s3").list_objects_v2(
        Bucket=lane.bucket, Prefix=f"_checkpoints/{contract['id']}/ingest-orders/"
    )
    assert checkpoint.get("KeyCount", 0) > 0

    code, status = _fluid(capsys, "spark", "status", path, "--build-id", "ingest-orders", "--json")
    assert code == 0, status
    # LocalStack's GetJobRun(s) return no Arguments, so the run's --fluid-config-hash (and
    # with it "current") cannot be read back here; AWS returns them.
    assert status[0]["runs"][0]["state"] == "SUCCEEDED", status


def test_a_config_left_with_the_broker_placeholder_fails_its_run(lane, capsys, tmp_path):
    """The runner refuses a job config whose ${...} the uploader did not fill."""
    contract = _contract(
        lane,
        "unfilled",
        runtime="glue",
        mode="streaming",
        source={"mskClusterArn": lane.msk_arn, "auth": "none"},
    )
    contract["builds"][0]["properties"]["mapping"]["columns"][1].pop("maskAtIngest")
    contract["builds"][0]["properties"]["stream"]["trigger"] = {"availableNow": True}
    run, _, path = _provision(lane, contract, tmp_path, fill_brokers=False)
    _glue_job(lane, run.target["glueJobName"], run.runner_uri, run.role_arn)
    code, outcome = _fluid(capsys, "spark", "run", path, "--build-id", "unfilled", "--json")
    assert code == 1 and outcome["state"] == "FAILED", outcome


def _seed(stack, config, table):
    rows = ", ".join(
        "('{order_id}', {customer_id}, {email_hash}, '{sku}', {qty}, {event_time})".format(
            order_id=r["order_id"],
            customer_id=f"'{r['customer_id']}'" if r["customer_id"] else "NULL",
            email_hash=f"'{r['email_hash']}'" if r["email_hash"] else "NULL",
            sku=r["sku"],
            qty=r["qty"],
            event_time=f"TIMESTAMP '{r['event_time']}'" if r["event_time"] else "NULL",
        )
        for r in LINES
    )
    _helper(
        stack,
        _catalog(config),
        sql=[
            f"CREATE NAMESPACE IF NOT EXISTS fluid.{stack.database}",
            f"CREATE TABLE IF NOT EXISTS fluid.{stack.database}.{table} ({LINES_DDL}) USING iceberg",
            f"INSERT INTO fluid.{stack.database}.{table} VALUES {rows}",
        ],
    )


def _run_batch(stack, capsys, tmp_path, monkeypatch, runtime, **spark):
    build = f"copy-{runtime}"
    source, target = f"seed_{runtime.replace('-', '_')}", f"big_{runtime.replace('-', '_')}"
    contract = _batch(_contract(stack, build, runtime=runtime, **spark), source, target)
    monkeypatch.setenv(CONTRACT_SQL_ALLOWLIST_ENV, f"{contract['id']}/{build}")
    run, config, path = _provision(stack, contract, tmp_path)
    if runtime == "glue":
        _glue_job(stack, run.target["glueJobName"], run.runner_uri, run.role_arn)
    _seed(stack, config, source)
    _helper(
        stack,
        _catalog(config),
        sql=[f"CREATE TABLE fluid.{stack.database}.{target} ({LINES_DDL}) USING iceberg"],
    )
    code, outcome = _fluid(capsys, "spark", "run", path, "--build-id", build, "--json")
    if code != 0 and not outcome.get("runId"):
        pytest.fail(json.dumps(outcome, indent=1))
    if code != 0 and runtime == "glue":
        pytest.fail(f"{outcome}\n{_failure(_glue_logs(stack, outcome['runId']))}")
    assert code == 0, outcome
    assert outcome["state"] == "SUCCEEDED", outcome
    table = f"fluid.{stack.database}.{target}"
    rows = _helper(stack, _catalog(config), dump=[table])[table]
    assert rows == [r for r in LINES if r["qty"] > 1]
    return run


def test_glue_runs_a_batch_build_over_catalog_tables(lane, capsys, tmp_path, monkeypatch):
    run = _run_batch(lane, capsys, tmp_path, monkeypatch, "glue")
    assert run.scheduled


def _emr_conf(monkeypatch):
    """LocalStack's Spark 3.3 needs its own Iceberg and the catalog's LocalStack endpoints.
    The test adds them to what forge submits on an EMR runtime; the contract sets none."""
    from fluid_build.build_runners.spark import _desired

    from ._localstack_emr import with_spark_conf

    monkeypatch.setattr(
        _desired,
        "_submit_conf",
        with_spark_conf(
            _desired._submit_conf,
            {
                "spark.jars.packages": SPARK33_PACKAGES,
                "spark.sql.catalog.fluid.glue.endpoint": "http://localhost:4566",
                "spark.sql.catalog.fluid.s3.endpoint": "http://localhost:4566",
                "spark.sql.catalog.fluid.s3.path-style-access": "true",
            },
        ),
    )
    return {}


#: EMR's batch runs start in LocalStack but end FAILED in CI; strict, so CI goes red the day
#: they pass and the marker comes off.
EMR_FAILS_IN_CI = pytest.mark.xfail(
    strict=True,
    reason=(
        "EMR Serverless/EC2 batch runs end FAILED inside LocalStack in CI (run 44132106546/job "
        "134211874996); cause not captured yet; tracked on Trello xZtxKx2N"
    ),
)


@EMR_FAILS_IN_CI
def test_emr_serverless_runs_a_batch_build(lane, capsys, tmp_path, monkeypatch):
    emr = lane.client("emr-serverless")
    product = f"iactest.spark-{lane.run}"
    from fluid_build.spark import names

    app = emr.create_application(
        name=names.application_name(product), type="SPARK", releaseLabel="emr-7.12.0"
    )["applicationId"]
    lane.made.append(("emr-app", app))
    _run_batch(
        lane,
        capsys,
        tmp_path,
        monkeypatch,
        "emr-serverless",
        sparkConf=_emr_conf(monkeypatch),
    )


@EMR_FAILS_IN_CI
def test_emr_on_ec2_runs_a_batch_build(lane, capsys, tmp_path, monkeypatch):
    cluster = lane.client("emr").run_job_flow(
        Name=f"{lane.prefix}-emr",
        ReleaseLabel="emr-7.12.0",
        Applications=[{"Name": "Spark"}],
        Instances={
            "InstanceCount": 1,
            "MasterInstanceType": "m5.xlarge",
            "KeepJobFlowAliveWhenNoSteps": True,
        },
        JobFlowRole="EMR_EC2_DefaultRole",
        ServiceRole="EMR_DefaultRole",
    )["JobFlowId"]
    lane.made.append(("emr-cluster", cluster))
    _run_batch(
        lane,
        capsys,
        tmp_path,
        monkeypatch,
        "emr-ec2",
        clusterId=cluster,
        sparkConf=_emr_conf(monkeypatch),
    )
