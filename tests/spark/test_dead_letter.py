# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A Spark stream's dead letters in the compiler and the runner's config checks: what the
job config says, and that a stream without them compiles to the bytes it always did."""

from __future__ import annotations

import copy
import json

import pytest

from fluid_build.spark import dead_letter
from fluid_build.spark.compiler import compile_job_config
from fluid_build.streaming.mapping import parse_stream

from .conftest import BROKERS, CONTRACT

DLQ = "commerce.orders.dlq"


@pytest.fixture(autouse=True)
def listed_topic(monkeypatch, operator_policy):
    """The operator lists writing the examples' dead-letter topic, on each broker."""
    from fluid_build.spark.compiler import TARGET_ALLOWLIST_ENV

    monkeypatch.setenv(
        TARGET_ALLOWLIST_ENV, ",".join(f"kafka-write:{b}/{DLQ}" for b in BROKERS.split(","))
    )


def with_dead_letters(contract, **spec):
    contract["builds"][0]["properties"]["stream"]["deadLetter"] = spec
    return contract


def config_of(contract):
    return compile_job_config(contract, contract["builds"][0]).config


def test_a_stream_without_dead_letters_keeps_its_job_config(contract):
    before = compile_job_config(copy.deepcopy(CONTRACT), CONTRACT["builds"][0]).text
    config = json.loads(before)
    assert "deadLetter" not in config and "check" not in config["sql"]
    assert config["source"]["value"] == {
        "format": "json",
        "schema": config["source"]["value"]["schema"],
    }
    off = with_dead_letters(copy.deepcopy(contract), enabled=False)
    assert compile_job_config(off, off["builds"][0]).text == before


def test_a_table_sink_lands_beside_the_output_by_default(contract):
    config = config_of(with_dead_letters(contract))
    assert config["deadLetter"] == {
        "sink": "table",
        "table": "commerce.order_lines__dead_letter",
        "checkpoint": "s3://commerce-lake/_checkpoints/commerce.orders-stream/ingest-orders/"
        "dead-letter/",
        "maxRecordsBeforeAbort": 10_000,
        "alertOn": [],
        "raw": False,  # the example masks email_hash at ingest
    }
    value = config["source"]["value"]
    assert value["corruptRecord"] == dead_letter.CORRUPT_COLUMN
    assert value["schema"].endswith(f", `{dead_letter.CORRUPT_COLUMN}` STRING")
    assert config["sql"]["check"].startswith("SELECT *,")


def test_a_build_that_masks_nothing_keeps_the_raw_bytes(contract):
    mapping = contract["builds"][0]["properties"]["mapping"]
    for column in mapping["columns"]:
        column.pop("maskAtIngest", None)
    contract["builds"][0]["properties"]["parameters"]["spark"].pop("maskKeySecretArn")
    assert config_of(with_dead_letters(contract))["deadLetter"]["raw"] is True


def test_a_named_table_a_topic_and_the_abort_threshold(contract):
    named = config_of(
        with_dead_letters(copy.deepcopy(contract), table="orders_rejects", maxRecordsBeforeAbort=0)
    )["deadLetter"]
    assert (named["table"], named["maxRecordsBeforeAbort"]) == ("commerce.orders_rejects", 0)
    topic = config_of(
        with_dead_letters(
            copy.deepcopy(contract),
            sink="topic",
            topic="commerce.orders.dlq",
            alertOn=["schema_violation"],
        )
    )["deadLetter"]
    assert topic["sink"] == "topic" and topic["topic"] == "commerce.orders.dlq"
    assert "table" not in topic and topic["alertOn"] == ["schema_violation"]


@pytest.mark.parametrize(
    ("spec", "says"),
    [
        ({"sink": "topic"}, "names its topic"),
        ({"sink": "topic", "topic": "t", "table": "x"}, "names its topic, and no table"),
        ({"topic": "t"}, "only a topic sink"),
        ({"table": "bad name"}, "expected a table name"),
        ({"table": "order_lines"}, "a table of their own"),
        ({"maxRecordsBeforeAbort": -1}, "whole number from 0"),
        ({"maxRecordsBeforeAbort": True}, "whole number from 0"),
        ({"alertOn": ["destination_write_failed"]}, "alertOn"),
        ({"sink": "bucket"}, "sink"),
        ({"enabled": "yes"}, "true or false"),
        ({"retries": 3}, "unsupported field"),
    ],
)
def test_a_dead_letter_block_the_runner_cannot_follow_is_refused(contract, spec, says):
    with pytest.raises(ValueError, match=says):
        config_of(with_dead_letters(contract, **spec))


