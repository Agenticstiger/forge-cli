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


def test_a_command_keeps_its_lines_and_its_continuations():
    assert safe_command("cd /x\nspark-submit job.py") == "cd /x\nspark-submit job.py"
    continued = "spark-submit \\\n  --master yarn \\\n  job.py"
    assert safe_command(continued) == continued
    assert safe_command("\nspark-submit x.py\n") == "spark-submit x.py"  # trimmed at the ends


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
                a = spark.table("retail.a")
                b = spark.table("retail.b")
                combine(a, b).write.saveAsTable("retail.o1")
                combine(b, a).write.saveAsTable("retail.o2")
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


@pytest.mark.parametrize(
    "command",
    [
        "mkdir -p /tmp/out/{{ ds }} && cp a.csv /tmp/out/",
        "ssh -p 22 host ls",
        "docker run -p 8080 image",
        "hadoop fs -mkdir -p ~/stage",
        "tool --partition-key dt --primary-key id --sort-key ts --key col",
        "spark-submit --conf spark.cassandra.connection.keyspace=ks job.py --keyspace ks",
    ],
)
def test_ordinary_flags_that_look_like_credential_flags_are_left_alone(command):
    assert safe_command(command) == command


@pytest.mark.parametrize(
    "command",
    [
        "tool --access-key hunter2hunter2",
        "tool --secret-key=hunter2hunter2",
        "tool --api-key hunter2hunter2",
        "tool --ssh-key hunter2hunter2",
        "tool --accessKey hunter2hunter2",
        "tool -p hunter2hunter2",
        "sshpass -p hunter2hunter2 ssh host",
    ],
)
def test_credential_flags_still_mask_their_value(command):
    shown = safe_command(command)
    assert "hunter2" not in shown and "***REDACTED***" in shown


# ── a credential next to an operator, in another spelling, or in a path ──


@pytest.mark.parametrize(
    "command, expected",
    [
        (
            "export TOKEN=hunter2hunter2; spark-submit main.py",
            "export TOKEN=***REDACTED***; spark-submit main.py",
        ),
        (
            "spark-submit x.py --password hunter2hunter2; echo done",
            "spark-submit x.py --password ***REDACTED***; echo done",
        ),
        ("PGPASSWORD=hunter2hunter2&&psql", "PGPASSWORD=***REDACTED***&&psql"),
        (
            "spark-submit x.py --token hunter2hunter2|tee log",
            "spark-submit x.py --token ***REDACTED***|tee log",
        ),
        ("(PGPASSWORD=hunter2hunter2 psql)", "(PGPASSWORD=***REDACTED*** psql)"),
        ("echo $(PGPASSWORD=hunter2hunter2 psql)", "echo $(PGPASSWORD=***REDACTED*** psql)"),
        ("echo `PGPASSWORD=hunter2hunter2 psql`", "echo `PGPASSWORD=***REDACTED*** psql`"),
        ("a &&\n b", "a &&\n b"),
        ("a\nb", "a\nb"),
        ("cd /x \\\n  && ls", "cd /x \\\n  && ls"),
        ("spark-submit a.py \\\r\n  --x 1", "spark-submit a.py \\\n  --x 1"),  # CRLF becomes LF
    ],
)
def test_masking_a_value_keeps_the_operators_and_the_lines_around_it(command, expected):
    assert safe_command(command) == expected


@pytest.mark.parametrize(
    "command",
    [
        "tool --passwd hunter2hunter2",
        "tool --pw hunter2hunter2",
        "tool --pswd hunter2hunter2",
        "tool --dbpass hunter2hunter2",
        "tool --dbpwd hunter2hunter2",
        "tool --accesskey hunter2hunter2",
        "curl --user admin:hunter2hunter2 http://h",
        "curl --user=admin:hunter2hunter2 http://h",
        "curl -uadmin:hunter2hunter2 http://h",
        "mysql -u root -phunter2hunter2 -e 'select 1'",
        'curl -H "X-Api-Key: hunter2hunter2" x',
        'curl -H "password: hunter2hunter2" x',
        "curl -H 'Authorization: Bearer hunter2hunter2' x",
        "sh -c $'PGPASSWORD=hunter2hunter2 run'",
        'bash -c \'bash -c "bash -c \\"PGPASSWORD=hunter2hunter2 run\\""\'',
        "cmd --password -hunter2hunter2",
        "tool --extra '--password hunter2hunter2'",
    ],
)
def test_the_credential_spellings_a_free_text_scan_misses_are_masked(command):
    shown = safe_command(command)
    assert "hunter2" not in shown and "***REDACTED***" in shown


@pytest.mark.parametrize(
    "command",
    [
        "tool --password {{ var.value.pw }} --x 1",
        "curl --user bob http://h",
        "mysql -p3306",
        "tool --partition-key dt",
    ],
)
def test_a_reference_a_user_name_a_port_and_a_column_are_left_alone(command):
    assert safe_command(command) == command


