# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The runner in a local Spark session: every Iceberg commit it makes carries the run's stamps.

No Docker and no network. Spark 3.5 runs in this process (``pyspark``), Iceberg's Spark runtime
is a local jar (``FLUID_SPARK_ICEBERG_JAR``) on a Hadoop catalog in a temporary directory, and
Java is ``JAVA_HOME`` or the ``jdk4py`` wheel. Kafka is the one part stood in for: the session's
``format("kafka")`` reads a directory of rows in the Kafka source's own schema, so the runner's
decode, dead letters, queries and writers run as they do on a cluster. Forge writes the job
config and the runner's stamp flags, as ``fluid apply`` does. Each commit's snapshot summary is
read back from ``<table>.snapshots``.

It skips unless all three are there, so CI (which installs no pyspark) never runs it. To run::

    FLUID_SPARK_ICEBERG_JAR=<iceberg-spark-runtime-3.5_2.12-1.6.1.jar> \\
    PYTHONPATH=<this checkout>:<site-packages of a pyspark 3.5 install> \\
    pytest tests/spark/test_runner_local_iceberg.py
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.build_runners.spark import _desired
from fluid_build.spark import dead_letter
from fluid_build.spark.compiler import CONTRACT_SQL_ALLOWLIST_ENV, compile_job_config

from .conftest import CONTRACT

pytestmark = [pytest.mark.integration, pytest.mark.slow]

ICEBERG_JAR_ENV = "FLUID_SPARK_ICEBERG_JAR"
ACCOUNT = "000000000005"
MASK_KEY = b"fluid-local-session-test-key-not-a-secret"  # pragma: allowlist secret
BUNDLE = "sha256:" + "cd" * 32
SCHEDULED = {
    "FLUID_SCHEDULER_JOB": "commerce.orders-stream__ingest-orders",
    "FLUID_SCHEDULER_RUN_ID": "scheduled__2026-11-21T00:00:00+00:00",
}
#: What Kafka's source hands a stream: the runner's decode reads ``value`` and ``key``.
KAFKA_SCHEMA = (
    "key BINARY, value BINARY, topic STRING, partition INT, offset BIGINT, "
    "timestamp TIMESTAMP, timestampType INT"
)
ORDERS = [
    {
        "order_id": "o1",
        "customer": {"id": "c1", "email": "a@example.com"},
        "items": [{"sku": "s1", "qty": 2}, {"sku": "s2", "qty": 1}],
        "event_time": "2026-11-16T10:00:00.123Z",
    },
    {"order_id": "o3", "customer": None, "items": [{"sku": "s3", "qty": 5}]},
]
#: A record that is not JSON: a dead letter.
NOT_JSON = b"{not json"


def _java_home() -> Any:
    home = os.environ.get("JAVA_HOME")
    if home and (Path(home) / "bin" / "java").exists():
        return home
    try:
        import jdk4py
    except ImportError:
        return None
    return str(jdk4py.JAVA_HOME)


@pytest.fixture(scope="module")
def spark(tmp_path_factory: pytest.TempPathFactory) -> Any:
    pytest.importorskip("pyspark", reason="a local Spark session needs pyspark 3.5")
    jar = os.environ.get(ICEBERG_JAR_ENV, "")
    if not jar or not Path(jar).is_file():
        pytest.skip(f"set {ICEBERG_JAR_ENV} to a local iceberg-spark-runtime-3.5 jar")
    java_home = _java_home()
    if java_home is None:
        pytest.skip("no Java: set JAVA_HOME or install jdk4py")
    from pyspark.sql import SparkSession

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("JAVA_HOME", java_home)
        mp.setenv("SPARK_LOCAL_IP", "127.0.0.1")
        # The masking UDF runs in Python workers: this interpreter, with this PYTHONPATH.
        mp.setenv("PYSPARK_PYTHON", sys.executable)
        mp.setenv("PYSPARK_DRIVER_PYTHON", sys.executable)
        session = (
            SparkSession.builder.master("local[2]")
            .appName("fluid-runner-local-iceberg")
            .config("spark.jars", jar)
            .config(
                "spark.sql.extensions",
                "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
            )
            # The submitter's catalog: the runner leaves a declared catalog as declared.
            .config("spark.sql.catalog.fluid", "org.apache.iceberg.spark.SparkCatalog")
            .config("spark.sql.catalog.fluid.type", "hadoop")
            .config("spark.sql.catalog.fluid.warehouse", str(tmp_path_factory.mktemp("lake")))
            # A stream commits through its query's own session: this one must not read a table
            # it cached before that commit.
            .config("spark.sql.catalog.fluid.cache-enabled", "false")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.shuffle.partitions", "2")
            .getOrCreate()
        )
    yield session
    session.stop()


