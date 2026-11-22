# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A task that runs jobs by name: ``driver.py --job-list silver_orders``.

The conventions' ``drivers`` say where the job lists and the job files are. A task whose script is
a driver is joined to every job file the list it is given names, and nothing about a name or a
path is trusted before it has been checked.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.cli.import_workflow import _conventions as C
from fluid_build.cli.import_workflow import _drivers as D
from fluid_build.cli.import_workflow import _pysource
from fluid_build.cli.import_workflow.pyspark import PySparkImporter

DRIVER = {
    "script": "driver.py",
    "argument": "--job-list",
    "job_lists": {"dir": "pkg/job_lists", "file": "{value}.yaml", "key": "jobs"},
    "jobs": {"dir": "pkg/jobs", "file": "{job}.py"},
}


def spark_job(table: str) -> str:
    return textwrap.dedent(
        f"""
        from pyspark.sql import SparkSession

        spark = SparkSession.builder.getOrCreate()
        spark.table("raw.{table}").write.saveAsTable("silver.{table}")
        """
    ).lstrip("\n")


def dag_source(args: str = '["--job-list", "silver", "--run-date", "{{ ds }}"]') -> str:
    return textwrap.dedent(
        f"""
        from airflow import DAG
        from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator

        with DAG(dag_id="load", schedule="@daily", default_args={{"owner": "orders-team"}}) as dag:
            SparkSubmitOperator(
                task_id="run",
                application="s3://code/pkg/driver.py",
                application_args={args},
            )
        """
    ).lstrip("\n")


def conventions(**driver: Any) -> str:
    import yaml

    body = {**DRIVER, **driver}
    return yaml.safe_dump(
        {
            "drivers": [body],
            "defaults": {"domain": "retail", "platform": "aws"},
            "owners": {"orders-team": {"team": "orders"}},
        }
    )


def project(root: Path, files: Optional[Dict[str, str]] = None) -> None:
    base = {
        "pkg/job_lists/silver.yaml": "jobs:\n  - customers\n  - orders\n",
        "pkg/jobs/customers.py": spark_job("customers"),
        "pkg/jobs/orders.py": spark_job("orders"),
        "pkg/driver.py": "from pkg.cli import main\n",
        "dags/load.py": dag_source(),
        "conventions.yaml": conventions(),
    }
    for rel, text in {**base, **(files or {})}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def run(root: Path, monkeypatch, **options: Any):
    monkeypatch.chdir(root)
    merged = {"dags": "dags", "conventions": "conventions.yaml", **options}
    return PySparkImporter().import_to_contracts("pkg", options=merged)


def dag_ids(contracts) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for contract in contracts:
        airflow = (contract["builds"][0].get("execution") or {}).get("orchestration", {})
        out[contract["id"]] = (airflow.get("airflow") or {}).get("dagId")
    return out


# ── the conventions ───────────────────────────────────────────────────


def parse(text: str, **kwargs) -> C.Conventions:
    return C.parse_conventions(__import__("yaml").safe_load(textwrap.dedent(text)), **kwargs)


def test_a_driver_is_read_with_its_directories_inside_the_conventions_folder(tmp_path):
    (tmp_path / "pkg").mkdir()
    path = tmp_path / "conventions.yaml"
    path.write_text(conventions(), encoding="utf-8")
    (driver,) = C.load_conventions(path).drivers
    root = tmp_path.resolve()
    assert driver == C.Driver(
        script="driver.py",
        argument="--job-list",
        lists_dir=root / "pkg" / "job_lists",
        list_file="{value}.yaml",
        list_key="jobs",
        jobs_dir=root / "pkg" / "jobs",
        job_file="{job}.py",
    )


def test_the_key_is_optional_and_the_argument_may_be_a_position():
    body = {**DRIVER, "argument": 0, "job_lists": {"dir": "l", "file": "{value}.yml"}}
    import yaml

    (driver,) = parse(yaml.safe_dump({"drivers": [body]})).drivers
    assert driver.argument == 0 and driver.list_key is None