def test_masking_does_not_unbalance_the_quotes_of_what_it_keeps():
    shown = safe_command('curl -H "X-Api-Key: pw1" -H "password: pw2" x')
    assert shown == "curl -H 'X-Api-Key: ***REDACTED***' -H 'password: ***REDACTED***' x"
    assert safe_command("curl -H 'Authorization: Bearer abc123' x") == (
        "curl -H 'Authorization: ***REDACTED***' x"
    )


def test_a_data_path_and_a_host_lose_the_credential_in_them():
    from fluid_build.cli.import_workflow._redact import scrub_host, scrub_path

    key, word = "AKIA" + "IOSFODNN7EXAMPLE", "hunter2" + "word"
    assert scrub_path(f"s3://{key}:{word}@bucket/curated/orders") == "s3://bucket/curated/orders"
    assert scrub_path(f"https://admin:{word}@files.example.com/x/y") == (
        "https://files.example.com/x/y"
    )
    assert scrub_path("gs://b/x/token=hunterE/y/dt=2026-01-01") == (
        "gs://b/x/token=REDACTED/y/dt=2026-01-01"
    )
    assert (
        scrub_path("s3://bucket/a@b/c") == "s3://bucket/a@b/c"
    )  # an @ in the path is not userinfo
    assert scrub_host(f"user:{word}@broker1:9092,broker2:9092") == "broker1:9092,broker2:9092"


def test_no_credential_in_a_path_reaches_an_id_a_file_name_or_a_binding(tmp_path, monkeypatch):
    import yaml

    monkeypatch.chdir(tmp_path)
    key, word, token = "AKIA" + "IOSFODNN7EXAMPLE", "hunter2" + "word", "hunterE" + "token"
    write(
        tmp_path,
        {
            "jobs/job.py": f"""
                src = spark.read.parquet("s3a://{key}:{word}@inbucket/raw/orders")
                src.write.parquet("s3://{key}:{word}@bucket/curated/orders")
                src.write.parquet("https://admin:{word}@files.example.com/x/y")
                src.write.parquet("gs://b/x/token={token}/y")
            """,
        },
    )
    contracts, report = propose(tmp_path)
    text = yaml.safe_dump(contracts) + "\n".join(
        [*report.mapped_one_to_one, *report.required_defaults, *report.unsupported, *report.notes]
    )
    for value in (key, word, token, key.lower(), word.lower(), token.lower()):
        assert value not in text, value
    assert any(c["id"].startswith("bucket.") for c in contracts)
    for contract in contracts:
        valid(contract)


# ── what the fan-out of a job's reads costs ───────────────────────────


def test_one_import_parses_a_bounded_amount_of_sql_and_says_what_it_left(tmp_path, monkeypatch):
    from fluid_build.cli.import_workflow import _pipeline
    from fluid_build.cli.import_workflow._pysource import SourceBudget

    monkeypatch.chdir(tmp_path)
    pad = ", ".join(f"c{i}" for i in range(100))  # about 500 characters a statement
    calls = "\n".join(
        f'spark.sql("insert into out.t{i} select {pad} from db.t{i}")' for i in range(6)
    )
    write(tmp_path, {"jobs/sql.py": "spark = SparkSession.builder.getOrCreate()\n" + calls + "\n"})
    monkeypatch.setattr(_pipeline, "SourceBudget", lambda: SourceBudget(sql_left=2000))
    contracts, report = propose(tmp_path)
    assert (
        len(contracts) == 3
    )  # three statements (about 640 characters each) fit in 2,000; three were left
    said = [n for n in report.notes if "stopped parsing SQL after" in n]
    assert len(said) == 1 and "1,500,000 characters" in said[0] and "3 statement(s)" in said[0]


def test_the_same_sql_text_is_charged_once(tmp_path, monkeypatch):
    from fluid_build.cli.import_workflow import _pipeline
    from fluid_build.cli.import_workflow._pysource import SourceBudget

    monkeypatch.chdir(tmp_path)
    call = 'spark.sql("insert into out.t select a, b, c from db.t")'
    write(
        tmp_path,
        {
            "jobs/sql.py": "spark = SparkSession.builder.getOrCreate()\n"
            + "\n".join([call] * 50)
            + "\n"
        },
    )
    monkeypatch.setattr(_pipeline, "SourceBudget", lambda: SourceBudget(sql_left=100))
    contracts, report = propose(tmp_path)
    assert len(contracts) == 1 and not [n for n in report.notes if "SQL" in n and "not read" in n]


