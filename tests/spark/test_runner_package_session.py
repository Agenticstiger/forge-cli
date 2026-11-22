# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The runner, unchanged, running a prebuilt package by digest on a local Spark 3.5 session
with an Iceberg catalog: what lands, what goes to the dead letters, and the writes it stops.

Opt-in: pyspark importable, a JDK, and ``FLUID_SPARK_ICEBERG_JAR`` naming an
``iceberg-spark-runtime-3.5_2.12`` jar on disk (nothing is downloaded). The catalog is a
Hadoop one in a temporary directory: no network, no Docker, no cloud.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from fluid_build.spark import names
from fluid_build.spark.compiler import LOCAL_NAMESPACE_ALLOWLIST_ENV, compile_job_config
from fluid_build.spark.package import PACKAGE_ALLOWLIST_ENV

from .package_fixtures import JOBS, PACKAGE_CONTRACT, package_zip

pyspark = pytest.importorskip("pyspark")
JAR = os.environ.get("FLUID_SPARK_ICEBERG_JAR", "")
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not (JAR and Path(JAR).is_file()), reason="FLUID_SPARK_ICEBERG_JAR unset"),
    pytest.mark.skipif(
        shutil.which("java") is None and not os.environ.get("JAVA_HOME"), reason="no JDK"
    ),
]

PRODUCT, BUILD = "commerce.orders-silver", "merge-orders"
OUTPUT, DEAD, RAW = (
    "fluid.commerce.orders",
    "fluid.commerce.orders__dead_letter",
    "fluid.commerce.orders_raw",
)


@pytest.fixture(scope="module")
def spark(tmp_path_factory):
    from pyspark.sql import SparkSession

    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)
    warehouse = tmp_path_factory.mktemp("warehouse")
    session = (
        SparkSession.builder.master("local[2]")
        .appName("fluid-package-test")
        .config("spark.jars", JAR)
        .config(
            "spark.sql.extensions",
            "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        )
        .config("spark.sql.catalog.fluid", "org.apache.iceberg.spark.SparkCatalog")
        .config("spark.sql.catalog.fluid.type", "hadoop")
        .config("spark.sql.catalog.fluid.warehouse", str(warehouse))
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "2")
        .getOrCreate()
    )
    session.sql("CREATE NAMESPACE IF NOT EXISTS fluid.commerce")
    session.sql(
        f"CREATE TABLE {RAW} (order_id STRING, customer_id STRING, email STRING, "
        "amount DOUBLE, ordered_at TIMESTAMP) USING iceberg"
    )
    session.sql(
        f"INSERT INTO {RAW} VALUES "
        "('o1', 'c1', 'a@example.com', 12.5, TIMESTAMP '2026-11-20 10:00:00'), "
        "('o2', 'c2', NULL, 3.0, TIMESTAMP '2026-11-20 11:00:00'), "
        "('o3', NULL, 'x@example.com', 1.0, TIMESTAMP '2026-11-20 12:00:00')"
    )
    yield session
    session.stop()


