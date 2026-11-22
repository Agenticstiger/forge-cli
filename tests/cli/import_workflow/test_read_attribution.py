# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Which reads feed which output.

A job that writes many tables used to give every product all of the job's reads: the cost of
checking the proposal grew with products times reads (a job of 440 ``INSERT ... SELECT`` took
about 50 s). A product now lists the reads that feed its outputs when the code says so: those of
the SQL statement that writes it, and those a DataFrame is made of, followed through names that
are assigned once. When the code does not say (a function call that returns the DataFrame, a
name that is a parameter, SQL that reads a temporary view) the product lists every read that no
single write owns, as it always did.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Dict, List

import pytest

from fluid_build.cli.import_workflow import _proposal
from fluid_build.cli.import_workflow import _spark_io as io
from fluid_build.cli.import_workflow.pyspark import PySparkImporter

HEAD = "from pyspark.sql import SparkSession\nfrom pyspark.sql import functions as F\n"
HEAD += "spark = SparkSession.builder.getOrCreate()\n"


def consumed(root: Path, monkeypatch, body: str, **options) -> Dict[str, List[str]]:
    """``{product id: sorted ids of what it consumes}`` for a job made of ``body``."""
    (root / "jobs").mkdir(exist_ok=True)
    (root / "jobs" / "j.py").write_text(HEAD + textwrap.dedent(body), encoding="utf-8")
    monkeypatch.chdir(root)
    contracts, _report = PySparkImporter().import_to_contracts("jobs", options=options)
    return {c["id"]: sorted(x["productId"] for x in c.get("consumes", [])) for c in contracts}


def facts_of(source: str) -> io.JobFacts:
    from fluid_build.cli.import_workflow._fold import Folder
    from fluid_build.cli.import_workflow._pysource import parse_module

    module = parse_module("j.py", (HEAD + textwrap.dedent(source)).encode("utf-8"))
    return io.read_job(module, Folder(module))


# ── SQL ───────────────────────────────────────────────────────────────


def test_each_insert_lists_the_tables_its_own_statement_reads(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.sql("INSERT OVERWRITE TABLE mart.t1 SELECT * FROM raw.a JOIN raw.b ON a.k = b.k")
        spark.sql("INSERT INTO mart.t2 SELECT * FROM raw.c")
        spark.sql("CREATE TABLE mart.t3 AS SELECT * FROM raw.d")
        spark.sql("MERGE INTO mart.t4 t USING raw.e s ON t.k = s.k WHEN MATCHED THEN DELETE")
        """,
    )
    assert got == {
        "mart.t1": ["raw.a", "raw.b"],
        "mart.t2": ["raw.c"],
        "mart.t3": ["raw.d"],
        "mart.t4": ["raw.e"],
    }


def test_a_cte_is_not_a_read_and_two_statements_in_one_call_are_two_feeds(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.sql('''
            WITH x AS (SELECT * FROM raw.a) INSERT INTO mart.t1 SELECT * FROM x;
            INSERT INTO mart.t2 SELECT * FROM raw.b
        ''')
        """,
    )
    assert got == {"mart.t1": ["raw.a"], "mart.t2": ["raw.b"]}


def test_two_statements_that_write_one_table_list_the_reads_of_both(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.sql("INSERT INTO mart.t1 SELECT * FROM raw.a")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM raw.b")
        spark.sql("INSERT INTO mart.t2 SELECT * FROM raw.c")
        """,
    )
    assert got == {"mart.t1": ["raw.a", "raw.b"], "mart.t2": ["raw.c"]}


def test_a_statement_that_reads_a_temporary_view_does_not_say_what_feeds_it(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.table("raw.a").createOrReplaceTempView("v")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM v")
        spark.sql("INSERT INTO mart.t2 SELECT * FROM raw.c")
        """,
    )
    # t1 reads a view that raw.a feeds: every read that no closed statement owns may feed it
    assert got["mart.t1"] == ["raw.a"]
    assert got["mart.t2"] == ["raw.c"]


def test_a_statement_with_a_name_that_does_not_fold_is_open(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        def run(env):
            spark.sql(f"INSERT INTO mart.t1 SELECT * FROM {env}_raw.a")
            spark.sql("INSERT INTO mart.t2 SELECT * FROM raw.c")
        """,
    )
    # the open statement's reads stay in the pool of what no statement owns; t2's are its own
    assert got["mart.t2"] == ["raw.c"]


def test_sql_that_does_not_parse_or_is_not_a_constant_is_open(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        def run(text):
            spark.sql(text)
        spark.sql("INSERT INTO mart.t1 SELECT * FROM raw.a")
        spark.sql("INSERT INTO mart.t2 SELEKT nonsense FROM FROM raw.b")
        combine(spark).write.saveAsTable("mart.t3")
        """,
    )
    assert got["mart.t1"] == ["raw.a"]


