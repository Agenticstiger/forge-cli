# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What a PySpark job reads, writes and streams, read statically from its source."""

from __future__ import annotations

import textwrap
from typing import List, Tuple

import pytest

from fluid_build.cli.import_workflow import _spark_sql as Q
from fluid_build.cli.import_workflow._fold import Folder
from fluid_build.cli.import_workflow._pysource import parse_module
from fluid_build.cli.import_workflow._spark_io import (
    HelperSpec,
    JobFacts,
    read_job,
    split_table_name,
)


def job(source: str, *, helpers=None, variables=None, rel: str = "job.py") -> JobFacts:
    module = parse_module(rel, textwrap.dedent(source).encode("utf-8"))
    return read_job(module, Folder(module, variables=variables), helpers=helpers)


def reads(facts: JobFacts) -> List[Tuple[str, str]]:
    return [(r.kind, r.name or r.shown) for r in facts.reads]


def writes(facts: JobFacts) -> List[Tuple[str, str]]:
    return [(w.kind, w.name or w.shown) for w in facts.writes]


# ── batch reads ───────────────────────────────────────────────────────


def test_a_table_read_through_the_session_and_the_reader():
    facts = job(
        """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        a = spark.table("Retail.Orders")
        b = spark.read.table("retail.customers")
        c = spark.readStream.table("retail.events")
        """
    )
    assert reads(facts) == [
        ("table", "retail.orders"),
        ("table", "retail.customers"),
        ("table", "retail.events"),
    ]
    assert [r.streaming for r in facts.reads] == [False, False, True]
    assert facts.is_spark and facts.streaming


def test_table_reads_need_a_session_like_receiver():
    facts = job(
        """
        spark = None
        x = tabulate.table("not-spark")
        y = self.spark.table("is.spark")
        z = other.table("neither")
        """
    )
    assert reads(facts) == [("table", "is.spark")]


def test_a_session_built_under_another_name_is_found_by_how_it_is_made():
    facts = job(
        """
        from pyspark.sql import SparkSession
        engine = SparkSession.builder.appName("a").getOrCreate()
        engine.table("retail.a")
        engine.sql("INSERT INTO retail.b SELECT * FROM retail.a")
        """
    )
    assert reads(facts) == [("table", "retail.a"), ("table", "retail.a")]
    assert writes(facts) == [("table", "retail.b")]


@pytest.mark.parametrize(
    "source, expected",
    [
        ('spark.read.format("iceberg").load("glue.retail.dim")', [("table", "glue.retail.dim")]),
        ('spark.read.format("iceberg").load("s3://l/wh/dim")', [("path", "s3://l/wh/dim")]),
        ('spark.read.format("delta").load("s3://l/d")', [("path", "s3://l/d")]),
        ('spark.read.load("s3://l/p", "parquet")', [("path", "s3://l/p")]),
        ('spark.read.format("csv").option("path", "s3://l/c").load()', [("path", "s3://l/c")]),
        ('spark.read.options(path="s3://l/o").load()', [("path", "s3://l/o")]),
        (
            'spark.read.parquet("s3://l/a", "s3://l/b")',
            [("path", "s3://l/a"), ("path", "s3://l/b")],
        ),
        (
            'spark.read.json(["s3://l/j1", "s3://l/j2"])',
            [("path", "s3://l/j1"), ("path", "s3://l/j2")],
        ),
        ('spark.read.csv(path="s3://l/k")', [("path", "s3://l/k")]),
        ('spark.read.format("iceberg").options(**{"path": "r.t"}).load()', [("table", "r.t")]),
    ],
)
def test_loads_and_file_readers(source, expected):
    facts = job(f"x = {source}\n")
    assert reads(facts) == expected
    assert all(r.fmt is not None or r.kind == "path" for r in facts.reads)


def test_the_format_of_a_read_is_kept():
    facts = job(
        'x = spark.read.format("Iceberg").load("glue.retail.dim")\ny = spark.read.orc("p")\n'
    )
    assert [r.fmt for r in facts.reads] == ["iceberg", "orc"]