def test_a_declared_field_named_like_the_runner_s_own_is_refused(contract):
    stream = contract["builds"][0]["properties"]["stream"]
    stream["schema"] = [*stream["schema"], {"name": dead_letter.CORRUPT_COLUMN, "type": "string"}]
    with pytest.raises(ValueError, match="rename it"):
        config_of(with_dead_letters(contract))


def test_the_check_names_the_first_reason_decoding_first(contract):
    stream = parse_stream(
        {
            "kind": "kafka",
            "topic": "t",
            "format": "json",
            "schema": [
                {"name": "id", "type": "string", "required": True},
                {
                    "name": "customer",
                    "type": "struct",
                    "fields": [{"name": "email", "type": "string", "required": True}],
                },
                {
                    "name": "items",
                    "type": "array",
                    "element": {
                        "type": "struct",
                        "fields": [{"name": "sku", "type": "string", "required": True}],
                    },
                },
            ],
        }
    )
    decoders = {"value": {"format": "json"}, "key": None}
    sql = dead_letter.check_select(stream, decoders)
    lines = [line.strip() for line in sql.splitlines() if line.strip().startswith("WHEN")]
    assert lines == [
        "WHEN `value`.`fluid_corrupt_record` IS NOT NULL THEN "
        + "'the value is not JSON of the declared fields and types'",
        "WHEN `value`.`id` IS NULL THEN 'required field id is null'",
        "WHEN (`value`.`customer` IS NOT NULL AND `value`.`customer`.`email` IS NULL) THEN "
        + "'required field customer.email is null'",
        "WHEN coalesce(exists(`value`.`items`, `fluid_e0` -> (`fluid_e0` IS NOT NULL AND "
        + "`fluid_e0`.`sku` IS NULL)), false) THEN 'required field items[].sku is null'",
    ]
    assert sql.endswith("FROM `kafka_records_decoded`")


def test_an_avro_stream_s_dead_letters_are_refused_for_now(contract):
    """The decoder's permissive mode would also turn a schema registry outage into dead
    letters, and the output and the dead letters decode each record apart."""
    stream = contract["builds"][0]["properties"]["stream"]
    stream["format"] = "avro-confluent"
    stream["registry"] = {
        "kind": "confluent",
        "url": "https://registry.example:8081",
        "subject": "orders-value",
    }
    with_dead_letters(contract)
    with pytest.raises(ValueError, match="Avro stream's dead letters are not supported"):
        # The operator's registry gate is apply's; the refusal is the contract's.
        compile_job_config(contract, contract["builds"][0], host_gate=False)


def test_a_local_stream_creates_its_dead_letter_table(contract, monkeypatch):
    from tests.build_runners.spark.test_local import local_contract

    monkeypatch.setenv("FLUID_SPARK_LOCAL_NAMESPACE_ALLOWLIST", "fluid/commerce")
    config = config_of(with_dead_letters(local_contract()))
    create = config["deadLetter"]["create"]
    assert create[0] == "CREATE NAMESPACE IF NOT EXISTS `fluid`.`commerce`"
    assert create[1].startswith(
        "CREATE TABLE IF NOT EXISTS `fluid`.`commerce`.`order_lines__dead_letter` (\n"
        "  `error` STRING NOT NULL,\n  `raw_value` BINARY,"
    )


# --- the runner's config checks --------------------------------------------------------


def runner_config(contract, **spec):
    return compile_job_config(with_dead_letters(contract, **spec), contract["builds"][0]).text


def test_the_runner_reads_a_dead_letter_config(runner, contract):
    config = runner.parse_config(runner_config(contract))
    assert config["deadLetter"]["sink"] == "table"


@pytest.mark.parametrize(
    ("change", "says"),
    [
        (lambda c: c["sql"].pop("check"), "sql.check"),
        (lambda c: c["deadLetter"].pop("table"), "names its table"),
        (lambda c: c["deadLetter"].update(sink="topic"), "names its topic"),
        (lambda c: c["deadLetter"].update(maxRecordsBeforeAbort="10"), "whole number"),
        (lambda c: c["deadLetter"].update(create=["DROP TABLE x"]), "CREATE ... IF NOT EXISTS"),
        (lambda c: c["deadLetter"].pop("checkpoint"), "checkpoint"),
    ],
)
def test_a_dead_letter_config_the_runner_cannot_follow_is_refused(runner, contract, change, says):
    config = json.loads(runner_config(contract))
    change(config)
    with pytest.raises(runner.JobConfigError, match=says):
        runner.parse_config(json.dumps(config))


