# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The runner's prebuilt-package path before and around Spark: the config it accepts, the
bytes it imports, the writes it refuses, the output it accepts and what it does with the
rows that fail. No pyspark needed here (``test_runner_package_session.py`` runs it on a
local Spark session)."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import sys
import types

import pytest

from fluid_build.spark import names
from fluid_build.spark.compiler import compile_job_config
from fluid_build.spark.package import PACKAGE_ALLOWLIST_ENV

from .package_fixtures import JOBS, PACKAGE, PACKAGE_CONTRACT, SHA, package_zip


@pytest.fixture(autouse=True)
def listed(monkeypatch):
    monkeypatch.setenv(PACKAGE_ALLOWLIST_ENV, "s3://commerce-artifacts/code/")


@pytest.fixture
def contract():
    return copy.deepcopy(PACKAGE_CONTRACT)


def config_text(contract):
    return compile_job_config(contract, contract["builds"][0]).text


# --- the config -------------------------------------------------------------------------


def test_the_runner_reads_a_package_config(runner, contract):
    config = runner.parse_config(config_text(contract))
    assert config["package"]["sha256"] == SHA


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda c: c["package"].update(sha256="x"), "64 lowercase hex"),
        (lambda c: c["package"].update(entryPoint="orders_pkg"), "<module>:<attribute>"),
        (lambda c: c["package"].update(arguments={"a": 1}), "strings"),
        (lambda c: c["package"].update(schema=""), "declared columns"),
        (
            lambda c: c.update(mode="streaming", checkpoint="s3://b/c/", source={"kind": "kafka"}),
            "batch",
        ),
        (lambda c: c.update(source={"kind": "kafka"}), "reads no topic"),
        (lambda c: c.update(sql={"select": "SELECT 1"}), "sql.check"),
        (lambda c: c["package"].update(deadLetter={"table": ""}), "deadLetter"),
        (
            lambda c: c["package"].update(deadLetter={"table": "t", "maxRecordsBeforeAbort": -1}),
            "deadLetter",
        ),
        (
            lambda c: c["package"].update(
                deadLetter={"table": "t", "maxRecordsBeforeAbort": 1, "create": ["DROP TABLE x"]}
            ),
            "CREATE",
        ),
        (lambda c: c.update(deadLetter={"sink": "table"}), "package.deadLetter"),
        (lambda c: c.update(package=[]), "object"),
    ],
)
def test_a_package_config_the_runner_cannot_run_is_refused(runner, contract, change, message):
    config = json.loads(config_text(contract))
    change(config)
    with pytest.raises(runner.JobConfigError, match=message):
        runner.parse_config(json.dumps(config))


def test_the_copy_is_where_fluid_apply_puts_it(runner):
    product, build = "commerce.orders-silver", "merge-orders"
    for root in ("s3://commerce-lake", "/srv/fluid", "file:///srv/fluid"):
        config_uri = names.job_config_uri(root, product, build)
        assert runner.package_location(config_uri, SHA) == names.package_uri(
            root, product, build, SHA
        )
    assert runner.PACKAGE_DIR == names.PACKAGE_DIR
    assert runner.PACKAGE_FILE == names.PACKAGE_FILE


def test_the_runner_and_forge_agree_on_the_limits(runner):
    from fluid_build.spark import dead_letter, package

    assert runner.MAX_PACKAGE_BYTES == package.MAX_PACKAGE_BYTES
    assert runner.PACKAGE_VIEW == dead_letter.OUTPUT_VIEW


# --- the bytes --------------------------------------------------------------------------


def _copy(tmp_path, body=PACKAGE, name="package.zip"):
    source = tmp_path / "layout" / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(body)
    return source


def test_the_pinned_package_is_copied_owner_only(runner, tmp_path):
    source = _copy(tmp_path)
    path = runner.fetch_package(None, str(source), SHA)
    assert open(path, "rb").read() == PACKAGE
    assert path != str(source) and path.endswith(f"{SHA[:16]}.zip")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700