@pytest.mark.parametrize(
    "change, expect",
    [
        ({"script": "../driver.py"}, "has a '..'"),
        ({"script": "/opt/driver.py"}, "not an acceptable name"),
        ({"script": "a b.py"}, "not an acceptable name"),
        ({"script": "pkg/../driver.py"}, "has a '..'"),
        ({"argument": "job-list"}, "not a flag"),
        ({"argument": "--"}, "not a flag"),
        ({"argument": True}, "non-empty string"),
        ({"argument": -1}, "0 or more"),
        ({"argument": ""}, "non-empty string"),
        ({"job_lists": {"dir": "l", "file": "list.yaml"}}, "not a file name with {value}"),
        ({"job_lists": {"dir": "l", "file": "{job}.yaml"}}, "not a file name with {value}"),
        ({"job_lists": {"dir": "l", "file": "a/{value}.yaml"}}, "not a file name"),
        ({"job_lists": {"dir": "l", "file": "{value}..yaml"}}, "not a file name"),
        ({"job_lists": {"dir": "l", "file": "{value}{value}"}}, "not a file name"),
        ({"job_lists": {"dir": "l", "file": "{value}", "key": "a b"}}, "not an acceptable name"),
        ({"job_lists": {"file": "{value}.yaml"}}, "give dir and file"),
        ({"job_lists": {"dir": "l", "file": "{value}.yaml", "extra": 1}}, "unknown key"),
        ({"jobs": {"dir": "j", "file": "{value}.py"}}, "not a file name with {job}"),
        ({"jobs": {"dir": "j"}}, "give dir and file"),
        ({"jobs": {"dir": "../j", "file": "{job}.py"}}, "leaves the directory"),
        ({"job_lists": {"dir": "/etc", "file": "{value}.yaml"}}, "leaves the directory"),
        ({"extra": 1}, "unknown key"),
    ],
)
def test_a_driver_that_is_wrong_is_refused_with_the_key_named(change, expect):
    import yaml

    body = {**DRIVER, **change}
    with pytest.raises(C.ConventionsError) as refused:
        parse(yaml.safe_dump({"drivers": [body]}), source=Path("/tmp/x/conventions.yaml"))
    assert expect in str(refused.value), str(refused.value)


def test_a_driver_must_give_every_key():
    import yaml

    for key in ("script", "argument", "job_lists", "jobs"):
        body = {k: v for k, v in DRIVER.items() if k != key}
        with pytest.raises(C.ConventionsError) as refused:
            parse(yaml.safe_dump({"drivers": [body]}))
        assert f"give {key}" in str(refused.value)


# ── the reader ────────────────────────────────────────────────────────


def driver_of(tmp_path: Path, **change: Any) -> C.Driver:
    (tmp_path / "conventions.yaml").write_text(conventions(**change), encoding="utf-8")
    return C.load_conventions(tmp_path / "conventions.yaml").drivers[0]


@pytest.mark.parametrize(
    "script, script_of_driver, found",
    [
        ("s3://code/pkg/driver.py", "driver.py", True),
        ("driver.py", "driver.py", True),
        ("/opt/spark/driver.py", "driver.py", True),
        ("s3://code/pkg/driver.py", "pkg/driver.py", True),
        ("s3://code/other/driver.py", "pkg/driver.py", False),
        ("s3://code/pkg/mydriver.py", "driver.py", False),  # whole segments
        ("s3://code/pkg/Driver.py", "driver.py", False),
        ("s3://code/pkg/driver.py?x=1", "driver.py", True),
        ("s3://code/pkg", "pkg/driver.py", False),
        ("", "driver.py", False),
    ],
)
def test_the_driver_of_a_script_is_found_by_its_trailing_segments(
    tmp_path, script, script_of_driver, found
):
    driver = driver_of(tmp_path, script=script_of_driver)
    assert (D.driver_for(script, [driver]) is driver) is found