def test_a_write_that_is_not_made_of_a_statement_lists_the_reads_no_statement_owns(
    tmp_path, monkeypatch
):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.sql("INSERT INTO mart.t1 SELECT * FROM raw.a")
        d = spark.table("raw.b")
        combine(d).write.saveAsTable("mart.t2")
        """,
    )
    # raw.a belongs to the statement that reads it, raw.b may feed whatever combine() returns
    assert got == {"mart.t1": ["raw.a"], "mart.t2": ["raw.b"]}


def test_a_job_split_lists_every_read_of_its_outputs(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.sql("INSERT INTO mart.t1 SELECT * FROM raw.a")
        spark.sql("INSERT INTO mart.t2 SELECT * FROM raw.b")
        """,
        split_by="job",
    )
    assert list(got.values()) == [["raw.a", "raw.b"]]


def test_a_read_the_statement_reads_back_is_the_products_own_output(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        'spark.sql("INSERT INTO mart.t1 SELECT * FROM raw.a UNION ALL SELECT * FROM mart.t1")\n',
    )
    assert got == {"mart.t1": ["raw.a"]}


# ── DataFrames ────────────────────────────────────────────────────────


def test_a_write_whose_dataframe_is_made_of_reads_in_the_chain_lists_those(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.table("raw.a").join(spark.table("raw.b"), "k").write.saveAsTable("mart.t1")
        spark.read.parquet("s3://b/c").write.saveAsTable("mart.t2")
        spark.table("raw.d").write.saveAsTable("mart.t3")
        """,
    )
    assert got["mart.t1"] == ["raw.a", "raw.b"]
    assert len(got["mart.t2"]) == 1 and got["mart.t3"] == ["raw.d"]


def test_a_dataframe_is_followed_through_names_assigned_once(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        a = spark.table("raw.a")
        b = spark.table("raw.b")
        joined = a.join(b, "k").filter(F.col("x") > 1)
        joined.write.saveAsTable("mart.t1")
        a.write.saveAsTable("mart.t2")
        """,
    )
    assert got == {"mart.t1": ["raw.a", "raw.b"], "mart.t2": ["raw.a"]}


def test_a_name_that_is_assigned_twice_is_not_followed(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        d = spark.table("raw.a")
        d = spark.table("raw.b")
        d.write.saveAsTable("mart.t1")
        spark.table("raw.c").write.saveAsTable("mart.t2")
        """,
    )
    assert got["mart.t2"] == ["raw.c"]
    assert got["mart.t1"] == ["raw.a", "raw.b"]  # every read that nothing owns


def test_writeto_and_streams_are_followed_too(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.table("raw.a").writeTo("mart.t1").createOrReplace()
        (spark.readStream.format("kafka").option("subscribe", "in").load()
            .writeStream.format("iceberg").option("checkpointLocation", "s3://c/x")
            .toTable("mart.t2"))
        spark.table("raw.b").write.saveAsTable("mart.t3")
        """,
    )
    assert got["mart.t1"] == ["raw.a"] and got["mart.t3"] == ["raw.b"]
    assert got["mart.t2"] == ["in"]


def test_a_stream_through_a_writer_variable_with_branches_is_followed(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        raw = spark.readStream.format("kafka").option("subscribe", "in").load()
        writer = raw.writeStream.format("iceberg").option("checkpointLocation", "s3://c/x")
        if True:
            writer = writer.trigger(availableNow=True)
        else:
            writer = writer.trigger(processingTime="1 minute")
        writer.toTable("mart.t1")
        spark.table("raw.b").write.saveAsTable("mart.t2")
        """,
    )
    assert got["mart.t1"] == ["in"] and got["mart.t2"] == ["raw.b"]


def test_a_read_written_inside_a_chain_belongs_to_it_and_a_name_stays_shared(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.table("raw.inline").write.saveAsTable("mart.t1")
        shared = spark.table("raw.shared")
        shared.write.saveAsTable("mart.t2")
        combine(spark).write.saveAsTable("mart.t3")
        """,
    )
    # t3 may be fed by anything that is not another write's own: the shared read, not the inline one
    assert got == {
        "mart.t1": ["raw.inline"],
        "mart.t2": ["raw.shared"],
        "mart.t3": ["raw.shared"],
    }


def test_the_facts_say_which_reads_a_write_is_made_of(tmp_path):
    facts = facts_of(
        """
        spark.sql("INSERT INTO mart.t1 SELECT * FROM raw.a")
        spark.table("raw.b").write.saveAsTable("mart.t2")
        combine(spark).write.saveAsTable("mart.t3")
        """
    )
    assert [r.name for r in facts.reads] == ["raw.a", "raw.b"]
    assert [w.feeds for w in facts.writes] == [
        (io.read_key(facts.reads[0]),),
        (io.read_key(facts.reads[1]),),
        None,
    ]
    assert [r.exclusive for r in facts.reads] == [True, True]