def test_a_file_uri_is_read_as_a_path(runner, tmp_path):
    source = _copy(tmp_path)
    assert open(runner.fetch_package(None, f"file://{source}", SHA), "rb").read() == PACKAGE


def test_other_bytes_are_refused_and_not_kept(runner, tmp_path):
    other = package_zip({"orders_pkg/__init__.py": "x = 1\n"})
    source = _copy(tmp_path, other)
    with pytest.raises(runner.PackageError, match="not the one the job config pins") as caught:
        runner.fetch_package(None, str(source), SHA)
    assert hashlib.sha256(other).hexdigest() in str(caught.value)
    assert "nothing was imported" in str(caught.value)


def test_a_missing_copy_says_to_run_fluid_apply(runner, tmp_path):
    with pytest.raises(runner.PackageError, match="run fluid apply"):
        runner.fetch_package(None, str(tmp_path / "nothing.zip"), SHA)


def test_an_oversized_package_is_refused_before_it_is_hashed(runner, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "MAX_PACKAGE_BYTES", 16)
    with pytest.raises(runner.PackageError, match="larger than"):
        runner.fetch_package(None, str(_copy(tmp_path)), SHA)


def test_a_file_that_is_no_zip_is_refused(runner, tmp_path):
    body = b"not a zip"
    with pytest.raises(runner.PackageError, match="not a wheel or a zip"):
        runner.fetch_package(None, str(_copy(tmp_path, body)), hashlib.sha256(body).hexdigest())


@pytest.mark.parametrize(
    "name", ["orders_pkg/_speedups.cpython-311-x86_64-linux-gnu.so", "x/y.pyd", "lib/z.so.1"]
)
def test_compiled_extensions_are_refused(runner, tmp_path, name):
    body = package_zip({"orders_pkg/__init__.py": "", name: "\x7fELF"})
    with pytest.raises(runner.PackageError, match="compiled extension modules") as caught:
        runner.fetch_package(None, str(_copy(tmp_path, body)), hashlib.sha256(body).hexdigest())
    assert "image or environment" in str(caught.value)


def test_a_remote_copy_goes_through_hadoop(runner, tmp_path):
    """An s3:// copy is read with the job's own file system, size first."""
    calls = []

    class Status:
        def getLen(self):
            return len(PACKAGE)

    class FileSystem:
        def getFileStatus(self, path):
            calls.append(("status", path.uri))
            return Status()

        def copyToLocalFile(self, delete, src, dst, raw):
            calls.append(("copy", delete, src.uri, dst.uri, raw))
            with open(dst.uri, "wb") as handle:
                handle.write(PACKAGE)

    class Path:
        def __init__(self, uri):
            self.uri = uri

        def getFileSystem(self, conf):
            return FileSystem()

    jvm = types.SimpleNamespace(
        org=types.SimpleNamespace(
            apache=types.SimpleNamespace(
                hadoop=types.SimpleNamespace(fs=types.SimpleNamespace(Path=Path))
            )
        )
    )
    context = types.SimpleNamespace(
        _jvm=jvm, _jsc=types.SimpleNamespace(hadoopConfiguration=lambda: None)
    )
    spark = types.SimpleNamespace(sparkContext=context)
    uri = "s3://commerce-lake/_fluid/spark/p/b/package/0/package.zip"
    path = runner.fetch_package(spark, uri, SHA)
    assert open(path, "rb").read() == PACKAGE
    assert calls[0] == ("status", uri)
    assert calls[1][:3] == ("copy", False, uri) and calls[1][4] is True


# --- the entry point --------------------------------------------------------------------


@pytest.fixture
def importable(tmp_path):
    """Put zips on sys.path as addPyFile does, and forget what they imported afterwards."""
    added, before = [], set(sys.modules)

    def add(body, name="pkg.zip"):
        path = tmp_path / name
        path.write_bytes(body)
        sys.path.insert(1, str(path))
        added.append(str(path))
        return str(path)

    yield add
    for path in added:
        sys.path.remove(path)
    for name in set(sys.modules) - before:
        del sys.modules[name]


TRANSFORM = "def run(spark, context):\n    return (spark, context)\n"


