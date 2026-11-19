# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What keeps a Spark stream from losing or doubling records.

- ``properties.stream.maxOffsetsPerTrigger`` bounds what one micro-batch reads.
- ``failOnDataLoss`` is pinned on every Kafka source, so offsets the topic no longer holds fail
  the stream instead of being skipped.
- A topic dead letter is keyed by where it came from (``<topic>-<partition>@<offset>``), so
  a batch sent twice (the Kafka sink is at least once) carries the same keys.
- A table dead letter's append stamps the query's id and the batch id in the Iceberg snapshot
  (``snapshot-property.<key>``) and is skipped when that batch already committed:
  ``foreachBatch`` hands a replayed batch over again, where Iceberg's own streaming sink would
  skip the epoch.

The sinks run without Spark here, against a stand-in for ``pyspark.sql.functions`` that
records the expressions they build; the local end-to-end run proves them on Spark.
"""

from __future__ import annotations

import copy
import json
import sys
import types

import pytest

from fluid_build.schema_manager import FluidSchemaManager
from fluid_build.spark.compiler import TARGET_ALLOWLIST_ENV, compile_job_config
from fluid_build.streaming.mapping import parse_stream

from .conftest import BROKERS
from .test_dead_letter import DLQ, runner_config


def stream_job(contract, **stream):
    contract["builds"][0]["properties"]["stream"].update(stream)
    return compile_job_config(contract, contract["builds"][0])


# --- maxOffsetsPerTrigger -------------------------------------------------------------


def test_a_rate_limit_reaches_the_kafka_source_through_the_job_config(runner, contract):
    job = stream_job(contract, maxOffsetsPerTrigger=5000)
    assert job.config["source"]["maxOffsetsPerTrigger"] == 5000
    source = runner.parse_config(job.text)["source"]
    assert runner.kafka_options(source)["maxOffsetsPerTrigger"] == "5000"


def test_without_a_rate_limit_the_job_config_has_no_key_and_kafka_no_limit(runner, contract):
    job = stream_job(contract)
    assert "maxOffsetsPerTrigger" not in job.config["source"]
    assert "maxOffsetsPerTrigger" not in runner.kafka_options(job.config["source"])


@pytest.mark.parametrize("value", [0, -1, True, "5", 1.5, 2**63])
def test_a_rate_limit_that_is_not_a_positive_long_is_refused(contract, value):
    with pytest.raises(ValueError, match="maxOffsetsPerTrigger: expected a whole number"):
        stream_job(contract, maxOffsetsPerTrigger=value)


def test_the_schema_takes_a_rate_limit_and_refuses_zero(contract):
    manager = FluidSchemaManager()

    def valid(value):
        changed = copy.deepcopy(contract)
        changed["builds"][0]["properties"]["stream"]["maxOffsetsPerTrigger"] = value
        return manager.validate_contract(
            changed, schema_version="0.7.7", offline_only=True
        ).is_valid

    assert valid(1) and valid(10_000)
    assert not valid(0) and not valid("10") and not valid(2**63)


def test_managed_flink_refuses_a_rate_limit():
    with pytest.raises(ValueError, match="maxOffsetsPerTrigger"):
        parse_stream(
            {
                "kind": "kafka",
                "topic": "t",
                "format": "json",
                "schema": [{"name": "id", "type": "string"}],
                "maxOffsetsPerTrigger": 10,
            }
        )


@pytest.mark.parametrize("value", [0, "10", True])
def test_the_runner_refuses_a_rate_limit_it_cannot_pass_on(runner, contract, value):
    config = json.loads(stream_job(contract).text)
    config["source"]["maxOffsetsPerTrigger"] = value
    with pytest.raises(runner.JobConfigError, match="maxOffsetsPerTrigger"):
        runner.parse_config(json.dumps(config))


# --- failOnDataLoss -------------------------------------------------------------------


@pytest.mark.parametrize("auth", ["none", "msk-iam"])
def test_offsets_the_topic_no_longer_holds_fail_the_stream(runner, contract, auth):
    source = runner.parse_config(stream_job(contract).text)["source"]
    assert runner.kafka_options({**source, "auth": auth})["failOnDataLoss"] == "true"


# --- a stand-in for pyspark.sql.functions ---------------------------------------------


class E:
    """An expression the sinks build: what made it, and from what."""

    def __init__(self, op, *args):
        self.op, self.args = op, args

    def __repr__(self):
        return f"{self.op}{self.args!r}"

    def __eq__(self, other):
        if isinstance(other, E):
            return (self.op, self.args) == (other.op, other.args)
        return E("==", self, other)

    __hash__ = None

    def __and__(self, other):
        return E("&", self, other)

    def __getitem__(self, key):
        return E("[]", self, key)

    def cast(self, to):
        return E("cast", self, to)

    def alias(self, name):
        return E("alias", self, name)

    def startswith(self, prefix):
        return E("startswith", self, prefix)

    def when(self, cond, value):
        return E("when", self, cond, value)

    def otherwise(self, value):
        return E("otherwise", self, value)


@pytest.fixture
def functions(monkeypatch):
    module = types.ModuleType("pyspark.sql.functions")
    for name in ("col", "lit", "struct", "array", "when", "format_string"):
        setattr(module, name, lambda *args, _op=name: E(_op, *args))
    sql = types.ModuleType("pyspark.sql")
    sql.functions = module
    root = types.ModuleType("pyspark")
    root.sql = sql
    for name, mod in (("pyspark", root), ("pyspark.sql", sql), ("pyspark.sql.functions", module)):
        monkeypatch.setitem(sys.modules, name, mod)
    return module


class Recorder:
    """A DataFrame, writer or snapshot reader that records what it was asked."""

    def __init__(self, calls, rows=()):
        self.calls, self.rows = calls, list(rows)

    def __getattr__(self, name):
        def step(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self

        return step

    @property
    def write(self):
        return self

    def collect(self):
        return self.rows


class Spark:
    """Answers the one read a table dead letter makes: its table's snapshots."""

    def __init__(self, committed):
        self.committed, self.calls = committed, []

    def table(self, name):
        self.calls.append(("table", (name,), {}))
        return Recorder(self.calls, self.committed)