def test_what_feeds_what_does_not_depend_on_the_order_the_reads_are_kept_in(tmp_path, monkeypatch):
    # on one line the reads are found in this order and kept in another (by name)
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.table("raw.z").write.saveAsTable("mart.t1"); spark.table("raw.a").write.saveAsTable("mart.t2")
        """,
    )
    assert got == {"mart.t1": ["raw.z"], "mart.t2": ["raw.a"]}


def test_a_read_seen_twice_is_shared_if_either_use_is(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.table("raw.a").write.saveAsTable("mart.t1"); d = spark.table("raw.a")
        combine(spark).write.saveAsTable("mart.t2")
        """,
    )
    assert got == {"mart.t1": ["raw.a"], "mart.t2": ["raw.a"]}


def test_two_writes_that_were_one_and_say_different_things_are_fed_by_anything(
    tmp_path, monkeypatch
):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.table("raw.a").write.saveAsTable("mart.t"); combine(spark).write.saveAsTable("mart.t")
        spark.table("raw.b")
        """,
    )
    assert got == {"mart.t": ["raw.a", "raw.b"]}


# ── what the code does not say, and the cost ──────────────────────────


def test_past_the_fan_out_limit_the_products_list_fewer_reads_and_the_report_says_so(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(_proposal, "MAX_FANOUT", 60)
    body = "\n".join(f'spark.table("raw.s{i}")' for i in range(10))
    body += "\n" + "\n".join(f'combine(spark).write.saveAsTable("mart.t{i}")' for i in range(10))
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "j.py").write_text(HEAD + body + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    contracts, report = PySparkImporter().import_to_contracts("jobs", options={})
    assert len(contracts) == 10 and all(len(c["consumes"]) == 6 for c in contracts)  # 10 x 6
    assert any("each lists at most" in n and "--split-by job" in n for n in report.notes)


@pytest.mark.parametrize(
    "counts, limit, expected",
    [
        ([100] * 10, 300, 30),
        ([100] * 10, 5_000, 500),
        ([3] * 10, 30, 500),  # the products are small: nothing to cut
        ([3] * 10, 29, None),
        ([100] * 10, 50, 5),
        ([100] * 10, 49, None),  # not even the smallest listing fits
        ([1, 1000], 100, 99),
        ([], 10, 500),
    ],
)
def test_the_listing_is_cut_to_what_fits(monkeypatch, counts, limit, expected):
    monkeypatch.setattr(_proposal, "MAX_FANOUT", limit)
    assert _proposal._fewer_reads(counts) == expected


def test_a_job_of_many_statements_is_not_quadratic(tmp_path, monkeypatch):
    """440 statements that each read two tables of their own list 880 reads, not 440 x 500."""
    body = "\n".join(
        f'spark.sql("INSERT OVERWRITE TABLE mart.t{i} SELECT * FROM raw.s{i} JOIN raw.u{i} USING (k)")'
        for i in range(440)
    )
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "j.py").write_text(HEAD + body + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    contracts, _report = PySparkImporter().import_to_contracts("jobs", options={})
    assert len(contracts) == 440
    assert sum(len(c.get("consumes", [])) for c in contracts) == 880


# ── what the walk accepts and what it does not: a shared read tells them apart ──
#
# `raw.x` is read into a name, so no write owns it: a write whose DataFrame the reader follows
# lists only the reads it is made of, and one it does not follow lists raw.x as well.

SHARED = 'x = spark.table("raw.x")\n'


def lineage(tmp_path, monkeypatch, body: str, write: str = '.write.saveAsTable("mart.t1")'):
    """The reads ``mart.t1`` lists, for a DataFrame built by ``body`` (``df`` is what it makes)."""
    source = textwrap.dedent(body).rstrip() + f"\ndf{write}\n"
    return consumed(
        tmp_path,
        monkeypatch,
        "def run(name, other, flag):\n" + textwrap.indent(SHARED + source, "    "),
    )["mart.t1"]


@pytest.mark.parametrize(
    "body, expected",
    [
        # followed: the reads it is made of
        ('df = spark.table("raw.c")', ["raw.c"]),
        ('df = F.broadcast(spark.table("raw.c"))', ["raw.c"]),
        ('df = spark.table("raw.c").join(other=spark.table("raw.d"), on="k")', ["raw.c", "raw.d"]),
        ('df = spark.table("raw.c")[spark.table("raw.d").k]', ["raw.c", "raw.d"]),
        ('df = spark.table("raw.c")[1:2]', ["raw.c"]),
        ('df = spark.table("raw.c").limit(flag)', ["raw.c"]),
        ('df = spark.table("raw.c") if flag else spark.table("raw.d")', ["raw.c", "raw.d"]),
        ('df = spark.table("raw.c").filter((F.col("a") > 1) & ~F.col("b").isNull())', ["raw.c"]),
        ('df = spark.table("raw.c").select(*[F.col("a"), F.col("b")])', ["raw.c"]),
        (
            'df = spark.table("raw.c").withColumn("m", F.create_map(F.lit("a"), F.lit(1)))',
            ["raw.c"],
        ),
        # not followed: the reads it is made of and every read no write owns
        ('df = spark.table("raw.c").join(other, "k")', ["raw.c", "raw.x"]),
        ('df = spark.table("raw.c").join(other=other, on="k")', ["raw.c", "raw.x"]),
        ("df = F.broadcast(other)", ["raw.x"]),
        ("df = spark.table(name)", ["raw.x"]),  # the read of `name` is not a read of this job
        ('df = spark.table("raw.c").join(spark.table(name), "k")', ["raw.c", "raw.x"]),
        ('df = fns["a"](spark.table("raw.c"))', ["raw.c", "raw.x"]),
        ('df = factory()(spark.table("raw.c"))', ["raw.c", "raw.x"]),
        ('df = (lambda t: t)(spark.table("raw.c"))', ["raw.c", "raw.x"]),
        ('df = spark.table("raw.c").transform(lib_fn)', ["raw.c", "raw.x"]),
        ('df = spark.table("raw.c").filter(lib_fn)', ["raw.c"]),  # a value from elsewhere: no data
        ('df = spark.table("raw.c").filter(lambda r: True)', ["raw.c", "raw.x"]),
        (
            'df = spark.table("raw.c").filter(local_fn)',
            ["raw.c", "raw.x"],
        ),  # a function of the file
        ('df = spark.table("raw.c").select(local_fn(other))', ["raw.c", "raw.x"]),
        ('df = spark.table("raw.c").select(LocalClass)', ["raw.c", "raw.x"]),
        ('df = spark.table("raw.c").join(LocalClass(), "k")', ["raw.c", "raw.x"]),
        ("df = holder.frame", ["raw.x"]),
        ("df = [t for t in other]", ["raw.x"]),
        ('df = {"a": spark.table("raw.c")}["a"]', ["raw.c"]),
        ('df = (spark.table("raw.c"), other)[0]', ["raw.c", "raw.x"]),
    ],
)
def test_what_the_walk_follows_and_what_it_does_not(tmp_path, monkeypatch, body, expected):
    prelude = (
        "from lib import lib_fn, factory, fns, holder\n"
        "def local_fn(*a):\n    return 1\n"
        "class LocalClass:\n    pass\n"
    )
    source = prelude + 'def run(spark, name, other, flag):\n    x = spark.table("raw.x")\n'
    source += (
        textwrap.indent(textwrap.dedent(body), "    ") + '\n    df.write.saveAsTable("mart.t1")\n'
    )
    got = consumed(tmp_path, monkeypatch, source)
    assert got["mart.t1"] == expected, body


def test_a_session_that_is_a_parameter_is_still_the_session(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        def run(spark):
            x = spark.table("raw.x")
            spark.table("raw.c").write.saveAsTable("mart.t1")
        """,
    )
    assert got == {"mart.t1": ["raw.c"]}