def test_managed_flink_refuses_dead_letters():
    from fluid_build.streaming.mapping import parse_stream as flink_reads

    with pytest.raises(ValueError, match="deadLetter"):
        flink_reads(
            {
                "kind": "kafka",
                "topic": "t",
                "format": "json",
                "schema": [{"name": "id", "type": "string"}],
                "deadLetter": {},
            }
        )


# --- the runner's dead-letter batches --------------------------------------------------


class Batch:
    """A micro-batch of ``n`` dead letters, as foreachBatch hands one over."""

    def __init__(self, n):
        self.n, self.cached = n, False

    def persist(self):
        self.cached = True
        return self

    def count(self):
        return self.n

    def unpersist(self):
        self.cached = False


def dead_letters(runner, contract, monkeypatch, **spec):
    config = runner.parse_config(runner_config(contract, **spec))
    landed = []
    sink = runner.DeadLetters(spark=None, config=config)
    monkeypatch.setattr(sink, "to_table", lambda batch, batch_id: landed.append(batch_id))
    monkeypatch.setattr(sink, "to_topic", lambda batch: landed.append("topic"))
    return sink, landed


def test_each_batch_lands_once_and_is_counted(runner, contract, monkeypatch):
    sink, landed = dead_letters(runner, contract, monkeypatch)
    sink(Batch(2), 0)
    sink(Batch(0), 1)  # a batch with none writes nothing
    sink(Batch(3), 2)
    assert (landed, sink.total) == ([0, 2], 5)


def test_past_the_threshold_the_stream_stops_after_the_batch_landed(runner, contract, monkeypatch):
    sink, landed = dead_letters(runner, contract, monkeypatch, maxRecordsBeforeAbort=3)
    sink(Batch(3), 0)  # at the threshold: going on
    with pytest.raises(runner.DeadLetterAbort, match="past maxRecordsBeforeAbort 3"):
        sink(Batch(1), 1)
    assert landed == [0, 1]  # the batch that crossed it landed first


def test_a_topic_sink_sends_and_an_alert_is_logged(runner, contract, monkeypatch, capsys):
    sink, landed = dead_letters(
        runner,
        contract,
        monkeypatch,
        sink="topic",
        topic="commerce.orders.dlq",
        alertOn=["schema_violation"],
    )
    sink(Batch(1), 0)
    assert landed == ["topic"]
    assert '"forge_spark_runner": "dead_letter_alert"' in capsys.readouterr().out


def test_fluid_validate_reports_a_dead_letter_block_apply_would_refuse(contract, tmp_path):
    import yaml

    from fluid_build.cli import main
    from fluid_build.spark.compiler import validation_errors

    same = with_dead_letters(copy.deepcopy(contract), table="order_lines")
    (error,) = validation_errors(same)
    assert "a table of their own" in error
    good = with_dead_letters(copy.deepcopy(contract), sink="topic", topic="commerce.orders.dlq")
    assert validation_errors(good) == []
    path = tmp_path / "contract.fluid.yaml"
    path.write_text(yaml.safe_dump(same, sort_keys=False), encoding="utf-8")
    assert main(["validate", str(path), "--offline"]) != 0
    path.write_text(yaml.safe_dump(good, sort_keys=False), encoding="utf-8")
    assert main(["validate", str(path), "--offline"]) == 0


# --- where a new dead-letter query starts ---------------------------------------------

#: The offset log and the initial offsets Spark 3.5 wrote for a live run of this lane.
OFFSETS_FILE = (
    'v1\n{"batchWatermarkMs":0,"batchTimestampMs":1795025520028,"conf":{}}\n'
    '{"source.events":{"1":3,"0":9}}'
)
INITIAL_FILE = '\x00v1\n{"source.events":{"1":0,"0":0}}'


def test_spark_s_checkpoint_offsets_read_as_starting_offsets(runner):
    assert runner.source_offsets(OFFSETS_FILE, 2) == '{"source.events": {"0": 9, "1": 3}}'
    assert runner.source_offsets(INITIAL_FILE, 1) == '{"source.events": {"0": 0, "1": 0}}'
    for text, line in (("v1\n{}", 1), ("v1\nnot json", 1), ('v1\n{"t": {"0": "x"}}', 1)):
        assert runner.source_offsets(text, line) is None
    assert runner.source_offsets("v1", 2) is None


