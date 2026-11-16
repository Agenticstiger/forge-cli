# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The runner, unchanged, on Spark 3.5 against Kafka (Redpanda) and an Iceberg table.

Opt-in: ``FLUID_SPARK_LIVE=1`` and a Docker daemon. It pulls the Spark and Redpanda
images and the Iceberg and Kafka connector jars, so it never runs by default.

The submitter declares a Hadoop Iceberg catalog in the container (the runner then
leaves the catalog as declared), the checkpoint sits beside it, and Secrets Manager is
a stand-in that holds the test's masking key: no AWS is reached.

Every container and network it starts is named ``fluid-iactest-spark-*-<run>`` and
removed by name at the end.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import time
import uuid

import pytest

from fluid_build.spark.compiler import compile_job_config
from fluid_build.streaming.policy import HOST_ALLOWLIST_ENV

from .conftest import CONTRACT, RUNNER_PATH

pytestmark = [pytest.mark.integration, pytest.mark.slow]

SPARK_IMAGE = "apache/spark:3.5.7-scala2.12-java17-python3-ubuntu"
REDPANDA_IMAGE = "docker.redpanda.com/redpandadata/redpanda:v24.2.7"
ICEBERG = "1.6.1"
PACKAGES = [
    f"org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:{ICEBERG}",
    "org.apache.spark:spark-sql-kafka-0-10_2.12:3.5.7",
]
MASK_KEY = "fluid-iactest-spark-test-key-not-a-secret"  # pragma: allowlist secret

ORDERS = [
    {
        "order_id": "o1",
        "customer": {"id": "c1", "email": "a@example.com"},
        "items": [{"sku": "s1", "qty": 2}, {"sku": "s2", "qty": 1}],
        "event_time": "2026-11-16T10:00:00.123Z",
    },
    # No items: no rows, as a CROSS JOIN UNNEST gives none.
    {"order_id": "o2", "customer": {"id": "c2"}, "items": [], "event_time": None},
    # No customer: its fields are null, the row stays.
    {"order_id": "o3", "customer": None, "items": [{"sku": "s3", "qty": 5}]},
]

DRIVER = r"""
import base64, json, os, runpy, struct, sys, urllib.request
from pyspark.sql import SparkSession

config_path = sys.argv[1]
config = json.load(open(config_path))
spark = SparkSession.builder.getOrCreate()
source = config["source"]
values = [base64.b64decode(v) for v in json.load(open("/w/records.json"))]
if os.path.exists("/w/writer_schema.json"):
    # Register the producer's schema, then frame each Avro body as Confluent does:
    # magic byte 0, the schema id as 4 bytes big-endian, the body.
    registry = source["registry"]["url"]
    request = urllib.request.Request(
        registry + "/subjects/" + source["value"]["subject"] + "/versions",
        data=json.dumps({"schema": open("/w/writer_schema.json").read()}).encode(),
        headers={"Content-Type": "application/vnd.schemaregistry.v1+json"},
    )
    schema_id = json.loads(urllib.request.urlopen(request).read())["id"]
    values = [b"\x00" + struct.pack(">I", schema_id) + v for v in values]
records = [(bytearray(v),) for v in values]
(spark.createDataFrame(records, "value BINARY").write.format("kafka")
    .option("kafka.bootstrap.servers", source["bootstrapServers"])
    .option("topic", source["topic"]).save())

# No AWS here: Secrets Manager is a stand-in that holds the test's masking key.
import types
secret = {"SecretString": open("/w/mask_key").read()}
client = types.SimpleNamespace(get_secret_value=lambda SecretId: secret)
sys.modules["boto3"] = types.SimpleNamespace(client=lambda service: client)
runner = runpy.run_path("/w/forge_spark_runner.py")
runner["configure_session"](spark, config)
table = config["sink"]["catalog"] + "." + config["sink"]["table"]
database = table.rsplit(".", 1)[0]
spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {database}")
spark.sql(
    f"CREATE TABLE {table} (order_id STRING NOT NULL, customer_id STRING, email_hash STRING, "
    f"sku STRING, qty INT, event_time TIMESTAMP) USING iceberg"
)
try:
    sys.argv = ["forge_spark_runner.py", "--config", config_path, "--JOB_NAME", "ignored"]
    runpy.run_path("/w/forge_spark_runner.py", run_name="__main__")
except SystemExit as exit_:
    assert exit_.code in (0, None), exit_.code
rows = [r.asDict() for r in spark.table(table).orderBy("order_id", "sku").collect()]
for r in rows:
    r["event_time"] = None if r["event_time"] is None else r["event_time"].isoformat()
json.dump(rows, open("/w/rows.json", "w"))
"""