def test_a_name_the_file_changes_in_place_is_not_followed(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        x = spark.table("raw.x")
        d = spark.table("raw.c")
        d.append(1)
        d.write.saveAsTable("mart.t1")
        """,
    )
    assert got == {"mart.t1": ["raw.c", "raw.x"]}


def test_a_chain_whose_read_was_not_recorded_lists_what_no_write_owns(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        def run(name):
            shared = spark.table("raw.x")
            spark.table(name).write.saveAsTable("mart.t1")
        """,
    )
    assert got == {"mart.t1": ["raw.x"]}


def test_names_are_followed_to_the_depth_the_reader_goes_and_no_further(tmp_path, monkeypatch):
    monkeypatch.setattr(io._Reader, "_MAX_LINEAGE_DEPTH", 2)
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        x = spark.table("raw.x")
        a = spark.table("raw.a")
        b = a.limit(1)
        c = b.limit(2)
        d = c.limit(3)
        d.write.saveAsTable("mart.t1")
        """,
    )
    assert got == {"mart.t1": ["raw.a", "raw.x"]}  # too deep to follow: any read no write owns


# ── what leaves a statement or a chain open ───────────────────────────


def test_a_statement_whose_name_does_not_fold_lists_what_no_statement_owns(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        def run(env):
            x = spark.table("raw.x")
            spark.sql(f"INSERT INTO mart.t1 SELECT * FROM {env}_raw.a")
            spark.sql("INSERT INTO mart.t2 SELECT * FROM raw.c")
        """,
    )
    assert got == {"mart.t1": ["raw.x"], "mart.t2": ["raw.c"]}


