# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A stream's release, on the runner's side: a stop that waits for the batch in flight, and a
checkpoint checked against the job before the stream reads.

No pyspark here: stand-ins for the queries, their manager and the checkpoint's file system.
``tests/spark/test_runner_stream_release_session.py`` runs the stop on Spark."""

from __future__ import annotations

import json
import sys
import types

import pytest

from fluid_build.spark.compiler import compile_job_config

from .test_dead_letter import OFFSETS_FILE, checkpoints, runner_config

CONFIG_URI = "s3://commerce-lake/_fluid/spark/commerce.orders-stream/ingest-orders/job.json"
KEY = "fluid-0123456789abcdef-0123456789abcdef0123"
CHECKPOINT = "s3://commerce-lake/_checkpoints/commerce.orders-stream/ingest-orders/"


def offsets(topic):
    """An offset log entry of a batch that read ``topic`` (Spark 3.5's layout)."""
    return OFFSETS_FILE.replace("source.events", topic)


# --- where a stop request is, and what may name it --------------------------------------


@pytest.mark.parametrize(
    ("config", "where"),
    [
        (CONFIG_URI, CONFIG_URI.replace("job.json", f"stop/{KEY}")),
        ("/srv/fluid/_fluid/spark/p/b/job.json", f"/srv/fluid/_fluid/spark/p/b/stop/{KEY}"),
        ("file:///srv/fluid/_fluid/spark/p/b/job.json", f"file:///srv/fluid/_fluid/spark/p/b/stop/{KEY}"),
    ],
)  # fmt: skip
def test_a_stop_request_sits_beside_the_job_config(runner, config, where):
    assert runner.stop_request_uri(config, KEY) == where


def _no_spark(monkeypatch):
    """A pyspark whose session fails the test if anything starts it."""

    def started():
        raise AssertionError("Spark started")

    session = types.SimpleNamespace(builder=types.SimpleNamespace(getOrCreate=started))
    monkeypatch.setitem(sys.modules, "pyspark", types.ModuleType("pyspark"))
    monkeypatch.setitem(sys.modules, "pyspark.sql", types.SimpleNamespace(SparkSession=session))


@pytest.mark.parametrize("key", ["../stop", "a/b", "-x", "a b", "k" * 65, "fluid.key", "x\n"])
def test_a_stop_key_that_could_leave_the_stop_folder_runs_nothing(runner, monkeypatch, key):
    _no_spark(monkeypatch)
    with pytest.raises(runner.JobConfigError, match="--fluid-stop-key"):
        runner.main(["--config", CONFIG_URI, f"--fluid-stop-key={key}"])


def _main(runner, monkeypatch, argv):
    """``main`` up to ``run``, with what it hands ``run``."""
    handed = {}
    session = types.SimpleNamespace(builder=types.SimpleNamespace(getOrCreate=lambda: "spark"))
    monkeypatch.setitem(sys.modules, "pyspark", types.ModuleType("pyspark"))
    monkeypatch.setitem(sys.modules, "pyspark.sql", types.SimpleNamespace(SparkSession=session))
    monkeypatch.setattr(runner, "read_text", lambda spark, path: "{}")
    monkeypatch.setattr(runner, "parse_config", lambda text: {})
    monkeypatch.setattr(runner, "release_stamps", lambda config, text, args: {})

    def run(spark, config, stamps=None, stop=None):
        handed["stop"] = stop

    monkeypatch.setattr(runner, "run", run)
    assert runner.main(argv) == 0
    return handed["stop"]


def test_main_hands_the_stream_a_watch_on_its_own_stop_request(runner, monkeypatch):
    stop = _main(runner, monkeypatch, ["--config", CONFIG_URI, "--fluid-stop-key", KEY])
    assert stop.uri == CONFIG_URI.replace("job.json", f"stop/{KEY}")
    # A run started without a key (a plain spark-submit, an older fluid apply) watches nothing.
    assert _main(runner, monkeypatch, ["--config", CONFIG_URI]) is None


# --- the watch ---------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


def test_a_stop_request_is_looked_for_at_most_once_per_interval(runner, monkeypatch):
    looked = []
    monkeypatch.setattr(runner, "_exists", lambda spark, uri: looked.append(uri) or False)
    clock = Clock()
    watch = runner.StopWatch(None, "s3://b/stop/k", every=10.0, now=clock.now)
    assert not watch.requested() and not watch.requested()
    clock.t = 9.9
    assert not watch.requested()
    clock.t = 10.0
    assert not watch.requested()
    assert looked == ["s3://b/stop/k", "s3://b/stop/k"]  # one S3 HEAD per interval


def test_a_local_stop_request_is_a_file_next_to_the_local_job_config(runner, tmp_path):
    clock = Clock()
    request = tmp_path / "stop" / KEY
    watch = runner.StopWatch(None, str(request), every=1.0, now=clock.now)
    assert not watch.requested()
    request.parent.mkdir()
    request.write_text("{}", encoding="utf-8")
    assert not watch.requested()  # within the interval: not looked at again yet
    clock.t = 1.0
    assert watch.requested()
    assert runner.StopWatch(None, f"file://{request}", now=clock.now).requested()


def test_a_remote_request_is_looked_for_through_spark_s_file_system(runner):
    looked = []

    class FileSystem:
        def exists(self, path):
            looked.append(path.uri)
            return True

    class HadoopPath:
        def __init__(self, uri):
            self.uri = uri

        def getFileSystem(self, conf):  # noqa: N802 - Hadoop's name
            assert conf == "hadoop-conf"
            return FileSystem()

    fs = types.SimpleNamespace(Path=HadoopPath)
    jvm = types.SimpleNamespace(
        org=types.SimpleNamespace(apache=types.SimpleNamespace(hadoop=types.SimpleNamespace(fs=fs)))
    )
    context = types.SimpleNamespace(
        _jvm=jvm, _jsc=types.SimpleNamespace(hadoopConfiguration=lambda: "hadoop-conf")
    )
    spark = types.SimpleNamespace(sparkContext=context)
    assert runner._exists(spark, "s3://b/stop/k") is True and looked == ["s3://b/stop/k"]


def test_a_look_that_fails_keeps_the_stream_going_and_is_logged_once(runner, monkeypatch, capsys):
    def denied(spark, uri):
        raise RuntimeError("java.nio.file.AccessDeniedException: s3://b/stop/k")

    monkeypatch.setattr(runner, "_exists", denied)
    clock = Clock()
    watch = runner.StopWatch(None, "s3://b/stop/k", every=1.0, now=clock.now)
    for t in (0.0, 1.0, 2.0):
        clock.t = t
        assert not watch.requested()
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines == [
        {"forge_spark_runner": "stop_check_failed", "uri": "s3://b/stop/k", "error": "RuntimeError"}
    ]


# --- a stop between batches -------------------------------------------------------------


class Query:
    """A streaming query whose status and last progress follow a script, one entry per look
    (the last one repeats)."""

    def __init__(self, qid, triggers, batches):
        self.id, self.name = qid, None
        self.triggers, self.batches = list(triggers), list(batches)
        self.stopped = False
        self.looks = 0

    @staticmethod
    def _next(script):
        return script.pop(0) if len(script) > 1 else script[0]

    @property
    def isActive(self):  # noqa: N802 - PySpark's name
        return not self.stopped

    @property
    def status(self):
        self.looks += 1
        return {
            "message": "",
            "isDataAvailable": True,
            "isTriggerActive": self._next(self.triggers),
        }

    @property
    def lastProgress(self):  # noqa: N802 - PySpark's name
        batch = self._next(self.batches)
        return None if batch is None else {"batchId": batch}

    def stop(self):
        self.stopped = True

    def awaitTermination(self):  # noqa: N802 - PySpark's name
        assert self.stopped


def test_a_query_between_batches_is_stopped_at_once(runner):
    clock = Clock()
    query = Query("q1", [False], [7])
    assert runner.stop_between_batches([query], sleep=clock.sleep) == {"q1": 7}
    assert query.stopped and clock.slept == []


def test_the_batch_in_flight_is_waited_for(runner):
    clock = Clock()
    query = Query("q1", [True, True, True, False], [7])
    runner.stop_between_batches([query], sleep=clock.sleep)
    assert query.stopped and len(clock.slept) == 3


def test_a_batch_committed_since_the_request_ends_the_wait_when_batches_run_back_to_back(runner):
    """With no trigger interval a new batch starts as the last one commits: the trigger is
    never seen idle, so the stop comes right after a commit, as the next batch starts (Spark
    plans it again from its offset log on the next run)."""
    clock = Clock()
    query = Query("q1", [True], [7, 7, 7, 8])
    assert runner.stop_between_batches([query], sleep=clock.sleep) == {"q1": 8}
    assert query.stopped and len(clock.slept) == 2


def test_each_query_stops_as_soon_as_it_is_between_batches(runner, capsys):
    clock = Clock()
    output = Query("out", [False], [3])
    dead = Query("dead", [True, True, False], [None])
    order = []
    output.stop = lambda: order.append("out") or setattr(output, "stopped", True)
    dead.stop = lambda: order.append("dead") or setattr(dead, "stopped", True)
    assert runner.stop_between_batches([output, dead], sleep=clock.sleep) == {
        "out": 3,
        "dead": None,
    }
    assert order == ["out", "dead"]
    (line,) = capsys.readouterr().out.splitlines()
    assert json.loads(line) == {
        "forge_spark_runner": "stopped_on_request",
        "lastBatch": {"dead": None, "out": 3},
    }


class Streams:
    """``spark.streams``: ``awaitAnyTermination`` returns what ``ends`` scripts (True: a
    query ended), or raises one that is an exception."""

    def __init__(self, ends):
        self.ends = list(ends)
        self.waits = []

    def awaitAnyTermination(self, timeout=None):  # noqa: N802 - PySpark's name
        self.waits.append(timeout)
        end = self.ends.pop(0) if len(self.ends) > 1 else self.ends[0]
        if isinstance(end, BaseException):
            raise end
        return end

    def resetTerminated(self):  # noqa: N802 - PySpark's name
        pass


class Watch:
    def __init__(self, answers, every=10.0):
        self.answers, self.every = list(answers), every

    def requested(self):
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def test_a_stop_request_ends_the_run_cleanly_once_every_query_is_between_batches(runner):
    spark = types.SimpleNamespace(streams=Streams([False]))
    queries = [Query("out", [True, False], [1]), Query("dead", [False], [0])]
    runner.await_queries(spark, queries, Watch([False, False, True]))
    assert all(q.stopped for q in queries)
    assert spark.streams.waits == [10.0, 10.0]  # the queries are awaited between looks


def test_without_a_stop_key_the_queries_are_awaited_as_before(runner):
    query = Query("out", [False], [1])

    class Ending(Streams):
        def awaitAnyTermination(self, timeout=None):  # noqa: N802 - PySpark's name
            query.stopped = True
            return super().awaitAnyTermination(timeout)

    spark = types.SimpleNamespace(streams=Ending([True]))
    runner.await_queries(spark, [query])
    assert spark.streams.waits == [None]  # no timeout: nothing to look for


def test_a_query_that_fails_while_a_stop_is_watched_for_fails_the_run(runner):
    class QueryError(Exception):
        pass

    spark = types.SimpleNamespace(streams=Streams([False, QueryError("batch 4 failed")]))
    other = Query("dead", [False], [0])
    with pytest.raises(QueryError):
        runner.await_queries(spark, [Query("out", [True], [1]), other], Watch([False]))
    assert other.stopped  # the other query does not outlive the failed one


# --- a checkpoint the job does not fit is refused before the stream reads ----------------


def _stream_config(runner, contract, *, dead=False):
    """The example stream's job config as the runner reads it; ``dead``: with dead letters."""
    if dead:
        return runner.parse_config(runner_config(contract))
    return runner.parse_config(compile_job_config(contract, contract["builds"][0]).text)


def test_a_checkpoint_that_holds_another_topic_s_position_is_refused(runner, contract, monkeypatch):
    config = _stream_config(runner, contract)
    assert config["source"]["topic"] == "commerce.orders" and config["checkpoint"] == CHECKPOINT
    checkpoints(
        runner,
        monkeypatch,
        {
            f"{CHECKPOINT}offsets/0": offsets("commerce.orders"),
            f"{CHECKPOINT}offsets/12": offsets("payments.events"),  # the newest batch
        },
    )
    with pytest.raises(runner.JobConfigError) as raised:
        runner.check_checkpoint_fits(None, config)
    text = str(raised.value)
    assert CHECKPOINT in text and "payments.events" in text and "commerce.orders" in text
    assert "nothing was read" in text and "properties.stream.checkpoint" in text


@pytest.mark.parametrize(
    "files",
    [
        {},  # a new checkpoint: the stream starts from startingOffsets
        {f"{CHECKPOINT}offsets/3": offsets("commerce.orders")},
        {f"{CHECKPOINT}offsets/3": "v1\nnot an offset log Spark wrote"},  # Spark has the last word
        {f"{CHECKPOINT}offsets/.3.crc": "", f"{CHECKPOINT}offsets/3": offsets("commerce.orders")},
    ],
)
def test_a_new_checkpoint_or_one_of_the_job_s_own_topic_fits(runner, contract, monkeypatch, files):
    config = _stream_config(runner, contract)
    checkpoints(runner, monkeypatch, files)
    runner.check_checkpoint_fits(None, config)


def test_the_dead_letters_checkpoint_is_checked_too(runner, contract, monkeypatch):
    config = _stream_config(runner, contract, dead=True)
    dead = config["deadLetter"]["checkpoint"]
    checkpoints(runner, monkeypatch, {f"{dead}offsets/2": offsets("payments.events")})
    with pytest.raises(runner.JobConfigError, match=dead):
        runner.check_checkpoint_fits(None, config)


def test_a_stream_checks_its_checkpoint_before_it_creates_or_reads_anything(
    runner, contract, monkeypatch
):
    config = _stream_config(runner, contract)
    config.update(maskedColumns=[], maskKey=None)
    done = []
    monkeypatch.setattr(runner, "configure_session", lambda spark, cfg: None)
    monkeypatch.setattr(runner, "stamp_conf", lambda spark, stamps: None)
    monkeypatch.setattr(runner, "create_sink_table", lambda spark, cfg: done.append("create"))
    monkeypatch.setattr(runner, "build_output", lambda spark, cfg: done.append("read"))
    checkpoints(runner, monkeypatch, {f"{CHECKPOINT}offsets/0": offsets("payments.events")})
    with pytest.raises(runner.JobConfigError, match="payments.events"):
        runner.run(None, config)
    assert done == []


def test_a_checkpoint_of_partitions_the_topic_no_longer_has_is_refused(
    runner, contract, monkeypatch
):
    """A topic deleted and made again with fewer partitions: Spark would resume offsets of
    partitions that are not there."""
    config = _stream_config(runner, contract)
    checkpoints(
        runner,
        monkeypatch,
        {f"{CHECKPOINT}offsets/7": 'v1\n{}\n{"commerce.orders":{"0":5,"1":9,"4":2}}'},
    )
    monkeypatch.setattr(runner, "topic_partitions", lambda spark, cfg: [0, 1])
    with pytest.raises(runner.JobConfigError, match=r"partition\(s\) 4 of topic commerce.orders"):
        runner.check_checkpoint_fits(None, config)
    monkeypatch.setattr(runner, "topic_partitions", lambda spark, cfg: [0, 1, 4, 5])
    runner.check_checkpoint_fits(None, config)  # partitions added since are the stream's to find
    monkeypatch.setattr(runner, "topic_partitions", lambda spark, cfg: None)
    runner.check_checkpoint_fits(None, config)  # unknown: Spark has the last word