@pytest.mark.parametrize(
    "argument, args, expected",
    [
        ("--job-list", ["--job-list", "silver"], "silver"),
        ("--job-list", ["--run-date", "d", "--job-list", "silver"], "silver"),
        ("--job-list", ["--job-list=silver"], "silver"),
        ("-j", ["-j", "silver"], "silver"),
        (0, ["silver", "--x"], "silver"),
        (1, ["--x", "silver"], "silver"),
    ],
)
def test_the_argument_that_names_the_list(tmp_path, argument, args, expected):
    assert D.argument_value(driver_of(tmp_path, argument=argument), args) == expected


@pytest.mark.parametrize(
    "argument, args, reason",
    [
        ("--job-list", [], "no --job-list"),
        ("--job-list", ["--job-list"], "with no value"),
        ("--job-list", ["--job-list", "{{ params.list }}"], "not a constant"),
        ("--job-list", ["--job-list", "<not constant>"], "not a constant"),
        (3, ["a"], "no argument 3"),
    ],
)
def test_an_argument_that_is_missing_or_not_a_constant_says_so(tmp_path, argument, args, reason):
    value = D.argument_value(driver_of(tmp_path, argument=argument), args)
    assert isinstance(value, tuple) and reason in value[0], value


def lists(tmp_path: Path, files: Dict[str, str], **change: Any):
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return D.Lists(), driver_of(tmp_path, **change)


def test_a_list_holds_the_names_of_its_jobs(tmp_path):
    reader, driver = lists(tmp_path, {"pkg/job_lists/a.yaml": "jobs: [x, y_1, z.2, w-3]\n"})
    assert reader.names(driver, "a") == ["x", "y_1", "z.2", "w-3"]


def test_without_a_key_the_file_is_the_list(tmp_path):
    reader, driver = lists(
        tmp_path,
        {"pkg/job_lists/a.yaml": "- x\n- y\n"},
        job_lists={"dir": "pkg/job_lists", "file": "{value}.yaml"},
    )
    assert reader.names(driver, "a") == ["x", "y"]


@pytest.mark.parametrize(
    "text, reason",
    [
        ("other: [x]\n", "has no key jobs"),
        ("jobs: x\n", "not a list"),
        ("jobs: {a: b}\n", "not a list"),
        ("jobs: [x, 1]\n", "not a plain name"),
        ("jobs: [x, null]\n", "not a plain name"),
        ("jobs: [x, ../y]\n", "not a plain name"),
        ("jobs: [x, a/b]\n", "not a plain name"),
        ("jobs: ['x y']\n", "not a plain name"),
        ("jobs: [x, '']\n", "not a plain name"),
        ("jobs: [x, '..']\n", "not a plain name"),
        ("jobs: [x, 'a..b']\n", "not a plain name"),
        ("jobs: [x, [y]]\n", "not a plain name"),
        ("- not a mapping\n", "has no key jobs"),
        ("jobs: [x\n", "not valid YAML"),
        ("a: &a [x]\nb: *a\n", "has no key jobs"),
        ("!!python/object/apply:os.system [x]", "not valid YAML"),
    ],
)
def test_a_list_that_is_not_names_is_refused_with_why(tmp_path, text, reason):
    reader, driver = lists(tmp_path, {"pkg/job_lists/a.yaml": text})
    assert reason in reader.names(driver, "a")


def test_a_list_that_is_too_long_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "MAX_JOBS_PER_LIST", 3)
    reader, driver = lists(tmp_path, {"pkg/job_lists/a.yaml": "jobs: [a, b, c, d]\n"})
    assert "more than 3 jobs" in reader.names(driver, "a")


@pytest.mark.parametrize("value", ["../x", "a/b", "", "a b", ".", "..", "a..b", "x\n", "é"])
def test_a_value_that_is_not_a_plain_name_is_never_made_into_a_path(tmp_path, value):
    reader, driver = lists(tmp_path, {"pkg/job_lists/a.yaml": "jobs: [x]\n"})
    assert "not a plain name" in reader.names(driver, value)


def test_a_missing_list_and_a_list_that_is_not_a_file_say_so(tmp_path):
    reader, driver = lists(tmp_path, {"pkg/job_lists/dir.yaml/keep": "x"})
    assert "there is no job list nope.yaml" in reader.names(driver, "nope")
    assert "there is no job list dir.yaml" in reader.names(driver, "dir")


