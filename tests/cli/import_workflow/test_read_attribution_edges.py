# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Which reads feed which output: the cases a second review found.

Each case is a job that says what feeds an output in a way the first reading did not follow, so
the product listed nothing it should have, or a name that is not a table. A product is allowed to
list more than it needs (a person removes it); it is not allowed to list less.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Dict, List

import pytest

from fluid_build.cli.import_workflow import _proposal
from fluid_build.cli.import_workflow import _spark_sql as sql
from fluid_build.cli.import_workflow.pyspark import PySparkImporter

HEAD = "from pyspark.sql import SparkSession\nfrom pyspark.sql import functions as F\n"
HEAD += "spark = SparkSession.builder.getOrCreate()\n"
IGNORE_PATHS = "defaults: {domain: retail, platform: aws}\nignore: {paths: ['/stg/']}\n"
IGNORE_TABLES = "defaults: {domain: retail, platform: aws}\nignore: {tables: ['^tmp_']}\n"


def consumed(root: Path, monkeypatch, body: str, conventions: str = "") -> Dict[str, List[str]]:
    """``{product id: sorted ids of what it consumes}`` for a job made of ``body``."""
    (root / "jobs").mkdir(exist_ok=True)
    (root / "jobs" / "j.py").write_text(HEAD + textwrap.dedent(body), encoding="utf-8")
    options = {}
    if conventions:
        (root / "conventions.yaml").write_text(conventions, encoding="utf-8")
        options["conventions"] = "conventions.yaml"
    monkeypatch.chdir(root)
    contracts, _report = PySparkImporter().import_to_contracts("jobs", options=options)
    return {c["id"]: sorted(x["productId"] for x in c.get("consumes", [])) for c in contracts}


# ── a path the conventions ignore, written and read under two spellings ──────────────────────


@pytest.mark.parametrize(
    "written, read",
    [
        ("s3://b/stg/x", "s3://b/stg/x"),
        ("s3://b/stg/x", "s3a://b/stg/x"),
        ("s3://b/stg/x", "s3n://b/stg/x/"),
        ("s3://b/stg/x/", "s3://b/stg/x/*"),
        ("s3://b/stg/x", "s3://b/stg/x/dt=1"),
        ("s3://b/stg/x", "s3://b/stg/x/dt=1/part-*.parquet"),
        ("gs://b/stg/x", "gcs://b/stg/x/"),
        ("/data/stg/x", "file:///data/stg/x"),
        ("file:///data/stg/x", "/data/stg/x"),
        ("/data/stg/x", "/data/stg/x/*"),
        ("/data/stg/x", "/data/stg/x/dt=1"),
    ],
)
def test_a_staging_path_is_found_through_another_spelling_of_it(
    tmp_path, monkeypatch, written, read
):
    got = consumed(
        tmp_path,
        monkeypatch,
        f"""
        spark.table("raw.a").write.parquet("{written}")
        spark.read.parquet("{read}").write.saveAsTable("mart.y")
        spark.table("raw.other").write.saveAsTable("mart.z")
        spark.table("raw.third").count()
        """,
        IGNORE_PATHS,
    )
    # found by its spelling: not by listing everything the job reads
    assert got == {"mart.y": ["raw.a"], "mart.z": ["raw.other"]}


def test_a_staging_path_in_another_bucket_is_not_the_one_that_was_written(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.table("raw.a").write.parquet("s3://one/stg/x")
        spark.table("raw.b").write.parquet("s3://two/stg/x")
        spark.read.parquet("s3://two/stg/x").write.saveAsTable("mart.y")
        spark.table("raw.other").write.saveAsTable("mart.z")
        """,
        IGNORE_PATHS,
    )
    assert got["mart.y"] == ["raw.b"] and got["mart.z"] == ["raw.other"]


def test_the_directory_that_holds_a_staging_path_is_its_start_not_its_end(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.table("raw.a").write.parquet("/data/stg/x/dt=1")
        spark.table("raw.b").write.parquet("/data/stg/y")
        spark.read.parquet("/data/stg/x").write.saveAsTable("mart.y")
        spark.table("raw.other").write.saveAsTable("mart.z")
        """,
        IGNORE_PATHS,
    )
    # the directory read is above the one written: nothing says which write made it, so the
    # product may be fed by what any write of the job reads
    assert {"raw.a", "raw.b"} <= set(got["mart.y"]) and got["mart.z"] == ["raw.other"]