def test_a_read_that_goes_through_variables_is_followed():
    facts = job(
        """
        reader = spark.read.format("iceberg")
        configured = reader.option("x", "y")
        df = configured.load("glue.retail.dim")
        also = spark.read
        other = also.parquet("s3://l/p")
        """
    )
    assert reads(facts) == [("table", "glue.retail.dim"), ("path", "s3://l/p")]


def test_jdbc_is_a_note_not_a_table():
    facts = job(
        """
        a = spark.read.jdbc("jdbc:x", "public.orders")
        b = spark.read.format("jdbc").option("dbtable", "public.t").load()
        c = df.write.jdbc("jdbc:x", "public.out")
        """
    )
    assert not facts.reads and not facts.writes
    assert len(facts.notes) == 3 and all("JDBC" in n for n in facts.notes)


# ── what does not resolve ─────────────────────────────────────────────


def test_a_name_that_is_not_a_constant_is_reported_with_its_reason():
    facts = job(
        """
        import sys
        args = parse()
        a = spark.table(sys.argv[1])
        b = spark.table(args.source)
        c = spark.table(f"{args.env}_orders")
        """
    )
    assert [r.resolved for r in facts.reads] == [False, False, False]
    assert "command-line argument" in facts.reads[0].why
    assert facts.reads[1].shown == "args.source"
    assert facts.reads[2].shown == "{args.env}_orders"
    assert not facts.reads[0].parts


def test_environment_variables_resolve_only_from_the_operators_variables():
    source = "import os\nx = spark.table(f\"{os.environ['ENV']}_db.orders\")\n"
    assert not job(source).reads[0].resolved
    got = job(source, variables={"ENV": "prod"})
    assert reads(got) == [("table", "prod_db.orders")]


def test_a_name_holding_a_placeholder_is_not_a_table():
    facts = job(
        'x = spark.table("{env}.orders")\ny = spark.table("db/orders")\nz = spark.table("")\n'
    )
    assert [r.why for r in facts.reads] == [
        "the name holds a placeholder",
        "the name is not a table name",
        "the name is empty",
    ]


def test_names_from_function_parameters_are_one_note_not_one_line_each():
    facts = job(
        """
        def read_it(spark, name):
            return spark.table(name)

        def write_it(df, name, path):
            df.write.saveAsTable(name)
            df.write.parquet(path)
            spark.sql(name)
        """,
        rel="io.py",
    )
    assert not facts.reads and not facts.writes
    assert len(facts.notes) == 1
    assert "io.py: 4 read(s) or write(s)" in facts.notes[0] and "`helpers`" in facts.notes[0]
    assert not facts.is_spark


def test_backtick_quoted_names_and_catalogs():
    assert split_table_name("`my db`.`t.x`") == ["my db", "t.x"]
    assert split_table_name("glue.retail.orders") == ["glue", "retail", "orders"]
    facts = job('x = spark.table("`My Db`.Orders")\n')
    assert facts.reads[0].parts == ("my db", "orders")


# ── batch writes ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source, kind, name, mode",
    [
        ('df.write.mode("overwrite").saveAsTable("retail.a")', "table", "retail.a", "overwrite"),
        ('df.write.saveAsTable("retail.a", "parquet", "append")', "table", "retail.a", "append"),
        (
            'df.write.saveAsTable(name="retail.a", mode="Overwrite")',
            "table",
            "retail.a",
            "overwrite",
        ),
        ('df.write.insertInto("retail.a")', "table", "retail.a", "append"),
        ('df.write.insertInto("retail.a", overwrite=True)', "table", "retail.a", "overwrite"),
        ('df.write.insertInto("retail.a", True)', "table", "retail.a", "overwrite"),
        (
            'df.write.format("iceberg").mode("append").save("glue.retail.a")',
            "table",
            "glue.retail.a",
            "append",
        ),
        ('df.write.format("iceberg").save("s3://l/wh/a")', "path", "s3://l/wh/a", None),
        ('df.write.parquet("s3://l/o", mode="overwrite")', "path", "s3://l/o", "overwrite"),
        ('df.write.parquet("s3://l/o", "append")', "path", "s3://l/o", "append"),
        ('df.write.csv("s3://l/c")', "path", "s3://l/c", None),
        ('df.write.text("s3://l/t", "gzip")', "path", "s3://l/t", None),
        ('df.write.format("delta").mode("append").save("s3://l/d")', "path", "s3://l/d", "append"),
        ('df.write.option("path", "s3://l/p").format("orc").save()', "path", "s3://l/p", None),
    ],
)
def test_batch_writes(source, kind, name, mode):
    facts = job(f"{source}\n")
    assert writes(facts) == [(kind, name)]
    assert facts.writes[0].mode == mode
    assert not facts.writes[0].streaming


