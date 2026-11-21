# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What a proposal does with names, ids and text that come from code nobody has reviewed."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.cli.import_workflow import _scan
from fluid_build.cli.import_workflow._conventions import Conventions
from fluid_build.cli.import_workflow._proposal import (
    _unique,
    ident,
    safe_args,
    safe_command,
    safe_text,
)
from fluid_build.cli.import_workflow.pyspark import PySparkImporter
from fluid_build.schema_manager import FluidSchemaManager


def write(root: Path, files: Dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "from pyspark.sql import SparkSession\n" + textwrap.dedent(text), encoding="utf-8"
        )


def propose(root: Path, **options: Any):
    return PySparkImporter().import_to_contracts(str(root / "jobs"), options=options)


def valid(contract: Dict[str, Any]) -> None:
    result = FluidSchemaManager().validate_contract(
        contract, schema_version=contract["fluidVersion"], offline_only=True
    )
    assert result.is_valid, result.errors


# ── identifiers ───────────────────────────────────────────────────────


def test_an_identifier_folds_accents_and_keeps_names_with_no_ascii_apart():
    assert ident("Café_Orders") == "cafe_orders"
    first, second = ident("日本語"), ident("中文表")
    assert re.fullmatch(r"x-[0-9a-f]{8}", first) and first != second
    assert ident("日本語", fallback="job").startswith("job-")
    assert ident("   ") == "x" and ident("") == "x"


def test_an_identifier_is_cut_with_a_digest_so_two_long_names_stay_two():
    a, b = ident("a" * 300 + "1"), ident("a" * 300 + "2")
    assert len(a) <= 120 and len(b) <= 120 and a != b
    assert re.fullmatch(r"[a-z0-9_][a-z0-9_.-]*[a-z0-9_]", a)


def test_a_number_is_not_handed_out_twice():
    taken = {"orders", "orders_2"}
    assert _unique("orders", taken, "_") == "orders_3"
    assert _unique("orders", taken, "_") == "orders_4"
    assert _unique("fresh", taken, "_") == "fresh"


# ── text from the code ────────────────────────────────────────────────


def test_text_from_the_code_loses_control_separator_and_bidi_characters():
    text = "a\nb\x1b[31mc\u2028d\u2029e\u202ef\u2066g\u200bh\x85i\x07j"
    cleaned = safe_text(text)
    for char in "\n\x1b\u2028\u2029\u202e\u2066\u200b\x85\x07":
        assert char not in cleaned
    assert cleaned == "a b [31mc d efgh i j"


def test_a_file_name_with_a_new_line_is_shown_clean(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, {"jobs/ev\nil\x1b[31m.py": 'spark.table("a.b")\n'})
    scan = _scan.scan_jobs(tmp_path / "jobs", Conventions())
    [job] = scan.jobs
    assert "\n" not in job.shown and "\x1b" not in job.shown


# ── redaction keeps what is not a secret exactly as written ───────────


def test_a_command_keeps_its_quoting_and_loses_only_the_credentials():
    command = (
        "PGPASSWORD=hunter2hunter2 spark-submit --name 'my job' --master yarn "
        "--conf spark.hadoop.fs.s3a.secret.key=abcd1234efgh "
        "--conf spark.sql.shuffle.partitions=10 --conf spark.cassandra.keyspace=ks "
        '--password hunter2hunter2 job.py "$@" $EXTRA {{ ds }} --day "{{ ds }}"'
    )
    shown = safe_command(command)
    assert "hunter2" not in shown and "abcd1234efgh" not in shown
    assert "PGPASSWORD=***REDACTED***" in shown
    assert "spark.hadoop.fs.s3a.secret.key=***REDACTED***" in shown
    assert "--name 'my job'" in shown and '"$@"' in shown and "$EXTRA" in shown
    assert "spark.sql.shuffle.partitions=10" in shown and "spark.cassandra.keyspace=ks" in shown
    assert '--day "{{ ds }}"' in shown and "job.py" in shown