def _docker(*args, check=True, **kw):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, **kw)


def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    return _docker("info", check=False, timeout=30).returncode == 0


@pytest.fixture
def stack(tmp_path):
    if os.environ.get("FLUID_SPARK_LIVE") != "1":
        pytest.skip("set FLUID_SPARK_LIVE=1 to run the Spark runner on Docker")
    if not _docker_ready():
        pytest.skip("no Docker daemon")
    run = uuid.uuid4().hex[:8]
    names = {
        "network": f"fluid-iactest-spark-net-{run}",
        "kafka": f"fluid-iactest-spark-redpanda-{run}",
        "spark": f"fluid-iactest-spark-run-{run}",
    }
    try:
        _docker("network", "create", names["network"])
        _docker(
            "run", "-d", "--name", names["kafka"], "--network", names["network"],
            REDPANDA_IMAGE, "redpanda", "start", "--mode", "dev-container", "--smp", "1",
            "--kafka-addr", "internal://0.0.0.0:9092",
            "--advertise-kafka-addr", f"internal://{names['kafka']}:9092",
        )  # fmt: skip
        for _ in range(60):
            health = _docker("exec", names["kafka"], "rpk", "cluster", "health", check=False)
            if re.search(r"Healthy:\s+true", health.stdout):
                break
            time.sleep(1)
        else:
            pytest.fail("Redpanda did not become healthy")
        yield tmp_path, names
    finally:
        for container in (names["spark"], names["kafka"]):
            _docker("rm", "-f", container, check=False)
        _docker("network", "rm", names["network"], check=False)


def _zigzag(n: int) -> bytes:
    n = (n << 1) ^ (n >> 63)
    out = bytearray()
    while n & ~0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def _string(text: str) -> bytes:
    data = text.encode("utf-8")
    return _zigzag(len(data)) + data


def _optional(value, encode) -> bytes:
    return _zigzag(0) if value is None else _zigzag(1) + encode(value)


#: The producer's schema: customer's fields and each item's in another order than the
#: contract declares, so a positional read would swap them.
WRITER_SCHEMA = {
    "type": "record",
    "name": "Order",
    "fields": [
        {
            "name": "customer",
            "type": [
                "null",
                {
                    "type": "record",
                    "name": "Customer",
                    "fields": [
                        {"name": "email", "type": ["null", "string"]},
                        {"name": "id", "type": ["null", "string"]},
                    ],
                },
            ],
        },
        {"name": "order_id", "type": "string"},
        {
            "name": "items",
            "type": {
                "type": "array",
                "items": {
                    "type": "record",
                    "name": "Item",
                    "fields": [{"name": "qty", "type": "int"}, {"name": "sku", "type": "string"}],
                },
            },
        },
        {
            "name": "event_time",
            "type": ["null", {"type": "long", "logicalType": "timestamp-millis"}],
        },
    ],
}
EVENT_MILLIS = 1_794_823_200_123  # 2026-11-16T10:00:00.123Z


def _avro(order) -> bytes:
    """One order as an Avro body under WRITER_SCHEMA."""

    def customer(c):
        return _optional(c.get("email"), _string) + _optional(c.get("id"), _string)

    items = order.get("items") or []
    body = _optional(order.get("customer"), customer) + _string(order["order_id"])
    if items:
        body += _zigzag(len(items))
        body += b"".join(_zigzag(i["qty"]) + _string(i["sku"]) for i in items)
    body += _zigzag(0)
    millis = EVENT_MILLIS if order.get("event_time") else None
    return body + _optional(millis, _zigzag)