def test_the_entry_point_is_imported_from_the_package(runner, importable):
    path = importable(package_zip({"fluidpkg_a/__init__.py": "", "fluidpkg_a/jobs.py": TRANSFORM}))
    function = runner.import_entry_point("fluidpkg_a.jobs:run", [path])
    assert function("s", "c") == ("s", "c")


def test_a_nested_attribute_is_followed(runner, importable):
    body = "class Jobs:\n    @staticmethod\n    def run(spark, context):\n        return 1\n"
    path = importable(package_zip({"fluidpkg_b/__init__.py": body}))
    assert runner.import_entry_point("fluidpkg_b:Jobs.run", [path])(None, None) == 1


def test_a_module_from_elsewhere_is_refused(runner, importable, tmp_path):
    """A module of the same name earlier on the path (an old copy in the image, say) is
    never run in the package's place."""
    shadow = tmp_path / "shadow"
    (shadow / "fluidpkg_c").mkdir(parents=True)
    (shadow / "fluidpkg_c" / "__init__.py").write_text("")
    (shadow / "fluidpkg_c" / "jobs.py").write_text(TRANSFORM)
    sys.path.insert(0, str(shadow))
    try:
        path = importable(
            package_zip({"fluidpkg_c/__init__.py": "", "fluidpkg_c/jobs.py": TRANSFORM})
        )
        with pytest.raises(runner.PackageError, match="not from the package"):
            runner.import_entry_point("fluidpkg_c.jobs:run", [path])
    finally:
        sys.path.remove(str(shadow))


def test_a_module_imported_before_the_package_is_refused(runner, importable, monkeypatch):
    monkeypatch.setitem(sys.modules, "fluidpkg_d", types.ModuleType("fluidpkg_d"))
    path = importable(package_zip({"fluidpkg_d/__init__.py": "", "fluidpkg_d/jobs.py": TRANSFORM}))
    with pytest.raises(runner.PackageError, match="imported before the package"):
        runner.import_entry_point("fluidpkg_d.jobs:run", [path])


@pytest.mark.parametrize(
    ("files", "entry", "message"),
    [
        ({"fluidpkg_e/__init__.py": ""}, "fluidpkg_e.jobs:run", "importing fluidpkg_e.jobs"),
        (
            {"fluidpkg_f/__init__.py": "import fluid_no_such_dependency\n"},
            "fluidpkg_f:run",
            "No module named",
        ),
        ({"fluidpkg_g/__init__.py": ""}, "fluidpkg_g:run", "has no run"),
        ({"fluidpkg_h/__init__.py": "run = 3\n"}, "fluidpkg_h:run", "not a function"),
        (
            {"fluidpkg_i/__init__.py": "def run(spark):\n    return spark\n"},
            "fluidpkg_i:run",
            r"takes \(spark, context\)",
        ),
    ],
)
def test_an_entry_point_the_runner_cannot_call_is_refused(
    runner, importable, files, entry, message
):
    path = importable(package_zip(files))
    with pytest.raises(runner.PackageError, match=message):
        runner.import_entry_point(entry, [path])


def test_the_context_is_read_only(runner):
    context = runner.PackageContext(
        arguments=types.MappingProxyType({"a": "b"}),
        product="p",
        build="b",
        output="fluid.db.t",
        run_id="app-1",
        package_sha256=SHA,
    )
    assert context.arguments["a"] == "b" and context.output == "fluid.db.t"
    with pytest.raises(AttributeError):
        context.output = "fluid.db.other"
    with pytest.raises(AttributeError):
        del context.arguments
    with pytest.raises(TypeError):
        context.arguments["a"] = "c"