@pytest.mark.parametrize(
    "terminal, mode",
    [
        ("append()", "append"),
        ("create()", "create"),
        ("replace()", "replace"),
        ("createOrReplace()", "overwrite"),
        ("overwrite(F.col('d') == 1)", "overwrite"),
        ("overwritePartitions()", "overwrite"),
    ],
)
def test_the_writer_v2_chain(terminal, mode):
    facts = job(
        f'df.writeTo("glue.retail.a").using("iceberg").tableProperty("k", "v").{terminal}\n'
    )
    assert writes(facts) == [("table", "glue.retail.a")]
    assert facts.writes[0].mode == mode and facts.writes[0].fmt == "iceberg"


def test_a_write_chain_through_a_variable_and_a_read_that_feeds_it():
    facts = job(
        """
        writer = df.write.format("iceberg").mode("append")
        writer.saveAsTable("retail.a")
        spark.read.parquet("s3://l/in").write.parquet("s3://l/out")
        """
    )
    assert reads(facts) == [("path", "s3://l/in")]
    assert writes(facts) == [("table", "retail.a"), ("path", "s3://l/out")]
    assert facts.writes[0].fmt == "iceberg" and facts.writes[0].mode == "append"


def test_the_same_chain_is_not_counted_twice():
    facts = job('spark.read.parquet("p").write.parquet("q")\n')
    assert len(facts.reads) == 1 and len(facts.writes) == 1


def test_a_sink_that_keeps_nothing_is_ignored():
    facts = job(
        """
        df.writeStream.format("console").start()
        df.writeStream.format("memory").queryName("m").start()
        df.write.format("noop").save()
        """
    )
    assert not facts.writes and not facts.streams


# ── streaming ─────────────────────────────────────────────────────────


def test_kafka_to_kafka_with_checkpoint_trigger_and_query_name():
    facts = job(
        """
        reader = (spark.readStream.format("kafka")
                  .option("kafka.bootstrap.servers", "b1:9092,b2:9092")
                  .option("subscribe", "clicks, views"))
        raw = reader.load()
        query = (raw.writeStream.format("kafka")
                 .option("topic", "clicks_clean")
                 .option("kafka.bootstrap.servers", "b1:9092")
                 .option("checkpointLocation", "s3://b/ckpt/clicks")
                 .outputMode("append")
                 .trigger(processingTime="1 minute")
                 .queryName("clean")
                 .start())
        """
    )
    assert [(r.kind, r.name, r.host, r.streaming) for r in facts.reads] == [
        ("topic", "clicks", "b1:9092,b2:9092", True),
        ("topic", "views", "b1:9092,b2:9092", True),
    ]
    (w,) = facts.writes
    assert (w.kind, w.name, w.fmt, w.streaming, w.host) == (
        "topic",
        "clicks_clean",
        "kafka",
        True,
        "b1:9092",
    )
    assert w.checkpoint == "s3://b/ckpt/clicks"
    assert w.trigger == "processingTime=1 minute"
    assert w.query_name == "clean" and w.output_mode == "append"


def test_kafka_source_options_by_every_spelling():
    facts = job(
        """
        opts = {"subscribe": "from_dict", "kafka.bootstrap.servers": "h:1"}
        a = spark.readStream.format("kafka").options(**opts).load()
        b = spark.readStream.format("kafka").options(subscribe="from_kwargs").load()
        c = spark.readStream.format("kafka").option("subscribePattern", "orders-.*").load()
        d = spark.readStream.format("kafka").option("assign", '{"t1": [0, 1], "t2": [0]}').load()
        e = spark.readStream.format("kafka").option("subscribe", f"{PREFIX}-events").load()
        f = spark.read.format("kafka").load()
        """
    )
    assert [(r.kind, r.name or r.shown, r.resolved) for r in facts.reads] == [
        ("topic", "from_dict", True),
        ("topic", "from_kwargs", True),
        ("topic_pattern", "orders-.*", True),
        ("topic", "t1", True),
        ("topic", "t2", True),
        ("topic", "{PREFIX}-events", False),
        ("topic", "", False),
    ]
    assert "subscribe, subscribePattern or assign" in facts.reads[-1].why