def checkpoints(runner, monkeypatch, files):
    """``files``: path -> text, standing in for the checkpoints' file system."""

    def names(spark, directory):
        prefix = directory.rstrip("/") + "/"
        return sorted({p[len(prefix) :].split("/")[0] for p in files if p.startswith(prefix)})

    monkeypatch.setattr(runner, "_names", names)
    monkeypatch.setattr(runner, "_read_small", lambda spark, path: files[path])


def seed_of(runner, contract, monkeypatch, files):
    config = runner.parse_config(runner_config(contract))
    checkpoints(runner, monkeypatch, files)
    return config, runner.dead_letter_start(None, config)


def test_new_dead_letters_start_where_the_output_resumes(runner, contract, monkeypatch):
    out = "s3://commerce-lake/_checkpoints/commerce.orders-stream/ingest-orders/"
    later = OFFSETS_FILE.replace('"0":9', '"0":12')
    config, seed = seed_of(
        runner,
        contract,
        monkeypatch,
        {
            f"{out}commits/0": "v1",
            f"{out}commits/1": "v1",
            f"{out}offsets/0": OFFSETS_FILE.replace('"0":9', '"0":4'),
            f"{out}offsets/1": OFFSETS_FILE,
            f"{out}offsets/2": later,  # planned, not committed: replayed from batch 1's end
            f"{out}sources/0/0": INITIAL_FILE,
        },
    )
    assert config["deadLetter"]["checkpoint"] == f"{out}dead-letter/"
    assert seed == '{"source.events": {"0": 9, "1": 3}}'


def test_an_output_that_never_committed_resumes_where_its_source_started(
    runner, contract, monkeypatch
):
    out = "s3://commerce-lake/_checkpoints/commerce.orders-stream/ingest-orders/"
    _, seed = seed_of(
        runner,
        contract,
        monkeypatch,
        {f"{out}offsets/0": OFFSETS_FILE, f"{out}sources/0/0": INITIAL_FILE},
    )
    assert seed == '{"source.events": {"0": 0, "1": 0}}'


def test_dead_letters_with_a_checkpoint_resume_it_and_a_new_output_is_waited_for(
    runner, contract, monkeypatch
):
    out = "s3://commerce-lake/_checkpoints/commerce.orders-stream/ingest-orders/"
    _, seed = seed_of(
        runner,
        contract,
        monkeypatch,
        {f"{out}dead-letter/offsets/0": OFFSETS_FILE, f"{out}commits/0": "v1"},
    )
    assert seed is None  # Spark resumes the dead letters' own checkpoint
    _, seed = seed_of(runner, contract, monkeypatch, {})
    assert seed is None  # a new output: run() waits for where its source starts

    class Query:
        isActive = False

    assert runner.await_initial_offsets(None, out, Query(), timeout=1) is None
    checkpoints(runner, monkeypatch, {f"{out}sources/0/0": INITIAL_FILE})
    assert runner.await_initial_offsets(None, out, Query(), timeout=1) == (
        '{"source.events": {"0": 0, "1": 0}}'
    )


def test_a_batch_is_known_by_its_query_s_id_which_a_new_checkpoint_changes(
    runner, contract, monkeypatch
):
    config = runner.parse_config(runner_config(contract))
    ckpt = config["deadLetter"]["checkpoint"]
    checkpoints(runner, monkeypatch, {f"{ckpt}metadata": '{"id":"8f6d6c1e"}'})
    assert runner.DeadLetters(spark=None, config=config).query_id() == "8f6d6c1e"


def test_the_runner_names_the_columns_the_compiler_does(runner):
    assert (runner.RAW_VALUE, runner.RAW_KEY, runner.ERROR_COLUMN, runner.DECODED_TABLE) == (
        dead_letter.RAW_VALUE,
        dead_letter.RAW_KEY,
        dead_letter.ERROR_COLUMN,
        dead_letter.DECODED_TABLE,
    )
    aliases = [s.rsplit(" AS ", 1)[1].strip("`") for s in runner.DEAD_LETTER_SELECT]
    assert aliases == [name for name, _, _ in dead_letter.COLUMNS]


def test_a_seed_names_every_partition_the_topic_has_now(runner):
    """Spark refuses starting offsets that leave a partition out; one added since starts
    at its beginning, as the output reads a partition it discovers."""
    seed = '{"source.events": {"0": 9, "1": 3}}'
    assert runner.with_every_partition(seed, [0, 1, 2]) == (
        '{"source.events": {"0": 9, "1": 3, "2": -2}}'
    )
    assert runner.with_every_partition(seed, None) == seed  # unknown: as read