# --- the writes a package may not make -------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM commerce.orders_raw",
        "  select 1",
        "-- the orders\nSELECT order_id FROM t",
        "-- the orders\rSELECT order_id FROM t",
        "/* a /* nested */ comment */ SELECT 1",
        "WITH a AS (SELECT 1 AS x) SELECT comment, x FROM a",
        "WITH a AS (INSERT_ME) SELECT 1",  # a word, not the verb
        "FROM t SELECT update_ts",
        "(SELECT 1) UNION (SELECT 2)",
        "VALUES (1, 'INSERT INTO t')",
        "TABLE commerce.orders_raw",
        "SELECT `insert` FROM t",
        "SELECT 'it''s', \"x\\\"y\", r'\\', X'1F'",
        "SHOW TABLES",
        "DESCRIBE TABLE t",
        "EXPLAIN INSERT INTO t SELECT 1",
        "SET spark.sql.shuffle.partitions = 8",
        "CACHE TABLE c AS SELECT * FROM t",
        "UNCACHE TABLE c",
        "REFRESH TABLE t",
        "USE commerce",
        "CREATE TEMP VIEW v AS SELECT 1",
        "create or replace global temporary view v as select 1",
    ],
)
def test_reads_pass_the_sql_check(runner, sql):
    assert runner.sql_write(sql) is None


@pytest.mark.parametrize(
    ("sql", "verb"),
    [
        ("INSERT INTO commerce.orders SELECT * FROM v", "INSERT"),
        ("insert overwrite directory 's3://b/x' select 1", "INSERT"),
        ("MERGE INTO t USING v ON t.id = v.id WHEN MATCHED THEN DELETE", "MERGE"),
        ("UPDATE t SET x = 1", "UPDATE"),
        ("DELETE FROM t", "DELETE"),
        ("CREATE TABLE t (x INT) USING iceberg", "CREATE"),
        ("CREATE TABLE t AS SELECT 1", "CREATE"),
        ("CREATE VIEW v AS SELECT 1", "CREATE"),
        ("CREATE TEMPORARY FUNCTION f AS 'x.Y' USING JAR 's3://b/f.jar'", "CREATE"),
        ("REPLACE TABLE t AS SELECT 1", "REPLACE"),
        ("ALTER TABLE t SET TBLPROPERTIES ('a' = 'b')", "ALTER"),
        ("DROP TABLE t", "DROP"),
        ("TRUNCATE TABLE t", "TRUNCATE"),
        ("CALL fluid.system.rollback_to_snapshot('db.t', 1)", "CALL"),
        ("ADD JAR s3://b/x.jar", "ADD"),
        ("LOAD DATA INPATH 's3://b/x' INTO TABLE t", "LOAD"),
        ("MSCK REPAIR TABLE t", "MSCK"),
        ("ANALYZE TABLE t COMPUTE STATISTICS", "ANALYZE"),
        ("WITH a AS (SELECT 1) INSERT INTO t SELECT * FROM a", "INSERT"),
        ("FROM v INSERT INTO t SELECT *", "INSERT"),
        ("/* SELECT */ INSERT INTO t SELECT 1", "INSERT"),
        ("-- SELECT\nDELETE FROM t", "DELETE"),
        # A lone carriage return ends a line comment, as Spark's lexer ends it.
        ("WITH a AS (SELECT 1) -- c\r INSERT INTO t SELECT * FROM a", "INSERT"),
        ("FROM v -- c\rINSERT INTO t SELECT *", "INSERT"),
        # Spark reads a backslash-newline as more comment; reading the line as SQL refuses.
        ("WITH a AS (SELECT 1) -- c\\\n INSERT INTO t SELECT * FROM a", "INSERT"),
        ("SELECT 'unterminated", "cannot read"),
        ("SELECT /* unterminated", "cannot read"),
        ("", "no statement"),
    ],
)
def test_writes_fail_the_sql_check(runner, sql, verb):
    assert verb in runner.sql_write(sql)


class _Writer:
    def save(self):
        return "saved"

    def append(self):
        return "appended"


class _Session:
    def sql(self, query, *args, **kwargs):
        return ("ran", query)


@pytest.fixture
def fake_pyspark(monkeypatch):
    """Just enough pyspark for the guard: the classes it patches."""
    readwriter = types.ModuleType("pyspark.sql.readwriter")
    readwriter.DataFrameWriter = type("DataFrameWriter", (_Writer,), {"save": _Writer.save})
    readwriter.DataFrameWriterV2 = type("DataFrameWriterV2", (), {"append": _Writer.append})
    session = types.ModuleType("pyspark.sql.session")
    session.SparkSession = type("SparkSession", (), {"sql": _Session.sql})
    for name, module in (("pyspark.sql.readwriter", readwriter), ("pyspark.sql.session", session)):
        monkeypatch.setitem(sys.modules, name, module)
    return readwriter, session


