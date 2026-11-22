# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The runner's stop between batches on a local Spark 3.5 session: a stream asked to stop ends
cleanly after its batch, and the next run resumes its checkpoint; a checkpoint is read through
Spark's own file system for the topic check.

Opt-in: pyspark importable and a JDK. The streams read Spark's ``rate`` source into the ``noop``
sink: no network, no Docker, no cloud.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

pyspark = pytest.importorskip("pyspark")
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("java") is None and not os.environ.get("JAVA_HOME"), reason="no JDK"
    ),
]


@pytest.fixture(scope="module")
def spark():
    from pyspark.sql import SparkSession

    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)
    session = (
        SparkSession.builder.master("local[2]")
        .appName("fluid-stream-release-test")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "2")
        .getOrCreate()
    )
    yield session
    session.stop()


def _start(spark, checkpoint, trigger=None):
    rows = spark.readStream.format("rate").option("rowsPerSecond", 200).load()
    writer = rows.writeStream.format("noop").option("checkpointLocation", str(checkpoint))
    if trigger:
        writer = writer.trigger(processingTime=trigger)
    return writer.start()


def _batches(directory: Path) -> list:
    """The batch ids a checkpoint's offset or commit log holds."""
    if not directory.is_dir():
        return []
    return sorted(int(p.name) for p in directory.iterdir() if p.name.isdigit())


def _until(predicate, timeout=90.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.2)


@pytest.mark.parametrize("trigger", ["1 second", None], ids=["interval", "back-to-back"])
def test_a_stream_asked_to_stop_ends_cleanly_and_the_next_run_resumes_it(
    runner, spark, tmp_path, trigger
):
    checkpoint = tmp_path / "checkpoint"
    request = tmp_path / "stop" / "fluid-key"
    query = _start(spark, checkpoint, trigger)
    _until(lambda: len(_batches(checkpoint / "commits")) >= 2)
    request.parent.mkdir()
    request.write_text("{}", encoding="utf-8")
    asked = time.monotonic()
    runner.await_queries(spark, [query], runner.StopWatch(spark, str(request), every=0.2))
    assert time.monotonic() - asked < 30  # the batch in flight, not the stream, was waited for
    assert not query.isActive and query.exception() is None
    committed = _batches(checkpoint / "commits")
    # At most the batch that started as the last one committed is planned past it, and Spark
    # runs it again from the offset log.
    assert _batches(checkpoint / "offsets")[-1] - committed[-1] in (0, 1)
    again = _start(spark, checkpoint, trigger)
    try:
        _until(lambda: _batches(checkpoint / "commits")[-1] > committed[-1])
    finally:
        again.stop()
    assert _batches(checkpoint / "commits")[0] <= committed[-1]  # the same log, carried on


def test_a_checkpoint_is_read_through_spark_s_file_system_for_the_topic_check(
    runner, spark, tmp_path
):
    other = tmp_path / "other"
    (other / "offsets").mkdir(parents=True)
    (other / "offsets" / "4").write_text(
        'v1\n{"batchWatermarkMs":0,"batchTimestampMs":1795025520028,"conf":{}}\n'
        '{"payments.events":{"0":5}}',
        encoding="utf-8",
    )
    config = {"source": {"topic": "commerce.orders"}, "checkpoint": f"file://{other}/"}
    with pytest.raises(runner.JobConfigError, match="payments.events"):
        runner.check_checkpoint_fits(spark, config)
    # A checkpoint of a source that is not Kafka (the rate source's): left to Spark.
    rate = tmp_path / "rate"
    query = _start(spark, rate, "1 second")
    try:
        _until(lambda: len(_batches(rate / "offsets")) >= 1)
    finally:
        query.stop()
    runner.check_checkpoint_fits(spark, {**config, "checkpoint": f"file://{rate}/"})