def test_a_job_with_many_outputs_and_reads_is_refused_before_it_is_listed_everywhere(
    tmp_path, monkeypatch
):
    from fluid_build._errors import FluidUserError
    from fluid_build.cli.import_workflow import _proposal

    monkeypatch.chdir(tmp_path)
    body = "\n".join(f'spark.table("t{i}")' for i in range(8))
    body += "\n" + "\n".join(f'combine(spark).write.saveAsTable("o{i}")' for i in range(8))
    write(tmp_path, {"jobs/fan.py": body + "\n"})
    monkeypatch.setattr(_proposal, "MAX_FANOUT", 50)  # 8 products x 8 reads = 64
    contracts, report = propose(tmp_path)  # each lists fewer: 8 x 6 = 48
    assert len(contracts) == 8 and all(len(c["consumes"]) == 6 for c in contracts)
    assert [n for n in report.notes if "64 reads between them (the most is 50)" in n]
    monkeypatch.setattr(_proposal, "MAX_FANOUT", 30)  # 8 products x 5 reads = 40: too many
    with pytest.raises(FluidUserError) as refused:
        propose(tmp_path)
    assert "too much to propose" in refused.value.what
    assert "64 reads between them (the most is 30)" in refused.value.why
    contracts, _ = propose(tmp_path, split_by="job")  # one product: 8 reads
    assert len(contracts) == 1 and len(contracts[0]["consumes"]) == 8


def test_what_a_product_lists_is_what_counts_toward_the_fan_out_not_every_read_of_its_job(
    tmp_path, monkeypatch
):
    from fluid_build.cli.import_workflow import _proposal

    monkeypatch.chdir(tmp_path)
    body = "\n".join(f'spark.table("t{i}")' for i in range(12))
    body += "\n" + "\n".join(f'df.write.saveAsTable("o{i}")' for i in range(6))
    write(tmp_path, {"jobs/wide.py": body + "\n"})
    monkeypatch.setattr(_proposal, "MAX_CONSUMES_PER_PRODUCT", 4)
    monkeypatch.setattr(_proposal, "MAX_FANOUT", 30)  # 6 products x 4 listed reads = 24
    contracts, _ = propose(tmp_path)  # 6 x 12 = 72 reads would be over 30, 6 x 4 is not
    assert len(contracts) == 6 and all(len(c["consumes"]) == 4 for c in contracts)


def test_the_reads_a_proposal_goes_through_are_counted_too(tmp_path, monkeypatch):
    from fluid_build._errors import FluidUserError
    from fluid_build.cli.import_workflow import _proposal

    monkeypatch.chdir(tmp_path)
    body = "\n".join(f'spark.table("t{i}")' for i in range(8))
    body += "\n" + "\n".join(f'combine(spark).write.saveAsTable("o{i}")' for i in range(8))
    write(tmp_path, {"jobs/fan.py": body + "\n"})
    monkeypatch.setattr(_proposal, "MAX_READ_VISITS", 50)  # 8 products x 8 reads = 64
    with pytest.raises(FluidUserError) as refused:
        propose(tmp_path)
    assert "go through 64 reads between them (the most is 50)" in refused.value.why


def test_a_product_lists_a_bounded_number_of_reads_and_the_report_counts_the_rest(
    tmp_path, monkeypatch
):
    from fluid_build.cli.import_workflow import _proposal

    monkeypatch.chdir(tmp_path)
    reads = "\n".join(f'    spark.table("t{i}")' for i in range(10))
    write(
        tmp_path, {"jobs/many.py": "def run():\n" + reads + '\n    df.write.saveAsTable("out")\n'}
    )
    monkeypatch.setattr(_proposal, "MAX_CONSUMES_PER_PRODUCT", 6)
    monkeypatch.setattr(_proposal, "MAX_READ_LINES", 2)
    (contract,), report = propose(tmp_path)
    assert len(contract["consumes"]) == 6
    assert [n for n in report.notes if "lists 6 reads in consumes[]; 4 more are not listed" in n]
    lines = [x for x in report.mapped_one_to_one if "→ consumes[" in x]
    assert len(lines) == 3
    assert lines[-1].endswith("4 more read(s) → consumes[] (not listed one by one)")


def test_a_task_that_submits_one_job_twice_is_one_entry_on_the_build(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path, {"jobs/ok.py": 'df.write.saveAsTable("retail.out")\n'})
    (tmp_path / "dags").mkdir()
    (tmp_path / "dags" / "twice.py").write_text(
        textwrap.dedent(
            """
            from airflow import DAG
            from airflow.operators.bash import BashOperator
            with DAG("twice"):
                BashOperator(task_id="t", bash_command="spark-submit jobs/ok.py --a 1; spark-submit jobs/ok.py --a 2")
            """
        ),
        encoding="utf-8",
    )
    (contract,), report = propose(tmp_path, dags=str(tmp_path / "dags"))
    tasks = contract["builds"][0]["execution"]["orchestration"]["airflow"]["tasks"]
    assert [t["taskId"] for t in tasks] == ["t"]
    assert any("submits this job more than once" in n for n in report.notes)
    valid(contract)