@pytest.mark.parametrize(
    "command, hidden",
    [
        ("tool -p hunter2hunter2 x", "hunter2"),
        ("tool -w hunter2hunter2 x", "hunter2"),
        ("tool --pwd=hunter2hunter2 x", "hunter2"),
        ("tool --access-key hunter2hunter2 x", "hunter2"),
        ("tool --api-token hunter2hunter2 x", "hunter2"),
        ("curl -u admin:hunter2hunter2 https://h/x", "hunter2"),
        ("tool --conf=spark.ssl.keyPassword=hunter2hunter2", "hunter2"),
        ("tool -c spark.hadoop.fs.s3a.session.token=hunter2hunter2", "hunter2"),
        ('bash -c "PGPASSWORD=hunter2hunter2 psql -h x"', "hunter2"),
        ("export DB_PASSWORD=hunter2hunter2; run.sh", "hunter2"),
        ("tool " + "jdbc:postgresql://u:" + "hunter2hunter2" + "@h/db", "hunter2"),
    ],
)
def test_a_credential_in_a_command_is_masked_wherever_the_command_puts_it(command, hidden):
    shown = safe_command(command)
    assert hidden not in shown
    assert "***REDACTED***" in shown


def test_a_command_text_after_dash_c_is_read_as_a_command_not_as_a_setting():
    shown = safe_command('bash -c "PGPASSWORD=hunter2hunter2 psql -h db; echo done"')
    assert shown == "bash -c 'PGPASSWORD=***REDACTED*** psql -h db; echo done'"
    # ``-c`` before a dotted key is a Spark setting, and a value with spaces stays as written
    assert (
        safe_command("spark-submit -c spark.hadoop.fs.s3a.secret.key=abcd1234efgh job.py")
        == "spark-submit -c spark.hadoop.fs.s3a.secret.key=***REDACTED*** job.py"
    )
    kept = 'spark-submit --conf "spark.driver.extraJavaOptions=-Dx=1 -Dy=2" job.py'
    assert safe_command(kept) == kept


def test_a_command_flattens_new_lines_without_joining_commands():
    assert safe_command("cd /x\nspark-submit job.py") == "cd /x; spark-submit job.py"
    assert (
        safe_command("spark-submit \\\n  --master yarn \\\n  job.py")
        == "spark-submit --master yarn job.py"
    )


def test_arguments_are_masked_the_same_way():
    shown = safe_args(
        [
            "--password",
            "hunter2hunter2",
            "--conf",
            "spark.hadoop.fs.s3a.secret.key=abcd1234efgh",
            "PGPASSWORD=zzzzzzzz",
            "--day",
            "2026-01-01",
            "--note",
            "two words",
            "--keyspace",
            "ks",
        ]
    )
    assert shown == [
        "--password",
        "***REDACTED***",
        "--conf",
        "spark.hadoop.fs.s3a.secret.key=***REDACTED***",
        "PGPASSWORD=***REDACTED***",
        "--day",
        "2026-01-01",
        "--note",
        "two words",
        "--keyspace",
        "ks",
    ]


# ── one product per thing, whatever the names look like ───────────────


