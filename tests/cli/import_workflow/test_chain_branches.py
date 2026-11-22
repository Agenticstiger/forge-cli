# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A read or write chain held in a variable that later assignments only add to.

``writer = df.writeStream.format("iceberg")``, then ``writer = writer.trigger(a)`` in one branch of
an ``if`` and ``writer = writer.trigger(b)`` in the other, then ``writer.toTable(T)``: the first
assignment starts the chain and each later one adds to it, so the write is the same in every run
and only the trigger depends on the branch. A name assigned more than once in any other way is
still not followed: which assignment is meant depends on the order the code runs in.
"""

from __future__ import annotations

import textwrap
from typing import Any, Dict, List

import pytest

from fluid_build.cli.import_workflow import _proposal
from fluid_build.cli.import_workflow._fold import Folder
from fluid_build.cli.import_workflow._pysource import parse_module
from fluid_build.cli.import_workflow._spark_io import TRIGGER_SEPARATOR, JobFacts, read_job

HEAD = """
from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
raw = spark.readStream.format("kafka").option("subscribe", "clicks_raw").load()
"""
BASE = """
writer = (
    raw.writeStream.format("iceberg")
    .outputMode("append")
    .option("checkpointLocation", "s3://state/clicks")
)
"""


def job(source: str) -> JobFacts:
    module = parse_module("job.py", textwrap.dedent(HEAD + source).encode("utf-8"))
    return read_job(module, Folder(module))


def only_write(facts: JobFacts):
    assert len(facts.writes) == 1, (facts.writes, facts.notes)
    return facts.writes[0]


EITHER = """
if True:
    writer = writer.trigger(availableNow=True)
else:
    writer = writer.trigger(processingTime="1 minute")
query = writer.toTable("glue.bronze.clicks")
"""


def test_a_trigger_picked_by_the_two_arms_of_an_if_gives_both_triggers():
    facts = job(BASE + EITHER)
    write = only_write(facts)
    assert (write.name, write.streaming, write.checkpoint) == (
        "glue.bronze.clicks",
        True,
        "s3://state/clicks",
    )
    assert write.trigger == f"availableNow{TRIGGER_SEPARATOR}processingTime=1 minute"
    assert any("picked by a branch" in note for note in facts.remarks), facts.remarks
    assert not any("assigned more than once" in note for note in facts.notes)


def test_such_a_stream_is_continuous_when_any_trigger_keeps_it_running():
    write = only_write(job(BASE + EITHER))
    assert _proposal._trigger([write]) == "continuous"
    both_end = job(
        BASE
        + """
if True:
    writer = writer.trigger(availableNow=True)
else:
    writer = writer.trigger(once=True)
writer.toTable("glue.bronze.clicks")
"""
    )
    assert _proposal._trigger([only_write(both_end)]) == "manual"


def test_a_trigger_set_in_one_arm_only_leaves_the_path_through_no_arm_possible():
    write = only_write(
        job(
            BASE
            + """
if True:
    writer = writer.trigger(availableNow=True)
writer.toTable("glue.bronze.clicks")
"""
        )
    )
    # the stream that runs with no trigger set keeps running: it is not taken for a one-shot
    assert write.trigger == f"availableNow{TRIGGER_SEPARATOR}default"
    assert _proposal._trigger([write]) == "continuous"


def test_the_trigger_set_before_the_branches_counts_when_a_branch_does_not_set_one():
    write = only_write(
        job(
            BASE.replace(".outputMode", '.trigger(processingTime="5 seconds").outputMode')
            + """
if True:
    writer = writer.option("maxOffsetsPerTrigger", 100)
else:
    writer = writer.trigger(availableNow=True)
writer.toTable("glue.bronze.clicks")
"""
        )
    )
    assert write.trigger == f"availableNow{TRIGGER_SEPARATOR}processingTime=5 seconds"


def test_one_trigger_everywhere_is_one_trigger_and_no_note():
    facts = job(
        BASE
        + """