def test_a_list_that_is_a_link_out_of_its_directory_is_not_read(tmp_path):
    reader, driver = lists(tmp_path, {"outside.yaml": "jobs: [x]\n", "pkg/job_lists/keep": ""})
    (tmp_path / "pkg" / "job_lists" / "a.yaml").symlink_to(tmp_path / "outside.yaml")
    assert "not inside the directory" in reader.names(driver, "a")


def test_a_list_that_is_a_link_inside_its_directory_is_read(tmp_path):
    reader, driver = lists(tmp_path, {"pkg/job_lists/real.yaml": "jobs: [x]\n"})
    (tmp_path / "pkg" / "job_lists" / "a.yaml").symlink_to(tmp_path / "pkg/job_lists/real.yaml")
    assert reader.names(driver, "a") == ["x"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes here")
def test_a_list_that_is_a_pipe_is_not_waited_for(tmp_path):
    reader, driver = lists(tmp_path, {"pkg/job_lists/keep": ""})
    os.mkfifo(tmp_path / "pkg" / "job_lists" / "a.yaml")
    assert "there is no job list" in reader.names(driver, "a")  # not a regular file


def test_a_list_over_the_size_limit_is_not_read(tmp_path):
    big = "jobs: [" + "x, " * 12_000 + "y]\n"  # 36 KB
    assert len(big) > D.MAX_LIST_BYTES
    reader, driver = lists(tmp_path, {"pkg/job_lists/a.yaml": big})
    assert "is larger than 16 KB" in reader.names(driver, "a")


def test_the_lists_of_one_scan_are_bounded_in_bytes_too(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "MAX_LIST_BYTES_TOTAL", 40)
    reader, driver = lists(
        tmp_path, {f"pkg/job_lists/{n}.yaml": "jobs: [x, y, z]\n" for n in ("a", "b", "c")}
    )  # 16 bytes each
    assert reader.names(driver, "a") == ["x", "y", "z"]
    assert reader.names(driver, "b") == ["x", "y", "z"]
    assert "would pass it" in reader.names(driver, "c")  # 48 bytes would pass the 40


def test_a_list_that_would_pass_the_total_is_refused_before_it_is_read(tmp_path, monkeypatch):
    from fluid_build.util import safe_yaml

    monkeypatch.setattr(D, "MAX_LIST_BYTES_TOTAL", 10)
    reader, driver = lists(tmp_path, {"pkg/job_lists/a.yaml": "jobs: [x, y, z]\n"})
    reads = []
    original = safe_yaml.read_bounded_file
    monkeypatch.setattr(
        safe_yaml, "read_bounded_file", lambda p, n: reads.append(p) or original(p, n)
    )
    assert "would pass it" in reader.names(driver, "a")
    assert reads == []


def test_a_list_is_counted_against_the_budget_of_the_scan(tmp_path):
    reader, driver = lists(tmp_path, {"pkg/job_lists/a.yaml": "jobs: [x]\n"})
    reader.budget = _pysource.SourceBudget(left=5)
    assert "was not read" in reader.names(driver, "a")
    assert reader.budget.skipped == 1


def test_a_list_is_read_once_however_many_tasks_name_it(tmp_path, monkeypatch):
    from fluid_build.util import safe_yaml

    reader, driver = lists(tmp_path, {"pkg/job_lists/a.yaml": "jobs: [x]\n"})
    reads: List[Path] = []
    original = safe_yaml.read_bounded_file
    monkeypatch.setattr(
        safe_yaml, "read_bounded_file", lambda p, n: reads.append(p) or original(p, n)
    )
    for _ in range(5):
        assert reader.names(driver, "a") == ["x"]
    assert len(reads) == 1


def test_the_lists_one_scan_reads_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "MAX_LISTS", 2)
    reader, driver = lists(
        tmp_path, {f"pkg/job_lists/{n}.yaml": "jobs: [x]\n" for n in ("a", "b", "c")}
    )
    assert reader.names(driver, "a") == ["x"] and reader.names(driver, "b") == ["x"]
    assert "at most 2 job lists" in reader.names(driver, "c")