def test_two_jobs_with_the_same_file_name_are_two_products(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(
        tmp_path,
        {
            "jobs/a/main.py": 'df.write.saveAsTable("retail.from_a")\n',
            "jobs/b/main.py": 'df.write.saveAsTable("retail.from_b")\n',
            "jobs/solo.py": 'df.write.saveAsTable("retail.from_solo")\n',
        },
    )
    contracts, _ = propose(tmp_path, split_by="job")
    assert sorted(c["id"] for c in contracts) == ["a-main", "b-main", "solo"]
    for contract in contracts:
        assert len(contract["builds"]) == 1 and len(contract["exposes"]) == 1
        valid(contract)


def test_a_folder_named_like_the_scanned_directory_is_not_the_files_at_the_top(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    write(
        tmp_path,
        {
            "jobs/top.py": 'df.write.saveAsTable("retail.t1")\n',
            "jobs/jobs/inner.py": 'df.write.saveAsTable("retail.t2")\n',
        },
    )
    contracts, _ = propose(tmp_path, split_by="folder")
    assert len(contracts) == 2
    assert sorted(c["id"] for c in contracts) == ["jobs", "jobs-2"]
    assert sorted(len(c["exposes"]) for c in contracts) == [1, 1]


def test_ids_are_unique_in_a_product_and_across_products(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(
        tmp_path,
        {
            "jobs/many.py": """
                df.write.saveAsTable("a.orders")
                df.write.saveAsTable("b.orders")
                df.write.saveAsTable("a.orders_2")
                df.write.parquet("s3://bucket/Orders/")
                df.write.parquet("s3://bucket/orders/")
            """,
        },
    )
    (project,), _ = propose(tmp_path, split_by="project")
    ids = [e["exposeId"] for e in project["exposes"]]
    assert len(ids) == 5 and len(set(ids)) == 5
    # a path differing only in case is another path on an object store
    paths = [
        e["binding"]["location"] for e in project["exposes"] if "bucket" in e["binding"]["location"]
    ]
    assert sorted(p["path"] for p in paths) == ["Orders", "orders"]

    contracts, _ = propose(tmp_path)  # one product per output
    pids = [c["id"] for c in contracts]
    assert len(pids) == 5 and len(set(pids)) == 5


def test_outputs_that_differ_only_by_catalog_are_proposed_as_one_and_said_so(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(
        tmp_path,
        {
            "jobs/both.py": """
                df.write.saveAsTable("prod.retail.t")
                df.write.saveAsTable("dev.retail.t")
            """,
        },
    )
    contracts, report = propose(tmp_path)
    assert len(contracts) == 1
    assert any("differ only by catalog" in note for note in report.notes)


def test_a_job_whose_outputs_are_in_different_products_says_its_lineage_is_per_job(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    write(
        tmp_path,
        {
            "jobs/fan.py": """
                src = spark.table("retail.src")
                src.write.saveAsTable("retail.o1")
                src.write.saveAsTable("retail.o2")
            """,
        },
    )
    contracts, report = propose(tmp_path)
    assert len(contracts) == 2
    assert [n for n in report.notes if "which input feeds which output" in n]
    together, report = propose(tmp_path, split_by="job")
    assert len(together) == 1
    assert not [n for n in report.notes if "which input feeds which output" in n]


def test_a_read_that_gets_the_products_own_id_is_not_listed_as_its_dependency(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    write(
        tmp_path,
        {
            "jobs/own.py": """
                spark.table("jobs.src").write.saveAsTable("retail.out")
            """,
        },
    )
    (tmp_path / "conventions.yaml").write_text(
        'version: 1\nproducts:\n  id: "{database}"\n', encoding="utf-8"
    )
    contracts, report = propose(
        tmp_path, split_by="project", conventions=str(tmp_path / "conventions.yaml")
    )
    (contract,) = contracts
    assert contract["id"] == "jobs"
    assert "consumes" not in contract
    assert any("own id" in n for n in report.notes)


def test_a_dag_id_that_does_not_match_is_reported_once_for_all_its_jobs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(
        tmp_path,
        {
            "jobs/one.py": 'df.write.saveAsTable("retail.one")\n',
            "jobs/two.py": 'df.write.saveAsTable("retail.two")\n',
        },
    )
    (tmp_path / "dags").mkdir()
    (tmp_path / "dags" / "bad.py").write_text(
        textwrap.dedent(
            """
            from airflow import DAG
            from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
            with DAG("Bad DAG Id") as dag:
                SparkSubmitOperator(task_id="Bad Task", application="jobs/one.py")
                SparkSubmitOperator(task_id="Bad Task Two", application="jobs/two.py")
            """
        ),
        encoding="utf-8",
    )
    contracts, report = propose(tmp_path, dags=str(tmp_path / "dags"))
    assert len(contracts) == 2
    lines = [n for n in report.unsupported if "DAG id" in n and "does not match" in n]
    assert len(lines) == 1 and lines[0].startswith("dags/bad.py:")


# ── a script path in a DAG file does not get to look at the file system ──


class _Spy:
    def __init__(self, monkeypatch) -> None:
        self.seen: List[str] = []
        original = Path.is_file

        def is_file(path: Path, *args: Any, **kwargs: Any) -> bool:
            self.seen.append(str(path))
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "is_file", is_file)


@pytest.mark.parametrize(
    "script",
    [
        "../outside.py",
        "jobs/../../outside.py",
        "/etc/hosts",
        "C:/Windows/win.ini",
        "jobs\\..\\..\\outside.py",
        "a\x00b.py",
    ],
)
def test_a_script_path_that_leaves_the_project_is_not_looked_at(script, tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "jobs").mkdir(parents=True)
    (tmp_path / "outside.py").write_text("x = 1\n", encoding="utf-8")
    spy = _Spy(monkeypatch)
    assert _scan.local_script(script, Conventions(), project) is None
    assert spy.seen == []


def test_a_script_root_path_that_leaves_its_directory_is_not_looked_at(tmp_path, monkeypatch):
    code = tmp_path / "code"
    code.mkdir()
    (tmp_path / "secret.py").write_text("x = 1\n", encoding="utf-8")
    conventions = Conventions(script_roots=(("s3://bucket/jobs/", code),))
    spy = _Spy(monkeypatch)
    assert _scan.local_script("s3://bucket/jobs/../secret.py", conventions, tmp_path) is None
    assert _scan.local_script("s3://bucket/jobs//etc/hosts", conventions, tmp_path) is None
    assert spy.seen == []


def test_a_script_inside_the_project_is_still_found(tmp_path):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "a.py").write_text("x = 1\n", encoding="utf-8")
    found = _scan.local_script("jobs/a.py", Conventions(), tmp_path)
    assert found is not None and found.name == "a.py"
    code = tmp_path / "jobs"
    conventions = Conventions(script_roots=(("s3://bucket/jobs/", code),))
    assert _scan.local_script("s3://bucket/jobs/a.py", conventions, tmp_path) == code / "a.py"


def test_a_symlink_that_leaves_the_project_is_not_followed(tmp_path):
    project = tmp_path / "project"
    (project / "jobs").mkdir(parents=True)
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    (project / "jobs" / "link.py").symlink_to(outside)
    assert _scan.local_script("jobs/link.py", Conventions(), project) is None


# ── how much one import holds ─────────────────────────────────────────


def test_a_scan_stops_after_the_most_reads_and_writes_it_keeps(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_scan, "MAX_REFS_PER_SCAN", 3)
    write(
        tmp_path,
        {
            f"jobs/j{i}.py": f'spark.table("db.in{i}")\ndf.write.saveAsTable("db.out{i}")\n'
            for i in range(6)
        },
    )
    scan = _scan.scan_jobs(tmp_path / "jobs", Conventions())
    assert len(scan.jobs) == 2  # the second file takes it past 3
    assert any("stopped after 4 reads and writes: 4 file(s) not read" in n for n in scan.notes)


def test_a_proposal_with_too_many_outputs_or_products_is_refused_with_a_way_out(
    tmp_path, monkeypatch
):
    from fluid_build._errors import FluidUserError
    from fluid_build.cli.import_workflow import _proposal

    monkeypatch.chdir(tmp_path)
    write(
        tmp_path,
        {"jobs/many.py": "\n".join(f'df.write.saveAsTable("db.o{i}")' for i in range(5)) + "\n"},
    )
    monkeypatch.setattr(_proposal, "MAX_OUTPUTS", 3)
    with pytest.raises(FluidUserError) as refused:
        propose(tmp_path)
    assert "too much to propose" in refused.value.what
    assert "more than 3 distinct outputs" in refused.value.why
    assert "smaller directory" in refused.value.fix and "ignore.files" in refused.value.fix

    monkeypatch.setattr(_proposal, "MAX_OUTPUTS", 50_000)
    monkeypatch.setattr(_proposal, "MAX_PRODUCTS", 2)
    with pytest.raises(FluidUserError) as refused:
        propose(tmp_path)  # five tables are five products
    assert "5 products (the most is 2)" in refused.value.why
    contracts, _ = propose(tmp_path, split_by="project")  # one product holding them all
    assert len(contracts) == 1 and len(contracts[0]["exposes"]) == 5