if True:
    writer = writer.trigger(availableNow=True)
else:
    writer = writer.trigger(availableNow=True)
writer.toTable("glue.bronze.clicks")
"""
    )
    assert only_write(facts).trigger == "availableNow"
    assert not facts.remarks


def test_a_use_before_the_reassignments_sees_the_chain_as_it_was_then():
    facts = job(
        BASE
        + """
early = writer.trigger(availableNow=True).toTable("glue.bronze.early")
if True:
    writer = writer.trigger(processingTime="1 minute")
"""
    )
    # the first assignment is the only one before the use: the same as a variable assigned once
    assert only_write(facts).trigger == "availableNow"


def test_an_option_that_is_not_the_identity_of_the_sink_is_a_branch_too():
    facts = job(
        BASE
        + """
if True:
    writer = writer.option("fanout-enabled", "true")
else:
    writer = writer.options(fanout_enabled="false")
writer.toTable("glue.bronze.clicks")
"""
    )
    assert only_write(facts).name == "glue.bronze.clicks"


@pytest.mark.parametrize(
    "addition",
    [
        'writer.format("kafka")',  # another sink
        'writer.option("checkpointLocation", "s3://state/other")',  # its state
        'writer.option("topic", "other")',  # where it writes
        'writer.option("path", "s3://elsewhere")',
        "writer.options(topic='x')",
        "writer.options({'topic': 'x'})",
        "writer.option(KEY, 'x')",  # a key that is not a constant
        "writer.outputMode('complete')",
        'writer.queryName("q")',
        "df.writeStream.format('iceberg')",  # not an addition to the chain: another chain
        "writer",  # nothing is added
        "writer.foreachBatch(fn)",
        "other.trigger(availableNow=True)",  # a trigger, but of another variable's chain
    ],
)
def test_a_later_assignment_that_is_anything_else_is_not_followed(addition):
    facts = job(
        BASE
        + f"""
KEY = lambda: 1
df = raw
other = raw
if True:
    writer = {addition}
else:
    writer = writer.trigger(availableNow=True)
writer.toTable("glue.bronze.clicks")
"""
    )
    assert facts.writes == []
    assert any("`writer`, which is assigned more than once" in n for n in facts.notes), facts.notes


def test_a_second_assignment_that_starts_another_chain_is_not_followed():
    facts = job(
        """
writer = raw.writeStream.format("iceberg")
writer = raw.writeStream.format("kafka")
writer.toTable("glue.bronze.clicks")
"""
    )
    assert facts.writes == []
    assert any("assigned more than once" in n for n in facts.notes)


def test_a_variable_that_is_also_changed_in_another_way_is_not_followed():
    for extra in (
        "writer += 1",
        "for writer in [raw]: pass",
        "(writer, other) = (raw, raw)",
        "import writer",
        "with open('f') as writer: pass",
    ):
        facts = job(BASE + f"\n{extra}\n" + EITHER)
        assert facts.writes == [], extra


def test_a_variable_the_file_changes_in_place_elsewhere_is_not_followed():
    facts = job(BASE + "globals()['writer'] = raw\n" + EITHER)
    assert facts.writes == []


def test_the_chain_is_followed_inside_a_function_and_through_a_second_variable():
    facts = job(
        """
def run(df):
    base = raw.writeStream.format("iceberg").option("checkpointLocation", "s3://state/x")
    writer = base
    if df:
        writer = writer.trigger(availableNow=True)
    else:
        writer = writer.trigger(processingTime="30 seconds")
    return writer.toTable("glue.bronze.x")
"""
    )
    write = only_write(facts)
    assert write.trigger == f"availableNow{TRIGGER_SEPARATOR}processingTime=30 seconds"
    assert write.checkpoint == "s3://state/x"


def test_a_reader_variable_that_later_assignments_add_options_to_is_followed():
    facts = job(
        """