def test_a_dead_letter_topic_the_operator_has_not_listed_is_refused_with_the_export_line(
    contract, monkeypatch
):
    from fluid_build.spark.compiler import TARGET_ALLOWLIST_ENV
    from fluid_build.spark.policy import SparkPolicyError

    monkeypatch.setenv(TARGET_ALLOWLIST_ENV, f"kafka-write:b-1.msk.example:9098/{DLQ}")
    with pytest.raises(SparkPolicyError) as caught:
        config_of(with_dead_letters(contract, sink="topic", topic="payments.commands"))
    listed = ",".join(
        [f"kafka-write:b-1.msk.example:9098/{DLQ}"]
        + [f"kafka-write:{b}/payments.commands" for b in BROKERS.split(",")]
    )
    assert f'export {TARGET_ALLOWLIST_ENV}="{listed}"' in caught.value.fix
    # Listing one broker is not listing the cluster: any of them may take the write.
    monkeypatch.setenv(TARGET_ALLOWLIST_ENV, f"kafka-write:b-1.msk.example:9098/{DLQ}")
    with pytest.raises(SparkPolicyError) as caught:
        config_of(with_dead_letters(copy.deepcopy(CONTRACT), sink="topic", topic=DLQ))
    assert f"kafka-write:b-2.msk.example:9098/{DLQ}" in caught.value.why


@pytest.mark.parametrize("runtime", ["local", "emr-serverless"])
def test_a_dead_letter_topic_on_a_private_broker_needs_its_entry_on_every_runtime(
    contract, monkeypatch, runtime
):
    from fluid_build.spark.compiler import TARGET_ALLOWLIST_ENV
    from fluid_build.spark.policy import SparkPolicyError

    build = contract["builds"][0]
    cfg = build["properties"]["parameters"]["spark"]
    cfg["source"] = {"bootstrapServers": "10.0.0.5:9092"}
    if runtime == "local":
        cfg["runtime"] = "local"
        cfg.pop("maskKeySecretArn")
        for column in build["properties"]["mapping"]["columns"]:
            column.pop("maskAtIngest", None)
        contract["exposes"][0]["binding"] = {
            "platform": "local",
            "format": "iceberg",
            "location": {
                "catalog": "lakekeeper",
                "database": "commerce",
                "table": "order_lines",
                "bucket": "lake",
            },
        }
        monkeypatch.setenv("FLUID_SPARK_LOCAL_NAMESPACE_ALLOWLIST", "fluid/commerce")
    spec = {"sink": "topic", "topic": DLQ}
    with pytest.raises(SparkPolicyError) as caught:
        config_of(with_dead_letters(copy.deepcopy(contract), **spec))
    assert f"kafka-write:10.0.0.5:9092/{DLQ}" in caught.value.why
    monkeypatch.setenv(TARGET_ALLOWLIST_ENV, f"kafka-write:10.0.0.5:9092/{DLQ}")
    assert config_of(with_dead_letters(contract, **spec))["deadLetter"]["topic"] == DLQ


def test_an_msk_cluster_s_dead_letter_topic_takes_an_msk_write_entry(contract, monkeypatch):
    from fluid_build.spark.compiler import (
        TARGET_ALLOWLIST_ENV,
        dead_letter_write_entries,
        unlisted_dead_letter_writes,
    )

    arn = "arn:aws:kafka:eu-west-1:123456789012:cluster/orders/0a1b2c3d-4e5f"
    kafka = {"mskClusterArn": arn, "bootstrapServers": "${msk}"}
    assert dead_letter_write_entries(kafka, DLQ) == [f"msk-write:{arn}/{DLQ}"]
    monkeypatch.setenv(TARGET_ALLOWLIST_ENV, f"msk:{arn},msk-write:{arn}/{DLQ}")
    assert unlisted_dead_letter_writes(kafka, DLQ) == []
    assert unlisted_dead_letter_writes(kafka, "other") == [f"msk-write:{arn}/other"]


def test_fluid_validate_compiles_an_unlisted_dead_letter_topic_and_identity_warns(
    contract, monkeypatch
):
    from fluid_build.iac.providers.aws_identity import identity_findings
    from fluid_build.spark.compiler import TARGET_ALLOWLIST_ENV, validation_errors

    monkeypatch.delenv(TARGET_ALLOWLIST_ENV)
    spec = with_dead_letters(contract, sink="topic", topic=DLQ)
    assert validation_errors(spec) == []  # a missing entry is a warning at validate
    _, missing = identity_findings(spec["builds"][0])
    assert any(f"kafka-write:b-2.msk.example:9098/{DLQ}" in m for m in missing)