@pytest.mark.parametrize(
    "call",
    [
        "spark.sql(text)",  # SQL that is not a constant
        'spark.sql("SELEKT nonsense FROM FROM raw.c")',  # SQL that does not parse
        'spark.table("v")',  # a temporary view: something else made it
        "spark.table(name)",  # a name that does not fold
    ],
)
def test_a_chain_whose_reads_are_not_all_known_is_open(tmp_path, monkeypatch, call):
    got = consumed(
        tmp_path,
        monkeypatch,
        f"""
        def run(text, name):
            x = spark.table("raw.x")
            x.createOrReplaceTempView("v")
            {call}.write.saveAsTable("mart.t1")
        """,
    )
    assert got == {"mart.t1": ["raw.x"]}


def test_a_statement_whose_reads_were_not_all_recorded_is_open(tmp_path, monkeypatch):
    monkeypatch.setattr(io, "MAX_REFS_PER_JOB", 2)
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        x = spark.table("raw.x")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM raw.a JOIN raw.b USING (k)")
        """,
    )
    # the second read of the job is the last that is kept: the statement's feeds are not all known
    assert got == {"mart.t1": ["raw.a", "raw.x"]}


def test_a_writeto_chain_lists_its_reads_and_not_every_read_no_write_owns(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        x = spark.table("raw.x")
        spark.table("raw.c").writeTo("mart.t1").createOrReplace()
        """,
    )
    assert got == {"mart.t1": ["raw.c"]}


