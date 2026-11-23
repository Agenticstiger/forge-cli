# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid import airflow|pyspark <source> --check``: the drift gate.

A small retail project is imported once, and the contracts it wrote are the baseline. The gate
reads the code again and must say, with an exit code a CI job can trust, whether the baseline
still matches: 0 in sync, 1 drift, 2 it cannot decide. What a person completed in a contract is
never drift, and the check writes nothing.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import pytest
import yaml

from fluid_build._errors import FluidUserError
from fluid_build.cli import build_parser
from fluid_build.cli.import_cmd import run
from fluid_build.cli.import_workflow import _drift
from fluid_build.cli.import_workflow._drift import DriftItem, DriftReport

LOG = logging.getLogger("test_import_check")

FILES: Dict[str, str] = {
    "dags/orders_daily.py": """
        from datetime import datetime
        from airflow import DAG
        from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator

        default_args = {"owner": "retail-data", "retries": 1}
        JOBS = "s3://acme-code/jobs"

        with DAG(
            dag_id="orders_daily",
            schedule="0 2 * * *",
            start_date=datetime(2026, 1, 1),
            catchup=False,
            default_args=default_args,
            tags=["retail"],
        ) as dag:
            clean = SparkSubmitOperator(
                task_id="clean_orders",
                application=f"{JOBS}/clean_orders.py",
                application_args=["--date", "{{ ds }}"],
            )
            daily = SparkSubmitOperator(task_id="orders_daily", application=f"{JOBS}/orders_daily.py")
            clean >> daily
        """,
    "dags/clicks_stream.py": """
        from airflow import DAG
        from airflow.providers.amazon.aws.operators.emr import EmrServerlessStartJobOperator

        with DAG("clicks_stream", schedule=None, default_args={"owner": "web-data"}) as dag:
            EmrServerlessStartJobOperator(
                task_id="start_clicks",
                application_id="app1",
                job_driver={"sparkSubmit": {"entryPoint": "s3://acme-code/jobs/clicks_clean.py", "entryPointArguments": ["--env", "prod"]}},
            )
        """,
    "jobs/clean_orders.py": """
        from pyspark.sql import SparkSession

        spark = SparkSession.builder.appName("clean_orders").getOrCreate()
        spark.table("retail.orders_raw").filter("amount > 0").write.mode("overwrite").format("iceberg").saveAsTable("glue_catalog.retail.orders_clean")
        """,
    "jobs/orders_daily.py": """
        from pyspark.sql import SparkSession

        spark = SparkSession.builder.appName("orders_daily").getOrCreate()
        TARGET = "retail.orders_daily"
        df = spark.table("glue_catalog.retail.orders_clean").join(spark.table("retail.customers"), "customer_id")
        df.createOrReplaceTempView("joined")
        spark.sql(f"INSERT OVERWRITE TABLE {TARGET} SELECT day, sum(amount) AS total FROM joined GROUP BY day")
        """,
    "jobs/clicks_clean.py": """
        from pyspark.sql import SparkSession

        spark = SparkSession.builder.getOrCreate()
        raw = (spark.readStream.format("kafka")
               .option("kafka.bootstrap.servers", "broker-1:9092")
               .option("subscribe", "clicks_raw")
               .load())
        (raw.writeStream.format("kafka")
            .option("kafka.bootstrap.servers", "broker-1:9092")
            .option("topic", "clicks_clean")
            .option("checkpointLocation", "s3://acme-state/checkpoints/clicks_clean")
            .trigger(processingTime="30 seconds")
            .start())
        """,
    "conventions.yaml": """
        script_roots:
          "s3://acme-code/jobs/": jobs
        defaults: {domain: retail, platform: aws}
        catalogs:
          glue_catalog: {platform: aws}
        layers: {retail: Silver}
        owners:
          retail-data: {team: retail-data}
        """,
}

#: The import that wrote the baseline, and so the one the check must repeat.
ARGV = ("pyspark", "jobs", "--dags", "dags", "--conventions", "conventions.yaml", "--out", "out")
ORDERS_DAILY = Path("out/contract.retail.orders_daily.fluid.yaml")


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    for rel, text in FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # so a job is shown as jobs/x.py, as in a real checkout
    return tmp_path


def parse(*argv: str):
    return build_parser().parse_args(["import", *argv])


def written(*argv: str) -> Path:
    """Import the project once: what it wrote is the committed baseline."""
    assert run(parse(*(argv or ARGV)), LOG) == 0
    return Path.cwd()


def check(*extra: str, argv: Tuple[str, ...] = ARGV) -> int:
    return run(parse(*argv, "--check", *extra), LOG)


def report_of(capsys, *extra: str, argv: Tuple[str, ...] = ARGV) -> Tuple[int, Dict[str, Any]]:
    capsys.readouterr()
    code = check("--format", "json", *extra, argv=argv)
    return code, json.loads(capsys.readouterr().out)