def test_a_read_of_an_ignored_path_that_no_written_path_matches_makes_the_product_loose(
    tmp_path, monkeypatch
):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.table("raw.a").write.parquet("s3://b/stg/x")
        spark.read.parquet("hdfs://nn:8020/stg/x").write.saveAsTable("mart.y")
        spark.table("raw.other").write.saveAsTable("mart.z")
        """,
        IGNORE_PATHS,
    )
    assert got == {"mart.y": ["raw.a"], "mart.z": ["raw.other"]}


def test_a_path_that_does_not_fold_is_not_the_root_path_a_job_wrote(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        import os

        spark.table("raw.a").write.parquet("/")
        spark.read.parquet(os.environ["X"]).write.saveAsTable("mart.y")
        spark.table("raw.other").write.saveAsTable("mart.z")
        """,
        "defaults: {domain: retail, platform: aws}\nignore: {paths: ['^/$']}\n",
    )
    assert "raw.a" not in got["mart.y"] and got["mart.z"] == ["raw.other"]


def test_a_read_of_a_path_that_is_listed_is_not_loose_for_want_of_a_bridge(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.table("raw.a").write.parquet("s3://b/stg/x")
        spark.read.parquet("s3://b/in/x").write.saveAsTable("mart.y")
        spark.table("raw.other").write.saveAsTable("mart.z")
        """,
        IGNORE_PATHS,
    )
    assert "raw.a" not in got["mart.y"] and got["mart.z"] == ["raw.other"]


def test_a_job_that_writes_no_ignored_path_does_not_make_an_ignored_read_loose(
    tmp_path, monkeypatch
):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.read.parquet("s3://b/stg/x").write.saveAsTable("mart.y")
        spark.table("raw.other").write.saveAsTable("mart.z")
        spark.table("raw.third").count()
        """,
        IGNORE_PATHS,
    )
    assert got == {"mart.y": [], "mart.z": ["raw.other"]}


@pytest.mark.parametrize(
    "text, key",
    [
        ("s3://b/x", ("s3", "b", "x")),
        ("s3a://b/x/", ("s3", "b", "x")),
        ("S3N://B/X", ("s3", "b", "x")),
        ("gcs://b/x", ("gs", "b", "x")),
        ("abfss://c@h.dfs.core.windows.net/x", ("abfs", "c@h.dfs.core.windows.net", "x")),
        ("file:///d/x", ("", "", "d", "x")),
        ("file:/d/x", ("", "", "d", "x")),
        ("/d/x", ("", "", "d", "x")),
        ("/d/x/*", ("", "", "d", "x")),
        ("/d/*/x", ("", "", "d")),
        ("/d/x/dt=1", ("", "", "d", "x", "dt=1")),
        ("/d/x?versionId=3", ("", "", "d", "x")),
        ("/d/./x//y", ("", "", "d", "x", "y")),
        ("s3://b", ("s3", "b")),
        ("/", ("", "")),
    ],
)
def test_the_key_of_a_path(text, key):
    assert _proposal._path_key(text) == key


def test_a_very_long_path_is_known_by_where_it_starts():
    key = _proposal._path_key("/" + "/".join(f"d{n}" for n in range(500)))
    assert len(key) == 2 + _proposal._MAX_PATH_SEGMENTS and key[2] == "d0"


# ── an ignored table written by a write that says what feeds it and one that does not ──────


@pytest.mark.parametrize("closed_first", [True, False])
def test_an_ignored_table_written_two_ways_keeps_what_feeds_it_either_way(
    tmp_path, monkeypatch, closed_first
):
    closed = 'spark.sql("INSERT INTO tmp_s1 SELECT * FROM raw.a")\n'
    unsaid = 'mystery(spark.table("raw.b")).write.saveAsTable("tmp_s1")\n'
    got = consumed(
        tmp_path,
        monkeypatch,
        (closed + unsaid if closed_first else unsaid + closed)
        + 'spark.sql("INSERT INTO mart.y SELECT * FROM tmp_s1")\n'
        + 'spark.table("raw.d").count()\n',
        IGNORE_TABLES,
    )
    assert got == {"mart.y": ["raw.a", "raw.b", "raw.d"]}