@pytest.fixture
def runner_module():
    import importlib.util

    from .conftest import RUNNER_PATH

    spec = importlib.util.spec_from_file_location("forge_spark_runner_session", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _drop(spark, *tables):
    for table in tables:
        spark.sql(f"DROP TABLE IF EXISTS {table}")


def _deploy(tmp_path, monkeypatch, jobs: str = JOBS, dead=None):
    """A local package build, laid out as fluid apply lays it out: job.json, and the
    package's copy beside it."""
    body = package_zip({"orders_pkg/__init__.py": "", "orders_pkg/jobs.py": jobs})
    sha = hashlib.sha256(body).hexdigest()
    wheel = tmp_path / "dist" / "orders_pkg.zip"
    wheel.parent.mkdir(parents=True, exist_ok=True)
    wheel.write_bytes(body)
    monkeypatch.setenv(PACKAGE_ALLOWLIST_ENV, f"file://{wheel.parent}/")
    monkeypatch.setenv(LOCAL_NAMESPACE_ALLOWLIST_ENV, "fluid/commerce")
    contract = copy.deepcopy(PACKAGE_CONTRACT)
    build = contract["builds"][0]
    build["properties"]["parameters"]["spark"] = {"runtime": "local"}
    package = build["properties"]["package"]
    package.update(uri=f"file://{wheel}", sha256=sha, arguments={"source": RAW})
    if dead is None:
        package.pop("deadLetter")
    else:
        package["deadLetter"] = dead
    contract["exposes"][0]["binding"] = {
        "platform": "local",
        "format": "iceberg",
        "location": {"catalog": "lakekeeper", "database": "commerce", "table": "orders"},
    }
    job = compile_job_config(contract, build)
    root = tmp_path / "artifacts"
    config_path = Path(names.job_config_uri(str(root), PRODUCT, BUILD))
    config_path.parent.mkdir(parents=True)
    config_path.write_text(job.text)
    copy_path = Path(names.package_uri(str(root), PRODUCT, BUILD, sha))
    copy_path.parent.mkdir(parents=True)
    copy_path.write_bytes(body)
    return json.loads(job.text), str(config_path), sha


def _snapshots(spark, table):
    return spark.sql(f"SELECT snapshot_id, summary FROM {table}.snapshots").collect()


def _forget_package():
    for name in [m for m in sys.modules if m == "orders_pkg" or m.startswith("orders_pkg.")]:
        del sys.modules[name]


@pytest.fixture(autouse=True)
def clean(spark):
    _forget_package()
    yield
    _forget_package()
    _drop(spark, OUTPUT, DEAD)


def test_the_package_runs_by_digest_and_the_runner_writes_and_checks(
    spark, runner_module, tmp_path, monkeypatch
):
    config, config_uri, sha = _deploy(tmp_path, monkeypatch, dead={"maxRecordsBeforeAbort": 10})
    runner_module.run_package(spark, runner_module.parse_config(json.dumps(config)), config_uri)
    rows = {r["order_id"]: r for r in spark.table(OUTPUT).collect()}
    assert sorted(rows) == ["o1", "o2"]  # o3 has no customer id, which the contract requires
    assert rows["o1"]["customer"]["id"] == "c1" and str(rows["o1"]["amount"]) == "12.50"
    assert spark.table(OUTPUT).columns == ["order_id", "customer", "amount", "order_date"]
    (commit,) = _snapshots(spark, OUTPUT)
    assert commit["summary"]["fluid.package.sha256"] == sha
    (letter,) = spark.table(DEAD).collect()
    assert letter["error"] == "required field customer.id is null"
    assert json.loads(letter["row_json"])["order_id"] == "o3"
    assert letter["run_id"] == spark.sparkContext.applicationId
    assert letter["package_sha256"] == sha
    # A re-attempt of the same run lands its dead letters once; the output is replaced.
    _forget_package()
    runner_module.run_package(spark, runner_module.parse_config(json.dumps(config)), config_uri)
    assert spark.table(DEAD).count() == 1
    assert spark.table(OUTPUT).count() == 2


def test_without_dead_letters_a_failing_row_writes_nothing(
    spark, runner_module, tmp_path, monkeypatch
):
    config, config_uri, _ = _deploy(tmp_path, monkeypatch)
    with pytest.raises(runner_module.PackageError, match="required field customer.id is null: 1"):
        runner_module.run_package(spark, runner_module.parse_config(json.dumps(config)), config_uri)
    assert spark.table(OUTPUT).count() == 0 and not _snapshots(spark, OUTPUT)


def test_past_the_cap_the_dead_letters_land_and_the_output_does_not(
    spark, runner_module, tmp_path, monkeypatch
):
    config, config_uri, _ = _deploy(tmp_path, monkeypatch, dead={"maxRecordsBeforeAbort": 0})
    with pytest.raises(runner_module.PackageError, match="past maxRecordsBeforeAbort 0"):
        runner_module.run_package(spark, runner_module.parse_config(json.dumps(config)), config_uri)
    assert spark.table(DEAD).count() == 1
    assert spark.table(OUTPUT).count() == 0


WRITES = (
    JOBS
    + """

def writes(spark, context):
    frame = merge_orders(spark, context)
    frame.writeTo(context.output).append()
    return frame
"""
)


def test_a_package_that_writes_is_stopped_and_fails_the_run(
    spark, runner_module, tmp_path, monkeypatch
):
    config, config_uri, _ = _deploy(tmp_path, monkeypatch, jobs=WRITES)
    config["package"]["entryPoint"] = "orders_pkg.jobs:writes"
    with pytest.raises(runner_module.PackageError, match="DataFrameWriterV2.append"):
        runner_module.run_package(spark, runner_module.parse_config(json.dumps(config)), config_uri)
    assert not _snapshots(spark, OUTPUT)


AROUND = (
    JOBS
    + """

def around(spark, context):
    frame = merge_orders(spark, context)
    # Past PySpark, straight to the JVM: the runner sees the commit and fails the run.
    spark._jsparkSession.sql(
        "INSERT INTO " + context.output + " SELECT 'x', named_struct('id', 'c', 'email', "
        "CAST(NULL AS STRING)), CAST(1 AS DECIMAL(18,2)), DATE '2026-11-20'"
    )
    return frame
"""
)


def test_a_commit_made_around_the_runner_fails_the_run(spark, runner_module, tmp_path, monkeypatch):
    config, config_uri, _ = _deploy(tmp_path, monkeypatch, jobs=AROUND)
    config["package"]["entryPoint"] = "orders_pkg.jobs:around"
    with pytest.raises(runner_module.PackageError, match="changed while the package ran"):
        runner_module.run_package(spark, runner_module.parse_config(json.dumps(config)), config_uri)
    # The runner wrote nothing of its own: the one commit is the package's.
    (commit,) = _snapshots(spark, OUTPUT)
    assert "fluid.package.sha256" not in commit["summary"]


AT_IMPORT = (
    JOBS
    + """

from pyspark.sql import SparkSession

# Run as the module is imported, before any entry point is called.
SparkSession.getActiveSession().sql(
    "INSERT INTO fluid.commerce.orders SELECT 'x', named_struct('id', 'c', 'email', "
    "CAST(NULL AS STRING)), CAST(1 AS DECIMAL(18,2)), DATE '2026-11-20'"
)
"""
)


def test_a_write_the_package_makes_as_it_is_imported_is_stopped(
    spark, runner_module, tmp_path, monkeypatch
):
    config, config_uri, _ = _deploy(tmp_path, monkeypatch, jobs=AT_IMPORT)
    with pytest.raises(runner_module.PackageError, match="INSERT statement is refused"):
        runner_module.run_package(spark, runner_module.parse_config(json.dumps(config)), config_uri)
    assert not _snapshots(spark, OUTPUT)


WRONG = JOBS.replace('.cast("decimal(18,2)")', '.cast("double")')


def test_an_output_of_other_types_writes_nothing(spark, runner_module, tmp_path, monkeypatch):
    config, config_uri, _ = _deploy(tmp_path, monkeypatch, jobs=WRONG)
    with pytest.raises(runner_module.PackageError, match="amount is double, declared decimal"):
        runner_module.run_package(spark, runner_module.parse_config(json.dumps(config)), config_uri)
    assert not _snapshots(spark, OUTPUT)


def test_a_copy_that_is_not_the_pinned_bytes_is_never_imported(
    spark, runner_module, tmp_path, monkeypatch
):
    config, config_uri, sha = _deploy(tmp_path, monkeypatch)
    location = runner_module.package_location(config_uri, sha)
    Path(location).write_bytes(package_zip({"orders_pkg/__init__.py": "raise SystemExit(3)\n"}))
    with pytest.raises(runner_module.PackageError, match="not the one the job config pins"):
        runner_module.run_package(spark, runner_module.parse_config(json.dumps(config)), config_uri)
    assert "orders_pkg" not in sys.modules


#: Statements Spark's own parser reads as a write, however the text hides the verb.
HIDDEN_WRITES = [
    "INSERT INTO fluid.commerce.orders_raw SELECT * FROM fluid.commerce.orders_raw",
    "WITH a AS (SELECT 1 AS x) INSERT INTO t SELECT * FROM a",
    "WITH a AS (SELECT 1 AS x) -- c\r INSERT INTO t SELECT * FROM a",
    "FROM v INSERT INTO t SELECT *",
    "/* SELECT */ INSERT INTO t SELECT 1",
    "MERGE INTO t USING v ON t.id = v.id WHEN MATCHED THEN DELETE",
    "DELETE FROM t WHERE x = 1",
    "CREATE TABLE t AS SELECT 1",
]
#: Reads, as Spark's parser reads them.
READS = [
    "SELECT 1 -- a comment\rFROM t",
    "WITH a AS (SELECT 1 AS x) SELECT comment, x FROM a",
    "SELECT 'INSERT INTO t', `insert` FROM t",
    "CREATE OR REPLACE TEMP VIEW v AS SELECT 1",
]
_WRITE_NODES = ("InsertInto", "MergeIntoTable", "DeleteFromTable", "UpdateTable", "CreateTable")


def _parsed_write(spark, text: str) -> bool:
    plan = spark._jsparkSession.sessionState().sqlParser().parsePlan(text)
    return any(node in plan.toString() for node in _WRITE_NODES)


@pytest.mark.parametrize("text", HIDDEN_WRITES)
def test_the_sql_check_refuses_what_spark_parses_as_a_write(spark, runner_module, text):
    assert _parsed_write(spark, text)
    assert runner_module.sql_write(text) is not None


@pytest.mark.parametrize("text", READS)
def test_the_sql_check_allows_what_spark_parses_as_a_read(spark, runner_module, text):
    assert not _parsed_write(spark, text)
    assert runner_module.sql_write(text) is None


MAINTAINED = (
    JOBS
    + '''

def maintained(spark, context):
    """Table maintenance runs while the package does: a compaction and snapshot expiry."""
    jvm = spark._jsparkSession
    jvm.sql("CALL fluid.system.rewrite_manifests('commerce.orders')")
    jvm.sql(
        "CALL fluid.system.expire_snapshots(table => 'commerce.orders', "
        "older_than => TIMESTAMP '9999-01-01 00:00:00', retain_last => 1)"
    )
    return merge_orders(spark, context)
'''
)


def test_table_maintenance_during_the_run_is_not_a_write(
    spark, runner_module, tmp_path, monkeypatch
):
    config, config_uri, _ = _deploy(tmp_path, monkeypatch, dead={"maxRecordsBeforeAbort": 10})
    runner_module.run_package(spark, runner_module.parse_config(json.dumps(config)), config_uri)
    spark.sql(f"INSERT INTO {OUTPUT} SELECT * FROM {OUTPUT}")  # a second data file to compact
    _forget_package()
    config, config_uri, _ = _deploy(
        tmp_path / "again", monkeypatch, jobs=MAINTAINED, dead={"maxRecordsBeforeAbort": 10}
    )
    config["package"]["entryPoint"] = "orders_pkg.jobs:maintained"
    runner_module.run_package(spark, runner_module.parse_config(json.dumps(config)), config_uri)
    assert spark.table(OUTPUT).count() == 2
    operations = [
        r["operation"] for r in spark.sql(f"SELECT operation FROM {OUTPUT}.snapshots").collect()
    ]
    assert "replace" in operations  # the compaction ran during the package, and was let be


OTHER_SESSION = (
    JOBS
    + """

def elsewhere(spark, context):
    return merge_orders(spark.newSession(), context)
"""
)


def test_a_dataframe_of_another_session_is_refused(spark, runner_module, tmp_path, monkeypatch):
    config, config_uri, _ = _deploy(tmp_path, monkeypatch, jobs=OTHER_SESSION)
    config["package"]["entryPoint"] = "orders_pkg.jobs:elsewhere"
    with pytest.raises(runner_module.PackageError, match="another Spark session"):
        runner_module.run_package(spark, runner_module.parse_config(json.dumps(config)), config_uri)
    assert not _snapshots(spark, OUTPUT)


def test_a_run_leaves_no_view_and_no_copy_behind(spark, runner_module, tmp_path, monkeypatch):
    made = []
    real = runner_module.tempfile.mkdtemp

    def mkdtemp(**kw):
        made.append(real(**kw))
        return made[-1]

    monkeypatch.setattr(runner_module.tempfile, "mkdtemp", mkdtemp)
    config, config_uri, _ = _deploy(tmp_path, monkeypatch, dead={"maxRecordsBeforeAbort": 10})
    runner_module.run_package(spark, runner_module.parse_config(json.dumps(config)), config_uri)
    assert made and not any(os.path.exists(d) for d in made)
    assert not spark.catalog.tableExists("package_output")


def test_the_package_s_commits_carry_the_run_s_stamps(spark, runner_module, tmp_path, monkeypatch):
    config, config_uri, sha = _deploy(tmp_path, monkeypatch, dead={"maxRecordsBeforeAbort": 10})
    text = Path(config_uri).read_text()
    run = "scheduled__2026-11-21T00:00:00+00:00"
    stamps = runner_module.release_stamps(config, text, {"fluid.run-id": run})
    runner_module.run_package(spark, runner_module.parse_config(text), config_uri, stamps)
    (commit,) = _snapshots(spark, OUTPUT)
    assert commit["summary"]["fluid.product"] == "commerce.orders-silver"
    assert commit["summary"]["fluid.build"] == "merge-orders"
    assert commit["summary"]["fluid.job-sha256"] == runner_module.config_digest(text)
    assert commit["summary"]["fluid.run-id"] == run
    assert commit["summary"]["fluid.package.sha256"] == sha
    # The dead letters land once per scheduler run, whatever application retries it.
    (letter,) = spark.table(DEAD).collect()
    assert letter["run_id"] == run
    (dead_commit,) = _snapshots(spark, DEAD)
    assert dead_commit["summary"]["fluid.dead-letter.run"] == run
    assert dead_commit["summary"]["fluid.run-id"] == run