def test_writes_are_refused_while_the_package_runs_and_allowed_after(runner, fake_pyspark):
    readwriter, session = fake_pyspark
    writer, spark = readwriter.DataFrameWriter(), session.SparkSession()
    guard = runner.WriteGuard()
    with guard:
        with pytest.raises(runner.PackageError, match="DataFrameWriter.save is refused"):
            writer.save()
        with pytest.raises(runner.PackageError, match="DataFrameWriterV2.append"):
            readwriter.DataFrameWriterV2().append()
        with pytest.raises(runner.PackageError, match="an? INSERT statement is refused"):
            spark.sql("INSERT INTO t SELECT 1")
        assert spark.sql("SELECT 1") == ("ran", "SELECT 1")
    assert guard.refused == ["DataFrameWriter.save", "DataFrameWriterV2.append", "spark.sql INSERT"]
    assert writer.save() == "saved"  # the runner's own writes, after
    assert spark.sql("INSERT INTO t SELECT 1") == ("ran", "INSERT INTO t SELECT 1")


def test_the_guard_comes_off_when_the_package_raises(runner, fake_pyspark):
    readwriter, _ = fake_pyspark
    with pytest.raises(ValueError):
        with runner.WriteGuard():
            raise ValueError("the package's own error")
    assert readwriter.DataFrameWriter().save() == "saved"


# --- the output -------------------------------------------------------------------------


def _schema(*fields):
    return {
        "type": "struct",
        "fields": [{"name": n, "type": t, "nullable": True, "metadata": {}} for n, t in fields],
    }


CUSTOMER = {
    "type": "struct",
    "fields": [
        {"name": "id", "type": "string", "nullable": False, "metadata": {}},
        {"name": "email", "type": "string", "nullable": True, "metadata": {}},
    ],
}
DECLARED = _schema(
    ("order_id", "string"),
    ("customer", CUSTOMER),
    ("amount", "decimal(18,2)"),
    ("order_date", "date"),
)


def test_the_declared_output_conforms_in_any_order_and_any_nullability(runner):
    reordered = copy.deepcopy(CUSTOMER)
    reordered["fields"].reverse()
    for field in reordered["fields"]:
        field["nullable"] = True
    actual = _schema(
        ("amount", "decimal(18,2)"),
        ("order_date", "date"),
        ("customer", reordered),
        ("order_id", "string"),
    )
    assert runner.conformance_problems(actual, DECLARED) == []


def test_every_difference_is_named(runner):
    wider = copy.deepcopy(CUSTOMER)
    wider["fields"].append({"name": "phone", "type": "string", "nullable": True, "metadata": {}})
    actual = _schema(
        ("order_id", "string"),
        ("order_id", "string"),
        ("customer", wider),
        ("Amount", "decimal(18,2)"),
        ("order_date", "timestamp"),
        ("note", "string"),
    )
    problems = runner.conformance_problems(actual, DECLARED)
    assert "order_id appears 2 times" in problems
    assert any(
        p.startswith("customer is struct<id:string,email:string,phone:string>") for p in problems
    )
    assert "amount is missing (the package returns Amount; names are case-sensitive)" in problems
    assert "order_date is timestamp, declared date" in problems
    assert "note is not declared" in problems
    assert not any("Amount is not declared" in p for p in problems)


@pytest.mark.parametrize(
    ("actual", "declared"),
    [
        (
            {"type": "array", "elementType": "int", "containsNull": True},
            {"type": "array", "elementType": "long", "containsNull": True},
        ),
        (
            {"type": "map", "keyType": "string", "valueType": "int", "valueContainsNull": True},
            {"type": "map", "keyType": "string", "valueType": "string", "valueContainsNull": False},
        ),
        ("decimal(18,2)", "decimal(38,9)"),
        ("timestamp", "timestamp_ntz"),
    ],
)
def test_a_type_must_be_the_declared_one_exactly(runner, actual, declared):
    assert runner.conformance_problems(_schema(("x", actual)), _schema(("x", declared)))