def test_what_two_ignored_writes_of_one_table_say_is_the_union_when_both_say(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.sql("INSERT INTO tmp_s1 SELECT * FROM raw.a")
        spark.table("raw.b").write.saveAsTable("tmp_s1")
        spark.sql("INSERT INTO mart.y SELECT * FROM tmp_s1")
        spark.sql("INSERT INTO mart.z SELECT * FROM raw.c")
        """,
        IGNORE_TABLES,
    )
    assert got == {"mart.y": ["raw.a", "raw.b"], "mart.z": ["raw.c"]}


# ── a scalar made by calling a name that holds a function ──────────────────────────────────


@pytest.mark.parametrize(
    "setup",
    [
        'f = lambda: spark.table("raw.cfg").first()[0]\n',
        'def get():\n    return spark.table("raw.cfg").first()[0]\n\n\nf = get\n',
        "import functools\n\n\n"
        'def get(n):\n    return spark.table("raw.cfg").first()[n]\n\n\n'
        "f = functools.partial(get, 0)\n",
        'def get():\n    return spark.table("raw.cfg").first()[0]\n\n\n' "g = get\nf = g\n",
    ],
    ids=["lambda", "alias of a function", "partial", "alias of an alias"],
)
def test_a_scalar_made_by_calling_a_name_that_holds_a_function_still_feeds(
    tmp_path, monkeypatch, setup
):
    got = consumed(
        tmp_path,
        monkeypatch,
        setup
        + 'spark.table("raw.a").limit(f()).write.saveAsTable("mart.y")\n'
        + 'spark.table("raw.c").write.saveAsTable("mart.z")\n',
    )
    assert got == {"mart.y": ["raw.a", "raw.cfg"], "mart.z": ["raw.c"]}


def test_a_scalar_made_by_calling_a_name_that_holds_a_value_is_still_a_value(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        import os

        limit = os.getenv
        spark.table("raw.a").limit(int(limit("N", "5"))).write.saveAsTable("mart.y")
        spark.table("raw.c").write.saveAsTable("mart.z")
        """,
    )
    assert got == {"mart.y": ["raw.a"], "mart.z": ["raw.c"]}


def test_a_frame_made_by_sql_that_does_not_fold_leaves_its_write_loose(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        import os

        df = spark.sql(os.environ["Q"])
        df.join(spark.table("raw.b"), "k").write.saveAsTable("mart.y")
        spark.table("raw.c").write.saveAsTable("mart.z")
        spark.table("raw.d").count()
        """,
    )
    # what the text reads is not known: the product lists what no write owns, not only raw.b
    assert got == {"mart.y": ["raw.b", "raw.d"], "mart.z": ["raw.c"]}


# ── a DataFrame given to a method that takes one ────────────────────────────────────────


@pytest.mark.parametrize(
    "use",
    [
        "spark.table('raw.a').lateralJoin(extra)",
        "spark.table('raw.a').join(extra, 'k')",
        "spark.table('raw.a').where(extra.exists())",
        "spark.table('raw.a').where(F.col('k') == extra.scalar())",
        "spark.table('raw.a').select(extra.asTable())",
    ],
)
def test_a_dataframe_that_a_parameter_brings_is_data_for_the_methods_that_take_one(
    tmp_path, monkeypatch, use
):
    got = consumed(
        tmp_path,
        monkeypatch,
        f"""
        def build(extra):
            out = {use}
            out.write.saveAsTable("mart.m")
            spark.table("raw.c").write.saveAsTable("mart.n")


        build(spark.read.parquet("s3://b/raw/b"))
        """,
    )
    # the frame comes from the caller: the product lists what no write owns, which has its reads
    assert "b.raw.b" in got["mart.m"] and got["mart.n"] == ["raw.c"]


def test_a_parameter_that_is_a_value_stays_a_value(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        def build(n, column):
            spark.table("raw.a").limit(n).select(column).write.saveAsTable("mart.m")
            spark.table("raw.c").write.saveAsTable("mart.n")


        build(10, "k")
        """,
    )
    assert got == {"mart.m": ["raw.a"], "mart.n": ["raw.c"]}