def kinds(document: Dict[str, Any]) -> List[Tuple[str, str]]:
    return [(item["kind"], item["what"]) for item in document["items"]]


def snapshot(root: Path) -> Dict[str, str]:
    """Every file and folder under ``root`` with a digest of its content."""
    found: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        relative = str(path.relative_to(root))
        found[relative] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "(folder)"
        )
    return found


def edit(path: Path, change: Callable[[Dict[str, Any]], None]) -> None:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def sub(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


# ── in sync ──────────────────────────────────────────────────────────


def test_the_contracts_just_written_are_in_sync_and_the_check_writes_nothing(project, capsys):
    written()
    before = snapshot(project)
    capsys.readouterr()
    assert check() == 0
    out = capsys.readouterr().out
    assert "3 product(s) compared" in out and "In sync" in out
    assert snapshot(project) == before


def test_what_a_person_completes_is_never_drift(project):
    """Descriptions, owners, columns, a binding the import left at `other`, extra keys, an
    output, a read and a build a person added, key order and comments are all the person's."""
    written()

    def complete(doc: Dict[str, Any]) -> None:
        doc["description"] = "The daily orders table, completed by hand"
        doc["metadata"]["owner"] = {"team": "someone-else", "email": "owner@example.com"}
        doc["metadata"]["tags"] = ["finance"]
        expose = doc["exposes"][0]
        expose["contract"]["schema"] = [{"name": "day", "type": "date"}]
        expose["binding"]["platform"] = "snowflake"  # the import left it at `other`
        expose["binding"]["format"] = "iceberg"
        expose["binding"]["location"]["warehouse"] = "ANALYTICS_WH"
        doc["exposes"].append({"exposeId": "dashboard", "kind": "table", "binding": {}})
        doc["consumes"].append(
            {"productId": "finance.fx", "exposeId": "rates", "purpose": "Currency conversion"}
        )
        doc["builds"].append({"id": "report", "pattern": "embedded-logic", "engine": "sql"})

    path = project / ORDERS_DAILY
    edit(path, complete)
    reordered = yaml.safe_load(path.read_text(encoding="utf-8"))
    reordered = {key: reordered[key] for key in reversed(list(reordered))}
    path.write_text("# completed by hand\n" + yaml.safe_dump(reordered, sort_keys=False), "utf-8")
    assert check() == 0


def test_a_build_a_person_added_brings_its_own_exposes_and_none_is_drift(project):
    written()

    def add_a_dbt_mart(doc: Dict[str, Any]) -> None:
        doc["builds"].append(
            {"id": "dbt_mart", "pattern": "embedded-logic", "engine": "dbt", "outputs": ["mart"]}
        )
        doc["exposes"].append(
            {"exposeId": "mart", "kind": "table", "binding": {"platform": "snowflake"}}
        )

    edit(project / ORDERS_DAILY, add_a_dbt_mart)
    assert check("--strict") == 0


def test_a_binding_the_import_left_at_other_is_the_persons_to_name(project):
    """Naming the platform changes what the location and the format look like: none of it is
    compared until the import decides the platform itself."""
    written()

    def name_the_binding(doc: Dict[str, Any]) -> None:
        doc["exposes"][0]["binding"] = {
            "platform": "bigquery",
            "format": "bigquery_table",
            "location": {"project": "acme", "dataset": "retail", "table": "orders_daily"},
        }

    edit(project / ORDERS_DAILY, name_the_binding)
    assert check("--strict") == 0


def test_a_binding_nobody_named_is_still_compared_with_what_the_code_says(project, capsys):
    """Until a person names the platform, the location is the import's: a table that moves to
    another database is drift (split by job, so the product keeps its id)."""
    argv = (*ARGV, "--split-by", "job")
    written(*argv)
    assert check(argv=argv) == 0
    sub(
        project / "jobs/orders_daily.py",
        'TARGET = "retail.orders_daily"',
        'TARGET = "mart.orders_daily"',
    )
    code, document = report_of(capsys, argv=argv)
    assert code == 1
    (item,) = [i for i in document["items"] if i["kind"] == "table_changed"]
    assert item["what"].endswith("binding.location.database")
    assert (item["before"], item["after"]) == ("retail", "mart")


def test_a_schedule_the_code_does_not_state_as_a_literal_is_the_persons_to_write_down(
    project, capsys
):
    """The DAG of the clicks stream has `schedule=None` and sets no catchup; the orders DAG's
    schedule is an expression the import copies as written."""
    sub(project / "dags/orders_daily.py", 'schedule="0 2 * * *"', 'schedule=Variable.get("cron")')
    written()
    orders = project / "out/contract.retail.orders_clean.fluid.yaml"
    assert "Variable" in orders.read_text("utf-8")  # the import wrote the expression as written

    def write_them_down(doc: Dict[str, Any]) -> None:
        config = doc["builds"][0]["execution"]["orchestration"]["airflow"]["dagConfig"]
        config["schedule"] = "0 2 * * *"

    edit(orders, write_them_down)

    def pin_the_clicks_dag(doc: Dict[str, Any]) -> None:
        airflow = doc["builds"][0]["execution"]["orchestration"]["airflow"]
        airflow["dagConfig"] = {"schedule": "@hourly", "catchup": False}

    edit(project / "out/contract.clicks_clean.fluid.yaml", pin_the_clicks_dag)
    capsys.readouterr()
    assert check("--strict") == 0, capsys.readouterr().out
    # a literal the code states is still compared, with the person's value in its place
    sub(project / "dags/orders_daily.py", 'schedule=Variable.get("cron")', 'schedule="0 3 * * *"')
    assert check() == 1


def test_the_gate_reads_an_airflow_project_the_same_way(project, capsys):
    argv = ("airflow", "dags", "--conventions", "conventions.yaml", "--out", "out")
    written(*argv)
    assert check(argv=argv) == 0
    sub(project / "dags/orders_daily.py", "0 2 * * *", "0 4 * * *")
    code, document = report_of(capsys, argv=argv)
    assert code == 1 and document["status"] == "drift"
    assert {kind for kind, _ in kinds(document)} == {"schedule_changed"}


def test_one_product_in_one_named_file_is_checked_in_that_file(project):
    argv = ("pyspark", "jobs", "--split-by", "project", "--conventions", "conventions.yaml")
    argv += ("--out", "everything.yaml")
    written(*argv)
    assert check(argv=argv) == 0
    (project / "jobs/extra.py").write_text(
        'from pyspark.sql import SparkSession\nSparkSession.builder.getOrCreate().table("a.b")'
        '.write.saveAsTable("a.c")\n',
        encoding="utf-8",
    )
    assert check(argv=argv) == 1


# ── each kind of drift ───────────────────────────────────────────────


def test_a_new_job_is_drift_and_says_where_the_code_is(project, capsys):
    written()
    (project / "jobs/new_job.py").write_text(
        "from pyspark.sql import SparkSession\n\nspark = SparkSession.builder.getOrCreate()\n"
        'spark.table("retail.customers").write.saveAsTable("retail.customers_clean")\n',
        encoding="utf-8",
    )
    code, document = report_of(capsys)
    assert code == 1
    assert kinds(document) == [
        ("job_added", "builds[new_job]"),
        ("output_added", "exposes[customers_clean]"),
    ]
    added = {item["kind"]: item for item in document["items"]}
    assert added["job_added"]["source_ref"] == "jobs/new_job.py"
    assert added["output_added"]["source_ref"] == "jobs/new_job.py:4"
    assert (
        added["output_added"]["contract_path"] == "out/contract.retail.customers_clean.fluid.yaml"
    )
    assert added["output_added"]["product_id"] == "retail.customers_clean"
    assert {item["severity"] for item in document["items"]} == {"error"}


def test_a_changed_schedule_is_drift_for_every_job_the_dag_runs(project, capsys):
    written()
    sub(project / "dags/orders_daily.py", "0 2 * * *", "0 3 * * *")
    code, document = report_of(capsys)
    assert code == 1
    products = {item["product_id"] for item in document["items"]}
    assert products == {"retail.orders_clean", "retail.orders_daily"}
    for item in document["items"]:
        assert item["kind"] == "schedule_changed"
        assert item["what"].endswith("airflow.dagConfig.schedule")
        assert (item["before"], item["after"]) == ("0 2 * * *", "0 3 * * *")
        assert item["source_ref"] == "dags/orders_daily.py:8"


def test_a_table_that_is_written_differently_is_drift(project, capsys):
    written()
    sub(project / "jobs/clean_orders.py", 'format("iceberg")', 'format("delta")')
    code, document = report_of(capsys)
    assert code == 1
    (item,) = document["items"]
    assert item["kind"] == "table_changed"
    assert item["what"] == "exposes[orders_clean].binding.format"
    assert (item["before"], item["after"]) == ("iceberg", "delta_table")
    assert item["source_ref"] == "jobs/clean_orders.py:4"


def test_a_table_a_job_starts_reading_is_drift(project, capsys):
    written()
    sub(
        project / "jobs/orders_daily.py",
        'spark.table("retail.customers")',
        'spark.table("retail.customers").join(spark.table("retail.regions"), "id")',
    )
    code, document = report_of(capsys)
    assert code == 1
    (item,) = document["items"]
    assert item["kind"] == "table_changed"
    assert item["what"] == "consumes[retail.regions.regions]"
    assert item["product_id"] == "retail.orders_daily"
    assert item["source_ref"].startswith("jobs/orders_daily.py:")


def test_an_output_the_code_stopped_writing_is_drift(project, capsys):
    (project / "jobs/two.py").write_text(
        "from pyspark.sql import SparkSession\n\nspark = SparkSession.builder.getOrCreate()\n"
        'df = spark.table("retail.customers")\n'
        'df.write.saveAsTable("retail.two_a")\n'
        'df.write.saveAsTable("retail.two_b")\n',
        encoding="utf-8",
    )
    argv = (*ARGV, "--split-by", "job")
    written(*argv)
    assert check(argv=argv) == 0
    sub(project / "jobs/two.py", 'df.write.saveAsTable("retail.two_b")\n', "")
    code, document = report_of(capsys, argv=argv)
    assert code == 1
    (item,) = document["items"]
    assert (item["kind"], item["what"]) == ("output_removed", "exposes[two_b]")
    assert item["product_id"] == "retail.two" and item["source_ref"] is None


def test_a_dag_the_import_no_longer_finds_is_a_warning_and_drift_only_with_strict(project, capsys):
    written()
    without_dags = ("pyspark", "jobs", "--conventions", "conventions.yaml", "--out", "out")
    code, document = report_of(capsys, argv=without_dags)
    assert code == 0 and document["status"] == "in_sync"
    assert {kind for kind, _ in kinds(document)} == {"coverage_regressed"}
    assert {item["severity"] for item in document["items"]} == {"warning"}
    assert document["counts"] == {"error": 0, "warning": 3}

    code, document = report_of(capsys, "--strict", argv=without_dags)
    assert code == 1 and document["status"] == "drift"
    assert {item["severity"] for item in document["items"]} == {"error"}


def test_a_task_the_dag_no_longer_has_is_coverage_that_regressed(project, capsys):
    written()
    sub(project / "dags/orders_daily.py", 'task_id="clean_orders"', 'task_id="clean_orders_v2"')
    code, document = report_of(capsys, "--strict")
    assert code == 1
    assert (
        "coverage_regressed",
        "builds[clean_orders].execution.orchestration.airflow.tasks" "[clean_orders]",
    ) in kinds(document)


def test_a_job_that_is_gone_is_a_warning_and_drift_only_with_strict(project, capsys):
    """A build of the shape the import writes, whose script the code no longer has."""
    written()

    def add_a_retired_job(doc: Dict[str, Any]) -> None:
        retired = copy.deepcopy(doc["builds"][0])
        retired["id"] = "retired"
        retired["properties"] = {"model": "jobs/retired.py"}
        retired["outputs"] = []
        doc["builds"].append(retired)

    edit(project / ORDERS_DAILY, add_a_retired_job)
    code, document = report_of(capsys)
    assert code == 0
    assert kinds(document) == [("job_removed", "builds[retired]")]
    assert document["items"][0]["severity"] == "warning"
    assert document["items"][0]["before"] == "jobs/retired.py"

    code, document = report_of(capsys, "--strict")
    assert code == 1 and document["items"][0]["severity"] == "error"


def test_a_read_a_person_added_is_not_a_read_the_code_lost(project):
    """An entry the import wrote has its purpose ("read by <job>"); one with any other purpose
    is the person's, and stays whatever the code reads."""
    written()

    def drop_the_code_side(doc: Dict[str, Any]) -> None:
        doc["consumes"].append({"productId": "ghost", "exposeId": "table", "purpose": "my own"})
        doc["consumes"].append(
            {"productId": "gone", "exposeId": "table", "purpose": "read by jobs/orders_daily.py"}
        )

    edit(project / ORDERS_DAILY, drop_the_code_side)
    assert check() == 0  # the one the import wrote is a warning, not drift
    assert check("--strict") == 1


def test_a_check_writes_nothing_even_when_it_finds_drift(project):
    written()
    sub(project / "dags/orders_daily.py", "0 2 * * *", "0 3 * * *")
    before = snapshot(project)
    assert check() == 1
    assert check("--format", "github") == 1
    assert snapshot(project) == before


# ── it cannot decide ─────────────────────────────────────────────────


def test_no_contract_where_the_import_writes_is_not_a_clean_tree(project, capsys):
    capsys.readouterr()
    assert check() == 2
    err = capsys.readouterr().err
    assert "no committed contract is where this import would write one" in err
    assert "out/contract.retail.orders_daily.fluid.yaml" in err


def test_a_source_that_is_not_a_directory_cannot_be_decided(project, capsys):
    capsys.readouterr()
    assert check(argv=("pyspark", "nowhere", "--out", "out")) == 2
    assert "is not a directory" in capsys.readouterr().err


def test_a_directory_that_proposes_nothing_is_not_in_sync(project, capsys):
    (project / "helpers").mkdir()
    (project / "helpers/util.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    capsys.readouterr()
    assert check(argv=("pyspark", "helpers", "--out", "out")) == 2
    assert "proposes no product" in capsys.readouterr().err


def test_invalid_conventions_cannot_be_decided(project):
    written()
    (project / "conventions.yaml").write_text("script_roots: [not, a, mapping]\n", "utf-8")
    assert check() == 2


@pytest.mark.parametrize(
    "content, said",
    [
        (b"a: [unclosed\n  - : :", "is not valid YAML"),
        (b"- just\n- a list\n", "is not a contract"),
        (b"", "is not a contract"),
        (b"a: \xff\xfe\n", "cannot be read as text"),
        (b"base: &a 1\n" + b"".join(b"x%d: *a\n" % n for n in range(60)), "is refused"),
    ],
    ids=["broken yaml", "a list", "empty", "not utf-8", "too many aliases"],
)
def test_a_committed_file_that_cannot_be_read_as_a_contract_cannot_be_decided(
    project, capsys, content, said
):
    written()
    (project / ORDERS_DAILY).write_bytes(content)
    capsys.readouterr()
    assert check() == 2
    assert said in capsys.readouterr().err


def test_a_committed_file_that_is_a_link_is_never_read_through(project, capsys):
    written()
    secret = project / "elsewhere.yaml"
    secret.write_text("password: hunter2-do-not-print\n", encoding="utf-8")
    (project / ORDERS_DAILY).unlink()
    (project / ORDERS_DAILY).symlink_to(secret)
    capsys.readouterr()
    assert check() == 2
    captured = capsys.readouterr()
    assert "is not a regular file" in captured.err
    assert "hunter2" not in captured.out + captured.err


def test_a_file_over_the_cap_is_refused_unread(project, capsys, monkeypatch):
    written()
    monkeypatch.setattr(_drift, "MAX_BASELINE_BYTES", 100)
    capsys.readouterr()
    assert check() == 2
    assert "is over 100 bytes" in capsys.readouterr().err


def test_an_error_nobody_foresaw_is_2_never_drift(project, capsys, monkeypatch):
    written()

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("something nobody foresaw")

    monkeypatch.setattr(_drift, "check", boom)
    capsys.readouterr()
    assert check() == 2
    err = capsys.readouterr().err
    assert "stopped before it could decide" in err and "RuntimeError" in err


def test_the_refusals_have_their_own_shape_in_json_and_github(project, capsys):
    capsys.readouterr()
    assert check("--format", "json") == 2
    document = json.loads(capsys.readouterr().out)
    assert document["status"] == "error" and document["schema"] == 1
    assert set(document["error"]) == {"what", "why", "fix", "doc"}

    assert check("--format", "github") == 2
    (line,) = capsys.readouterr().out.splitlines()
    assert line.startswith("::error title=import check::no committed contract")


# ── the flags ────────────────────────────────────────────────────────


def test_force_is_refused_with_check_and_nothing_is_written(project, capsys):
    before = snapshot(project)
    capsys.readouterr()
    assert check("--force") == 2
    assert "--check cannot be used with --force" in capsys.readouterr().err
    assert snapshot(project) == before


@pytest.mark.parametrize("tool", ["dbt", "meltano"])
def test_check_belongs_to_the_importers_that_read_code(project, capsys, tool):
    capsys.readouterr()
    assert run(parse(tool, "x", "--check"), LOG) == 2
    assert "--check applies to `fluid import airflow` and `fluid import pyspark`" in (
        capsys.readouterr().err
    )


def test_check_is_not_for_the_directory_scan(project, capsys):
    capsys.readouterr()
    assert run(parse("--check"), LOG) == 2
    assert "the directory scan has no such thing" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra, flag",
    [
        (("--format", "json"), "--format"),
        (("--strict",), "--strict"),
        (("--out-report", "r"), "--out-report"),
    ],
)
def test_the_report_flags_need_check(project, extra, flag):
    with pytest.raises(FluidUserError) as refused:
        run(parse(*ARGV, *extra), LOG)
    assert f"{flag} applies to `fluid import <tool> <source> --check`" in refused.value.what


def test_a_dry_run_beside_check_changes_nothing(project):
    written()
    assert check("--dry-run") == 0


def test_a_registry_is_never_contacted_by_a_gate(project, monkeypatch):
    """The columns of a topic are not compared, so a registry that is down cannot fail it: the
    registry options never reach the import."""
    from fluid_build.cli.import_workflow import _pipeline

    written()
    seen: List[Dict[str, Any]] = []
    real = _pipeline.topic_schema_reader

    def spy(options: Dict[str, Any]) -> Any:
        seen.append(dict(options))
        return real(options)

    monkeypatch.setattr(_pipeline, "topic_schema_reader", spy)
    argv = (*ARGV, "--registry", "confluent", "--url", "https://registry.example.com")
    assert check(argv=argv) == 0
    assert seen and not any({"registry", "url"} & set(options) for options in seen)


# ── --out-report ─────────────────────────────────────────────────────


def test_out_report_writes_the_report_as_printed_and_nothing_else(project, capsys):
    written()
    sub(project / "dags/orders_daily.py", "0 2 * * *", "0 3 * * *")
    before = snapshot(project)
    capsys.readouterr()
    assert check("--format", "json", "--out-report", "reports/drift.json") == 1
    printed = capsys.readouterr().out
    after = snapshot(project)
    assert set(after) - set(before) == {"reports", "reports/drift.json"}
    assert {k: v for k, v in after.items() if k in before} == before
    assert json.loads((project / "reports/drift.json").read_text("utf-8")) == json.loads(printed)


def test_out_report_never_replaces_a_contract_the_check_reads(project, capsys):
    written()
    before = snapshot(project)
    capsys.readouterr()
    assert check("--out-report", str(ORDERS_DAILY)) == 2
    assert "is a contract this check reads" in capsys.readouterr().err
    assert snapshot(project) == before


def test_out_report_is_never_written_through_a_link_or_into_a_directory(project, capsys):
    written()
    target = project / "keep.txt"
    target.write_text("keep me\n", encoding="utf-8")
    (project / "link.json").symlink_to(target)
    capsys.readouterr()
    assert check("--out-report", "link.json") == 2
    assert target.read_text(encoding="utf-8") == "keep me\n"
    assert check("--out-report", "out") == 2


def test_a_report_path_that_cannot_be_written_is_the_one_thing_said(project, capsys):
    written()
    sub(project / "dags/orders_daily.py", "0 2 * * *", "0 3 * * *")
    capsys.readouterr()
    assert check("--format", "json", "--out-report", str(ORDERS_DAILY)) == 2
    document = json.loads(capsys.readouterr().out)  # one document, not the report and an error
    assert document["status"] == "error"
    assert "is a contract this check reads" in document["error"]["what"]


def test_a_refusal_is_kept_in_the_report_file_too(project):
    assert check("--format", "json", "--out-report", "refusal.json") == 2
    assert json.loads((project / "refusal.json").read_text("utf-8"))["status"] == "error"


def test_out_report_replaces_an_earlier_report_and_nothing_else(project, capsys):
    written()
    sub(project / "dags/orders_daily.py", "0 2 * * *", "0 3 * * *")
    refused = {
        "conventions.yaml": "not a report of this check",
        "jobs/clean_orders.py": "not a report of this check",
        str(ORDERS_DAILY): "is a contract this check reads",
        "dags": "cannot write the report",
    }
    for target, said in refused.items():
        before = snapshot(project)
        capsys.readouterr()
        assert check("--out-report", target) == 2, target
        assert said in capsys.readouterr().err, target
        assert snapshot(project) == before, target
    # an empty file, a report of any format, and a refusal that was kept are all replaced
    (project / "report.out").write_text("", encoding="utf-8")
    for fmt in ("json", "text", "github", "json"):
        assert check("--format", fmt, "--out-report", "report.out") == 1
    assert json.loads((project / "report.out").read_text("utf-8"))["status"] == "drift"
    (project / "report.out").write_text("::error title=import check::kept\n", encoding="utf-8")
    assert check("--out-report", "report.out") == 1


def test_an_empty_out_report_is_refused_not_ignored(project, capsys):
    written()
    capsys.readouterr()
    assert check("--out-report", "") == 2
    assert "cannot write the report" in capsys.readouterr().err


# ── the report ───────────────────────────────────────────────────────


def test_the_json_report_has_a_stable_shape(project, capsys):
    written()
    sub(project / "jobs/clean_orders.py", 'format("iceberg")', 'format("delta")')
    code, document = report_of(capsys)
    assert code == 1
    assert list(document) == [
        "schema",
        "status",
        "tool",
        "source",
        "strict",
        "products",
        "counts",
        "omitted",
        "items",
    ]
    assert document["schema"] == 1 and document["tool"] == "pyspark"
    assert document["products"] == 3 and document["omitted"] == 0
    assert list(document["items"][0]) == [
        "kind",
        "severity",
        "product_id",
        "contract_path",
        "what",
        "source_ref",
        "before",
        "after",
    ]


def test_the_text_report_groups_by_contract_and_says_what_to_do(project, capsys):
    written()
    sub(project / "dags/orders_daily.py", "0 2 * * *", "0 3 * * *")
    capsys.readouterr()
    assert check() == 1
    out = capsys.readouterr().out
    assert "✗ Drift: 2 error(s), 0 warning(s)" in out
    assert "out/contract.retail.orders_daily.fluid.yaml (retail.orders_daily)" in out
    assert '"0 2 * * *" → "0 3 * * *"' in out and "[dags/orders_daily.py:8]" in out
    assert "--force" in out


def test_the_github_report_is_one_annotation_per_item(project, capsys):
    written()
    sub(project / "dags/orders_daily.py", "0 2 * * *", "0 3 * * *")
    capsys.readouterr()
    assert check("--format", "github") == 1
    lines = capsys.readouterr().out.splitlines()
    notes = [line for line in lines if line.startswith("::")]
    assert len(notes) == 2 and len(lines) == 3
    assert notes[0].startswith(
        "::error file=out/contract.retail.orders_clean.fluid.yaml,line=1,title=schedule_changed::"
    )
    assert "%" not in lines[-1] and "error(s)" in lines[-1]


def _item(index: int, kind: str = "table_changed", **fields: Any) -> DriftItem:
    base: Dict[str, Any] = {
        "kind": kind,
        "product_id": f"p{index:04d}",
        "contract_path": f"out/c{index:04d}.yaml",
        "what": "exposes[t].binding.format",
        "before": "a",
        "after": "b",
    }
    base.update(fields)
    return DriftItem(**base)


def test_github_escapes_what_could_end_a_command_or_start_another():
    nasty = DriftItem(
        kind="table_changed",
        product_id="p",
        contract_path="a,b:c\nline=9,title=forged::error ::x",
        what="exposes[t]",
        before="50% off\r\n::error ::injected",
        after="b",
    )
    text = _drift.render(DriftReport("pyspark", "jobs", False, 1, [nasty]), "github")
    command, summary = text.split("\n")  # one line for the command: no newline got through
    assert command.startswith(
        "::error file=a%2Cb%3Ac line=9%2Ctitle=forged%3A%3Aerror %3A%3Ax,line=1,title=table_changed::"
    )
    assert "50%25 off" in command and "\r" not in command
    assert summary.startswith("Import drift check:")


def test_the_escapes_are_the_ones_github_reads():
    assert _drift.escape_data("100%\r\n:,") == "100%25%0D%0A:,"
    assert _drift.escape_property("100%\r\n:,") == "100%25%0D%0A%3A%2C"


def test_the_report_lists_a_hundred_items_and_counts_the_rest(capsys):
    items = [_item(n) for n in range(130)]
    report = DriftReport("pyspark", "jobs", False, 130, items)
    assert report.exit_code == 1  # decided on all 130, not on the 100 listed
    document = json.loads(_drift.render(report, "json"))
    assert len(document["items"]) == 100 and document["omitted"] == 30
    assert document["counts"] == {"error": 130, "warning": 0}
    text = _drift.render(report, "text")
    assert text.count("table_changed") == 100 and "30 more item(s)" in text
    github = _drift.render(report, "github").splitlines()
    assert len([line for line in github if line.startswith("::")]) == 100
    assert "30 more item(s)" in github[-1]


def test_errors_are_listed_before_warnings_so_the_cap_never_hides_one():
    warnings = [_item(n, "coverage_regressed") for n in range(150)]
    error = _item(999, "job_added")
    report = DriftReport("pyspark", "jobs", False, 151, [*warnings, error])
    listed, omitted = report.listed()
    assert listed[0] is error and omitted == 51
    assert report.exit_code == 1


def test_warnings_alone_are_in_sync_until_strict():
    items = [_item(1, "job_removed"), _item(2, "coverage_regressed")]
    assert DriftReport("pyspark", "jobs", False, 2, items).exit_code == 0
    assert DriftReport("pyspark", "jobs", True, 2, items).exit_code == 1
    assert (
        DriftReport("pyspark", "jobs", False, 2, [*items, _item(3, "output_added")]).exit_code == 1
    )


def test_a_secret_in_a_committed_value_is_never_printed(project, capsys):
    written()
    leaked = "0 2 * * * " + "pass" + "word=hunter2" + "xyz"

    def plant(doc: Dict[str, Any]) -> None:
        doc["builds"][0]["execution"]["orchestration"]["airflow"]["dagConfig"]["schedule"] = leaked

    edit(project / ORDERS_DAILY, plant)
    for fmt in ("text", "json", "github"):
        capsys.readouterr()
        assert check("--format", fmt, "--out-report", f"r.{fmt}") == 1
        captured = capsys.readouterr()
        assert "hunter2" not in captured.out + captured.err, fmt
        assert "hunter2" not in (project / f"r.{fmt}").read_text("utf-8"), fmt


def test_a_value_too_long_to_be_a_name_is_not_scanned_or_shown(project, capsys, monkeypatch):
    """The credential scan costs more than linear time on a value made to make it so, so a
    committed value of hundreds of kilobytes is counted, not scanned."""
    import time

    written()
    huge = ";a=b" * 100_000
    text = (project / ORDERS_DAILY).read_text("utf-8")
    (project / "out/contract.retail.orders_clean.fluid.yaml").write_text(
        (project / "out/contract.retail.orders_clean.fluid.yaml")
        .read_text("utf-8")
        .replace("table: orders_clean", f"table: '{huge}'"),
        encoding="utf-8",
    )
    assert text  # the file the test left alone
    seen: List[int] = []
    real = _drift.safe_text

    def spy(value: Any, limit: int = 200) -> str:
        seen.append(len(str(value)))
        return real(value, limit)

    monkeypatch.setattr(_drift, "safe_text", spy)
    started = time.monotonic()
    code, document = report_of(capsys)
    assert code == 1 and time.monotonic() - started < 30
    assert max(seen) <= _drift.MAX_DISPLAYED
    (item,) = [i for i in document["items"] if i["what"].endswith("location.table")]
    assert item["before"] == "<400,000 characters, not shown>"


def test_control_characters_in_the_code_cannot_reach_the_terminal(project, capsys):
    written()
    (project / "jobs/odd.py").write_text(
        "from pyspark.sql import SparkSession\n\nspark = SparkSession.builder.getOrCreate()\n"
        'spark.table("a.b").write.saveAsTable("a.odd\\x1b[31m\\u202e")\n',
        encoding="utf-8",
    )
    capsys.readouterr()
    code = check("--format", "github")
    out = capsys.readouterr().out
    assert code in (1, 2)
    assert "\x1b" not in out and "‮" not in out


# ── the projection ───────────────────────────────────────────────────


def test_the_projection_survives_a_contract_that_is_not_shaped_like_one():
    for garbage in (
        {},
        {"exposes": "x", "builds": [1, None, "b"], "consumes": {}},
        {"exposes": [None, {"exposeId": 7, "binding": []}], "builds": [{"id": 1, "execution": []}]},
        {"builds": [{"id": "b", "execution": {"orchestration": {"airflow": {"tasks": "t"}}}}]},
    ):
        projection = _drift.project(garbage)
        assert isinstance(projection.outputs, dict) and isinstance(projection.jobs, dict)


def test_a_yaml_date_in_a_contract_is_a_value_not_a_crash(project):
    written()
    text = (project / ORDERS_DAILY).read_text("utf-8")
    (project / ORDERS_DAILY).write_text(
        text.replace("table: orders_daily", "table: 2026-01-01"), encoding="utf-8"
    )
    assert check() == 1


# ── where the code says it ───────────────────────────────────────────


def test_the_proposal_records_where_each_field_was_read(project):
    from fluid_build.cli.import_workflow._pipeline import run_import_traced

    options = {"dags": "dags", "conventions": "conventions.yaml"}
    contracts, _report, sources = run_import_traced("pyspark", "jobs", options)
    assert [c["id"] for c in contracts] == [
        "retail.orders_clean",
        "clicks_clean",
        "retail.orders_daily",
    ]
    assert sources[("retail.orders_clean", "exposes", "orders_clean")] == "jobs/clean_orders.py:4"
    assert sources[("retail.orders_clean", "builds", "clean_orders")] == "jobs/clean_orders.py"
    assert sources[("retail.orders_clean", "schedule", "clean_orders")] == "dags/orders_daily.py:8"
    assert sources[("retail.orders_daily", "consumes", "retail.customers.customers")].startswith(
        "jobs/orders_daily.py:"
    )
    assert ("clicks_clean", "schedule", "clicks_clean") in sources


def test_run_import_still_returns_the_contracts_and_the_report(project):
    from fluid_build.cli.import_workflow._pipeline import run_import, run_import_traced

    options = {"dags": "dags", "conventions": "conventions.yaml"}
    contracts, report = run_import("pyspark", "jobs", options)
    traced, _report, _sources = run_import_traced("pyspark", "jobs", options)
    assert contracts == traced and report.mapped_one_to_one


def test_the_paths_are_the_ones_the_import_writes_and_never_a_glob(project):
    from fluid_build.cli._import_workflow_handler import _plan_writes, _planned_paths

    contracts = [{"id": "a.b"}, {"id": "c"}]
    assert _planned_paths(contracts, "out") == [
        Path("out/contract.a.b.fluid.yaml"),
        Path("out/contract.c.fluid.yaml"),
    ]
    assert _planned_paths(contracts, None) == [
        Path("contract.a.b.fluid.yaml"),
        Path("contract.c.fluid.yaml"),
    ]
    assert _planned_paths(contracts[:1], "one.yaml") == [Path("one.yaml")]
    assert _planned_paths(contracts[:1], None) == [Path("contract.a.b.fluid.yaml")]
    assert [p for p, _t, _s in _plan_writes(contracts, "out")] == _planned_paths(contracts, "out")
    (project / "out").mkdir()
    (project / "out/contract.unrelated.fluid.yaml").write_text("id: other\n", encoding="utf-8")
    assert len(_planned_paths(contracts, "out")) == 2  # a file nobody proposed is not looked for


def test_several_products_into_a_file_cannot_be_checked(project, capsys):
    (project / "out").write_text("a file\n", encoding="utf-8")
    capsys.readouterr()
    assert check() == 2
    assert "--out names a file" in capsys.readouterr().err


# ── through the CLI's own entry point ────────────────────────────────


def test_the_exit_codes_are_the_process_exit_codes(project):
    written()

    def exit_code(*extra: str) -> int:
        env = {**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"}
        done = subprocess.run(
            [sys.executable, "-m", "fluid_build.cli", "import", *ARGV, "--check", *extra],
            cwd=project,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        return done.returncode

    assert exit_code() == 0
    sub(project / "dags/orders_daily.py", "0 2 * * *", "0 3 * * *")
    assert exit_code() == 1
    assert exit_code("--force") == 2
