# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A table in a Spark catalog is a contract ``fluid validate`` accepts.

``glue_catalog.raw.orders`` names a Spark catalog: an alias the job's session was configured with.
``binding.location.catalog`` is a *kind* (``glue``, ``rest``, ``hive``, ...), and copying the alias
into it wrote a contract ``fluid validate`` refused ("'glue_catalog' is not a catalog kind FLUID
knows") for every Iceberg write to a three-part name, under every ``--split-by``. The kind is
written only when the code or the conventions say it: the alias is a kind itself, a conventions
rule maps it, or the scanned code sets ``spark.sql.catalog.<alias>.type`` (or a ``catalog-impl``)
to one. Otherwise there is no ``catalog``, and the report says to set it.
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml

from fluid_build.cli import build_parser, main
from fluid_build.cli.import_cmd import run
from fluid_build.cli.import_workflow._proposal import SPLITS
from fluid_build.cli.import_workflow.pyspark import PySparkImporter
from fluid_build.cli.validate import validate_loaded_contract

LOG = logging.getLogger("test_catalog_kinds")

SESSION = "from pyspark.sql import SparkSession\n"

STREAM = (
    SESSION
    + """
spark = SparkSession.builder.getOrCreate()
raw = (spark.readStream.format("kafka")
       .option("kafka.bootstrap.servers", "broker:9092").option("subscribe", "orders").load())
(raw.writeStream.format("iceberg").option("checkpointLocation", "s3://state/orders")
    .toTable("glue_catalog.raw.orders"))
"""
)
BATCH = (
    SESSION
    + """
spark = SparkSession.builder.getOrCreate()
spark.table("glue_catalog.raw.orders").write.format("iceberg").saveAsTable("glue_catalog.cur.b")
"""
)


def put(root: Path, files: Dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


def propose(root: Path, monkeypatch, files: Dict[str, str], **options: Any):
    put(root, files)
    monkeypatch.chdir(root)
    return PySparkImporter().import_to_contracts("jobs", options=options)


def binding_of(contracts: List[Dict[str, Any]], product: str) -> Dict[str, Any]:
    (found,) = [c for c in contracts if c["id"] == product]
    return found["exposes"][0]["binding"]


def accepted(contract: Dict[str, Any]) -> None:
    result = validate_loaded_contract(contract)
    assert result.is_valid and result.errors == [], result.errors


# --- the two writes that were refused -------------------------------------------------


@pytest.mark.parametrize(
    "source, product, table",
    [(STREAM, "raw.orders", "orders"), (BATCH, "cur.b", "b")],
    ids=["streaming write", "batch write"],
)
def test_a_write_to_a_three_part_iceberg_table_does_not_copy_the_alias_into_the_binding(
    tmp_path, monkeypatch, source, product, table
):
    contracts, report = propose(tmp_path, monkeypatch, {"jobs/j.py": source})
    location = binding_of(contracts, product)["location"]
    assert location["table"] == table and "catalog" not in location
    for contract in contracts:
        accepted(contract)
    said = [line for line in report.required_defaults if "binding.location.catalog" in line]
    assert (
        len(said) == 1 and "glue_catalog" in said[0] and "Set binding.location.catalog" in said[0]
    )


def test_the_report_names_the_ways_to_say_which_kind_it_is(tmp_path, monkeypatch):
    _contracts, report = propose(tmp_path, monkeypatch, {"jobs/j.py": BATCH})
    line = next(x for x in report.required_defaults if "binding.location.catalog" in x)
    assert "`catalogs` in the conventions" in line
    assert "spark.sql.catalog.glue_catalog.type" in line


# --- when the kind is known -----------------------------------------------------------


def test_an_alias_that_is_a_kind_fluid_knows_is_that_kind(tmp_path, monkeypatch):
    source = BATCH.replace("glue_catalog", "glue")
    contracts, report = propose(tmp_path, monkeypatch, {"jobs/j.py": source})
    assert binding_of(contracts, "cur.b")["location"]["catalog"] == "glue"
    assert not [x for x in report.required_defaults if "binding.location.catalog" in x]
    for contract in contracts:
        accepted(contract)


def test_a_conventions_rule_maps_the_alias(tmp_path, monkeypatch):
    files = {
        "jobs/j.py": BATCH,
        "conventions.yaml": """
            catalogs:
              glue_catalog: {platform: aws, format: iceberg, catalog: glue}
            """,
    }
    contracts, report = propose(tmp_path, monkeypatch, files, conventions="conventions.yaml")
    binding = binding_of(contracts, "cur.b")
    assert binding["platform"] == "aws" and binding["location"]["catalog"] == "glue"
    assert not [x for x in report.required_defaults if "binding.location.catalog" in x]
    for contract in contracts:
        accepted(contract)


CONFIGURED_IN_A_JOB = {
    "builder": 'SparkSession.builder.config("spark.sql.catalog.glue_catalog.type", "glue")',
    "conf.set": 'SparkSession.builder.getOrCreate().conf.set("spark.sql.catalog.glue_catalog.type", "glue")',
    "a constant key": "SparkSession.builder.config(CATALOG_TYPE, 'glue')",
    "an f-string key": "SparkSession.builder.config(f'spark.sql.catalog.{ALIAS}.type', 'glue')",
    "catalog-impl": (
        'SparkSession.builder.config("spark.sql.catalog.glue_catalog.catalog-impl", '
        '"org.apache.iceberg.aws.glue.GlueCatalog")'
    ),
}


@pytest.mark.parametrize("setter", sorted(CONFIGURED_IN_A_JOB))
def test_a_catalog_the_job_file_configures_has_the_kind_it_is_configured_as(
    tmp_path, monkeypatch, setter
):
    source = (
        SESSION
        + 'CATALOG_TYPE = "spark.sql.catalog.glue_catalog.type"\nALIAS = "glue_catalog"\n'
        + f"spark = {CONFIGURED_IN_A_JOB[setter]}.getOrCreate()\n"
        + 'spark.table("glue_catalog.raw.orders").write.format("iceberg")'
        + '.saveAsTable("glue_catalog.cur.b")\n'
    )
    contracts, report = propose(tmp_path, monkeypatch, {"jobs/j.py": source})
    assert binding_of(contracts, "cur.b")["location"]["catalog"] == "glue"
    assert not [x for x in report.required_defaults if "binding.location.catalog" in x]
    for contract in contracts:
        accepted(contract)


DAG_HEAD = """
    from airflow import DAG
    from airflow.operators.bash import BashOperator
    from airflow.providers.amazon.aws.operators.emr import EmrServerlessStartJobOperator
    from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator

    with DAG("etl", schedule=None, default_args={"owner": "data"}) as dag:
"""
CONFIGURED_IN_A_DAG = {
    "conf dict": """
        SparkSubmitOperator(task_id="t", application="jobs/j.py",
                            conf={"spark.sql.catalog.glue_catalog.type": "glue"})
        """,
    "bash --conf": """
        BashOperator(task_id="t", bash_command=(
            "spark-submit --conf spark.sql.catalog.glue_catalog.type=glue jobs/j.py"))
        """,
    "arguments": """
        SparkSubmitOperator(task_id="t", application="jobs/j.py",
                            application_args=["--conf", "spark.sql.catalog.glue_catalog.type=glue"])
        """,
    "sparkSubmitParameters": """
        EmrServerlessStartJobOperator(task_id="t", application_id="a", job_driver={"sparkSubmit": {
            "entryPoint": "jobs/j.py",
            "sparkSubmitParameters": "--conf spark.sql.catalog.glue_catalog.type=glue"}})
        """,
}


@pytest.mark.parametrize("setter", sorted(CONFIGURED_IN_A_DAG))
def test_a_catalog_a_dag_configures_has_the_kind_it_is_configured_as(tmp_path, monkeypatch, setter):
    dag = textwrap.dedent(DAG_HEAD) + textwrap.indent(
        textwrap.dedent(CONFIGURED_IN_A_DAG[setter]).strip("\n"), "    "
    )
    files = {"jobs/j.py": BATCH, "dags/etl.py": dag}
    contracts, report = propose(tmp_path, monkeypatch, files, dags="dags")
    assert binding_of(contracts, "cur.b")["location"]["catalog"] == "glue"
    assert not [x for x in report.required_defaults if "binding.location.catalog" in x]
    for contract in contracts:
        accepted(contract)


@pytest.mark.parametrize(
    "impl, kind",
    [
        ("org.apache.iceberg.rest.RESTCatalog", "rest"),
        ("org.apache.iceberg.hive.HiveCatalog", "hive"),
        ("org.apache.iceberg.nessie.NessieCatalog", "nessie"),
        ("org.apache.iceberg.hadoop.HadoopCatalog", "hadoop"),
    ],
)
def test_a_catalog_impl_is_the_kind_its_class_is(tmp_path, monkeypatch, impl, kind):
    source = BATCH.replace(
        "spark = SparkSession.builder.getOrCreate()",
        'spark = SparkSession.builder.config("spark.sql.catalog.glue_catalog.catalog-impl", '
        f'"{impl}").getOrCreate()',
    )
    contracts, _report = propose(tmp_path, monkeypatch, {"jobs/j.py": source})
    assert binding_of(contracts, "cur.b")["location"]["catalog"] == kind


# --- when the code does not say, or says it twice -------------------------------------


@pytest.mark.parametrize(
    "setting",
    [
        'SparkSession.builder.config("spark.sql.catalog.other_catalog.type", "glue")',  # another alias
        'SparkSession.builder.config("spark.sql.catalog.glue_catalog.type", "custom")',  # not a kind
        'SparkSession.builder.config("spark.sql.catalog.glue_catalog", "org.apache.iceberg.spark.SparkCatalog")',
        'SparkSession.builder.config("spark.sql.catalog.glue_catalog.warehouse", "s3://w/")',
        'SparkSession.builder.config("spark.sql.catalog.glue_catalog.catalog-impl", "com.acme.OwnCatalog")',
        'SparkSession.builder.config("spark.sql.catalog.glue_catalog.type", spark_type())',  # not a constant
    ],
)
def test_a_setting_that_does_not_name_a_kind_for_this_alias_leaves_the_binding_without_one(
    tmp_path, monkeypatch, setting
):
    source = BATCH.replace("SparkSession.builder", setting)
    contracts, report = propose(tmp_path, monkeypatch, {"jobs/j.py": source})
    assert "catalog" not in binding_of(contracts, "cur.b")["location"]
    assert [x for x in report.required_defaults if "binding.location.catalog" in x]
    accepted(contracts[0])


def test_an_alias_set_to_two_kinds_is_no_kind_and_the_report_says_both(tmp_path, monkeypatch):
    files = {
        "jobs/a.py": BATCH.replace(
            "SparkSession.builder",
            'SparkSession.builder.config("spark.sql.catalog.glue_catalog.type", "glue")',
        ),
        "jobs/b.py": SESSION
        + 'spark = SparkSession.builder.config("spark.sql.catalog.glue_catalog.type", "rest")'
        + '.getOrCreate()\nspark.table("glue_catalog.raw.x").write.format("iceberg")'
        + '.saveAsTable("glue_catalog.cur.y")\n',
    }
    contracts, report = propose(tmp_path, monkeypatch, files)
    for product in ("cur.b", "cur.y"):
        assert "catalog" not in binding_of(contracts, product)["location"]
    said = " ".join(x for x in report.required_defaults if "binding.location.catalog" in x)
    assert "more than one kind (glue, rest)" in said


def test_a_path_rule_in_the_conventions_still_names_its_catalog(tmp_path, monkeypatch):
    source = (
        SESSION
        + "spark = SparkSession.builder.getOrCreate()\n"
        + 'spark.read.parquet("s3://lake/raw/orders").write.mode("overwrite")'
        + '.parquet("s3://lake/curated/orders")\n'
    )
    files = {
        "jobs/j.py": source,
        "conventions.yaml": """
            paths:
              - {prefix: "s3://lake/curated/", database: cur, platform: aws, format: iceberg, catalog: glue}
            """,
    }
    contracts, _report = propose(tmp_path, monkeypatch, files, conventions="conventions.yaml")
    assert binding_of(contracts, "cur.orders")["location"]["catalog"] == "glue"


# --- everything the command writes is accepted by fluid validate ---------------------


def test_the_proposal_for_each_split_is_accepted_by_fluid_validate(tmp_path, monkeypatch):
    files = {
        "jobs/stream.py": STREAM,
        "jobs/batch.py": BATCH,
        "jobs/mapped.py": SESSION
        + "spark = SparkSession.builder.getOrCreate()\n"
        + 'spark.table("lake.raw.x").write.format("iceberg").saveAsTable("lake.cur.x")\n',
        "dags/etl.py": textwrap.dedent(DAG_HEAD)
        + '        SparkSubmitOperator(task_id="b", application="jobs/batch.py")\n'
        + '        SparkSubmitOperator(task_id="s", application="jobs/stream.py")\n',
        "conventions.yaml": """
            catalogs:
              lake: {platform: aws, format: iceberg, catalog: glue}
            """,
    }
    put(tmp_path, files)
    monkeypatch.chdir(tmp_path)
    for split in SPLITS:
        out = tmp_path / f"out-{split}"
        args = build_parser().parse_args(
            [
                "import", "pyspark", "jobs", "--dags", "dags", "--conventions", "conventions.yaml",
                "--split-by", split, "--out", str(out),
            ]
        )  # fmt: skip
        assert run(args, LOG) == 0, split
        written = [out] if out.is_file() else sorted(out.iterdir())  # one product is one file
        assert written, split
        for number, path in enumerate(written):
            text = path.read_text(encoding="utf-8")
            accepted(yaml.safe_load(text))
            # the command reads a file by its extension, and one product is written to the path given
            checked = tmp_path / "checked" / f"{split}-{number}.fluid.yaml"
            checked.parent.mkdir(exist_ok=True)
            checked.write_text(text, encoding="utf-8")
            cache = str(tmp_path / "schema-cache")
            assert main(["validate", str(checked), "--offline", "--cache-dir", cache]) == 0, checked
