# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""An Oracle JDBC URL with its login in it never reaches a contract or a report.

``jdbc:oracle:thin:user/password@host`` has no ``://`` and no ``:`` before the password, so the
rules for a URL's user information and for ``user:password`` never saw it, and the postgres form
was masked while this one was written into the contract as it stood. Each way the importer
reads a string from the scanned code is checked: a task's arguments, a ``--conf`` value, a
command, a data path, and a ``spark.read.option("url", ...)`` in a job file, and then a whole
import.

Credential-shaped values are assembled at run time.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import List

import pytest
import yaml

from fluid_build.cli.import_workflow._redact import safe_args, safe_command, safe_text, scrub_path
from fluid_build.cli.import_workflow.pyspark import PySparkImporter

PHRASE = "hunter" + "2phrase"
MASK = "***REDACTED***"


def thin(rest: str = "dbhost:1521/ORCL", secret: str = PHRASE, kind: str = "thin") -> str:
    return f"jdbc:oracle:{kind}:scott/{secret}@{rest}"


MASKED = f"jdbc:oracle:thin:scott/{MASK}@dbhost:1521/ORCL"

#: Every spelling that holds the password: with ``@`` or ``@//``, the other drivers, any case, and
#: a password that holds an ``@`` (quoted, or not).
SHAPES = [
    thin(),
    thin("//dbhost:1521/ORCL"),
    thin("dbhost:1521:ORCL"),
    thin("ORCL", kind="oci8"),
    thin("//h/S", kind="oci"),
    "JDBC:ORACLE:THIN:SCOTT/" + PHRASE + "@H:1521/S",
    'jdbc:oracle:thin:"SCOTT"/' + PHRASE + "@dbhost:1521/ORCL",
    'jdbc:oracle:thin:"SCOTT"/"p@' + PHRASE + '"@h:1521/S',
    'jdbc:oracle:thin:scott/"p@' + PHRASE + '"@h:1521/S',
    "jdbc:oracle:thin:scott/p@" + PHRASE + "@h:1521/S",
]


def hidden(text: str) -> bool:
    return PHRASE.lower() not in text.lower()


@pytest.mark.parametrize("url", SHAPES)
def test_every_way_the_importer_reads_text_masks_the_password(url):
    assert hidden(safe_text(url)) and MASK in safe_text(url)
    assert hidden("".join(safe_args(["--src", url, "--conf", "spark.jdbc.url=" + url])))
    assert hidden(safe_command("spark-submit --src " + url + " job.py"))
    assert hidden(safe_command("spark-submit --conf spark.jdbc.url=" + url))
    assert hidden(safe_command('spark-submit --src "' + url + '"'))
    assert hidden(scrub_path(url))


def test_a_url_in_a_template_field_is_read_like_any_text():
    command = "spark-submit --url {{ var.value.get('u', '" + thin() + "') }} --day {{ ds }} job.py"
    shown = safe_command(command)
    assert hidden(shown) and "{{ ds }}" in shown and MASK in shown
    assert "{{ var.value.get(" in shown  # the field is still the field


def test_a_long_argument_cannot_be_cut_inside_the_password_by_the_white_space_before_it():
    secret = PHRASE * 3
    head = "jdbc:oracle:thin:scott/"
    padded = " " * (2_000 - len(head) - 10) + head + secret + "@dbhost:1521/ORCL"  # cut: 10 in
    shown = safe_args([padded])[0]
    assert PHRASE[:8].lower() not in shown.lower() and MASK in shown
    assert PHRASE[:8].lower() not in safe_args(["--src", padded])[1].lower()


def test_the_host_and_the_user_stay_so_the_url_still_says_where_it_goes():
    assert safe_text(thin()) == MASKED
    assert safe_args(["--src", thin()]) == ["--src", MASKED]
    assert safe_command("spark-submit --src " + thin() + " job.py") == (
        "spark-submit --src " + MASKED + " job.py"
    )
    assert scrub_path(thin()) == MASKED


@pytest.mark.parametrize(
    "url",
    [
        "jdbc:oracle:thin:@//dbhost:1521/ORCL",
        "jdbc:oracle:thin:@dbhost:1521:ORCL",
        "jdbc:oracle:thin:scott@//dbhost:1521/ORCL",
        "jdbc:postgresql://dbhost:5432/db",
    ],
)
def test_a_url_with_no_password_comes_out_as_it_went_in(url):
    assert safe_text(url) == url
    assert safe_args(["--src", url]) == ["--src", url]
    assert scrub_path(url) == url


# --- a whole import ---------------------------------------------------------------------


def project(root: Path, url: str) -> None:
    command = f"spark-submit --conf spark.jdbc.url={url} jobs/load.py"
    files = {
        "dags/etl.py": f"""
            from airflow import DAG
            from airflow.operators.bash import BashOperator
            from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator

            with DAG("etl", schedule=None, default_args={{"owner": "data"}}) as dag:
                SparkSubmitOperator(
                    task_id="load",
                    application="jobs/load.py",
                    application_args=["--src", {url!r}],
                )
                BashOperator(
                    task_id="load_again",
                    bash_command={command!r},
                )
            """,
        "jobs/load.py": f"""
            from pyspark.sql import SparkSession

            spark = SparkSession.builder.getOrCreate()
            rows = spark.read.format("jdbc").option("url", {url!r}).option("dbtable", "S.T").load()
            rows.write.format("iceberg").saveAsTable("db.orders")
            """,
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


def whole_import(root: Path, monkeypatch) -> str:
    monkeypatch.chdir(root)
    contracts, report = PySparkImporter().import_to_contracts("jobs", options={"dags": "dags"})
    lines: List[str] = [yaml.safe_dump(contracts)]
    for name in ("mapped_one_to_one", "required_defaults", "unsupported", "notes"):
        lines.extend(getattr(report, name))
    return "\n".join(lines)


@pytest.mark.parametrize("url", [thin(), thin("//dbhost:1521/ORCL"), SHAPES[6], SHAPES[8]])
def test_an_import_of_a_job_with_the_url_in_a_task_a_conf_and_an_option_writes_no_password(
    tmp_path, monkeypatch, url
):
    project(tmp_path, url)
    written = whole_import(tmp_path, monkeypatch)
    assert PHRASE not in written
    assert "/" + MASK + "@" in written  # the task's argument is recorded, masked