@pytest.mark.parametrize(
    "command",
    [
        "spark-submit --master yarn --deploy-mode cluster --conf spark.executor.memory=4g --py-files s3://b/deps.zip s3://b/job.py --date {{ ds }}",
        "hdfs dfs -mkdir -p /data/{{ ds }}",
        "aws s3 cp s3://bucket/key.csv /tmp/key.csv --region eu-west-1",
        "kubectl apply -f deploy.yaml -n prod",
        "docker run --rm -v /data:/data -e ENV=prod img:latest python run.py --key id",
        """curl -s -X POST -H "Content-Type: application/json" -d '{"a": 1}' https://api.example.com/v1/x""",
        'psql -h db -U app -d warehouse -c "select 1"',
        "git clone https://github.com/org/repo.git && cd repo && git checkout -b feature",
        """dbt run --select tag:daily --target prod --vars '{"run_date": "{{ ds }}"}'""",
        'echo "done" > /tmp/flag; cat /tmp/flag',
        "bq query --use_legacy_sql=false 'SELECT 1'",
        "sqoop import --connect jdbc:mysql://h/db --username app --password-file /user/app/pw --table t",
        "spark-submit --keytab /etc/x.keytab --principal app@REALM job.py",
        "tool --secret-name prod/db --token-url https://auth.example.com/token --access-key-id AKIA0000",
        "tar -czf out.tgz -C /data . && scp -P 2222 out.tgz host:/x",
    ],
)
def test_realistic_commands_that_hold_no_secret_come_out_as_they_went_in(command):
    assert safe_command(command) == command


def test_a_path_to_a_secret_is_not_the_secret_and_a_url_in_a_url_flag_is_still_scanned():
    userinfo = "admin" + ":" + "pw" + "@"
    shown = safe_command(
        f"tool --password-file /run/pw --token-url https://{userinfo}auth.example.com/t"
    )
    assert "--password-file /run/pw" in shown
    assert "pw@" not in shown and "***REDACTED***" in shown


def test_a_partition_directory_is_not_a_credential_unless_its_key_is_one():
    from fluid_build.cli.import_workflow._redact import scrub_path

    kept = "s3://bucket/dt=2026-01-01/region=eu/key=abc/part-0.parquet"
    assert scrub_path(kept) == kept
    assert scrub_path("s3://bucket/password=abc/x") == "s3://bucket/password=REDACTED/x"
    words = "s3://bucket/author=bob/auth_type=basic/token_type=bearer/x.parquet"
    assert scrub_path(words) == words  # names that hold a credential's name and are not one


def test_an_azure_container_is_a_place_and_a_container_with_a_password_is_not():
    from fluid_build.cli.import_workflow._redact import scrub_path

    for kept in (
        "abfss://container@account.dfs.core.windows.net/path/x",
        "wasbs://c@a.blob.core.windows.net/x",
    ):
        assert scrub_path(kept) == kept
    word = "hunter2" + "word"
    assert scrub_path(f"abfss://container:{word}@account.dfs.core.windows.net/x") == (
        "abfss://account.dfs.core.windows.net/x"
    )


def test_a_credential_in_a_query_string_or_a_jdbc_url_does_not_stay_in_a_path():
    from fluid_build.cli.import_workflow._redact import scrub_path

    assert scrub_path("https://h/x?token=abc&limit=5") == "https://h/x?token=REDACTED&limit=5"
    assert scrub_path("https://h/x?sig=abc&x=1") == "https://h/x?sig=REDACTED&x=1"
    word = "hunter2" + "word"
    assert scrub_path(f"jdbc:postgresql://app:{word}@db:5432/x") == "jdbc:postgresql://db:5432/x"


# ── the spellings that went through, and the words that must not be touched ──


@pytest.mark.parametrize(
    "command",
    [
        "tool -Dpwd=hunter2hunter2",
        "spark-submit --conf spark.executor.extraJavaOptions=-Dpassword=hunter2hunter2 job.py",
        "redis-cli -a hunter2hunter2 ping",
        "openssl enc -aes-256-cbc -k hunter2hunter2 -in a -out b",
        "aws s3 cp a s3://b/c --sse-c-key hunter2hunter2",
        "tool --sas hunter2hunter2",
        "psql 'host=h user=app password=hunter2hunter2 dbname=d'",
        "tool 'Server=h;Database=d;Uid=app;Pwd=hunter2hunter2;'",
        "tool 'https://h/x?a=1&pwd=hunter2hunter2'",
        "tool 'https://h/x?token=hunter2hunter2'",
        "tool 'https://h/x?sig=hunter2hunter2&x=1'",
        'tool --conf \'{"db": {"pass": "hunter2hunter2"}}\'',
        'tool \'{"user": "bob", "' + "pw" + 'd": "hunter2hunter2"}\'',
        'tool --json \'{"db":{"pass":"hunter2hunter2"}}\'',
        "echo ok; (PGPASSWORD=hunter2hunter2 psql)",
        "if true; then PGPASSWORD=hunter2hunter2 psql; fi",
        "sudo -u app PGPASSWORD=hunter2hunter2 psql",
        'tool "password=a b;c"',
    ],
)
def test_more_credential_spellings_are_masked_and_nothing_of_the_value_is_left(command):
    shown = safe_command(command)
    assert "hunter2" not in shown and "***REDACTED***" in shown
    assert "b;c" not in shown