def test_a_job_file_is_where_the_template_puts_it_and_nothing_is_looked_at(tmp_path):
    reader, driver = lists(tmp_path, {})
    assert D.job_file(driver, "x") == driver.jobs_dir / "x.py"
    assert D.job_file(driver, "missing") == driver.jobs_dir / "missing.py"


def test_a_job_that_is_a_link_to_a_file_elsewhere_or_a_directory_is_not_joined(
    tmp_path, monkeypatch
):
    project(
        tmp_path,
        {
            "pkg/job_lists/silver.yaml": "jobs: [customers, linked, dir, missing]\n",
            "elsewhere/other.py": spark_job("other"),
        },
    )
    (tmp_path / "pkg" / "jobs" / "linked.py").symlink_to(tmp_path / "elsewhere" / "other.py")
    (tmp_path / "pkg" / "jobs" / "dir.py").mkdir()
    contracts, report = run(tmp_path, monkeypatch)
    assert dag_ids(contracts) == {"silver.customers": "load", "silver.orders": None}
    assert any("names 3 job(s) with no job file" in note for note in report.notes), report.notes


# ── the importer ──────────────────────────────────────────────────────


def test_a_task_that_runs_a_driver_is_joined_to_every_job_of_its_list(tmp_path, monkeypatch):
    project(tmp_path)
    contracts, report = run(tmp_path, monkeypatch)
    assert dag_ids(contracts) == {"silver.customers": "load", "silver.orders": "load"}
    assert any("2 job file(s) run through a driver" in note for note in report.notes)
    assert any("(1 by driver)" in note for note in report.notes), report.notes
    assert any(note.endswith("; 0 not joined") for note in report.notes), report.notes
    owner = {c["id"]: c["metadata"]["owner"] for c in contracts}
    assert owner == {"silver.customers": {"team": "orders"}, "silver.orders": {"team": "orders"}}


def test_the_argument_may_be_written_with_an_equals_sign_or_come_from_a_constant(
    tmp_path, monkeypatch
):
    project(
        tmp_path,
        {
            "dags/load.py": dag_source('["--job-list=" + LIST]').replace(
                "with DAG", 'LIST = "silver"\nwith DAG'
            )
        },
    )
    contracts, _report = run(tmp_path, monkeypatch)
    assert set(dag_ids(contracts).values()) == {"load"}


def test_the_products_of_a_driver_split_by_task_are_one_product(tmp_path, monkeypatch):
    project(tmp_path)
    contracts, _report = run(tmp_path, monkeypatch, split_by="task")
    (contract,) = contracts
    assert contract["id"] == "retail.load.run"
    assert [b["id"] for b in contract["builds"]] == ["customers", "orders"]
    assert [e["exposeId"] for e in contract["exposes"]] == ["customers", "orders"]
    airflow = contract["builds"][0]["execution"]["orchestration"]["airflow"]
    assert airflow["dagId"] == "load"
    assert airflow["tasks"][0]["params"]["args"][:2] == ["--job-list", "silver"]


@pytest.mark.parametrize(
    "files, reason",
    [
        ({"dags/load.py": dag_source("[]")}, "passes no --job-list"),
        ({"dags/load.py": dag_source('["--job-list"]')}, "with no value"),
        ({"dags/load.py": dag_source('["--job-list", "{{ params.list }}"]')}, "not a constant"),
        ({"dags/load.py": dag_source('["--job-list", "../secret"]')}, "not a plain name"),
        ({"dags/load.py": dag_source('["--job-list", "nope"]')}, "there is no job list nope.yaml"),
        ({"pkg/job_lists/silver.yaml": "jobs: [ghost]\n"}, "no job of the list silver"),
        ({"pkg/job_lists/silver.yaml": "jobs: [../x]\n"}, "not a plain name"),
    ],
)
def test_a_task_whose_driver_cannot_be_read_is_not_joined_and_says_why(
    tmp_path, monkeypatch, files, reason
):
    project(tmp_path, files)
    contracts, report = run(tmp_path, monkeypatch)
    assert set(dag_ids(contracts).values()) == {None}
    assert any(reason in line for line in report.notes), report.notes