reader = spark.readStream.format("kafka").option("subscribe", "clicks")
if True:
    reader = reader.option("maxOffsetsPerTrigger", 10)
else:
    reader = reader.option("startingOffsets", "earliest")
df = reader.load()
"""
    )
    assert [(r.kind, r.name) for r in facts.reads if r.name == "clicks"] == [("topic", "clicks")]


def test_a_reader_variable_whose_later_assignment_names_another_topic_is_not_followed():
    facts = job(
        """
reader = spark.readStream.format("kafka").option("subscribe", "a")
if True:
    reader = reader.option("subscribe", "b")
df = reader.load()
"""
    )
    assert [r for r in facts.reads if r.name in {"a", "b"}] == []


def test_three_branches_are_three_alternatives():
    facts = job(
        BASE
        + """
if 1:
    writer = writer.trigger(availableNow=True)
elif 2:
    writer = writer.trigger(processingTime="1 minute")
else:
    writer = writer.trigger(continuous="1 second")
writer.toTable("glue.bronze.clicks")
"""
    )
    # three arms are not "the two arms of one if": the path through no arm stays possible
    parts = only_write(facts).trigger.split(TRIGGER_SEPARATOR)
    assert parts == ["availableNow", "processingTime=1 minute", "continuous=1 second", "default"]


def test_two_ifs_in_a_row_are_not_the_two_arms_of_one():
    facts = job(
        BASE
        + """
if 1:
    writer = writer.trigger(availableNow=True)
if 2:
    writer = writer.trigger(processingTime="1 minute")
writer.toTable("glue.bronze.clicks")
"""
    )
    # both, one or neither may run: the path through none stays possible
    assert only_write(facts).trigger.split(TRIGGER_SEPARATOR) == [
        "availableNow",
        "processingTime=1 minute",
        "default",
    ]


def test_an_assignment_after_the_use_does_not_change_what_the_use_sees():
    facts = job(
        BASE
        + """
writer = writer.trigger(availableNow=True)
writer.toTable("glue.bronze.clicks")
writer = writer.trigger(processingTime="1 minute")
"""
    )
    assert only_write(facts).trigger == "availableNow"


def test_a_straight_line_addition_is_a_step_of_the_chain_not_a_branch():
    facts = job(
        BASE
        + """
writer = writer.trigger(processingTime="1 minute")
writer = writer.trigger(availableNow=True)
writer.toTable("glue.bronze.clicks")
"""
    )
    # both run, in order: the last trigger is the one that counts, and nothing is picked
    assert only_write(facts).trigger == "availableNow"
    assert not facts.remarks


def test_an_addition_inside_a_branch_after_a_straight_line_one_is_still_a_branch():
    facts = job(
        BASE
        + """
writer = writer.trigger(processingTime="1 minute")
if 1:
    writer = writer.trigger(availableNow=True)
writer.toTable("glue.bronze.clicks")
"""
    )
    assert only_write(facts).trigger.split(TRIGGER_SEPARATOR) == [
        "availableNow",
        "processingTime=1 minute",
    ]


def test_a_use_above_the_first_assignment_is_not_followed():
    facts = job(
        """
def later():
    return writer.toTable("glue.bronze.clicks")


writer = raw.writeStream.format("iceberg")
writer = writer.trigger(availableNow=True)
"""
    )
    # the function may run after both assignments or never: the lines do not say which
    assert facts.writes == []


def test_a_first_assignment_and_an_addition_in_the_two_arms_of_an_if_are_not_one_block():
    facts = job(
        """
if 1:
    writer = raw.writeStream.format("iceberg").option("checkpointLocation", "s3://state/x")
else:
    writer = writer.trigger(availableNow=True)
writer.toTable("glue.bronze.clicks")
"""
    )
    # the addition runs on one path only: the other keeps the stream's default trigger
    assert only_write(facts).trigger.split(TRIGGER_SEPARATOR) == ["availableNow", "default"]