def test_a_write_made_of_no_read_counts_none_and_lists_none(tmp_path, monkeypatch):
    monkeypatch.setattr(_proposal, "MAX_FANOUT", 10)
    reads = "\n".join(f'r{i} = spark.table("raw.s{i}")' for i in range(8))
    writes = "\n".join(f'spark.range({i}).write.saveAsTable("mart.t{i}")' for i in range(8))
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "j.py").write_text(HEAD + reads + "\n" + writes + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    contracts, report = PySparkImporter().import_to_contracts("jobs", options={})
    assert len(contracts) == 8 and not any(c.get("consumes") for c in contracts)
    assert not any("each lists at most" in note for note in report.notes)  # nothing is listed


def test_the_number_of_reads_counted_is_the_number_listed(tmp_path, monkeypatch):
    seen = []
    original = _proposal._read_count

    def counted(group, job):
        found = original(group, job)
        seen.append((found, len(_proposal._reads_of(group, job))))
        return found

    monkeypatch.setattr(_proposal, "_read_count", counted)
    consumed(
        tmp_path,
        monkeypatch,
        """
        x = spark.table("raw.x")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM raw.a")
        spark.table("raw.b").write.saveAsTable("mart.t2")
        combine(x).write.saveAsTable("mart.t3")
        spark.range(1).write.saveAsTable("mart.t4")
        """,
    )
    assert seen and all(counted == listed for counted, listed in seen), seen


def test_the_note_about_a_job_is_for_outputs_the_code_does_not_account_for(tmp_path, monkeypatch):
    def notes(body: str):
        (tmp_path / "jobs").mkdir(exist_ok=True)
        (tmp_path / "jobs" / "j.py").write_text(HEAD + textwrap.dedent(body), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        _contracts, report = PySparkImporter().import_to_contracts("jobs", options={})
        return [n for n in report.notes if "which input feeds which output" in n]

    exact = textwrap.dedent(
        """
        x = spark.table("raw.x")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM raw.a")
        spark.sql("INSERT INTO mart.t2 SELECT * FROM raw.b")
        """
    )
    assert notes(exact) == []  # each says what feeds it
    assert len(notes(exact + 'combine(x).write.saveAsTable("mart.t3")\n')) == 1


def test_what_is_followed_is_bounded_in_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr(io._Reader, "_MAX_LINEAGE_NODES", 6)
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        x = spark.table("raw.x")
        spark.table("raw.a").select("a", "b", "c", "d", "e", "f", "g").write.saveAsTable("mart.t1")
        """,
    )
    assert got == {"mart.t1": ["raw.a", "raw.x"]}  # too long to follow


# ── order, and what a product that has both kinds of output lists ─────


def listed_in_order(root: Path, monkeypatch, body: str, **options) -> Dict[str, List[str]]:
    (root / "jobs").mkdir(exist_ok=True)
    (root / "jobs" / "j.py").write_text(HEAD + textwrap.dedent(body), encoding="utf-8")
    monkeypatch.chdir(root)
    contracts, _report = PySparkImporter().import_to_contracts("jobs", options=options)
    return {c["id"]: [x["productId"] for x in c.get("consumes", [])] for c in contracts}


def test_what_a_product_lists_is_in_the_order_the_job_reads_in(tmp_path, monkeypatch):
    reads = "\n".join(f'r{i} = spark.table("raw.s{i}")' for i in range(8))
    joined = "r7" + "".join(f".join(r{i}, 'k')" for i in range(6, -1, -1))
    got = listed_in_order(
        tmp_path, monkeypatch, reads + f'\n{joined}.write.saveAsTable("mart.t1")\n'
    )
    assert got == {"mart.t1": [f"raw.s{i}" for i in range(8)]}


def test_a_product_with_an_output_the_code_accounts_for_and_one_it_does_not_lists_both_kinds(
    tmp_path, monkeypatch
):
    got = listed_in_order(
        tmp_path,
        monkeypatch,
        """
        x = spark.table("raw.x")
        spark.table("raw.a").write.saveAsTable("mart.t1")
        combine(x).write.saveAsTable("mart.t2")
        """,
        split_by="job",
    )
    assert list(got.values()) == [["raw.x", "raw.a"]]  # the pool, and the read that is its own


def test_a_read_of_one_table_on_two_lines_is_two_reads(tmp_path, monkeypatch):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "j.py").write_text(
        HEAD
        + 'a = spark.table("raw.a")\nb = spark.table("raw.a")\ncombine(a, b).write.saveAsTable("mart.t1")\n'
        + 'spark.table("raw.q").write.saveAsTable("mart.t2")\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    _contracts, report = PySparkImporter().import_to_contracts("jobs", options={"split_by": "job"})
    lines = [x for x in report.mapped_one_to_one if x.startswith("read raw.a")]
    assert len(lines) == 1 and ":4)" in lines[0], lines  # the first of them is the one listed


def test_a_key_a_write_names_that_the_job_does_not_read_is_not_counted(tmp_path):
    from fluid_build.cli.import_workflow._fold import Folder
    from fluid_build.cli.import_workflow._pysource import parse_module
    from fluid_build.cli.import_workflow._scan import JobRecord

    module = parse_module("j.py", (HEAD + 'x = spark.table("raw.a")\n').encode("utf-8"))
    facts = io.read_job(module, Folder(module))
    job = JobRecord(rel="j.py", path=tmp_path / "j.py", shown="j.py", facts=facts)
    real = io.read_key(facts.reads[0])
    ghost = ("table", ("ghost",), "ghost", 99)
    identity = _proposal.Identity("table", "t1", database="mart")
    write = io.Write("table", ("mart", "t1"), "mart.t1", "", "j.py", 5, feeds=(real, ghost))
    output = _proposal.Output(identity, job, write)
    group = _proposal.Group("retail.t1", [output])
    assert [r.name for r in _proposal._reads_of(group, job)] == ["raw.a"]
    assert _proposal._read_count(group, job) == 1


# ── what a review of the first version found ─────────────────────────────────


def with_conventions(root: Path, monkeypatch, body: str, conventions: str, **options):
    (root / "conventions.yaml").write_text(conventions, encoding="utf-8")
    return consumed(root, monkeypatch, body, conventions="conventions.yaml", **options)


def test_a_scalar_made_of_a_read_through_a_name_that_is_assigned_twice_still_feeds(
    tmp_path, monkeypatch
):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        cutoff = spark.table("raw.b").agg(F.max("d")).first()[0]
        if x:
            cutoff = spark.table("raw.b2").agg(F.max("d")).first()[0]
        a = spark.table("raw.a")
        a.where(F.col("d") > cutoff).write.saveAsTable("mart.t1")
        spark.table("raw.c").write.saveAsTable("mart.t2")
        spark.table("raw.d").count()
        """,
    )
    # the write is made of what it names (and the name of every assignment), not of every read
    assert got == {"mart.t1": ["raw.a", "raw.b", "raw.b2"], "mart.t2": ["raw.c"]}


@pytest.mark.parametrize(
    "setup",
    [
        'keys = spark.table("raw.b").select("k").collect()\nkeys = [k[0] for k in keys]',
        'lo, hi = spark.table("raw.b").agg(F.min("d"), F.max("d")).first()\nkeys = [lo, hi]',
        'for k in spark.table("raw.b").select("k").collect():\n    keys = k',
        'cfg = {}\ncfg["c"] = spark.table("raw.b").agg(F.max("d")).first()[0]\nkeys = cfg["c"]',
    ],
)
def test_a_scalar_made_of_a_read_in_a_way_that_is_not_followed_lists_the_read_on_the_product(
    tmp_path, monkeypatch, setup
):
    got = consumed(
        tmp_path,
        monkeypatch,
        setup
        + """
a = spark.table("raw.a")
a.where(F.col("k").isin(keys)).write.saveAsTable("mart.t1")
spark.table("raw.c").write.saveAsTable("mart.t2")
""",
    )
    assert "raw.b" in got["mart.t1"] and "raw.a" in got["mart.t1"], got
    assert got["mart.t2"] == ["raw.c"]


def test_a_value_that_comes_in_as_a_parameter_or_is_not_bound_is_taken_as_a_value(
    tmp_path, monkeypatch
):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        def build(day):
            spark.table("raw.a").where(F.col("d") == day).write.saveAsTable("mart.t1")


        build(date.today())
        spark.table("raw.c").write.saveAsTable("mart.t2")
        """,
    )
    assert got == {"mart.t1": ["raw.a"], "mart.t2": ["raw.c"]}


def test_a_statement_that_reads_a_global_temporary_view_is_not_closed(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.table("raw.b").createGlobalTempView("gv")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM global_temp.gv JOIN raw.a ON 1=1")
        spark.sql("INSERT INTO mart.t2 SELECT * FROM raw.c")
        """,
    )
    assert got == {"mart.t1": ["raw.a", "raw.b"], "mart.t2": ["raw.c"]}