@pytest.mark.parametrize(
    "command",
    [
        "docker run -u 1000:1000 --rm img",
        "git log --author bob --since 2026-01-01",
        "tool --max-tokens 10 --skip-auth --author bob",
        "wc -w notes.txt && diff -w a b",
        "spark-submit --conf spark.authenticate=true job.py",
        "tool --partition-key dt --primary-key id --sort-key ts",
        "GOOGLE_APPLICATION_CREDENTIALS=/etc/gcp/key.json spark-submit job.py",
        "hdfs dfs -ls abfss://container@acct.dfs.core.windows.net/data",
        "tool s3://b/author=bob/auth_type=basic/token_type=bearer/x.parquet",
        "echo ok; (mkdir -p tmp)",
        "beeline -u jdbc:hive2://h:10000/default -n app",
        "curl -w '%{http_code}' https://h/x",
        "sudo -u app run.sh",
        "# the password comes from the vault\nspark-submit x.py --day 1",
        "for d in a b; do\n  spark-submit job.py --day $d\ndone",
    ],
)
def test_commands_with_no_secret_in_them_come_out_exactly_as_written(command):
    assert safe_command(command) == command


_WORD = "hunter2" + "hunter2"


@pytest.mark.parametrize(
    "command",
    [
        f"curl -H 'Cookie: session={_WORD}' http://x",
        f"curl -H 'Ocp-Apim-Subscription-Key: {_WORD}' http://x",
        f"curl -HX-Api-Key:{_WORD} http://x",
        f'curl -H"Authorization: token {_WORD}" http://x',
        f"curl -b session={_WORD} http://x",
        f"curl --cookie 'sid={_WORD}' http://x",
        f"docker login -u u -p {_WORD} registry",
        f"bcp t out f -U sa -P{_WORD}",
        f"x s3a://AKIA0000:wJalr/K7MDENG/{_WORD}@bucket/x",
        f"x Authorization: Bearer {_WORD} tail",
        f"azcopy copy 'https://a.blob.core.windows.net/c/f?sv=2022-11-02&sig={_WORD}' /tmp/x",
        f"curl https://host/api?key={_WORD}",
        f"curl 'https://host/api?x=1&code={_WORD}'",
        f"sqlplus -S app/{_WORD}@db @script.sql",
        f"sqlldr userid=app/{_WORD}@db control=load.ctl",
        f"curl https://storage.googleapis.com/b/o?X-Goog-Signature={_WORD}&X-Goog-Credential=x",
        "python -c 'import os; os.environ[" + '"PASS' + 'WORD"]="' + _WORD + "\"'",
    ],
)
def test_signatures_cookies_headers_and_keys_in_urls_are_masked(command):
    shown = safe_command(command)
    assert "hunter2" not in shown and "***REDACTED***" in shown


@pytest.mark.parametrize(
    "command",
    [
        "run --cookie-jar /tmp/jar --partition-key dt",
        "curl -H 'Content-Type: application/json' -H 'Accept: text/csv' https://h/x",
        "curl 'https://h/api?limit=5&page=2' -o out.json",
        "docker run -p 8080:80 img",
        "x s3a://bucket/a@b/c https://host:8080/users/bob@example.com",
        "sqlplus / as sysdba @script.sql",
        "sqlplus -S app@db @script.sql",
        "cp a/b /tmp/x && sqlplus -S @jobs/run.sql",
    ],
)
def test_headers_urls_and_flags_that_hold_no_credential_are_left_alone(command):
    assert safe_command(command) == command


def test_text_in_a_report_masks_a_signature_a_key_in_a_query_and_a_pair():
    shown = safe_text(f"see https://h/api?key={_WORD} and sig={_WORD}x then token={_WORD}y")
    assert "hunter2" not in shown and shown.count("***REDACTED***") == 3
    assert safe_text("read https://h/x?limit=5&page=2") == "read https://h/x?limit=5&page=2"


