# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A path whose beginning is a constant is the table a conventions rule gives it.

``spark.read.json(f"{LANDING}/orders/dt={run_date}/")`` takes its run date from a parameter, so
the whole path never folds. The part that does fold names the lake prefix and the segment after
it, which is all a ``paths`` rule needs to say what the table is.
"""

from __future__ import annotations

import textwrap

import pytest

from fluid_build.cli.import_workflow._fold import Folder
from fluid_build.cli.import_workflow._pysource import parse_module
from fluid_build.cli.import_workflow._spark_io import HelperSpec, JobFacts, path_root, read_job

RULES = [("s3://lake/landing/retail/", False)]
HEAD = """
from pyspark.sql import SparkSession

spark = SparkSession.builder.getOrCreate()
LAKE = "s3://lake"
LANDING = f"{LAKE}/landing/retail"
"""


def job(source: str, *, rules=RULES, helpers=None) -> JobFacts:
    module = parse_module("job.py", textwrap.dedent(HEAD + source).encode("utf-8"))
    return read_job(module, Folder(module), helpers=helpers, path_rules=rules)


@pytest.mark.parametrize(
    "known, rules, expected",
    [
        ("s3://lake/landing/retail/orders/dt=", RULES, "s3://lake/landing/retail/orders"),
        ("s3://lake/landing/retail/orders/", RULES, "s3://lake/landing/retail/orders"),
        ("s3://lake/landing/retail/orders", RULES, None),  # the segment may go on
        ("s3://lake/landing/retail/ord", RULES, None),
        ("s3://lake/landing/retail/", RULES, None),
        ("s3://lake/landing/retail", RULES, None),
        ("s3://lake/landing", RULES, None),  # shorter than the prefix
        ("s3://lake/landing/retail-x/orders/", RULES, None),  # a sibling, not under the prefix
        ("s3://other/landing/retail/orders/", RULES, None),
        ("s3://lake/landing/retail/orders/dt=1?x=y#z", RULES, "s3://lake/landing/retail/orders"),
        (
            "s3://lake/curated/fixed/2026-01-01",
            [("s3://lake/curated/fixed/", True)],
            "s3://lake/curated/fixed",
        ),
        (
            "s3://lake/curated/fixed/",
            [("s3://lake/curated/fixed/", True)],
            "s3://lake/curated/fixed",
        ),
        ("s3://lake/curated", [("s3://lake/curated/fixed/", True)], None),
        ("s3://lake/curated/fixed", [("s3://lake/curated/fixed/", True)], None),  # a longer name
        ("", RULES, None),
        ("s3://lake/landing/retail/../secret/x/", RULES, None),  # a path that goes up
        ("s3://lake/landing/retail/./orders/x", RULES, None),
        ("s3://lake/landing/retail/%2e%2e/orders/x", RULES, None),
        ("s3://lake/landing/retail/orders/../x/", RULES, None),
        # the first rule that applies is the one: a later, longer one is not looked at
        (
            "s3://lake/a/b/x/",
            [("s3://lake/a/", False), ("s3://lake/a/b/", True)],
            "s3://lake/a/b",
        ),
        ("s3://lake/a/", [("s3://lake/a/", False), ("s3://lake/a/b/", True)], None),
        # the first rule that applies decides, even when it cannot place the path
        ("s3://lake/a/b", [("s3://lake/a/", False), ("s3://lake/a/b/", True)], None),
        # a query or a fragment is not part of the path
        ("s3://lake/landing/retail/orders?x=1/more", RULES, None),
    ],
)
def test_the_table_root_of_a_path_that_is_known_only_at_its_beginning(known, rules, expected):
    assert path_root(known, rules) == expected


def paths(facts: JobFacts):
    return [(r.kind, r.name) for r in [*facts.reads, *facts.writes]]


def test_a_read_whose_path_ends_in_a_run_date_is_the_table_the_rule_gives_it():
    facts = job(
        """
def run(run_date):
    return spark.read.json(f"{LANDING}/orders/dt={run_date}/")