def test_a_statement_that_reads_a_view_a_statement_made_global_is_not_closed(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.sql("CREATE GLOBAL TEMPORARY VIEW gv AS SELECT * FROM raw.b")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM global_temp.gv JOIN raw.a ON 1=1")
        spark.sql("INSERT INTO mart.t2 SELECT * FROM raw.c")
        """,
    )
    assert got == {"mart.t1": ["raw.a", "raw.b"], "mart.t2": ["raw.c"]}


def test_a_view_registered_under_a_name_the_file_does_not_give_may_be_read_by_a_bare_name(
    tmp_path, monkeypatch
):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        def reg(df, name):
            df.createOrReplaceTempView(name)


        reg(spark.table("raw.b"), "v")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM v JOIN raw.a ON 1=1")
        spark.sql("INSERT INTO mart.t2 SELECT * FROM raw.c")
        """,
    )
    assert {"raw.a", "raw.b"} <= set(got["mart.t1"]) and got["mart.t2"] == ["raw.c"]


def test_a_view_the_file_registers_below_the_statement_that_reads_it_is_still_a_view(
    tmp_path, monkeypatch
):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        def go():
            spark.sql("INSERT INTO mart.t1 SELECT * FROM v JOIN raw.a ON 1=1")


        spark.sql("CREATE TEMP VIEW v AS SELECT * FROM raw.b")
        spark.sql("INSERT INTO mart.t2 SELECT * FROM raw.c")
        go()
        """,
    )
    assert got == {"mart.t1": ["raw.a", "raw.b"], "mart.t2": ["raw.c"]}


def test_a_table_a_memory_sink_or_a_cache_names_is_a_view_to_sql(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.readStream.table("raw.s").writeStream.format("memory").queryName("m").start()
        spark.sql("INSERT INTO mart.t1 SELECT * FROM m JOIN raw.a ON 1=1")
        spark.sql("CACHE TABLE v AS SELECT * FROM raw.b")
        spark.sql("INSERT INTO mart.t2 SELECT * FROM v JOIN raw.c ON 1=1")
        """,
    )
    # neither statement is closed (each reads a view), so each lists what no write owns; and
    # neither view is listed as a table
    assert got == {
        "mart.t1": ["raw.a", "raw.b", "raw.c", "raw.s"],
        "mart.t2": ["raw.a", "raw.b", "raw.c", "raw.s"],
    }


def test_broadcast_imported_by_name_does_not_hide_the_data_it_wraps(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        from pyspark.sql.functions import broadcast as bc


        def build(src):
            spark.table("raw.a").join(bc(src), "k").write.saveAsTable("mart.t1")


        build(spark.table("raw.b"))
        spark.table("raw.c").write.saveAsTable("mart.t2")
        """,
    )
    assert got == {"mart.t1": ["raw.a", "raw.b"], "mart.t2": ["raw.c"]}


def test_the_keyword_frames_of_spark_sql_are_data(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        def build(src):
            spark.sql("SELECT * FROM {s} JOIN raw.x ON 1=1", s=src).write.saveAsTable("mart.t1")


        build(spark.table("raw.b"))
        spark.table("raw.c").write.saveAsTable("mart.t2")
        """,
    )
    assert "raw.b" in got["mart.t1"] and got["mart.t2"] == ["raw.c"]


IGNORE = "defaults: {domain: retail, platform: aws}\nignore: {tables: ['^tmp_']}\n"


def test_what_feeds_a_write_the_conventions_ignore_feeds_what_reads_it_sql(tmp_path, monkeypatch):
    got = with_conventions(
        tmp_path,
        monkeypatch,
        """
        spark.sql("INSERT INTO tmp_stage SELECT * FROM raw.a")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM tmp_stage JOIN raw.b ON 1=1")
        spark.sql("INSERT INTO mart.t2 SELECT * FROM raw.c")
        """,
        IGNORE,
    )
    assert got == {"mart.t1": ["raw.a", "raw.b"], "mart.t2": ["raw.c"]}