def col(name):
    return E("col", name)


def called(calls, name):
    return [(args, kwargs) for step, args, kwargs in calls if step == name]


# --- the topic dead letters' keys -----------------------------------------------------


def test_a_topic_dead_letter_is_keyed_by_where_it_came_from(
    runner, contract, functions, monkeypatch
):
    # The operator lists the dead-letter topic on each broker, or the compile refuses it.
    monkeypatch.setenv(
        TARGET_ALLOWLIST_ENV, ",".join(f"kafka-write:{b}/{DLQ}" for b in BROKERS.split(","))
    )
    config = runner.parse_config(runner_config(contract, sink="topic", topic=DLQ))
    batch = Recorder([])
    runner.DeadLetters(spark=None, config=config).to_topic(batch)
    (((key, value, headers), _),) = called(batch.calls, "select")
    where = E(
        "format_string", "%s-%d@%d", col("kafka_topic"), col("kafka_partition"), col("kafka_offset")
    )
    assert key == E("alias", E("cast", where, "binary"), "key")
    assert value == E("alias", col("raw_value"), "value")
    # The record's own key rides along in a header, as the bytes it was read as.
    own_key = E(
        "struct",
        E("alias", E("lit", runner.RAW_KEY_HEADER), "key"),
        E("alias", col("raw_key"), "value"),
    )
    assert any(own_key == h for h in headers.args[0].args)
    # The writer gets the connection settings and the topic, not the source's read options.
    ((_, connection),) = called(batch.calls, "options")
    assert connection and all(k.startswith("kafka.") for k in connection)
    assert called(batch.calls, "option") == [(("topic", DLQ), {})]
    assert called(batch.calls, "save") == [((), {})]


# --- the table dead letters' one append per batch -------------------------------------


def table_sink(runner, contract, committed):
    config = runner.parse_config(runner_config(contract))
    spark = Spark(committed)
    sink = runner.DeadLetters(spark=spark, config=config)
    sink._query_id = "4d8f6a2e-query"  # what the dead letters' checkpoint metadata holds
    return sink, spark, f"{config['sink']['catalog']}.{config['deadLetter']['table']}"


def test_a_table_dead_letter_append_stamps_its_query_and_batch(runner, contract, functions):
    sink, spark, table = table_sink(runner, contract, committed=[])
    batch = Recorder([])
    sink.to_table(batch, 7)
    assert ("table", (f"{table}.snapshots",), {}) in spark.calls
    # The look-up asks for this query's id and this batch's, in the snapshot summary.
    (((cond,), _),) = called(spark.calls, "where")
    summary = col("summary")
    assert cond == E(
        "&",
        E("==", E("[]", summary, "fluid.dead-letter.query"), "4d8f6a2e-query"),
        E("==", E("[]", summary, "fluid.dead-letter.batch"), "7"),
    )
    stamped = dict(args for args, _ in called(batch.calls, "option"))
    assert stamped["snapshot-property.fluid.dead-letter.query"] == "4d8f6a2e-query"
    assert stamped["snapshot-property.fluid.dead-letter.batch"] == "7"
    assert called(batch.calls, "writeTo") == [((table,), {})]
    assert called(batch.calls, "append") == [((), {})]


def test_a_batch_already_committed_is_not_appended_again(runner, contract, functions):
    sink, spark, _ = table_sink(runner, contract, committed=[object()])
    batch = Recorder([])
    sink.to_table(batch, 7)
    assert batch.calls == []
    assert called(spark.calls, "where")  # it looked, found the batch, and wrote nothing