def test_kafka_credentials_and_odd_hosts_are_never_kept():
    jaas = (
        "org.apache.kafka.common.security.plain.PlainLoginModule required pass" + "word='s3cr3t';"
    )
    facts = job(
        f"""
        x = (spark.readStream.format("kafka")
             .option("kafka.bootstrap.servers", "user:pw@host:9092")
             .option("kafka.sasl.jaas.config", "{jaas}")
             .option("subscribe", "t").load())
        y = (spark.readStream.format("kafka")
             .option("kafka.bootstrap.servers", f"{{host}}:9092")
             .option("subscribe", "t2").load())
        """
    )
    assert [r.host for r in facts.reads] == [None, None]
    assert "s3cr3t" not in repr(facts) and "sasl" not in repr(facts)


def test_a_kafka_sink_with_no_topic_option_says_so():
    facts = job('df.writeStream.format("kafka").option("checkpointLocation", "c").start()\n')
    (w,) = facts.writes
    assert not w.resolved and "may come from a column" in w.why


@pytest.mark.parametrize(
    "trigger, expected",
    [
        ('trigger(processingTime="30 seconds")', "processingTime=30 seconds"),
        ("trigger(availableNow=True)", "availableNow"),
        ("trigger(once=True)", "once"),
        ('trigger(continuous="1 second")', "continuous=1 second"),
        ("trigger(availableNow=False)", None),
        ("outputMode('append')", None),
    ],
)
def test_triggers(trigger, expected):
    facts = job(f'df.writeStream.format("parquet").{trigger}.start("s3://l/s")\n')
    assert facts.writes[0].trigger == expected


def test_start_and_totable_take_their_options_from_keywords():
    facts = job(
        """
        df.writeStream.format("parquet").start("s3://l/out", checkpointLocation="s3://l/ck", queryName="qq")
        df.writeStream.toTable("retail.stream_out", "iceberg", "append", checkpointLocation="s3://l/ck2")
        """
    )
    first, second = facts.writes
    assert (first.name, first.checkpoint, first.query_name) == ("s3://l/out", "s3://l/ck", "qq")
    assert (second.kind, second.name, second.fmt, second.output_mode, second.checkpoint) == (
        "table",
        "retail.stream_out",
        "iceberg",
        "append",
        "s3://l/ck2",
    )


def test_the_session_conf_gives_the_default_checkpoint():
    facts = job(
        """
        spark = (SparkSession.builder
                 .config("spark.sql.streaming.checkpointLocation", "s3://l/default")
                 .getOrCreate())
        df.writeStream.format("parquet").start("s3://l/out")
        df.writeStream.format("parquet").option("checkpointLocation", "s3://l/own").start("s3://l/out2")
        """
    )
    assert [w.checkpoint for w in facts.writes] == ["s3://l/default", "s3://l/own"]


def test_a_checkpoint_that_is_not_a_constant_is_shown_as_such():
    facts = job(
        'df.writeStream.format("parquet").option("checkpointLocation", args.ckpt).start("p")\n'
    )
    assert facts.writes[0].checkpoint.startswith("(not a constant")


def test_foreach_batch_writes_carry_the_stream_facts():
    facts = job(
        """
        def process(batch_df, epoch_id):
            batch_df.write.format("iceberg").mode("append").save("retail.events")
            spark.sql("INSERT INTO retail.audit SELECT * FROM retail.events")

        outside = df.write.saveAsTable("retail.batch_only")
        (stream.writeStream.foreachBatch(process)
               .option("checkpointLocation", "s3://l/c")
               .trigger(availableNow=True)
               .queryName("fb")
               .start())
        """
    )
    by_name = {w.name: w for w in facts.writes}
    assert by_name["retail.events"].streaming and by_name["retail.audit"].streaming
    assert by_name["retail.events"].checkpoint == "s3://l/c"
    assert by_name["retail.events"].trigger == "availableNow"
    assert by_name["retail.events"].query_name == "fb"
    assert not by_name["retail.batch_only"].streaming
    (stream,) = facts.streams
    assert stream.via_foreach_batch and stream.batch_function == (2, 4)