# ── a stream sink whose format is not a constant ────────────────────────────────────────


def test_a_stream_sink_of_an_unknown_format_that_names_a_topic_is_still_a_write(
    tmp_path, monkeypatch
):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "j.py").write_text(
        HEAD
        + textwrap.dedent(
            """
            import os

            FMT = os.environ["SINK_FORMAT"]
            (
                spark.readStream.table("raw.events")
                .writeStream.format(FMT)
                .option("kafka.bootstrap.servers", "broker:9092")
                .option("topic", "orders_out")
                .option("checkpointLocation", "/chk/x")
                .start()
            )
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    _contracts, report = PySparkImporter().import_to_contracts("jobs", options={})
    # not dropped: the unresolved write is reported, as it was before the format was looked at
    assert any("no path is given" in line for line in report.unsupported), report.unsupported


@pytest.mark.parametrize(
    "option",
    [
        "path",
        "table",
        "dbtable",
        "url",
        "topic",
        "kafka.bootstrap.servers",
        "es.resource",
        "collection",
    ],
)
def test_a_stream_sink_of_an_unknown_format_with_a_target_option_is_not_taken_for_a_memory_sink(
    tmp_path, monkeypatch, option
):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "j.py").write_text(
        HEAD
        + textwrap.dedent(
            f"""
            import os

            spark.readStream.table("raw.events").writeStream.format(os.environ["F"]).option(
                "{option}", "x"
            ).start()
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    _contracts, report = PySparkImporter().import_to_contracts("jobs", options={})
    assert not any("taken for a memory or console sink" in line for line in report.notes)


@pytest.mark.parametrize(
    "options",
    ["", '.option("checkpointLocation", "/chk/x")', '.option("queryName", "q")']
    + [
        '.option("numRows", "5")',
        '.option("truncate", "false")',
        '.option("checkpointLocation", "/chk/x").option("queryName", "q")',
    ],
)
def test_a_stream_sink_of_an_unknown_format_with_no_target_is_taken_for_a_memory_sink_and_said(
    tmp_path, monkeypatch, options
):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "j.py").write_text(
        HEAD
        + textwrap.dedent(
            f"""
            import os

            spark.readStream.table("raw.events").writeStream.format(os.environ["F"]){options}.start()
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    _contracts, report = PySparkImporter().import_to_contracts("jobs", options={})
    assert any("taken for a memory or console sink" in line for line in report.notes), report.notes


# ── a sink the reader cannot follow, and the name of a memory sink given as an option ─────────

AFTER_SINK = """
spark.sql("INSERT INTO mart.y SELECT * FROM m1")
spark.table("raw.d").write.saveAsTable("mart.z")
"""


@pytest.mark.parametrize(
    "sink",
    [
        'w = spark.readStream.table("raw.a").writeStream\n'
        'w = w.format("memory")\nw = w.queryName("m1")\nw.start()\n',
        'w = spark.readStream.table("raw.a").writeStream.format("memory").queryName("m1")\n'
        "(w if c else w).start()\n",
        'ws = {"x": spark.readStream.table("raw.a").writeStream.format("memory").queryName("m1")}\n'
        'ws["x"].start()\n',
        'fmt = "console"\nif c:\n    fmt = "memory"\n'
        'spark.readStream.table("raw.a").writeStream.format(fmt).queryName("m1").start()\n',
        'spark.readStream.table("raw.a").writeStream.format("memory").queryName(name).start()\n',
        'import os\nspark.readStream.table("raw.a").writeStream.queryName("m1")'
        '.start(format=os.environ["F"])\n',
    ],
    ids=[
        "reassigned writer",
        "picked by a condition",
        "in a container",
        "format",
        "name",
        "format argument",
    ],
)
def test_a_memory_sink_the_reader_cannot_follow_leaves_a_bare_name_uncertain(
    tmp_path, monkeypatch, sink
):
    got = consumed(tmp_path, monkeypatch, sink + AFTER_SINK)
    assert "raw.a" in got["mart.y"] and got["mart.z"] == ["raw.d"]


def test_a_start_in_a_file_that_writes_no_stream_does_not_make_a_name_uncertain(
    tmp_path, monkeypatch
):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        import threading

        threading.Thread(target=print).start()
        spark.sql("INSERT INTO mart.y SELECT * FROM m1")
        spark.table("raw.d").write.saveAsTable("mart.z")
        spark.table("raw.third").count()
        """,
    )
    assert got == {"mart.y": ["m1"], "mart.z": ["raw.d"]}  # closed: no read of raw.third


@pytest.mark.parametrize(
    "sink",
    [
        'spark.readStream.table("raw.s").writeStream.format("memory").option("queryName", "m1")'
        ".start()\n",
        'spark.readStream.table("raw.s").writeStream.format("memory").option("QueryName", "m1")'
        ".start()\n",
        'spark.readStream.table("raw.s").writeStream.format("memory").options(queryName="m1")'
        ".start()\n",
        'spark.readStream.table("raw.s").writeStream.format("memory").start(queryName="m1")\n',
        'spark.readStream.table("raw.s").writeStream.format("memory").queryName("m1").start()\n',
        'spark.readStream.table("raw.s").writeStream.queryName("m1").format("memory").start()\n',
        'spark.readStream.table("raw.s").writeStream.start(format="memory", queryName="m1")\n',
    ],
)
def test_the_query_name_of_a_memory_sink_is_a_view_however_it_is_given(tmp_path, monkeypatch, sink):
    got = consumed(tmp_path, monkeypatch, sink + AFTER_SINK)
    # the statement reads a view: it is not closed, and the view is not listed as a table
    assert got["mart.y"] == ["raw.s"] and got["mart.z"] == ["raw.d"]


def test_a_sink_with_a_path_and_a_format_that_is_not_a_constant_is_still_a_write(
    tmp_path, monkeypatch
):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        import os

        spark.readStream.table("raw.s").writeStream.format(os.environ["F"]).option(
            "path", "s3://out/x"
        ).option("checkpointLocation", "s3://cp/x").start()
        spark.table("raw.d").write.saveAsTable("mart.z")
        """,
    )
    assert got["out.x"] == ["raw.s"] and got["mart.z"] == ["raw.d"]


def test_a_sink_whose_last_format_is_not_memory_names_no_view(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        'spark.readStream.table("raw.s").writeStream.format("memory").format("parquet")'
        '.queryName("m1").start("s3://o/x")\n' + AFTER_SINK,
    )
    assert got["mart.y"] == ["m1"] and got["mart.z"] == ["raw.d"]


# ── a view registered with the name as a keyword ───────────────────────────────────────────


@pytest.mark.parametrize(
    "method", ["createOrReplaceTempView", "registerTempTable", "createTempView"]
)
def test_a_view_registered_with_its_name_as_a_keyword_is_a_view(tmp_path, monkeypatch, method):
    got = consumed(
        tmp_path,
        monkeypatch,
        f"""
        spark.table("raw.a").{method}(name="vv")
        spark.sql("INSERT INTO mart.y SELECT * FROM vv")
        spark.table("raw.c").write.saveAsTable("mart.z")
        """,
    )
    assert got == {"mart.y": ["raw.a"], "mart.z": ["raw.c"]}


def test_a_view_registered_with_no_name_the_reader_can_see_leaves_a_bare_name_uncertain(
    tmp_path, monkeypatch
):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        names = ["vv"]
        spark.table("raw.a").createOrReplaceTempView(*names)
        spark.sql("INSERT INTO mart.y SELECT * FROM vv JOIN raw.x ON 1=1")
        spark.table("raw.c").write.saveAsTable("mart.z")
        """,
    )
    assert {"raw.a", "raw.x"} <= set(got["mart.y"]) and got["mart.z"] == ["raw.c"]


# ── a CTE is a read only where the name is not a CTE in scope ─────────────────────────────


def reads_of(text: str) -> List[str]:
    return [name.shown for name in sql.analyze(text).reads]


@pytest.mark.parametrize(
    "text, reads",
    [
        (
            "WITH orders AS (SELECT * FROM orders WHERE x > 1) INSERT INTO mart.t "
            "SELECT * FROM orders",
            ["orders"],
        ),
        (
            "INSERT INTO mart.y SELECT * FROM (WITH x AS (SELECT 1) SELECT * FROM x) q "
            "UNION ALL SELECT * FROM x",
            ["x"],
        ),
        (
            "CREATE TABLE mart.t AS WITH a AS (SELECT * FROM raw.c), b AS (SELECT * FROM a) "
            "SELECT * FROM b",
            ["raw.c"],
        ),
        (
            "WITH a AS (SELECT * FROM b), b AS (SELECT * FROM raw.c) INSERT INTO mart.t "
            "SELECT * FROM a",
            ["b", "raw.c"],
        ),
        (
            "INSERT INTO mart.t WITH x AS (SELECT * FROM raw.a) SELECT * FROM x JOIN raw.b ON 1=1",
            ["raw.a", "raw.b"],
        ),
        (
            "INSERT INTO mart.t SELECT * FROM (WITH x AS (SELECT * FROM raw.a) "
            "SELECT * FROM (WITH y AS (SELECT * FROM x) SELECT * FROM y) z) w",
            ["raw.a"],
        ),
        (
            "INSERT INTO mart.t WITH x AS (SELECT * FROM raw.a) SELECT * FROM x "
            "UNION ALL SELECT * FROM x",
            ["raw.a"],
        ),
        ("WITH RECURSIVE r AS (SELECT 1 UNION ALL SELECT * FROM r) SELECT * FROM r", []),
        ("WITH X AS (SELECT * FROM raw.a) SELECT * FROM x", ["raw.a"]),
        ("WITH x AS (SELECT * FROM raw.a) SELECT * FROM X", ["raw.a"]),
        ("WITH x AS (SELECT * FROM raw.a) SELECT * FROM db.x", ["raw.a", "db.x"]),
    ],
    ids=[
        "its own body is the table",
        "another branch of the union",
        "after it",
        "before it",
        "in the query of the insert",
        "nested",
        "both branches of the query",
        "recursive",
        "case of the definition",
        "case of the reference",
        "qualified name is a table",
    ],
)
def test_a_name_is_a_cte_only_where_one_of_that_name_is_in_scope(text, reads):
    assert sorted(reads_of(text)) == sorted(reads)


def test_a_long_with_and_a_long_union_are_read_without_recursion():
    many = "WITH " + ", ".join(f"c{n} AS (SELECT * FROM raw.t{n})" for n in range(1_000))
    assert len(reads_of(many + " SELECT * FROM c999")) == 1_000
    union = " UNION ALL ".join(f"SELECT * FROM raw.u{n}" for n in range(1_000))
    assert len(reads_of("INSERT INTO mart.t " + union)) in {0, 1_000}  # not parsed, or all of it


def test_a_cte_named_like_a_table_does_not_hide_the_table_from_a_product(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.sql("WITH orders AS (SELECT * FROM retail.orders) INSERT INTO mart.t SELECT * FROM orders")
        spark.sql("INSERT INTO mart.u SELECT * FROM retail.c")
        """,
    )
    assert got == {"mart.t": ["retail.orders"], "mart.u": ["retail.c"]}


# ── a name in a hint is not a table ────────────────────────────────────────────────────────


def test_a_name_in_a_hint_is_not_a_read():
    text = (
        "INSERT INTO mart.y SELECT /*+ BROADCAST(b), MERGE(c) */ * FROM raw.a a JOIN raw.b b ON 1=1"
    )
    assert reads_of(text) == ["raw.a", "raw.b"]
    assert reads_of("SELECT /*+ BROADCAST(raw.b) */ * FROM raw.a JOIN raw.b ON 1=1") == [
        "raw.a",
        "raw.b",
    ]


def test_a_hinted_statement_lists_what_it_reads_and_no_alias(tmp_path, monkeypatch):
    got = consumed(
        tmp_path,
        monkeypatch,
        """
        spark.sql("INSERT INTO mart.y SELECT /*+ BROADCAST(b) */ * FROM raw.a a JOIN raw.b b ON 1=1")
        spark.sql("INSERT INTO mart.z SELECT * FROM raw.c")
        """,
    )
    assert got == {"mart.y": ["raw.a", "raw.b"], "mart.z": ["raw.c"]}