def test_an_aws_secret_with_a_slash_does_not_stay_in_the_path_or_the_ids(tmp_path, monkeypatch):
    from fluid_build.cli.import_workflow._redact import scrub_path

    word = "wJalr/K7MDENG/" + _WORD
    assert scrub_path(f"s3a://AKIA0000:{word}@bkt3/raw/c") == "s3a://bkt3/raw/c"
    assert scrub_path("s3://bucket/a@b/c") == "s3://bucket/a@b/c"
    assert scrub_path(f"https://h/api?key={_WORD}&code=abc&limit=5") == (
        "https://h/api?key=REDACTED&code=REDACTED&limit=5"
    )
    monkeypatch.chdir(tmp_path)
    write(
        tmp_path, {"jobs/job.py": f'src = spark.read.parquet("s3a://AKIA0000:{word}@bkt3/raw/c")\n'}
    )
    contracts, report = propose(tmp_path)
    import yaml

    text = yaml.safe_dump(contracts) + "\n".join(
        [*report.mapped_one_to_one, *report.required_defaults, *report.unsupported, *report.notes]
    )
    assert "hunter2" not in text and "wjalr" not in text.lower() and "akia0000" not in text.lower()


def test_a_lone_surrogate_is_not_left_in_text_that_is_printed():
    from fluid_build.cli.import_workflow._pysource import clean_chars

    shown = clean_chars("db.t" + chr(0xD800) + "x")
    assert shown == "db.t" + chr(0xFFFD) + "x"
    shown.encode("utf-8")  # does not raise


def test_a_json_argument_is_masked_by_its_keys():
    shown = safe_args(['{"db": {"pass": "hunter2hunter2"}, "port": 5432, "user": "bob"}'])
    assert shown == ['{"db": {"pass": "***REDACTED***"}, "port": 5432, "user": "bob"}']


def test_a_masked_word_is_quoted_again_when_it_holds_an_operator_or_white_space():
    shown = safe_command("tool 'X-Api-Key: a;b|c' --note ok")
    assert shown == "tool 'X-Api-Key: ***REDACTED***' --note ok"
    name = "PGPASS" + "WORD"
    assert safe_command(f"{name}='a b;c' run") == f"{name}=***REDACTED*** run"