def test_a_job_the_list_names_that_has_no_file_is_said_and_the_others_are_joined(
    tmp_path, monkeypatch
):
    project(tmp_path, {"pkg/job_lists/silver.yaml": "jobs: [customers, ghost, orders, ghost]\n"})
    contracts, report = run(tmp_path, monkeypatch)
    assert dag_ids(contracts) == {"silver.customers": "load", "silver.orders": "load"}
    assert any("names 1 job(s) with no job file" in n and "ghost" in n for n in report.notes)


def test_a_list_outside_the_directory_the_conventions_name_is_not_read(tmp_path, monkeypatch):
    project(tmp_path)
    secret = tmp_path / "secret.yaml"
    secret.write_text("jobs: [customers]\n", encoding="utf-8")
    link = tmp_path / "pkg" / "job_lists" / "evil.yaml"
    link.symlink_to(secret)
    project(tmp_path, {"dags/load.py": dag_source('["--job-list", "evil"]')})
    contracts, report = run(tmp_path, monkeypatch)
    assert set(dag_ids(contracts).values()) == {None}
    assert any("not inside the directory" in line for line in report.notes), report.notes


def test_a_script_that_is_no_driver_is_matched_as_before(tmp_path, monkeypatch):
    project(
        tmp_path,
        {
            "dags/load.py": dag_source("[]").replace("pkg/driver.py", "pkg/jobs/orders.py"),
        },
    )
    contracts, _report = run(tmp_path, monkeypatch)
    assert dag_ids(contracts) == {"silver.customers": None, "silver.orders": "load"}


def two_tasks(args_a: str, args_b: str = '["--job-list", "silver"]') -> str:
    return textwrap.dedent(
        f"""
        from airflow import DAG
        from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator

        with DAG(dag_id="load", schedule="@daily", default_args={{"owner": "orders-team"}}) as dag:
            first = SparkSubmitOperator(
                task_id="first", application="s3://code/pkg/driver.py", application_args={args_a})
            second = SparkSubmitOperator(
                task_id="second", application="s3://code/pkg/driver.py", application_args={args_b})
            first >> second
        """
    ).lstrip("\n")


def test_two_tasks_that_name_one_list_say_a_missing_job_once(tmp_path, monkeypatch):
    project(
        tmp_path,
        {
            "pkg/job_lists/silver.yaml": "jobs: [customers, ghost]\n",
            # other arguments, the same list: the second is no new reading of it
            "dags/load.py": two_tasks(
                '["--job-list", "silver", "--run-date", "a"]',
                '["--job-list", "silver", "--run-date", "b"]',
            ),
        },
    )
    _contracts, report = run(tmp_path, monkeypatch)
    assert len([n for n in report.notes if "names 1 job(s) with no job file" in n]) == 1


def test_a_driver_with_the_same_arguments_is_read_once_however_many_tasks_run_it(
    tmp_path, monkeypatch
):
    from fluid_build.cli.import_workflow import _scan

    calls: List[str] = []
    original = _scan.job_file
    monkeypatch.setattr(_scan, "job_file", lambda d, name: calls.append(name) or original(d, name))
    project(tmp_path, {"dags/load.py": two_tasks('["--job-list", "silver"]')})
    run(tmp_path, monkeypatch)
    assert calls == ["customers", "orders"]  # the second task's answer is the first one's


def test_a_job_that_more_tasks_run_than_are_recorded_is_counted(tmp_path, monkeypatch):
    from fluid_build.cli.import_workflow import _scan

    monkeypatch.setattr(_scan, "MAX_TASKS_PER_JOB", 1)
    project(tmp_path, {"dags/load.py": two_tasks('["--job-list", "silver"]')})
    contracts, report = run(tmp_path, monkeypatch)
    assert any("2 more task(s) submit" in n or "1 more task(s) submit" in n for n in report.notes)
    for contract in contracts:
        assert len(contract["builds"][0]["execution"]["orchestration"]["airflow"]["tasks"]) == 1