def test_foreach_batch_with_a_lambda():
    facts = job(
        """
        stream.writeStream.foreachBatch(lambda b, i: b.write.saveAsTable("retail.t")).start()
        """
    )
    assert facts.writes[0].streaming and facts.streams[0].batch_function == (2, 2)


# ── SQL ───────────────────────────────────────────────────────────────


def test_sql_reads_and_writes_with_temp_views_and_ctes_left_out():
    facts = job(
        """
        df.createOrReplaceTempView("staging")
        spark.sql('''
            WITH recent AS (SELECT * FROM retail.orders WHERE d > 1)
            INSERT OVERWRITE TABLE retail.orders_daily
            SELECT r.*, s.x, c.name
            FROM recent r JOIN staging s ON 1 = 1 JOIN glue.retail.customers c ON 1 = 1
        ''')
        spark.sql("CREATE OR REPLACE TEMPORARY VIEW later AS SELECT * FROM retail.events")
        spark.sql("SELECT * FROM later JOIN global_temp.g ON 1 = 1")
        """
    )
    # one statement's names come out in name order; later statements follow
    assert reads(facts) == [
        ("table", "glue.retail.customers"),
        ("table", "retail.orders"),
        ("table", "retail.events"),
    ]
    assert writes(facts) == [("table", "retail.orders_daily")]
    assert facts.writes[0].mode == "overwrite"


@pytest.mark.parametrize(
    "statement, expected_writes, expected_reads",
    [
        (
            "INSERT INTO retail.t (a, b) SELECT a, b FROM retail.s",
            [("retail.t", "append")],
            ["retail.s"],
        ),
        (
            "CREATE TABLE IF NOT EXISTS retail.t USING iceberg AS SELECT * FROM retail.s",
            [("retail.t", "create")],
            ["retail.s"],
        ),
        ("CREATE VIEW retail.v AS SELECT * FROM retail.s", [("retail.v", "view")], ["retail.s"]),
        (
            "MERGE INTO retail.t USING retail.s ON t.i = s.i WHEN MATCHED THEN UPDATE SET *",
            [("retail.t", "merge")],
            ["retail.s"],
        ),
        (
            "MERGE INTO retail.t USING (SELECT * FROM retail.s) x ON 1=1 WHEN MATCHED THEN DELETE",
            [("retail.t", "merge")],
            ["retail.s"],
        ),
        (
            "DELETE FROM retail.t WHERE a IN (SELECT a FROM retail.s)",
            [("retail.t", "delete")],
            ["retail.s"],
        ),
        ("UPDATE retail.t SET a = 1", [("retail.t", "update")], []),
        ("CREATE TABLE retail.t (a INT) USING iceberg", [], []),
        ("SELECT * FROM range(10)", [], []),
        ("SELECT * FROM (VALUES (1, 2)) AS v(a, b)", [], []),
        ("USE retail", [], []),
        ("ALTER TABLE retail.t ADD COLUMNS (c INT)", [], []),
        ("DROP TABLE IF EXISTS retail.t", [], []),
        ("REFRESH TABLE retail.t", [], []),
        (
            "SELECT * FROM a; INSERT INTO b SELECT * FROM a",
            [("b", "append")],
            ["a"],
        ),  # one line, one read
    ],
)
def test_sql_statements(statement, expected_writes, expected_reads):
    facts = job(f"spark.sql({statement!r})\n")
    assert [(w.name, w.mode) for w in facts.writes] == expected_writes
    assert sorted(r.name for r in facts.reads) == sorted(expected_reads)


def test_sql_paths_with_their_format():
    facts = job('spark.sql("SELECT * FROM delta.`s3://l/d` JOIN parquet.`/p` ON 1 = 1")\n')
    assert sorted((r.kind, r.name, r.fmt) for r in facts.reads) == [
        ("path", "/p", "parquet"),
        ("path", "s3://l/d", "delta"),
    ]