def _run(stack, contract, values, *, writer_schema=None, packages=(), repositories=()):
    """Compile, produce, run the runner in Spark; the runner's events and the table's rows."""
    work, names = stack
    spark = contract["builds"][0]["properties"]["parameters"]["spark"]
    spark["source"] = {"bootstrapServers": f"{names['kafka']}:9092", "auth": "none"}
    environ = {HOST_ALLOWLIST_ENV: names["kafka"]}
    config = compile_job_config(contract, contract["builds"][0], environ=environ).config
    conf = [f"spark.jars.packages={','.join([*PACKAGES, *packages])}", "spark.jars.ivy=/tmp/ivy"]
    if repositories:
        conf.append(f"spark.jars.repositories={','.join(repositories)}")
    # The submitter declares the catalog, so the runner leaves it as declared; the
    # checkpoint moves next to it, as there is no S3 here.
    config["checkpoint"] = "/w/checkpoint/"
    conf += [
        "spark.sql.catalog.fluid=org.apache.iceberg.spark.SparkCatalog",
        "spark.sql.catalog.fluid.type=hadoop",
        "spark.sql.catalog.fluid.warehouse=/w/warehouse",
    ]
    (work / "job.json").write_text(json.dumps(config), encoding="utf-8")
    encoded = [base64.b64encode(v).decode() for v in values]
    (work / "records.json").write_text(json.dumps(encoded), encoding="utf-8")
    if writer_schema is not None:
        (work / "writer_schema.json").write_text(json.dumps(writer_schema), encoding="utf-8")
    (work / "driver.py").write_text(DRIVER, encoding="utf-8")
    (work / "mask_key").write_text(MASK_KEY, encoding="utf-8")
    shutil.copy(RUNNER_PATH, work / "forge_spark_runner.py")
    os.chmod(work, 0o777)
    result = _docker(
        "run", "--name", names["spark"], "--network", names["network"], "-v", f"{work}:/w",
        SPARK_IMAGE, "/opt/spark/bin/spark-submit", "--master", "local[2]",
        *[arg for c in conf for arg in ("--conf", c)],
        "/w/driver.py", "/w/job.json",
        check=False, timeout=1200,
    )  # fmt: skip
    if result.returncode != 0:
        # The Python traceback, not the tail of Ivy's download log.
        start = result.stderr.find("Traceback")
        pytest.fail(
            result.stdout[-3000:]
            + (result.stderr[start : start + 6000] if start >= 0 else result.stderr[-1500:])
        )
    events = [
        json.loads(line)
        for line in result.stdout.splitlines()
        if line.startswith("{") and '"forge_spark_runner": ' in line
    ]
    rows = json.loads((work / "rows.json").read_text(encoding="utf-8"))
    return events, rows


def _contract(**stream):
    contract = json.loads(json.dumps(CONTRACT))
    contract["builds"][0]["properties"]["stream"].update(trigger={"availableNow": True}, **stream)
    return contract


DIGEST = hmac.new(MASK_KEY.encode(), b"a@example.com", hashlib.sha256).hexdigest()
EXPECTED = [
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


def test_the_runner_lands_nested_json_records_in_iceberg(stack):
    values = [json.dumps(order).encode() for order in ORDERS]
    events, rows = _run(stack, _contract(), values)
    assert [e["forge_spark_runner"] for e in events] == ["start", "streaming", "done"]
    assert events[0]["masked_columns"] == ["email_hash"]
    assert rows == EXPECTED


def test_the_runner_decodes_confluent_avro_by_field_name(stack):
    """ABRiS reads with the registry's schema, whose structs order their fields otherwise."""
    work, names = stack
    registry = {"kind": "confluent", "url": f"http://{names['kafka']}:8081"}
    events, rows = _run(
        stack,
        _contract(format="avro-confluent", registry=registry),
        [_avro(order) for order in ORDERS],
        writer_schema=WRITER_SCHEMA,
        packages=("org.apache.spark:spark-avro_2.12:3.5.7", "za.co.absa:abris_2.12:6.4.0"),
        repositories=("https://packages.confluent.io/maven/",),
    )
    assert [e["forge_spark_runner"] for e in events] == ["start", "streaming", "done"]
    assert rows == EXPECTED