def test_a_task_whose_id_is_not_a_constant_falls_back_to_its_job_in_a_task_split(
    tmp_path, monkeypatch
):
    source = dag_source().replace('task_id="run"', "task_id=unknown_name")
    project(tmp_path, {"dags/load.py": source})
    contracts, _report = run(tmp_path, monkeypatch, split_by="task")
    assert sorted(c["id"] for c in contracts) == ["retail.customers", "retail.orders"]


def test_a_build_depends_on_every_build_the_task_before_it_ran(tmp_path, monkeypatch):
    project(
        tmp_path,
        {
            "pkg/jobs/report.py": spark_job("report"),
            "dags/load.py": two_tasks('["--job-list", "silver"]', '["--job-list", "report"]'),
            "pkg/job_lists/report.yaml": "jobs: [report]\n",
        },
    )
    contracts, _report = run(tmp_path, monkeypatch, split_by="dag")
    (contract,) = contracts
    builds = {b["id"]: b for b in contract["builds"]}
    assert builds["report"]["dependencies"] == [
        {"buildId": "customers", "condition": "success"},
        {"buildId": "orders", "condition": "success"},
    ]
    assert "dependencies" not in builds["customers"]


# ── what a review of the first version found ─────────────────────────────────


def test_two_drivers_that_share_a_directory_do_not_share_their_lists(tmp_path):
    reader, first = lists(
        tmp_path, {"pkg/job_lists/silver.yaml": "jobs: [customers]\nsteps: [orders]\n"}
    )
    second = driver_of(
        tmp_path, script="other.py", job_lists={**DRIVER["job_lists"], "key": "steps"}
    )
    assert reader.names(first, "silver") == ["customers"]
    assert reader.names(second, "silver") == ["orders"]
    third = driver_of(
        tmp_path, script="x.py", job_lists={**DRIVER["job_lists"], "file": "{value}.yml"}
    )
    assert "there is no job list silver.yml" in reader.names(third, "silver")