def test_sql_built_from_an_unknown_name_keeps_everything_else():
    facts = job(
        """
        TGT = "retail.orders_daily"
        spark.sql(f"INSERT INTO {TGT} SELECT * FROM {args.source} s JOIN retail.dim d ON 1 = 1")
        spark.sql("INSERT INTO {}.facts SELECT * FROM retail.dim".format(args.db))
        spark.sql("INSERT INTO %s.facts2 SELECT * FROM retail.dim" % args.db)
        spark.sql("INSERT INTO " + target + " SELECT * FROM retail.dim")
        """
    )
    assert [(w.shown, w.resolved) for w in facts.writes] == [
        ("retail.orders_daily", True),
        ("{args.db}.facts", False),
        ("{args.db}.facts2", False),
        ("{target}", False),
    ]
    assert all(w.parts == () for w in facts.writes[1:])
    assert [r.shown for r in facts.reads if r.resolved] == ["retail.dim"] * 4
    assert any(not r.resolved and r.shown == "{args.source}" for r in facts.reads)


def test_sql_that_is_not_a_constant_or_does_not_parse_is_a_note():
    facts = job(
        """
        import sys
        spark.sql(sys.argv[1])
        spark.sql("INSERT OVERWRITE DIRECTORY 's3://b/out' USING parquet SELECT * FROM t")
        spark.sql("this is not sql at all (((")
        """
    )
    assert not facts.reads and not facts.writes
    assert any("takes SQL that is not a constant" in n for n in facts.notes)
    assert sum("did not parse" in n for n in facts.notes) == 2


def test_the_sql_analyser_is_bounded(monkeypatch):
    monkeypatch.setattr(Q, "MAX_SQL_CHARS", 20)
    assert "was not parsed" in Q.analyze("SELECT * FROM a_table_with_a_long_name").notes[0]
    monkeypatch.setattr(Q, "MAX_SQL_CHARS", 200_000)
    monkeypatch.setattr(Q, "MAX_STATEMENTS", 2)
    facts = Q.analyze("SELECT 1 FROM a; SELECT 1 FROM b; SELECT 1 FROM c")
    assert [r.parts for r in facts.reads] == [("a",), ("b",)]
    assert "first 2 statements" in facts.notes[0]


def test_deeply_nested_sql_does_not_crash():
    sql = "SELECT * FROM " + "(SELECT * FROM " * 400 + "t" + ")" * 400
    facts = Q.analyze(sql)
    assert isinstance(facts, Q.SqlFacts)


# ── helpers from the conventions ──────────────────────────────────────


def test_helpers_the_operator_names_are_read_at_their_call_sites():
    helpers = {
        "read_table": HelperSpec("reads", "table", 0),
        "write_table": HelperSpec("writes", "table", 1, fmt="iceberg"),
        "save_to": HelperSpec("writes", "path", "path"),
        "from_topic": HelperSpec("reads", "topic", 0),
    }
    facts = job(
        """
        from acme.io import read_table, write_table
        import acme.io as io
        a = read_table("retail.orders")
        b = io.read_table(name)
        write_table(df, "retail.daily")
        io.save_to(df, path="s3://l/out")
        c = from_topic("clicks")
        d = read_table()
        """,
        helpers=helpers,
    )
    assert [(r.kind, r.name or r.shown, r.resolved) for r in facts.reads] == [
        ("table", "retail.orders", True),
        ("table", "name", False),
        ("topic", "clicks", True),
        ("table", "", False),
    ]
    assert [(w.kind, w.name, w.fmt) for w in facts.writes] == [
        ("table", "retail.daily", "iceberg"),
        ("path", "s3://l/out", None),
    ]


def test_a_helper_name_does_nothing_unless_declared():
    assert not job('read_table("retail.orders")\n').reads


def test_a_helper_called_with_a_parameter_is_counted_as_a_helper_itself():
    helpers = {"read_table": HelperSpec("reads", "table", 0)}
    facts = job("def outer(t):\n    return read_table(t)\n", helpers=helpers)
    assert not facts.reads and "function parameter" in facts.notes[0]


# ── shape of the answer ───────────────────────────────────────────────