@pytest.mark.parametrize("names", [("foo", "FOO"), ("Foo", "foo")])
def test_names_that_differ_only_in_case_are_one_column(runner, names):
    """Spark resolves names regardless of case: two of them are one ambiguous column."""
    problems = runner.conformance_problems(
        _schema(*((n, "string") for n in names)), _schema(("foo", "string"))
    )
    assert problems == [f"{names[0]}, {names[1]} differ only in case, and are one column to Spark"]


def test_nullability_inside_arrays_and_maps_is_the_row_checks_business(runner):
    actual = {"type": "array", "elementType": "int", "containsNull": True}
    declared = {"type": "array", "elementType": "int", "containsNull": False}
    assert runner.conformance_problems(_schema(("x", actual)), _schema(("x", declared))) == []


@pytest.mark.parametrize(
    ("failed", "dead", "outcome"),
    [
        (0, None, (False, True)),
        (0, {"maxRecordsBeforeAbort": 0}, (False, True)),
        (3, None, (False, False)),
        (3, {"maxRecordsBeforeAbort": 3}, (True, True)),
        (4, {"maxRecordsBeforeAbort": 3}, (True, False)),
        (1, {"maxRecordsBeforeAbort": 0}, (True, False)),
    ],
)
def test_what_a_run_lands_and_writes(runner, failed, dead, outcome):
    """(land the dead letters, write the output)."""
    assert runner.package_outcome(failed, dead) == outcome


def test_the_entry_point_text_never_reaches_the_runner_s_log_as_data(runner, capsys):
    """The log names the package by digest and entry point, never a row."""
    runner.log("start", package_sha256=SHA, entry_point="orders_pkg.jobs:merge_orders")
    line = json.loads(capsys.readouterr().out)
    assert line == {
        "forge_spark_runner": "start",
        "entry_point": "orders_pkg.jobs:merge_orders",
        "package_sha256": SHA,
    }


def test_the_jobs_fixture_is_a_real_module():
    compile(JOBS, "jobs.py", "exec")


def test_a_pinned_zip_that_does_not_read_is_refused(runner, tmp_path):
    body = bytearray(package_zip({"orders_pkg/__init__.py": "x = 1\n"}))
    at = body.find(b"PK\x01\x02")  # the central directory: is_zipfile still says yes
    body[at : at + 4] = b"XXXX"
    body = bytes(body)
    with pytest.raises(runner.PackageError, match="not a readable zip"):
        runner.fetch_package(None, str(_copy(tmp_path, body)), hashlib.sha256(body).hexdigest())


def test_a_remote_read_that_fails_names_the_java_error(runner, tmp_path):
    class Denied(Exception):
        class java_exception:  # Py4J keeps the Java exception here
            @staticmethod
            def getClass():
                return types.SimpleNamespace(getSimpleName=lambda: "AccessDeniedException")

    class FileSystem:
        def getFileStatus(self, path):
            return types.SimpleNamespace(getLen=lambda: 10)

        def copyToLocalFile(self, *args):
            raise Denied("a message that is never shown")

    class Path:
        def __init__(self, uri):
            self.uri = uri

        def getFileSystem(self, conf):
            return FileSystem()

    jvm = types.SimpleNamespace(
        org=types.SimpleNamespace(
            apache=types.SimpleNamespace(
                hadoop=types.SimpleNamespace(fs=types.SimpleNamespace(Path=Path))
            )
        )
    )
    spark = types.SimpleNamespace(
        sparkContext=types.SimpleNamespace(
            _jvm=jvm, _jsc=types.SimpleNamespace(hadoopConfiguration=lambda: None)
        )
    )
    with pytest.raises(runner.PackageError, match=r"\(AccessDeniedException\)") as caught:
        runner.fetch_package(spark, "s3://b/_fluid/spark/p/b/package/0/package.zip", SHA)
    assert "never shown" not in str(caught.value)