def test_what_feeds_a_write_the_conventions_ignore_feeds_what_reads_it_dataframes(
    tmp_path, monkeypatch
):
    got = with_conventions(
        tmp_path,
        monkeypatch,
        """
        spark.table("raw.a").write.saveAsTable("tmp_stage")
        spark.table("tmp_stage").write.saveAsTable("mart.t1")
        spark.table("raw.c").write.saveAsTable("mart.t2")
        """,
        IGNORE,
    )
    assert got == {"mart.t1": ["raw.a"], "mart.t2": ["raw.c"]}


def test_a_chain_of_ignored_writes_is_followed_to_the_end(tmp_path, monkeypatch):
    got = with_conventions(
        tmp_path,
        monkeypatch,
        """
        spark.sql("INSERT INTO tmp_a SELECT * FROM raw.a")
        spark.sql("INSERT INTO tmp_b SELECT * FROM tmp_a")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM tmp_b")
        """,
        IGNORE,
    )
    assert got == {"mart.t1": ["raw.a"]}


def test_what_only_feeds_an_ignored_write_nobody_reads_is_on_no_product(tmp_path, monkeypatch):
    got = with_conventions(
        tmp_path,
        monkeypatch,
        """
        spark.sql("INSERT INTO tmp_export SELECT * FROM raw.a")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM raw.c")
        """,
        IGNORE,
    )
    assert got == {"mart.t1": ["raw.c"]}


def test_a_list_built_by_additions_is_not_taken_for_a_constant_of_the_module(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        cols = ["x"]


        def build():
            cols = ["a"]
            cols += [spark.table("raw.b").first()[0]]
            spark.table("raw.a").select(*cols).write.saveAsTable("mart.t1")


        build()
        spark.table("raw.c").write.saveAsTable("mart.t2")
        """,
    )
    assert got == {"mart.t1": ["raw.a", "raw.b"], "mart.t2": ["raw.c"]}


def test_a_memory_sink_with_a_name_the_file_does_not_give_makes_a_bare_name_uncertain(
    tmp_path, monkeypatch
):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        def sink(df, name):
            df.writeStream.format("memory").queryName(name).start()


        sink(spark.readStream.table("raw.s"), "m")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM m JOIN raw.a ON 1=1")
        spark.sql("INSERT INTO mart.t2 SELECT * FROM raw.c")
        spark.table("raw.d").count()
        """,
    )
    assert {"raw.a", "raw.s", "raw.d"} <= set(got["mart.t1"]) and got["mart.t2"] == ["raw.c"]


def test_a_sink_that_is_not_a_memory_sink_names_no_view(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.readStream.table("raw.s").writeStream.format("parquet").queryName("q").start("s3://o/")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM q JOIN raw.a ON 1=1")
        spark.table("raw.d").count()
        """,
    )
    assert got["mart.t1"] == ["q", "raw.a"]  # closed: it lists what the statement names, no more


def test_a_write_the_conventions_ignore_that_does_not_say_what_feeds_it_makes_what_reads_it_loose(
    tmp_path, monkeypatch
):
    got = with_conventions(
        tmp_path,
        monkeypatch,
        """
        def build():
            return spark.table("raw.a")


        build().write.saveAsTable("tmp_stage")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM tmp_stage JOIN raw.b ON 1=1")
        spark.table("raw.d").count()
        """,
        IGNORE,
    )
    assert got == {"mart.t1": ["raw.a", "raw.b", "raw.d"]}


def test_the_reads_that_only_feed_an_ignored_write_may_feed_a_product_that_does_not_say(
    tmp_path, monkeypatch
):
    got = with_conventions(
        tmp_path,
        monkeypatch,
        """
        def build():
            return spark.table("raw.c")


        spark.sql("INSERT INTO tmp_stage SELECT * FROM raw.a")
        build().write.saveAsTable("mart.t1")
        """,
        IGNORE,
    )
    assert got == {"mart.t1": ["raw.a", "raw.c"]}


def test_two_ignored_writes_of_one_table_both_feed_what_reads_it(tmp_path, monkeypatch):
    got = with_conventions(
        tmp_path,
        monkeypatch,
        """
        spark.sql("INSERT INTO tmp_stage SELECT * FROM raw.a")
        spark.sql("INSERT INTO tmp_stage SELECT * FROM raw.b")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM tmp_stage")
        """,
        IGNORE,
    )
    assert got == {"mart.t1": ["raw.a", "raw.b"]}


def test_ignored_writes_that_feed_each_other_do_not_loop(tmp_path, monkeypatch):
    got = with_conventions(
        tmp_path,
        monkeypatch,
        """
        spark.sql("INSERT INTO tmp_a SELECT * FROM tmp_b JOIN raw.a ON 1=1")
        spark.sql("INSERT INTO tmp_b SELECT * FROM tmp_a JOIN raw.b ON 1=1")
        spark.sql("INSERT INTO mart.t1 SELECT * FROM tmp_a")
        """,
        IGNORE,
    )
    assert got == {"mart.t1": ["raw.a", "raw.b"]}