def test_results_are_ordered_by_line_and_unique():
    facts = job(
        """
        b = spark.table("z.last")
        a = spark.table("a.first")
        c = spark.table("a.first")
        """
    )
    assert [(r.line, r.name) for r in facts.reads] == [
        (2, "z.last"),
        (3, "a.first"),
        (4, "a.first"),
    ]
    assert all(r.file == "job.py" for r in facts.reads)


def test_a_file_with_no_spark_in_it_has_no_facts():
    facts = job("import pandas as pd\ndf = pd.read_csv('a.csv')\nprint(pd.table)\n")
    assert not facts.is_spark and not facts.has_io and not facts.notes


def test_the_reader_does_not_run_the_job(tmp_path):
    marker = tmp_path / "ran"
    job(
        f"""
        from pyspark.sql import SparkSession
        open({str(marker)!r}, "w").write("x")
        raise SystemExit(1)
        """
    )
    assert not marker.exists()


# ── what the reader cannot follow is said, not dropped ────────────────


def test_a_placeholder_in_sql_that_is_not_an_f_string_is_a_hole_not_a_table():
    facts = job(
        """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        spark.sql("INSERT INTO ${env}.orders SELECT * FROM {src}.events JOIN retail.users")
        spark.sql("SELECT * FROM %(db)s.clicks")
        spark.sql("SELECT * FROM {{ db }}.sessions")
        """
    )
    resolved = [r for r in facts.reads if r.name]
    unresolved = [r for r in facts.reads if not r.name]
    assert [r.name for r in resolved] == ["retail.users"]
    assert sorted(r.shown for r in unresolved) == [
        "%(db)s.clicks",
        "{src}.events",
        "{{ db }}.sessions",
    ]
    assert all(r.why for r in unresolved)
    [write] = facts.writes
    assert not write.name and write.shown == "${env}.orders" and write.why


def test_a_placeholder_inside_a_string_literal_is_harmless():
    facts = job(
        """
        spark = SparkSession.builder.getOrCreate()
        spark.sql("SELECT * FROM retail.orders WHERE dt = '{{ ds }}' AND k = '${key}'")
        """
    )
    assert reads(facts) == [("table", "retail.orders")]


def test_a_temp_view_registered_under_a_name_that_is_not_a_constant_is_noted():
    facts = job(
        """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        df = spark.table("retail.orders")
        df.createOrReplaceTempView(view_name)
        spark.table("whatever")
        """
    )
    assert any("temporary view" in note and "not a constant" in note for note in facts.notes)
    assert [n for n in facts.notes if "temporary view" in n][0].startswith("job.py:5 ")


def test_a_reader_variable_assigned_twice_is_not_followed_and_the_note_says_so():
    facts = job(
        """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        reader = spark.read.format("parquet")
        if flag:
            reader = spark.read.format("csv")
        df = reader.load("s3://bucket/in/")
        """
    )
    assert facts.reads == []
    [note] = [n for n in facts.notes if "assigned more than once" in n]
    assert note.startswith("job.py:7 ") and ".load(...)" in note and "`reader`" in note


def test_a_writer_variable_assigned_twice_is_not_followed_and_the_note_says_so():
    facts = job(
        """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        out = df.write.mode("append")
        out = df.write.mode("overwrite")
        out.saveAsTable("retail.totals")
        """
    )
    assert facts.writes == []
    assert any("assigned more than once" in n and "`out`" in n for n in facts.notes)


def test_a_variable_assigned_once_is_still_followed_without_a_note():
    facts = job(
        """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        reader = spark.read.format("parquet")
        df = reader.load("s3://bucket/in/")
        """
    )
    assert reads(facts) == [("path", "s3://bucket/in/")]
    assert not [n for n in facts.notes if "assigned more than once" in n]


def test_folding_that_runs_out_of_budget_is_said_once_for_the_file():
    lines = ['x0 = ""']
    for level in range(1, 11):
        lines.append(f'x{level} = f"' + ("{x%d}" % (level - 1)) * 300 + '"')
    source = "\n".join(
        [
            "from pyspark.sql import SparkSession",
            "spark = SparkSession.builder.getOrCreate()",
            *lines,
            'spark.read.parquet(x10 + "/a")',
            'spark.read.parquet(x10 + "/b")',
        ]
    )
    facts = job(source)
    assert [n for n in facts.notes if "folding stopped early" in n] == [
        "job.py: folding stopped early (the file asks for too much work): some names are "
        "reported as not constant"
    ]
    assert all(not r.name for r in facts.reads)