@pytest.mark.parametrize(
    ("known", "now", "main_now", "changed"),
    [
        ({1}, {1: "append", 2: "append"}, 2, True),  # a write the runner did not make
        ({1}, {1: "append", 2: "overwrite"}, 2, True),
        ({1}, {1: "append", 2: "delete"}, 2, True),
        ({1}, {1: "append", 2: "replace"}, 2, False),  # a compaction: the same rows
        ({0, 1}, {1: "append"}, 1, False),  # an older snapshot expired: no data changed
        ({0, 1}, {0: "append", 1: "append"}, 0, True),  # main moved back
        ({1}, {1: "append"}, 1, False),
        (set(), {}, None, False),  # an empty table stays empty
    ],
)
def test_maintenance_is_not_a_write_and_a_rollback_is(
    runner, monkeypatch, known, now, main_now, changed
):
    monkeypatch.setattr(runner, "_snapshots", lambda spark, table: dict(now))
    monkeypatch.setattr(runner, "_main_snapshot", lambda spark, table: main_now)
    main_before = max(known) if known else None
    assert runner.changed_since(None, "fluid.db.t", (frozenset(known), main_before)) is changed


@pytest.fixture
def made_dirs(runner, monkeypatch):
    """The directories fetch_package makes, to see none outlives a refusal."""
    made = []
    real = runner.tempfile.mkdtemp

    def mkdtemp(**kw):
        made.append(real(**kw))
        return made[-1]

    monkeypatch.setattr(runner.tempfile, "mkdtemp", mkdtemp)
    return made


@pytest.mark.parametrize(
    "body", [package_zip({"orders_pkg/__init__.py": "x = 1\n"}), b"pinned, but not a zip"]
)
def test_a_refused_package_leaves_no_copy_behind(runner, tmp_path, made_dirs, body):
    sha = SHA if body.startswith(b"PK") else hashlib.sha256(body).hexdigest()
    with pytest.raises(runner.PackageError):
        runner.fetch_package(None, str(_copy(tmp_path, body)), sha)
    assert made_dirs and not any(os.path.exists(d) for d in made_dirs)


def test_a_zip_python_cannot_open_is_refused_by_name(runner, tmp_path, monkeypatch):
    def unsupported(*args, **kwargs):
        raise NotImplementedError("zip file version 23.6")

    monkeypatch.setattr(runner.zipfile, "ZipFile", unsupported)
    with pytest.raises(runner.PackageError, match=r"not a readable zip \(NotImplementedError\)"):
        runner.fetch_package(None, str(_copy(tmp_path)), SHA)


def test_main_runs_a_package_config_by_its_location_with_the_run_s_stamps(
    runner, contract, monkeypatch
):
    """The package's copy is found beside the job config read from --config, and the run's
    commits carry the stamps fluid apply's flags name, as any run's do."""
    session = types.SimpleNamespace(builder=types.SimpleNamespace(getOrCreate=lambda: "spark"))
    monkeypatch.setitem(sys.modules, "pyspark", types.ModuleType("pyspark"))
    monkeypatch.setitem(sys.modules, "pyspark.sql", types.SimpleNamespace(SparkSession=session))
    text = config_text(contract)
    monkeypatch.setattr(runner, "read_text", lambda spark, path: text)
    seen = []
    monkeypatch.setattr(runner, "run", lambda *a: seen.append(("run", a)))
    monkeypatch.setattr(runner, "run_package", lambda *a: seen.append(("package", a)))
    uri = "s3://commerce-lake/_fluid/spark/commerce.orders-silver/merge-orders/job.json"
    assert runner.main(["--config", uri, "--fluid-run-id", "scheduled__2026-11-21"]) == 0
    ((kind, (spark, config, where, stamps)),) = seen
    assert (kind, spark, where) == ("package", "spark", uri)
    assert config["package"]["sha256"] == SHA
    assert stamps["fluid.run-id"] == "scheduled__2026-11-21"
    assert stamps["fluid.job-sha256"] == runner.config_digest(text)