"""
    )
    assert paths(facts) == [("path", "s3://lake/landing/retail/orders")]
    assert facts.notes == []  # it is no longer a table named by a parameter
    assert any("is taken as that table" in remark for remark in facts.remarks)


@pytest.mark.parametrize(
    "call",
    [
        'spark.read.json(f"{LANDING}/orders/dt={d}")',
        'spark.read.format("json").load(f"{LANDING}/orders/dt={d}")',
        'spark.read.format("json").load(path=f"{LANDING}/orders/dt={d}")',
        'spark.read.json(path=f"{LANDING}/orders/dt={d}")',
        'spark.read.parquet(LANDING + "/orders/dt=" + d)',
        'spark.read.json("%s/orders/dt=%s" % (LANDING, d))',
        'spark.read.json("{}/orders/dt={}".format(LANDING, d))',
        'spark.read.format("json").option("path", f"{LANDING}/orders/dt={d}").load()',
    ],
)
def test_every_way_to_read_a_path_is_rooted(call):
    facts = job(f"def run(d):\n    return {call}\n")
    assert paths(facts) == [("path", "s3://lake/landing/retail/orders")], call


@pytest.mark.parametrize(
    "call",
    [
        'df.write.parquet(f"{LANDING}/orders/dt={d}")',
        'df.write.format("parquet").save(f"{LANDING}/orders/dt={d}")',
        'df.write.mode("append").json(path=f"{LANDING}/orders/dt={d}")',
    ],
)
def test_a_write_to_a_partition_directory_is_a_write_to_its_table(call):
    facts = job(f"def run(df, d):\n    {call}\n")
    assert paths(facts) == [("path", "s3://lake/landing/retail/orders")], call


def test_a_path_held_in_a_name_is_rooted_too():
    facts = job(
        """
def run(d):
    where = f"{LANDING}/orders/dt={d}"
    return spark.read.json(where)
"""
    )
    assert paths(facts) == [("path", "s3://lake/landing/retail/orders")]


def test_a_helper_that_reads_a_path_is_rooted():
    facts = job(
        """
from acme.io import read_landing

def run(d):
    return read_landing(spark, f"{LANDING}/orders/dt={d}/", None)
""",
        helpers={"read_landing": HelperSpec("reads", "path", 1)},
    )
    assert paths(facts) == [("path", "s3://lake/landing/retail/orders")]


@pytest.mark.parametrize(
    "path",
    [
        'f"{LANDING}/{entity}/dt=1"',  # the table's own name is not known
        'f"{LANDING}/orders{suffix}/x"',
        'f"{LAKE}/landing/{zone}/orders/dt=1"',  # not under the prefix before the gap
        'f"{base}/landing/retail/orders/dt=1"',  # nothing is known at the start
        'f"{LANDING}/{d}"',  # no segment at all
        'f"{LAKE}/other/orders/dt={d}"',  # no rule for it
    ],
)
def test_a_path_the_rules_cannot_place_is_left_as_it_was(path):
    facts = job(f"def run(d, entity, suffix, zone, base):\n    return spark.read.json({path})\n")
    assert facts.reads == []
    assert any("function parameter" in note for note in facts.notes), facts.notes


def test_without_rules_a_partial_path_is_what_it_always_was():
    facts = job('def run(d):\n    return spark.read.json(f"{LANDING}/orders/dt={d}")\n', rules=())
    assert facts.reads == [] and any("function parameter" in n for n in facts.notes)


def test_a_path_that_folds_completely_is_not_touched():
    facts = job('spark.read.json(f"{LANDING}/orders/dt=2026-01-01/")\n')
    assert paths(facts) == [("path", "s3://lake/landing/retail/orders/dt=2026-01-01/")]
    assert facts.remarks == []


def test_the_remark_is_made_once_for_a_path_read_twice():
    facts = job(
        """
def run(d):
    a = spark.read.json(f"{LANDING}/orders/dt={d}")
    b = spark.read.json(f"{LANDING}/orders/dt={d}")
"""
    )
    # two lines, two remarks: the line is part of what a person looks for
    assert len(facts.remarks) == 2
    assert len(facts.reads) == 2


def test_only_a_path_option_is_rooted_not_any_option():
    facts = job(
        """
def run(d):
    return spark.readStream.format("kafka").option("subscribe", f"{LANDING}/orders/{d}").load()
"""
    )
    assert [r for r in facts.reads if r.kind == "topic" and r.resolved] == []


def test_the_remarks_are_bounded(monkeypatch):
    from fluid_build.cli.import_workflow import _spark_io

    monkeypatch.setattr(_spark_io, "MAX_NOTES_PER_JOB", 1)
    facts = job(
        """
def run(d):
    a = spark.read.json(f"{LANDING}/orders/dt={d}")
    b = spark.read.json(f"{LANDING}/customers/dt={d}")
"""
    )
    assert len(facts.remarks) == 1 and len(facts.reads) == 2


def test_a_fixed_rule_does_not_take_a_name_that_only_starts_like_its_prefix():
    facts = job(
        """
sfx = spark.conf.get("suffix")
spark.read.parquet(f"s3://lake/curated/fixed{sfx}/part")
""",
        rules=[("s3://lake/curated/fixed/", True)],
    )
    assert [r.resolved for r in facts.reads] == [False]