# ── a file of nothing but reads is not a job ──────────────────────────


def test_a_job_keeps_a_bounded_number_of_reads_and_writes_and_says_so(monkeypatch):
    from fluid_build.cli.import_workflow import _spark_io as IO

    monkeypatch.setattr(IO, "MAX_REFS_PER_JOB", 5)
    lines = "\n".join(f'spark.table("db.t{i}")' for i in range(9))
    lines += "\n" + "\n".join(f'df.write.saveAsTable("db.o{i}")' for i in range(7))
    facts = job(
        "from pyspark.sql import SparkSession\nspark = SparkSession.builder.getOrCreate()\n" + lines
    )
    assert len(facts.reads) == 5 and len(facts.writes) == 5
    assert [n for n in facts.notes if "more than 5 reads" in n] == [
        "job.py: more than 5 reads: the rest are not listed"
    ]
    assert [n for n in facts.notes if "more than 5 writes" in n] == [
        "job.py: more than 5 writes: the rest are not listed"
    ]


def test_placeholders_in_the_longest_sql_the_folder_gives_are_found_in_bounded_time():
    import time

    from fluid_build.cli.import_workflow import _spark_io as IO

    for text in ("${" * 2048, "{{" * 2048, "{a" * 2048, "%(a" * 1365):
        started = time.monotonic()
        IO._placeholders_to_holes(text, {})
        assert time.monotonic() - started < 0.5


# ── names the SQL does not state, and statements that repeat ──────────


def test_a_table_named_by_identifier_is_a_name_that_is_not_a_constant():
    facts = job(
        """
        spark = SparkSession.builder.getOrCreate()
        spark.sql("INSERT INTO IDENTIFIER(:tbl) SELECT * FROM raw.src")
        spark.sql("INSERT INTO out.t SELECT * FROM IDENTIFIER('a.b')")
        """
    )
    assert [r.name for r in facts.reads if r.name] == ["raw.src"]
    unresolved = [x for x in (*facts.reads, *facts.writes) if not x.name]
    assert sorted(x.shown for x in unresolved) == ["{IDENTIFIER('a.b')}", "{IDENTIFIER(:tbl)}"]
    assert [w.name for w in facts.writes if w.name] == ["out.t"]
    assert not [x for x in (*facts.reads, *facts.writes) if x.name == "identifier"]


def test_the_same_statement_is_parsed_once_however_often_it_is_passed(monkeypatch):
    calls = []
    original = Q.analyze

    def counting(sql, holes=None):
        calls.append(sql)
        return original(sql, holes)

    monkeypatch.setattr(Q, "analyze", counting)
    lines = "\n".join("spark.sql(QUERY)" for _ in range(50))
    facts = job(
        'spark = SparkSession.builder.getOrCreate()\nQUERY = "INSERT INTO a.out SELECT * FROM a.src"\n'
        + lines
        + "\n"
    )
    assert len(calls) == 1
    assert len(facts.reads) == 50 and len(facts.writes) == 50  # one per call site, as always


def test_a_path_with_a_credential_in_it_is_kept_without_it():
    facts = job(
        """
        spark = SparkSession.builder.getOrCreate()
        spark.read.parquet("s3a://KEY:SECRETVALUE@inbucket/raw/orders")
        df.write.parquet("gs://b/x/token=hunterE/y")
        df.write.saveAsTable("db.\\u202ename")
        """
    )
    assert reads(facts) == [("path", "s3a://inbucket/raw/orders")]
    assert writes(facts) == [("path", "gs://b/x/token=REDACTED/y"), ("table", "db.name")]


def test_a_file_whose_every_statement_is_a_note_keeps_a_few_and_counts_the_rest():
    lines = "\n".join(f"spark.sql(build({i}))" for i in range(500))
    facts = job("spark = SparkSession.builder.getOrCreate()\n" + lines + "\n")
    assert len(facts.notes) == 201
    assert facts.notes[-1] == "job.py: and 300 more notes"