def test_a_name_that_is_not_plain_does_not_use_up_a_list_slot(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "MAX_LISTS", 2)
    reader, driver = lists(tmp_path, {"pkg/job_lists/a.yaml": "jobs: [x]\n"})
    for index in range(10):
        assert "is not a plain name" in reader.names(driver, f"../x{index}")
    assert reader.names(driver, "a") == ["x"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can enter any directory")
def test_a_directory_that_cannot_be_entered_leaves_the_task_not_joined(tmp_path):
    reader, driver = lists(tmp_path, {"pkg/job_lists/a.yaml": "jobs: [x]\n"})
    locked = tmp_path / "pkg" / "job_lists"
    locked.chmod(0)
    try:
        assert "could not be" in reader.names(driver, "a") or "no job list" in reader.names(
            driver, "a"
        )
    finally:
        locked.chmod(0o755)


def test_a_file_name_the_system_refuses_leaves_the_task_not_joined(tmp_path):
    long_prefix = "p" * 250
    reader, driver = lists(
        tmp_path,
        {},
        job_lists={**DRIVER["job_lists"], "file": long_prefix + "{value}.yaml"},
    )
    got = reader.names(driver, "a" * 100)
    assert isinstance(got, str) and ("could not be looked at" in got or "no job list" in got)


def test_a_nul_in_a_path_of_the_conventions_is_refused_with_the_key_named(tmp_path):
    with pytest.raises(C.ConventionsError) as refused:
        parse(
            """
            drivers:
              - script: driver.py
                argument: --job-list
                job_lists: {dir: "pkg\\0x", file: "{value}.yaml"}
                jobs: {dir: pkg/jobs, file: "{job}.py"}
            """,
            source=tmp_path / "c.yaml",
        )
    assert "drivers[0].job_lists.dir" in str(refused.value)


@pytest.mark.parametrize(
    "text, key",
    [
        ("external_consumers: [a.b]\nexternal_consumers: [c.d]\n", "external_consumers"),
        ("environment: prod\nenvironment: dev\n", "environment"),
        ("airflow_variables:\n  prod:\n    a: '1'\n    a: '2'\n", "'a'"),
        (
            "drivers:\n  - script: driver.py\n    script: other.py\n    argument: --j\n",
            "'script'",
        ),
        ('layers:\n  "<<": gold\n  "<<": silver\n', "'<<'"),
        ("env: {a: 1, a: 2}\n", "'a'"),
    ],
)
def test_a_key_given_twice_is_refused_with_the_key_and_the_line(tmp_path, text, key):
    path = tmp_path / "c.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(Exception) as refused:
        C.load_conventions(path)
    why = getattr(refused.value, "why", str(refused.value))
    assert "given twice" in why and key in why and "(line " in why


def test_a_list_that_gives_a_key_twice_is_not_read(tmp_path):
    reader, driver = lists(tmp_path, {"pkg/job_lists/a.yaml": "jobs: [x]\njobs: [y]\n"})
    assert "gives a key twice" in reader.names(driver, "a")


def test_a_directory_is_not_inside_itself_and_a_file_below_it_is(tmp_path):
    assert not D._inside(tmp_path, tmp_path)
    assert D._inside(tmp_path / "a" / "b.yaml", tmp_path)
    assert not D._inside(tmp_path.parent / "b.yaml", tmp_path)


def load_unique(text):
    from fluid_build.util.safe_yaml import load_yaml_safe

    return load_yaml_safe(text, loader=C.unique_key_loader())


def test_a_merge_key_and_a_repeated_key_in_two_mappings_are_not_duplicates():
    assert load_unique("base: &b {a: 1}\nx: {<<: *b, c: 2}\ny: {c: 3}\n")["x"] == {"a": 1, "c": 2}
    two_merges = "base: &b {a: 1}\nc: &c {d: 2}\ny: {<<: *b, <<: *c}\n"
    assert load_unique(two_merges)["y"] == {"a": 1, "d": 2}  # a merge key may come twice
    assert load_unique("1: a\n'1': b\n") == {1: "a", "1": "b"}  # a number and a text are two keys
    assert load_unique("a: 1\nb: {a: 1}\nc: [{a: 1}, {a: 2}]\n")["c"] == [{"a": 1}, {"a": 2}]


def test_a_key_given_twice_in_a_nested_mapping_names_the_key_and_the_line():
    with pytest.raises(C.unique_key_loader().DuplicateKey) as refused:
        load_unique("a: 1\nb: [{a: 1, a: 2}]\n")
    assert C.duplicate_key_text(refused.value) == "the key 'a' is given twice (line 2)"


def test_true_and_the_same_key_written_twice_are_one_key():
    for text in ("true: 1\nTrue: 2\n", "1: a\n01: b\n", 'a: 1\n"a": 2\n'):
        with pytest.raises(C.unique_key_loader().DuplicateKey):
            load_unique(text)


def test_a_long_key_does_not_make_a_long_refusal():
    with pytest.raises(C.ConventionsError) as refused:
        C.parse_conventions({"layers": {"k" * 100_000: "purple"}})
    assert len(str(refused.value)) <= 400 and "expected Bronze, Silver or Gold" in str(
        refused.value
    )


def test_a_control_character_in_a_text_value_of_the_conventions_is_refused():
    for value in ("t\\e[31mx", "a\\nb", "x\\u202ey"):
        with pytest.raises(C.ConventionsError) as refused:
            C.parse_conventions(
                __import__("yaml").safe_load(f'owners: {{orders-team: {{team: "{value}"}}}}')
            )
        assert "control or invisible" in str(refused.value) and "\x1b" not in str(refused.value)


def test_an_escape_sequence_in_a_key_does_not_reach_the_refusal(tmp_path):
    for text in ('"x\\e[2Jy": 1', 'drivers:\n  - "x\\e[31mRED": 1'):
        with pytest.raises(C.ConventionsError) as refused:
            C.parse_conventions(__import__("yaml").safe_load(text))
        assert "\x1b" not in str(refused.value) and "unknown key" in str(refused.value)