@pytest.fixture
def kafka(spark: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``spark.readStream.format("kafka")`` reads the parquet rows under the returned directory
    (in the Kafka source's schema); every other format is Spark's own."""
    from pyspark.sql import SparkSession

    rows = tmp_path / "kafka"
    real = SparkSession.readStream

    class KafkaFromFiles:
        def __init__(self, session: Any) -> None:
            self.reader = real.fget(session)

        def format(self, name: str) -> Any:
            return self if name == "kafka" else self.reader.format(name)

        def options(self, **_options: Any) -> "KafkaFromFiles":
            return self  # brokers, topic and offsets are Kafka's, not the files'

        def load(self) -> Any:
            return self.reader.schema(KAFKA_SCHEMA).parquet(str(rows))

    monkeypatch.setattr(SparkSession, "readStream", property(KafkaFromFiles))
    return rows


def _forge_args(contract: Dict[str, Any], run_id: str, scheduler_run_id: str) -> List[str]:
    """The stamp flags ``fluid apply`` hands the runner for ``contract``'s first build, under
    a scheduler's run, from a plan that binds :data:`BUNDLE`."""
    environ = {
        **SCHEDULED,
        "FLUID_SCHEDULER_RUN_ID": scheduler_run_id,
        "FLUID_RELEASE_ID": "v1.4.0",
    }
    stamps = _desired.run_stamps(contract, run_id=run_id, environ=environ, bundle_digest=BUNDLE)
    (run, *_rest) = _desired.desired_runs(contract, account=ACCOUNT, stamps=stamps)
    return run.stamp_args()


def _job(contract: Dict[str, Any], where: Path) -> Dict[str, Any]:
    """The build's job config as forge compiles it, its checkpoints moved to ``where``."""
    config: Dict[str, Any] = json.loads(compile_job_config(contract, contract["builds"][0]).text)
    if config.get("checkpoint"):
        config["checkpoint"] = (where / "checkpoint").as_uri() + "/"
    if config.get("deadLetter"):
        config["deadLetter"]["checkpoint"] = (where / "dead-letters").as_uri() + "/"
    return config


def _run(runner: Any, config: Dict[str, Any], where: Path, args: List[str]) -> str:
    text = json.dumps(config)
    path = where / "job.json"
    path.write_text(text, encoding="utf-8")
    assert runner.main(["--config", str(path), *args]) == 0
    return text


def _summaries(spark: Any, table: str) -> List[Dict[str, str]]:
    """The snapshot summary of each commit to ``table``, oldest first."""
    rows = spark.sql(f"SELECT summary FROM {table}.snapshots ORDER BY committed_at").collect()
    return [dict(row["summary"]) for row in rows]


def _operations(spark: Any, table: str) -> List[str]:
    rows = spark.sql(f"SELECT operation FROM {table}.snapshots ORDER BY committed_at").collect()
    return [row["operation"] for row in rows]


def _flag_stamps(args: List[str]) -> Dict[str, str]:
    """``args``, the runner's stamp flags, as the summary keys they become."""
    keys = dict(_runner_flag_keys())
    return {keys[flag]: value for flag, value in zip(args[::2], args[1::2], strict=True)}


def _runner_flag_keys() -> List[Any]:
    import importlib.util

    from .conftest import RUNNER_PATH

    spec = importlib.util.spec_from_file_location("forge_spark_runner_keys", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.STAMP_FLAGS)


def _stream_contract() -> Dict[str, Any]:
    contract = copy.deepcopy(CONTRACT)
    contract["exposes"][0]["version"] = "1.4.0"
    stream = contract["builds"][0]["properties"]["stream"]
    stream.update(trigger={"availableNow": True}, deadLetter={"sink": "table"})
    return contract


def test_a_streams_commits_and_its_dead_letters_carry_the_runs_stamps(
    spark: Any, kafka: Path, runner: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _stream_contract()
    config = _job(contract, tmp_path)
    table = f"{config['sink']['catalog']}.{config['sink']['table']}"
    dead_table = f"{config['sink']['catalog']}.{config['deadLetter']['table']}"
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {table.rsplit('.', 1)[0]}")
    spark.sql(
        f"CREATE TABLE {table} (order_id STRING NOT NULL, customer_id STRING, email_hash STRING, "
        f"sku STRING, qty INT, event_time TIMESTAMP) USING iceberg"
    )
    columns = ", ".join(f"{name} {kind}" for name, kind, _required in dead_letter.COLUMNS)
    spark.sql(f"CREATE TABLE {dead_table} ({columns}) USING iceberg")
    values = [json.dumps(order).encode() for order in ORDERS] + [NOT_JSON]
    records = [
        (None, value, "commerce.orders", 0, offset, None, 0) for offset, value in enumerate(values)
    ]
    spark.createDataFrame(records, KAFKA_SCHEMA).write.parquet(str(kafka))
    monkeypatch.setattr(runner, "load_mask_key", lambda _arn: MASK_KEY)

    args = _forge_args(contract, "3f9a3f9a3f9a", SCHEDULED["FLUID_SCHEDULER_RUN_ID"])
    text = _run(runner, config, tmp_path, args)

    expected = {
        "fluid.product": "commerce.orders-stream",
        "fluid.build": "ingest-orders",
        "fluid.job-sha256": runner.config_digest(text),
        **_flag_stamps(args),
    }
    assert {
        "fluid.product-version": "1.4.0",
        "fluid.bundle-digest": BUNDLE,
        "fluid.release-id": "v1.4.0",
        "fluid.run-id": "3f9a3f9a3f9a",
        "fluid.scheduler-job": SCHEDULED["FLUID_SCHEDULER_JOB"],
        "fluid.scheduler-run-id": SCHEDULED["FLUID_SCHEDULER_RUN_ID"],
    }.items() <= expected.items()
    assert expected["fluid.contract-digest"].startswith("sha256:")
    output, dead = _summaries(spark, table), _summaries(spark, dead_table)
    assert output and dead, "the stream committed nothing"
    for summary in output + dead:
        assert expected.items() <= summary.items(), summary
    # The stream's own sink: Iceberg's query and epoch ids sit beside the stamps.
    assert all("spark.sql.streaming.queryId" in s for s in output)
    # The dead letters' foreachBatch commit: once per batch, by its own query and batch ids.
    assert all("fluid.dead-letter.query" in s and "fluid.dead-letter.batch" in s for s in dead)

    digest = hmac.new(MASK_KEY, b"a@example.com", hashlib.sha256).hexdigest()
    rows = [r.asDict() for r in spark.table(table).orderBy("order_id", "sku").collect()]
    assert [(r["order_id"], r["sku"], r["qty"]) for r in rows] == [
        ("o1", "s1", 2),
        ("o1", "s2", 1),
        ("o3", "s3", 5),
    ]
    assert rows[0]["email_hash"] == digest and rows[2]["email_hash"] is None
    (letter,) = spark.table(dead_table).collect()
    assert letter["error"] == "the value is not JSON of the declared fields and types"
    assert letter["kafka_offset"] == len(ORDERS)
    # The build masks a field at ingest, so a dead letter keeps no raw bytes to leak it.
    assert letter["raw_value"] is None


def _batch_contract() -> Dict[str, Any]:
    return {
        "fluidVersion": "0.7.7",
        "kind": "DataProduct",
        "id": "platform.events",
        "name": "Events",
        "metadata": {"owner": {"team": "data-platform"}},
        "builds": [
            {
                "id": "daily-curated",
                "engine": "spark",
                "pattern": "embedded-logic",
                "outputs": ["events_daily"],
                "properties": {
                    "sql": "SELECT CAST(event_time AS DATE) AS event_date, COUNT(*) AS events "
                    "FROM raw.events GROUP BY CAST(event_time AS DATE)",
                    "parameters": {"spark": {"runtime": "emr-serverless", "mode": "batch"}},
                },
            }
        ],
        "exposes": [
            {
                "exposeId": "events_daily",
                "kind": "table",
                "version": "2.0.0",
                "binding": {
                    "platform": "aws",
                    "format": "iceberg",
                    "location": {
                        "database": "curated",
                        "table": "events_daily",
                        "bucket": "platform-lake",
                        "region": "eu-west-1",
                    },
                },
                "contract": {
                    "schema": [
                        {"name": "event_date", "type": "date", "required": True},
                        {"name": "events", "type": "bigint"},
                    ]
                },
            }
        ],
    }


def test_each_batch_run_commits_its_own_runs_stamps(
    spark: Any, runner: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _batch_contract()
    monkeypatch.setenv(CONTRACT_SQL_ALLOWLIST_ENV, contract["id"])
    config = _job(contract, tmp_path)
    table = f"{config['sink']['catalog']}.{config['sink']['table']}"
    spark.sql("CREATE NAMESPACE IF NOT EXISTS fluid.raw")
    spark.sql("CREATE TABLE fluid.raw.events (event_time TIMESTAMP) USING iceberg")
    spark.sql("INSERT INTO fluid.raw.events VALUES (TIMESTAMP '2026-11-20 10:00:00')")
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {table.rsplit('.', 1)[0]}")
    spark.sql(
        f"CREATE TABLE {table} (event_date DATE NOT NULL, events BIGINT) USING iceberg "
        "PARTITIONED BY (event_date)"
    )

    first = _forge_args(contract, "aaaa1111aaaa", "scheduled__2026-11-21T02:00:00+00:00")
    second = _forge_args(contract, "bbbb2222bbbb", "scheduled__2026-11-22T02:00:00+00:00")
    _run(runner, config, tmp_path, first)
    text = _run(runner, config, tmp_path, second)

    summaries = _summaries(spark, table)
    assert _operations(spark, table) == ["overwrite", "overwrite"]
    for summary, args in zip(summaries, (first, second), strict=True):
        assert summary["fluid.product"] == "platform.events"
        assert summary["fluid.build"] == "daily-curated"
        assert summary["fluid.job-sha256"] == runner.config_digest(text)
        assert _flag_stamps(args).items() <= summary.items(), summary
    assert summaries[0]["fluid.run-id"] == "aaaa1111aaaa"
    assert summaries[1]["fluid.scheduler-run-id"] == "scheduled__2026-11-22T02:00:00+00:00"
    assert summaries[1]["fluid.product-version"] == "2.0.0"
    assert [tuple(r) for r in spark.table(table).collect()] == [(datetime.date(2026, 11, 20), 1)]
