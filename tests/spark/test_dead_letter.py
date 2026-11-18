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

from .conftest import CONTRACT


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
        "'the value is not JSON of the declared fields and types'",
        "WHEN `value`.`id` IS NULL THEN 'required field id is null'",
        "WHEN (`value`.`customer` IS NOT NULL AND `value`.`customer`.`email` IS NULL) THEN "
        "'required field customer.email is null'",
        "WHEN coalesce(exists(`value`.`items`, `fluid_e0` -> (`fluid_e0` IS NOT NULL AND "
        "`fluid_e0`.`sku` IS NULL)), false) THEN 'required field items[].sku is null'",
    ]
    assert sql.endswith("FROM `kafka_records_decoded`")


def test_an_avro_value_is_checked_by_its_decoded_mark():
    stream = parse_stream(
        {
            "kind": "kafka",
            "topic": "t",
            "format": "avro-confluent",
            "schema": [{"name": "id", "type": "string"}],
        }
    )
    sql = dead_letter.check_select(stream, {"value": {"format": "avro-confluent"}, "key": None})
    assert (
        "WHEN `fluid_raw_value` IS NOT NULL AND `value`.`fluid_decoded` IS NULL THEN "
        "'the value is not Avro that the registry schema decodes'" in sql
    )


def test_a_local_stream_creates_its_dead_letter_table(contract):
    from tests.build_runners.spark.test_local import local_contract

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


def test_the_reader_schema_gets_the_decoded_mark(runner):
    schema = json.dumps(
        {"type": "record", "name": "o", "fields": [{"name": "id", "type": "string"}]}
    )
    marked = json.loads(runner.with_decoded_mark(schema, dead_letter.DECODED_MARK))
    assert marked["fields"][-1] == {
        "name": "fluid_decoded",
        "type": ["boolean", "null"],
        "default": True,
    }
    assert runner.with_decoded_mark(schema, None) == schema
    with pytest.raises(runner.JobConfigError, match="is a record"):
        runner.with_decoded_mark('"string"', dead_letter.DECODED_MARK)
    with pytest.raises(runner.JobConfigError, match="already has a field"):
        runner.with_decoded_mark(json.dumps(marked), dead_letter.DECODED_MARK)


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
