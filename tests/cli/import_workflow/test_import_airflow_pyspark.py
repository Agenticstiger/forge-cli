# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid import airflow`` and ``fluid import pyspark``, end to end on a small project.

The project is a retail pipeline: DAGs submit PySpark jobs that clean orders, build a daily
table and stream clicks through Kafka. Every proposed contract must be a valid FLUID contract.
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build._errors import FluidUserError
from fluid_build.cli import build_parser
from fluid_build.cli.import_cmd import run
from fluid_build.cli.import_workflow import get_importer, importer_names
from fluid_build.cli.import_workflow.airflow import AirflowImporter
from fluid_build.cli.import_workflow.pyspark import PySparkImporter
from fluid_build.schema_manager import FluidSchemaManager

LOG = logging.getLogger("test_import_airflow_pyspark")

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
        spark.table("retail.orders_raw").filter("amount > 0").write.mode("overwrite").format("iceberg").saveAsTable("retail.orders_clean")
        """,
    "jobs/orders_daily.py": """
        from pyspark.sql import SparkSession

        spark = SparkSession.builder.appName("orders_daily").getOrCreate()
        TARGET = "retail.orders_daily"
        df = spark.table("retail.orders_clean").join(spark.table("retail.customers"), "customer_id")
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
        layers: {retail: Silver}
        owners:
          retail-data: {team: retail-data}
        """,
}


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    for rel, text in FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # so a job is shown as jobs/x.py, as in a real checkout
    return tmp_path


def pyspark(options: Dict[str, Any] | None = None, source: str = "jobs"):
    merged = {"dags": "dags", "conventions": "conventions.yaml", **(options or {})}
    return PySparkImporter().import_to_contracts(source, options=merged)


def by_id(contracts: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {c["id"]: c for c in contracts}


def assert_valid(contract: Dict[str, Any]) -> None:
    result = FluidSchemaManager().validate_contract(
        contract, schema_version=contract["fluidVersion"], offline_only=True
    )
    assert result.is_valid, result.errors


# ── one product per output ────────────────────────────────────────────


def test_pyspark_with_dags_proposes_one_product_per_output(project):
    contracts, report = pyspark()
    assert [c["id"] for c in contracts] == [
        "retail.orders_clean",
        "clicks_clean",
        "retail.orders_daily",
    ]
    for contract in contracts:
        assert_valid(contract)
        assert contract["kind"] == "DataProduct" and contract["domain"] == "retail"
        assert len(contract["builds"]) == 1 and len(contract["exposes"]) == 1
    products = by_id(contracts)

    daily = products["retail.orders_daily"]
    assert daily["metadata"] == {
        "owner": {"team": "retail-data"},
        "layer": "Silver",
        "productType": "ADP",
    }
    (build,) = daily["builds"]
    assert (build["pattern"], build["engine"], build["properties"]) == (
        "hybrid-reference",
        "spark",
        {"model": "jobs/orders_daily.py"},
    )
    assert build["outputs"] == ["orders_daily"]
    # the job it runs after belongs to another product: a dependency between products is a
    # consumes[] (and the task's own dependencies, below), not a build dependency
    assert "dependencies" not in build
    execution = build["execution"]
    assert execution["trigger"] == {"type": "manual"} and execution["runtime"] == {
        "platform": "aws"
    }
    airflow = execution["orchestration"]
    assert (airflow["engine"], airflow["mode"], airflow["generateOnChange"]) == (
        "airflow",
        "manual",
        False,
    )
    assert airflow["airflow"]["dagId"] == "orders_daily"
    assert airflow["airflow"]["dagConfig"] == {
        "schedule": "0 2 * * *",
        "tags": ["retail"],
        "catchup": False,
    }
    (task,) = airflow["airflow"]["tasks"]
    assert task["taskId"] == "orders_daily" and task["dependencies"] == ["clean_orders"]
    assert task["params"]["script"] == "s3://acme-code/jobs/orders_daily.py"
    expose = daily["exposes"][0]
    assert expose["exposeId"] == "orders_daily" and expose["kind"] == "table"
    assert expose["binding"]["location"] == {"table": "orders_daily", "database": "retail"}
    assert expose["contract"]["schema"] == [] and expose["description"].startswith("TODO")
    # a table read that another job writes is a consumes[] of that product; one nobody writes is
    # named by the same id rule
    assert daily["consumes"] == [
        {
            "productId": "retail.customers",
            "exposeId": "customers",
            "purpose": "read by jobs/orders_daily.py; no scanned job writes it",
        },
        {
            "productId": "retail.orders_clean",
            "exposeId": "orders_clean",
            "purpose": "read by jobs/orders_daily.py",
        },
    ]

    clean = products["retail.orders_clean"]
    assert clean["consumes"][0]["productId"] == "retail.orders_raw"
    task = clean["builds"][0]["execution"]["orchestration"]["airflow"]["tasks"][0]
    assert (
        task["params"]["args"] == ["--date", "{{ ds }}"]
        and task["operator"] == "SparkSubmitOperator"
    )

    clicks = products["clicks_clean"]
    assert clicks["metadata"]["owner"] == {"team": "web-data"} and "layer" not in clicks["metadata"]
    kafka = clicks["exposes"][0]
    assert kafka["kind"] == "topic"
    assert kafka["binding"] == {
        "platform": "kafka",
        "format": "kafka_topic",
        "location": {"topic": "clicks_clean", "host": "broker-1:9092"},
    }
    stream = clicks["builds"][0]
    assert stream["execution"]["trigger"] == {"type": "continuous"}
    assert "checkpoint s3://acme-state/checkpoints/clicks_clean" in stream["description"]
    assert "trigger processingTime=30 seconds" in stream["description"]
    assert clicks["consumes"] == [
        {
            "productId": "clicks_raw",
            "exposeId": "clicks_raw",
            "purpose": "read by jobs/clicks_clean.py; no scanned job writes it",
        }
    ]
    assert "clicks_stream" == stream["execution"]["orchestration"]["airflow"]["dagId"]
    assert "dagConfig" not in stream["execution"]["orchestration"]["airflow"]  # schedule=None


def test_the_report_says_what_mapped_what_defaulted_and_what_was_not_read(project):
    _contracts, report = pyspark()
    mapped = "\n".join(report.mapped_one_to_one)
    assert (
        "write retail.orders_daily (jobs/orders_daily.py:" in mapped
        and "→ exposes[orders_daily]" in mapped
    )
    assert "read retail.orders_clean" in mapped and "→ consumes[retail.orders_clean]" in mapped
    assert "DAG orders_daily task orders_daily (SparkSubmitOperator)" in mapped
    defaults = "\n".join(report.required_defaults)
    assert "exposes[orders_daily] has an empty schema" in defaults
    assert "binding platform and format default to other" in defaults
    notes = "\n".join(report.notes)
    assert "read 3 Python file(s)" in notes and "3 do Spark reads or writes" in notes
    assert "read 2 DAG file(s)" in notes and "2 DAG(s), 3 task(s), 3 submit a script" in notes
    assert "3 submitted script(s) joined to a job file (3 by script_root); 0 not joined" in notes
    assert "3 upstream table(s) or topic(s) are read but written by no scanned job" in notes
    assert report.unsupported == []


def test_the_airflow_importer_reads_the_same_jobs_through_script_roots(project):
    from_jobs, _ = pyspark()
    from_dags, report = AirflowImporter().import_to_contracts(
        "dags", options={"conventions": "conventions.yaml"}
    )
    assert from_dags == from_jobs
    assert report.unsupported == []


def test_airflow_without_script_roots_lists_each_task_and_the_way_out(project):
    contracts, report = AirflowImporter().import_to_contracts("dags", options={})
    assert contracts == []
    lines = [x for x in report.unsupported if "not joined" in x]
    assert len(lines) == 3
    assert all(
        "script_roots" in x and "fluid import pyspark <jobs-dir> --dags <dags-dir>" in x
        for x in lines
    )


def test_pyspark_without_dags_proposes_products_with_no_dag_facts(project):
    contracts, report = PySparkImporter().import_to_contracts(
        "jobs", options={"conventions": "conventions.yaml"}
    )
    daily = by_id(contracts)["retail.orders_daily"]
    assert daily["metadata"]["owner"] == {"team": "imported"}
    assert "orchestration" not in daily["builds"][0]["execution"]
    assert any("owner defaulted to team 'imported'" in x for x in report.required_defaults)
    assert not any("submitted by no DAG task" in x for x in report.notes)  # no DAGs were read
    assert_valid(daily)


def test_a_proposal_is_deterministic(project):
    first, _ = pyspark()
    second, _ = pyspark()
    assert yaml.safe_dump(first) == yaml.safe_dump(second)


# ── the product boundary ──────────────────────────────────────────────


def test_split_by_job_dag_folder_and_project(project):
    extra = {
        "jobs/two_outputs.py": 'df.write.saveAsTable("retail.facts_a")\ndf.write.saveAsTable("retail.facts_b")\n',
        "jobs/ingest/load_a.py": 'df.write.saveAsTable("retail.a")\n',
        "jobs/serve/build_b.py": 'df.write.saveAsTable("retail.b")\n',
    }
    for rel, body in extra.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("from pyspark.sql import SparkSession\n" + body, encoding="utf-8")

    table, _ = pyspark()
    assert len(table) == 7  # one per output: the job that writes two feeds two products

    job, _ = pyspark({"split_by": "job"})
    assert {c["id"] for c in job} == {
        "retail.clean_orders",
        "retail.clicks_clean",
        "retail.orders_daily",
        "retail.two_outputs",
        "retail.load_a",
        "retail.build_b",
    }
    two = by_id(job)["retail.two_outputs"]
    assert [e["exposeId"] for e in two["exposes"]] == ["facts_a", "facts_b"]
    assert two["builds"][0]["outputs"] == ["facts_a", "facts_b"]

    dag, _ = pyspark({"split_by": "dag"})
    assert {c["id"] for c in dag} == {
        "retail.orders_daily",  # the DAG's two jobs
        "retail.clicks_stream",
        "retail.two_outputs",  # no DAG: the job
        "retail.load_a",
        "retail.build_b",
    }
    orders = by_id(dag)["retail.orders_daily"]
    assert [b["id"] for b in orders["builds"]] == ["clean_orders", "orders_daily"]
    assert orders["builds"][1]["dependencies"] == [
        {"buildId": "clean_orders", "condition": "success"}
    ]
    # a read of another output of the same product is not a dependency of the product
    assert all(c["productId"] != "retail.orders_daily" for c in orders["consumes"])
    assert [c["productId"] for c in orders["consumes"]] == ["retail.orders_raw", "retail.customers"]

    folder, _ = pyspark({"split_by": "folder"})
    # the files at the top of the scanned directory are named after it, like the project split
    assert {c["id"] for c in folder} == {"retail.jobs", "retail.ingest", "retail.serve"}

    everything, _ = pyspark({"split_by": "project"})
    assert [c["id"] for c in everything] == ["retail.jobs"]  # the scanned directory's name
    assert len(everything[0]["exposes"]) == 7 and len(everything[0]["builds"]) == 6
    for contract in (*job, *dag, *folder, *everything):
        assert_valid(contract)


def test_a_boundary_that_is_not_for_code_is_refused_with_the_valid_ones(project):
    with pytest.raises(FluidUserError) as refused:
        pyspark({"split_by": "group"})
    assert "--split-by group does not apply" in refused.value.what
    assert "table | job | dag | folder | project" in refused.value.fix


# ── single writer, one owner ──────────────────────────────────────────


def test_an_output_with_two_writers_is_proposed_once_and_reported(project):
    (project / "jobs" / "zz_second_writer.py").write_text(
        'from pyspark.sql import SparkSession\ndf.write.mode("append").saveAsTable("retail.orders_daily")\n',
        encoding="utf-8",
    )
    contracts, report = pyspark()
    assert [c["id"] for c in contracts].count("retail.orders_daily") == 1
    daily = by_id(contracts)["retail.orders_daily"]
    assert daily["builds"][0]["properties"]["model"] == "jobs/orders_daily.py"  # the first by path
    (line,) = [x for x in report.unsupported if "more than one job" in x]
    assert "jobs/zz_second_writer.py" in line and "single writer" in line


def test_tasks_with_different_owners_give_one_owner_and_a_report_line(project):
    (project / "dags" / "orders_daily.py").write_text(
        (project / "dags" / "orders_daily.py")
        .read_text(encoding="utf-8")
        .replace(
            'application=f"{JOBS}/orders_daily.py")',
            'application=f"{JOBS}/orders_daily.py", owner="someone-else")',
        ),
        encoding="utf-8",
    )
    contracts, report = pyspark()
    daily = by_id(contracts)["retail.orders_daily"]
    assert daily["metadata"]["owner"] == {"team": "someone-else"}
    other = by_id(contracts)["retail.orders_clean"]
    assert other["metadata"]["owner"] == {"team": "retail-data"}
    assert not [x for x in report.unsupported if "owners" in x]  # one task: one owner

    (project / "dags" / "both.py").write_text(
        textwrap.dedent(
            """
            from airflow import DAG
            from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
            with DAG("both", default_args={"owner": "team-a"}):
                SparkSubmitOperator(task_id="x", application="s3://acme-code/jobs/orders_daily.py", owner="team-b")
            """
        ),
        encoding="utf-8",
    )
    _, report = pyspark()
    assert any("name 2 owners" in x and "orders_daily" in x for x in report.unsupported)


# ── what is not a constant, and what is hostile ───────────────────────


def test_a_write_that_is_not_a_constant_is_reported_and_proposes_nothing(project):
    (project / "jobs" / "dynamic.py").write_text(
        "from pyspark.sql import SparkSession\nimport sys\ndf.write.saveAsTable(sys.argv[1])\n"
        'spark.table(f"{ENV}.thing")\n',
        encoding="utf-8",
    )
    contracts, report = pyspark()
    assert len(contracts) == 3
    lines = "\n".join(report.unsupported)
    assert "jobs/dynamic.py:3 writes " in lines and "command-line argument" in lines


def test_nothing_in_the_scanned_files_runs(project, tmp_path):
    marker = tmp_path / "ran-marker"
    for rel in ("jobs/clean_orders.py", "dags/orders_daily.py"):
        path = project / rel
        path.write_text(
            f"open({str(marker)!r}, 'w').write('x')\nraise SystemExit(5)\n"
            + path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    pyspark()
    assert not marker.exists()


def test_names_and_arguments_from_the_code_are_sanitised_and_redacted(project, capsys):
    # credential-shaped values are built at run time: no token shape sits in the repository
    word = "hunter2" * 2
    token = "gh" + "p_" + "a" * 36
    (project / "dags" / "orders_daily.py").write_text(
        (project / "dags" / "orders_daily.py")
        .read_text(encoding="utf-8")
        .replace(
            'application_args=["--date", "{{ ds }}"]',
            f'application_args=["--password", "{word}", "--token={token}"]',
        ),
        encoding="utf-8",
    )
    (project / "jobs" / "evil.py").write_text(
        "from pyspark.sql import SparkSession\n"
        'df.write.saveAsTable("db./../../etc.passwd\\x1b[31m")\n'
        'df.write.parquet("s3://b/\\x1b]0;pwn\\x07out")\n',
        encoding="utf-8",
    )
    contracts, report = pyspark()
    text = yaml.safe_dump(contracts)
    assert word not in text and token not in text
    for contract in contracts:
        assert_valid(contract)
        assert "/" not in contract["id"] and "\x1b" not in contract["id"]
    for line in (*report.mapped_one_to_one, *report.unsupported, *report.notes):
        assert "\x1b" not in line and "\x07" not in line


# ── conventions ───────────────────────────────────────────────────────


def test_conventions_map_paths_catalogs_layers_helpers_and_environments(project):
    (project / "jobs" / "lake.py").write_text(
        textwrap.dedent(
            """
            import os
            from pyspark.sql import SparkSession
            from acme.io import read_table, write_table

            ENV = os.environ["ENV"]
            src = read_table("raw.events")
            ref = spark.read.parquet("s3://lake/curated/orders/dt=1")
            out = spark.read.format("iceberg").load("glue.marts.rollup")
            tmp = spark.table("tmp_scratch")
            write_table(src, f"{ENV}_marts.sessions")
            src.write.mode("append").parquet("s3://lake/curated/customers/")
            src.write.format("iceberg").save("glue.marts.rollup")
            src.write.saveAsTable("tmp_scratch")
            """
        ),
        encoding="utf-8",
    )
    (project / "conventions.yaml").write_text(
        textwrap.dedent(
            """
            script_roots: {"s3://acme-code/jobs/": jobs}
            paths:
              - prefix: "s3://lake/curated/"
                database: curated
                platform: aws
                format: parquet
            helpers:
              read_table: {reads: table, arg: 0}
              write_table: {writes: table, arg: 1, format: iceberg}
            environment: prod
            environments: {prod: {ENV: prod}}
            defaults: {domain: retail, platform: aws}
            catalogs: {glue: {platform: aws, format: iceberg, catalog: glue}}
            layers: {curated: Silver, marts: Gold, raw: Bronze}
            ignore: {tables: ["^tmp_"]}
            products: {id: "{database}.{table}"}
            owners: {retail-data: {team: retail-data}}
            """
        ),
        encoding="utf-8",
    )
    contracts, report = pyspark()
    products = by_id(contracts)
    assert {"prod_marts.sessions", "curated.customers", "marts.rollup"} <= set(products)
    assert "tmp_scratch" not in "".join(products)
    sessions = products["prod_marts.sessions"]
    assert sessions["exposes"][0]["binding"]["format"] == "iceberg"
    assert sessions["consumes"][0]["productId"] == "raw.events"
    customers = products["curated.customers"]
    assert (
        customers["metadata"]["layer"] == "Silver" and customers["metadata"]["productType"] == "ADP"
    )
    assert customers["exposes"][0]["binding"] == {
        "platform": "aws",
        "format": "parquet",
        "location": {
            "table": "customers",
            "database": "curated",
            "path": "s3://lake/curated/customers",
        },
    }
    rollup = products["marts.rollup"]
    assert rollup["exposes"][0]["binding"] == {
        "platform": "aws",
        "format": "iceberg",
        "location": {"table": "rollup", "database": "marts", "catalog": "glue"},
    }
    assert rollup["metadata"]["productType"] == "CDP"
    assert "ignored by the conventions" in " ".join(report.notes)
    for contract in contracts:
        assert_valid(contract)


def test_a_path_no_rule_explains_is_identified_by_the_path(project):
    (project / "jobs" / "files.py").write_text(
        'from pyspark.sql import SparkSession\ndf.write.parquet("s3://lake/out/orders/")\n',
        encoding="utf-8",
    )
    contracts, _ = pyspark()
    product = by_id(contracts)["lake.out.orders"]
    assert product["exposes"][0]["kind"] == "file"
    assert product["exposes"][0]["binding"] == {
        "platform": "aws",
        "format": "parquet",
        "location": {"bucket": "lake", "path": "out/orders"},
    }
    assert_valid(product)


# ── topic schemas through the registry importer ───────────────────────


def test_a_topics_columns_come_from_the_schema_registry_importer(project, monkeypatch):
    from fluid_build.schema_registry import clients
    from fluid_build.schema_registry.clients import RegistryError, SubjectVersion
    from fluid_build.schema_registry.schemas import avro_schema, schema_text

    seen: List[str] = []

    class Fake:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def get_version(self, *args: Any) -> SubjectVersion:
            seen.append(str(args))
            columns = [
                {"name": "click_id", "type": "string", "required": True},
                {"name": "ts", "type": "long"},
            ]
            if "clicks_clean-value" not in str(args):
                raise RegistryError("no such subject")
            return SubjectVersion(
                "clicks_clean-value", 3, "AVRO", schema_text(avro_schema(columns, name="clicks"))
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(clients, "ConfluentRegistry", Fake)
    contracts, report = pyspark({"registry": "confluent", "url": "https://sr.example.com"})
    clicks = by_id(contracts)["clicks_clean"]
    assert any("clicks_clean-value" in call for call in seen)  # the subject is <topic>-value
    assert [c["name"] for c in clicks["exposes"][0]["contract"]["schema"]] == ["click_id", "ts"]
    assert "description" not in clicks["exposes"][0]
    assert any("schema from the registry (2 columns)" in x for x in report.mapped_one_to_one)
    assert_valid(clicks)

    class Down(Fake):
        def get_version(self, *args: Any) -> SubjectVersion:
            raise RegistryError("registry unavailable")

    monkeypatch.setattr(clients, "ConfluentRegistry", Down)
    contracts, report = pyspark({"registry": "confluent", "url": "https://sr.example.com"})
    clicks = by_id(contracts)["clicks_clean"]
    assert clicks["exposes"][0]["contract"]["schema"] == []
    assert "schema registry: could not read subject" in clicks["exposes"][0]["description"]


def test_without_registry_flags_no_registry_is_contacted(project, monkeypatch):
    from fluid_build.schema_registry import clients

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a registry was built")

    monkeypatch.setattr(clients, "ConfluentRegistry", boom)
    monkeypatch.setattr(clients, "GlueRegistry", boom)
    pyspark()


# ── refusals ──────────────────────────────────────────────────────────


def test_a_source_that_is_not_a_directory_or_holds_no_python(project, tmp_path):
    with pytest.raises(FluidUserError) as not_dir:
        PySparkImporter().import_to_contracts("conventions.yaml", options={})
    assert "is not a directory" in not_dir.value.what
    (tmp_path / "empty").mkdir()
    with pytest.raises(FluidUserError) as empty:
        PySparkImporter().import_to_contracts("empty", options={})
    assert "no Python files" in empty.value.what
    with pytest.raises(FluidUserError) as no_dags:
        PySparkImporter().import_to_contracts("jobs", options={"dags": "nope"})
    assert "DAGs directory" in no_dags.value.what


def test_the_single_contract_protocol_is_one_product_for_everything(project):
    contract, _ = PySparkImporter().import_to_contract(
        "jobs", options={"conventions": "conventions.yaml"}
    )
    assert len(contract["exposes"]) == 3
    assert get_importer("pyspark") is not None and get_importer("airflow") is not None
    assert {"airflow", "pyspark"} <= set(importer_names())


# ── the command ───────────────────────────────────────────────────────


def parse(*argv: str):
    return build_parser().parse_args(["import", *argv])


def test_dry_run_prints_the_proposal_and_the_report_and_writes_nothing(project, capsys):
    args = parse(
        "pyspark", "jobs", "--dags", "dags", "--conventions", "conventions.yaml", "--dry-run"
    )
    assert run(args, LOG) == 0
    out = capsys.readouterr().out
    assert "Proposal (3 product(s))" in out
    assert (
        "retail.orders_daily  (owner retail-data) → contract.retail.orders_daily.fluid.yaml [new]"
        in out
    )
    assert "build orders_daily: jobs/orders_daily.py" in out
    assert "consumes: retail.customers.customers, retail.orders_clean.orders_clean" in out
    assert "Mapped 1:1" in out and "Used defaults" in out
    assert "Dry run: nothing was written" in out
    assert not list(project.glob("contract.*.fluid.yaml"))


def test_a_second_import_leaves_identical_files_and_will_not_replace_edited_ones(project, capsys):
    argv = (
        "pyspark",
        "jobs",
        "--dags",
        "dags",
        "--conventions",
        "conventions.yaml",
        "--out",
        "contracts",
    )
    assert run(parse(*argv), LOG) == 0
    first = sorted((project / "contracts").iterdir())
    assert [p.name for p in first] == [
        "contract.clicks_clean.fluid.yaml",
        "contract.retail.orders_clean.fluid.yaml",
        "contract.retail.orders_daily.fluid.yaml",
    ]
    capsys.readouterr()
    assert run(parse(*argv), LOG) == 0
    assert capsys.readouterr().out.count("= Unchanged") == 3

    edited = project / "contracts" / "contract.retail.orders_daily.fluid.yaml"
    text = edited.read_text(encoding="utf-8").replace(
        "TODO: declare the columns", "Columns of the daily table"
    )
    edited.write_text(text, encoding="utf-8")
    with pytest.raises(FluidUserError) as refused:
        run(parse(*argv), LOG)
    assert "refusing to overwrite 1 existing file(s)" in refused.value.what
    assert "orders_daily" in refused.value.why and "--force" in refused.value.fix
    assert "Columns of the daily table" in edited.read_text(encoding="utf-8")  # untouched

    assert run(parse(*argv, "--force"), LOG) == 0
    assert "TODO: declare the columns" in edited.read_text(encoding="utf-8")


def test_the_written_contracts_validate_and_are_read_back_by_fluid_validate(project):
    run(
        parse(
            "pyspark",
            "jobs",
            "--dags",
            "dags",
            "--conventions",
            "conventions.yaml",
            "--out",
            "contracts",
        ),
        LOG,
    )
    for path in (project / "contracts").iterdir():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert_valid(loaded)


@pytest.mark.parametrize(
    "argv, expect",
    [
        (
            ("dbt", "x", "--conventions", "c.yaml"),
            "--conventions does not apply to `fluid import dbt`",
        ),
        (("meltano", "x", "--dags", "d"), "--dags does not apply to `fluid import meltano`"),
        (("airflow", "dags", "--dags", "d"), "--dags does not apply to `fluid import airflow`"),
        (("--conventions", "c.yaml"), "--conventions applies to `fluid import <tool> <source>`"),
        (("--dry-run",), "--dry-run applies to `fluid import <tool> <source>`"),
        (("--force",), "--force applies to `fluid import <tool> <source>`"),
        (("pyspark", "jobs", "--url", "https://x"), "--url requires --registry"),
        (("pyspark", "jobs", "--registry", "confluent"), "--registry confluent requires --url"),
        (
            ("pyspark", "jobs", "--registry", "glue", "--url", "https://x"),
            "--url does not apply with --registry glue",
        ),
        (("pyspark", "jobs", "--registry", "glue", "--version", "2"), "--version does not apply"),
    ],
)
def test_flags_that_do_not_apply_are_refused(project, argv, expect):
    with pytest.raises(FluidUserError) as refused:
        run(parse(*argv), LOG)
    assert expect in refused.value.what


def test_the_help_names_both_importers_and_the_new_flags(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["import", "--help"])
    out = capsys.readouterr().out
    for word in (
        "airflow",
        "pyspark",
        "--conventions",
        "--dags",
        "--dry-run",
        "--force",
        "{project,folder,group,table,job,dag}",
    ):
        assert word in out.replace("\n", " ") or word in out


# ── what the command does to the files it is asked to write ───────────

ARGV = ("pyspark", "jobs", "--dags", "dags", "--conventions", "conventions.yaml", "--out", "out")
ORDERS_DAILY = "contract.retail.orders_daily.fluid.yaml"


def test_a_link_at_an_output_path_is_never_written_through_and_nothing_is_written(project, capsys):
    victim = project / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    (project / "out").mkdir()
    (project / "out" / ORDERS_DAILY).symlink_to(victim)
    for extra in ((), ("--force",)):
        with pytest.raises(FluidUserError) as refused:
            run(parse(*ARGV, *extra), LOG)
        assert "not regular files" in refused.value.what
        assert ORDERS_DAILY in refused.value.why and "never written" in refused.value.why
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    assert sorted(p.name for p in (project / "out").iterdir()) == [ORDERS_DAILY]  # nothing else


@pytest.mark.skipif(not Path("/dev/zero").exists(), reason="needs /dev/zero")
def test_a_link_to_an_endless_file_is_not_read_by_a_dry_run(project, capsys):
    (project / "out").mkdir()
    (project / "out" / ORDERS_DAILY).symlink_to("/dev/zero")
    assert run(parse(*ARGV, "--dry-run"), LOG) == 0  # returns: the link is not opened
    out = capsys.readouterr().out
    assert "refused: not a regular file" in out and "1 refused" in out


def test_the_dry_run_says_what_each_file_would_do(project, capsys):
    assert run(parse(*ARGV), LOG) == 0
    edited = project / "out" / ORDERS_DAILY
    edited.write_text(edited.read_text(encoding="utf-8") + "# by hand\n", encoding="utf-8")
    capsys.readouterr()

    assert run(parse(*ARGV, "--dry-run"), LOG) == 0
    out = capsys.readouterr().out
    assert out.count("[unchanged]") == 2
    assert "[differs from the existing file: refused without --force]" in out
    assert "0 file(s) would be written, 2 unchanged, 1 refused" in out

    assert run(parse(*ARGV, "--dry-run", "--force"), LOG) == 0
    out = capsys.readouterr().out
    assert "[replaces the existing file (--force)]" in out
    assert "1 file(s) would be written, 2 unchanged" in out and "refused" not in out
    assert edited.read_text(encoding="utf-8").endswith("# by hand\n")  # a dry run writes nothing


def test_an_existing_file_much_bigger_than_the_proposal_is_not_read_in_full(project, capsys):
    (project / "out").mkdir()
    big = project / "out" / ORDERS_DAILY
    with big.open("wb") as handle:
        handle.truncate(1024**3)  # sparse: nothing is stored for the gap
    assert run(parse(*ARGV, "--dry-run"), LOG) == 0
    assert "differs from the existing file" in capsys.readouterr().out
    with pytest.raises(FluidUserError) as refused:
        run(parse(*ARGV), LOG)
    assert "refusing to overwrite 1 existing file(s)" in refused.value.what
    assert big.stat().st_size == 1024**3


def test_an_existing_file_that_only_has_other_line_endings_is_unchanged(project, capsys):
    assert run(parse(*ARGV), LOG) == 0
    target = project / "out" / ORDERS_DAILY
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
    capsys.readouterr()
    assert run(parse(*ARGV), LOG) == 0
    assert capsys.readouterr().out.count("= Unchanged") == 3


def test_a_file_is_written_whole_with_the_permissions_the_umask_allows(project, monkeypatch):
    import os

    old = os.umask(0o027)
    try:
        assert run(parse(*ARGV), LOG) == 0
        written = project / "out" / ORDERS_DAILY
        assert (written.stat().st_mode & 0o777) == 0o640
        assert not [p for p in (project / "out").iterdir() if p.name.endswith(".tmp")]
        # a file that is replaced keeps the permission bits it has
        written.write_text(written.read_text(encoding="utf-8") + "# by hand\n", encoding="utf-8")
        written.chmod(0o600)
        assert run(parse(*ARGV, "--force"), LOG) == 0
        assert (written.stat().st_mode & 0o777) == 0o600
        assert "# by hand" not in written.read_text(encoding="utf-8")
    finally:
        os.umask(old)


def test_several_products_into_an_existing_file_is_refused_before_anything_is_written(project):
    (project / "out").write_text("a file\n", encoding="utf-8")
    with pytest.raises(FluidUserError) as refused:
        run(parse(*ARGV), LOG)
    assert "--out names a file, but this import writes 3 files" in refused.value.what
    assert (project / "out").read_text(encoding="utf-8") == "a file\n"


@pytest.mark.parametrize(
    "argv, ok",
    [
        (("dbt", "x", "--split-by", "table"), False),
        (("meltano", "x", "--split-by", "job"), False),
        (("--split-by", "dag"), False),
        (("dbt", "x", "--split-by", "group"), True),
        (("dbt", "x", "--split-by", "folder"), True),
        (("pyspark", "jobs", "--split-by", "job"), True),
        (("airflow", "dags", "--split-by", "dag"), True),
    ],
)
def test_the_splits_about_code_belong_to_the_importers_that_read_code(project, argv, ok):
    from fluid_build.cli._import_workflow_handler import check_import_flags

    args = parse(*argv)
    tool = args.tool
    if ok:
        check_import_flags(args, tool)
        return
    with pytest.raises(FluidUserError) as refused:
        check_import_flags(args, tool)
    assert "applies to `fluid import airflow` and `fluid import pyspark`" in refused.value.what


def test_the_printed_lines_are_clean_and_never_wrapped(project, capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "40")
    name = "jobs/evil‮ name.py"
    (project / "jobs" / "evil‮ name.py").write_text(
        'from pyspark.sql import SparkSession\ndf.write.saveAsTable("retail.evil")\n',
        encoding="utf-8",
    )
    assert run(parse(*ARGV, "--dry-run"), LOG) == 0
    out = capsys.readouterr().out
    assert "‮" not in out and " " not in out and name not in out
    # one proposal line stays one line, whatever the width of the terminal
    assert (
        "retail.orders_daily  (owner retail-data) → out/contract.retail.orders_daily.fluid.yaml [new]"
        in out
    )


def test_an_unread_command_is_listed_without_advice_about_script_roots(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dags").mkdir()
    (tmp_path / "dags" / "long.py").write_text(
        textwrap.dedent(
            f"""
            from airflow import DAG
            from airflow.operators.bash import BashOperator
            from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
            with DAG("long"):
                BashOperator(task_id="long", bash_command={"echo " + "x" * 5000!r})
                SparkSubmitOperator(task_id="named", application="s3://code/jobs/a.py")
            """
        ),
        encoding="utf-8",
    )
    _contracts, report = AirflowImporter().import_to_contracts("dags", options={})
    lines = [x for x in report.unsupported if "task long" in x or "task named" in x]
    long_line = next(x for x in lines if "task long" in x)
    named_line = next(x for x in lines if "task named" in x)
    assert "longer than" in long_line and "script_roots" not in long_line
    assert "script_roots" in named_line  # a script that was found but not joined gets the way out