def test_a_table_or_path_name_longer_than_any_object_key_is_not_kept(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    long_path = "s3://b/" + "a" * 600
    write(
        tmp_path,
        {"jobs/long.py": f'df.write.parquet("{long_path}")\ndf.write.saveAsTable("db.ok")\n'},
    )
    contracts, report = propose(tmp_path)
    assert len(contracts) == 1
    assert [x for x in report.unsupported if "the name is longer than 512 characters" in x]
    assert "a" * 200 not in "".join(report.unsupported)  # the name is not repeated in full


# ── what a job's tasks cost when the job feeds many products ──────────


def _fan_project(root: Path, outputs: int, tasks: int, args=(), command: str = "") -> None:
    """One job that writes ``outputs`` tables, submitted by ``tasks`` tasks of one DAG."""
    writes = "".join(f"df.write.parquet('s3://b/o{i}')\n" for i in range(outputs))
    write(root, {"jobs/job.py": "df = spark.read.parquet('s3://src/in')\n" + writes})
    (root / "dags").mkdir(exist_ok=True)
    listing = ", ".join(repr(a) for a in args)
    if command:
        body = f"    BashOperator(task_id='t', bash_command={command!r})\n"
    else:
        body = "".join(
            f"    SparkSubmitOperator(task_id='t{i}', application='jobs/job.py', "
            f"application_args=[{listing}])\n"
            for i in range(tasks)
        )
    head = (
        "from airflow import DAG\n"
        "from airflow.operators.bash import BashOperator\n"
        "from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator\n"
        "with DAG('d1'):\n"
    )
    (root / "dags" / "d.py").write_text(head + body, encoding="utf-8")


def _tasks_of(contract: Dict[str, Any]) -> List[Dict[str, Any]]:
    return contract["builds"][0]["execution"]["orchestration"]["airflow"]["tasks"]


def test_a_job_records_a_bounded_number_of_its_tasks_and_says_how_many_it_left(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    _fan_project(tmp_path, outputs=1, tasks=6)
    monkeypatch.setattr(_scan, "MAX_TASKS_PER_JOB", 3)
    (contract,), report = propose(tmp_path, dags=str(tmp_path / "dags"))
    assert len(_tasks_of(contract)) == 3
    assert [n for n in report.notes if "3 more task(s) submit" in n and "first 3 are recorded" in n]


def test_a_task_that_submits_many_scripts_keeps_the_first_ones_and_says_so(tmp_path, monkeypatch):
    from fluid_build.cli.import_workflow import _airflow_dags

    monkeypatch.chdir(tmp_path)
    command = "; ".join(f"spark-submit jobs/job.py --a {i}" for i in range(5))
    _fan_project(tmp_path, outputs=1, tasks=1, command=command)
    monkeypatch.setattr(_airflow_dags, "_MAX_SUBMITS_PER_TASK", 2)
    _contracts, report = propose(tmp_path, dags=str(tmp_path / "dags"))
    assert [n for n in report.unsupported if "it submits 5 scripts; the first 2 are read" in n]


def test_a_task_lists_a_bounded_number_of_arguments_and_the_report_counts_the_rest(
    tmp_path, monkeypatch
):
    from fluid_build.cli.import_workflow import _proposal

    monkeypatch.chdir(tmp_path)
    _fan_project(tmp_path, outputs=1, tasks=1, args=[f"--a{i}" for i in range(5)])
    monkeypatch.setattr(_proposal, "MAX_ARGS_PER_TASK", 3)
    (contract,), report = propose(tmp_path, dags=str(tmp_path / "dags"))
    assert _tasks_of(contract)[0]["params"]["args"] == ["--a0", "--a1", "--a2"]
    assert [n for n in report.notes if "passes 5 arguments: the first 3 are listed" in n]


def test_the_tasks_of_a_job_are_read_once_however_many_products_the_job_feeds(
    tmp_path, monkeypatch
):
    import yaml

    from fluid_build.cli.import_workflow import _proposal

    monkeypatch.chdir(tmp_path)
    _fan_project(tmp_path, outputs=6, tasks=4, args=["--day", "2026-01-01"])
    calls: List[int] = []
    real = _proposal.safe_args
    monkeypatch.setattr(_proposal, "safe_args", lambda *a, **k: calls.append(1) or real(*a, **k))
    contracts, _report = propose(tmp_path, dags=str(tmp_path / "dags"))
    assert len(contracts) == 6 and len(calls) == 4  # not 6 x 4
    first, second = _tasks_of(contracts[0])[0], _tasks_of(contracts[1])[0]
    assert first == second and first is not second
    assert first["params"] is not second["params"]
    assert first["params"]["args"] is not second["params"]["args"]
    dumped = yaml.safe_dump(contracts[0])
    assert "&id" not in dumped and "*id" not in dumped  # shared lists would be anchors


def test_the_text_of_the_tasks_a_proposal_writes_is_bounded(tmp_path, monkeypatch):
    from fluid_build._errors import FluidUserError
    from fluid_build.cli.import_workflow import _proposal

    monkeypatch.chdir(tmp_path)
    _fan_project(tmp_path, outputs=4, tasks=3, args=["--day", "2026-01-01"])
    monkeypatch.setattr(_proposal, "MAX_TASK_TEXT", 1_000)  # 4 products x 3 tasks is more
    with pytest.raises(FluidUserError) as refused:
        propose(tmp_path, dags=str(tmp_path / "dags"))
    assert "characters of DAG tasks between them (the most is 1,000)" in refused.value.why
    assert "--split-by job" in refused.value.fix
    contracts, _report = propose(tmp_path, dags=str(tmp_path / "dags"), split_by="job")
    assert len(contracts) == 1 and len(_tasks_of(contracts[0])) == 3


def test_the_task_links_a_proposal_goes_through_are_counted_too(tmp_path, monkeypatch):
    from fluid_build._errors import FluidUserError
    from fluid_build.cli.import_workflow import _proposal

    monkeypatch.chdir(tmp_path)
    _fan_project(tmp_path, outputs=4, tasks=3)
    monkeypatch.setattr(_proposal, "MAX_LINK_VISITS", 5)  # 4 products x 3 links
    with pytest.raises(FluidUserError) as refused:
        propose(tmp_path, dags=str(tmp_path / "dags"))
    assert "go through 12 DAG task links between them (the most is 5)" in refused.value.why


def test_the_report_names_a_few_tasks_of_a_product_and_counts_the_rest(tmp_path, monkeypatch):
    from fluid_build.cli.import_workflow import _proposal

    monkeypatch.chdir(tmp_path)
    _fan_project(tmp_path, outputs=1, tasks=5)
    monkeypatch.setattr(_proposal, "MAX_TASK_LINES", 2)
    (contract,), report = propose(tmp_path, dags=str(tmp_path / "dags"))
    assert len(_tasks_of(contract)) == 5
    named = [x for x in report.mapped_one_to_one if x.startswith("DAG d1 task")]
    assert len(named) == 2
    assert [x for x in report.mapped_one_to_one if "3 more DAG task(s)" in x]


def test_a_script_name_is_looked_up_once_however_many_tasks_submit_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _fan_project(tmp_path, outputs=1, tasks=30)
    calls: List[str] = []
    real = _scan._match
    monkeypatch.setattr(
        _scan, "_match", lambda script, *a, **k: calls.append(script) or real(script, *a, **k)
    )
    (contract,), _report = propose(tmp_path, dags=str(tmp_path / "dags"))
    assert len(_tasks_of(contract)) == 30 and calls == ["jobs/job.py"]


def test_ten_thousand_products_that_share_a_name_are_numbered_in_linear_time():
    import time

    from fluid_build.cli.import_workflow._proposal import _Names, _unique

    taken = _Names()
    taken.add("a-2")  # a name the numbering would reach is skipped
    started = time.monotonic()
    names = [_unique("a", taken, "-") for _ in range(10_000)]
    assert time.monotonic() - started < 2.0
    assert len(set(names)) == 10_000 and names[0] == "a" and names[1] == "a-3"


def test_one_long_argument_is_not_read_to_its_end():
    import time

    started = time.monotonic()
    shown = safe_args(["a b " * 50_000, "--x"])
    assert time.monotonic() - started < 1.0
    assert len(shown[0]) <= 200 and shown[1] == "--x"


# ── the streams a build describes ─────────────────────────────────────────────────────


def _streams_project(root: Path, count: int) -> None:
    """One job with ``count`` streaming writes, each to a table of its own."""
    lines = "\n".join(
        f'spark.readStream.table("s.t{i}").writeStream.option("checkpointLocation", "s3://c/{i}")'
        f'.toTable("a.o{i}")'
        for i in range(count)
    )
    write(root, {"jobs/j.py": "spark = SparkSession.builder.getOrCreate()\n" + lines + "\n"})


def _build_description(contract: Dict[str, Any]) -> str:
    return contract["builds"][0]["description"]


def test_a_product_describes_the_streams_of_the_outputs_it_owns_not_the_jobs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _streams_project(tmp_path, 6)
    contracts, _ = propose(tmp_path)  # one product per table
    assert len(contracts) == 6
    for number, contract in enumerate(contracts):
        description = _build_description(contract)
        assert f"s3://c/{number}" in description
        assert description.count("checkpoint") == 1
    (whole,), _ = propose(tmp_path, split_by="job")
    assert _build_description(whole).count("checkpoint") == 6


def test_a_job_that_writes_many_streams_lists_some_and_counts_the_rest(tmp_path, monkeypatch):
    from fluid_build.cli.import_workflow import _proposal

    monkeypatch.chdir(tmp_path)
    _streams_project(tmp_path, 30)
    monkeypatch.setattr(_proposal, "MAX_STREAMS_LISTED", 4)
    (contract,), _ = propose(tmp_path, split_by="job")
    description = _build_description(contract)
    assert description.count("checkpoint") == 4
    assert "... and 26 more" in description
    for i in range(4):
        assert f"s3://c/{i}" in description
    assert "s3://c/29" not in description


def test_the_trigger_of_a_build_is_the_trigger_of_the_outputs_it_owns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(
        tmp_path,
        {
            "jobs/mixed.py": """
            spark = SparkSession.builder.getOrCreate()
            spark.readStream.table("s.a").writeStream.trigger(availableNow=True).toTable("a.once")
            spark.readStream.table("s.b").writeStream.trigger(processingTime="1 minute").toTable("a.live")
            spark.table("s.c").write.saveAsTable("a.batch")
            """
        },
    )
    contracts, _ = propose(tmp_path)
    kinds = {
        c["builds"][0]["outputs"][0]: c["builds"][0]["execution"]["trigger"]["type"]
        for c in contracts
    }
    assert kinds == {"once": "manual", "live": "continuous", "batch": "manual"}


def test_the_text_of_the_streams_a_proposal_writes_is_bounded(tmp_path, monkeypatch):
    from fluid_build._errors import FluidUserError
    from fluid_build.cli.import_workflow import _proposal

    monkeypatch.chdir(tmp_path)
    _streams_project(tmp_path, 8)
    monkeypatch.setattr(_proposal, "MAX_STREAM_TEXT", 100)  # 8 products x ~25 characters is more
    with pytest.raises(FluidUserError) as refused:
        propose(tmp_path)
    assert "characters of Structured Streaming writes between them (the most is 100)" in (
        refused.value.why
    )
    assert "--split-by job" in refused.value.fix


def test_streams_are_not_described_again_for_every_product_of_a_job(tmp_path, monkeypatch):
    from fluid_build.cli.import_workflow import _proposal

    monkeypatch.chdir(tmp_path)
    calls: List[int] = []
    real = _proposal.safe_text
    monkeypatch.setattr(_proposal, "safe_text", lambda *a, **k: calls.append(1) or real(*a, **k))

    def count(streams: int) -> int:
        _streams_project(tmp_path, streams)
        calls.clear()
        contracts, _ = propose(tmp_path)
        assert len(contracts) == streams
        return len(calls)

    small, large = count(40), count(160)
    # four times the streams (and the products): about four times the work, not sixteen
    assert large < small * 6, (small, large)
