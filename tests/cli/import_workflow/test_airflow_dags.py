# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""What an Airflow DAG file says, read statically: never imported, never run."""

from __future__ import annotations

import sys
import textwrap
from typing import Dict, List, Optional, Tuple

import pytest

from fluid_build.cli.import_workflow._airflow_dags import Dag, ModuleDags, read_dags
from fluid_build.cli.import_workflow._fold import Folder
from fluid_build.cli.import_workflow._pysource import parse_module


def dags(source: str, *, operators: Optional[Dict[str, str]] = None) -> ModuleDags:
    module = parse_module("dag.py", textwrap.dedent(source).encode("utf-8"))
    return read_dags(module, Folder(module), operators=operators)


def one(source: str, **kw) -> Dag:
    found = dags(source, **kw)
    assert len(found.dags) == 1, found
    return found.dags[0]


def edges(dag: Dag) -> List[Tuple[str, str]]:
    ids = {t.key: t.task_id for t in dag.tasks}
    return sorted((ids[a], ids[b]) for a, b in dag.edges)  # type: ignore[type-var]


CLASSIC = """
from datetime import datetime, timedelta
from airflow import DAG
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
from airflow.operators.bash import BashOperator

default_args = {"owner": "retail-data", "retries": 2}
JOBS = "/opt/airflow/jobs"

with DAG(
    dag_id="orders_daily",
    schedule="0 2 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    default_args=default_args,
    tags=["retail", "daily"],
    description="Daily orders",
) as dag:
    extract = SparkSubmitOperator(
        task_id="extract",
        application=f"{JOBS}/extract_orders.py",
        application_args=["--date", "{{ ds }}"],
    )
    load = SparkSubmitOperator(task_id="load", application=f"{JOBS}/load_orders.py", owner="alice")
    notify = BashOperator(task_id="notify", bash_command="echo done")
    extract >> load >> notify
"""


def test_a_classic_dag_with_spark_submit_tasks():
    dag = one(CLASSIC)
    assert (dag.dag_id, dag.schedule_kind, dag.schedule) == ("orders_daily", "cron", "0 2 * * *")
    assert dag.owner == "retail-data" and dag.tags == ("retail", "daily")
    assert dag.catchup is False and dag.description == "Daily orders"
    assert dag.start_date == "datetime(2026, 1, 1)"
    assert [(t.task_id, t.operator, t.kind, t.owner) for t in dag.tasks] == [
        ("extract", "SparkSubmitOperator", "spark_submit", None),
        ("load", "SparkSubmitOperator", "spark_submit", "alice"),
        ("notify", "BashOperator", "bash", None),
    ]
    extract, load, notify = dag.tasks
    (submit,) = extract.submits
    assert (submit.runtime, submit.script) == (
        "spark-submit",
        "/opt/airflow/jobs/extract_orders.py",
    )
    assert submit.args == ("--date", "{{ ds }}")
    assert (
        load.submits[0].script == "/opt/airflow/jobs/load_orders.py" and load.submits[0].args == ()
    )
    assert notify.submits == []
    assert edges(dag) == [("extract", "load"), ("load", "notify")]
    assert not dag.notes


def test_airflow_3_spellings_and_the_dag_decorator():
    found = dags(
        """
        from airflow.sdk import dag, task
        from airflow.providers.standard.operators.bash import BashOperator

        @dag(schedule=None, tags=["x"], default_args={"owner": "o"})
        def etl_flow():
            submit = BashOperator(
                task_id="submit",
                bash_command="spark-submit --master yarn --conf a=b /jobs/etl.py --mode full",
            )

            @task
            def check():
                return 1

            submit >> check()

        etl_flow()
        """
    )
    (dag,) = found.dags
    assert (dag.dag_id, dag.schedule_kind, dag.schedule, dag.owner) == (
        "etl_flow",
        "none",
        None,
        "o",
    )
    assert [(t.task_id, t.kind) for t in dag.tasks] == [("submit", "bash"), ("check", "python")]
    (submit,) = dag.tasks[0].submits
    assert (submit.runtime, submit.script, submit.args) == (
        "bash",
        "/jobs/etl.py",
        ("--mode", "full"),
    )
    assert edges(dag) == [("submit", "check")]
    assert not dag.notes


def test_a_dag_decorator_function_never_called_is_not_a_dag_airflow_creates():
    (dag,) = dags("from airflow.decorators import dag\n@dag\ndef never():\n    pass\n").dags
    assert dag.dag_id == "never"
    assert any("never called at module level" in n for n in dag.notes)


def test_an_aliased_import_is_still_seen():
    dag = one(
        """
        from airflow import DAG as AirflowDag
        from airflow.providers.apache.spark.operators import spark_submit as ss
        from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator as Go

        with AirflowDag("aliased") as d:
            Go(task_id="a", application="/j/a.py")
            ss.SparkSubmitOperator(task_id="b", application="/j/b.py")
        """
    )
    assert [(t.task_id, t.submits[0].script) for t in dag.tasks] == [
        ("a", "/j/a.py"),
        ("b", "/j/b.py"),
    ]


def test_a_class_that_only_has_an_airflow_looking_name_is_not_an_operator():
    dag = one(
        """
        from airflow import DAG
        from acme.ops import SparkSubmitOperator
        with DAG("d"):
            SparkSubmitOperator(task_id="mine", application="/j/a.py")
        """
    )
    assert dag.tasks == []


def test_the_conventions_can_map_a_project_operator_to_a_known_kind():
    source = """
        from airflow import DAG
        from acme.ops import AcmeSparkOperator
        with DAG("d"):
            AcmeSparkOperator(task_id="mine", application="/j/a.py", application_args=["x"])
    """
    dag = one(source, operators={"AcmeSparkOperator": "spark_submit"})
    assert [(t.operator, t.kind, t.submits[0].script) for t in dag.tasks] == [
        ("AcmeSparkOperator", "spark_submit", "/j/a.py")
    ]


# ── the other submitting operators ────────────────────────────────────


def test_emr_add_steps_and_create_job_flow():
    dag = one(
        """
        from airflow import DAG
        from airflow.providers.amazon.aws.operators.emr import EmrAddStepsOperator, EmrCreateJobFlowOperator

        STEPS = [
            {
                "Name": "orders",
                "ActionOnFailure": "CONTINUE",
                "HadoopJarStep": {
                    "Jar": "command-runner.jar",
                    "Args": ["spark-submit", "--deploy-mode", "cluster", "s3://code/jobs/orders.py", "--day", "2026-01-01"],
                },
            },
            {"Name": "other", "HadoopJarStep": {"Jar": "command-runner.jar", "Args": ["bash", "-c", "spark-submit s3://code/jobs/second.py a b"]}},
            {"Name": "no spark", "HadoopJarStep": {"Jar": "x.jar", "Args": ["--flag"]}},
        ]
        with DAG("emr") as dag:
            add = EmrAddStepsOperator(task_id="add", job_flow_id="j-1", steps=STEPS)
            create = EmrCreateJobFlowOperator(
                task_id="create",
                job_flow_overrides={"Name": "c", "Steps": [STEPS[0]]},
            )
            from_file = EmrAddStepsOperator(task_id="file", job_flow_id="j-1", steps="steps.json")
        """
    )
    add, create, from_file = dag.tasks
    assert [(s.runtime, s.script, s.args) for s in add.submits] == [
        ("emr-step", "s3://code/jobs/orders.py", ("--day", "2026-01-01")),
        ("emr-step", "s3://code/jobs/second.py", ("a", "b")),
    ]
    assert [s.script for s in create.submits] == ["s3://code/jobs/orders.py"]
    (unread,) = from_file.submits
    assert unread.script is None and "in a file" in unread.why


def test_emr_on_eks_and_serverless_job_drivers():
    dag = one(
        """
        from airflow import DAG
        from airflow.providers.amazon.aws.operators.emr import EmrContainerOperator, EmrServerlessStartJobOperator

        with DAG("emr2"):
            EmrContainerOperator(
                task_id="eks",
                virtual_cluster_id="vc",
                job_driver={"sparkSubmitJobDriver": {"entryPoint": "s3://code/a.py", "entryPointArguments": ["--x", "1"], "sparkSubmitParameters": "--conf a=b"}},
            )
            EmrServerlessStartJobOperator(
                task_id="srv",
                application_id="app",
                job_driver={"sparkSubmit": {"entryPoint": "s3://code/b.py", "entryPointArguments": ["--y"]}},
            )
        """
    )
    eks, srv = dag.tasks
    assert [(s.runtime, s.script, s.args) for s in eks.submits] == [
        ("emr-eks", "s3://code/a.py", ("--x", "1"))
    ]
    assert [(s.runtime, s.script, s.args) for s in srv.submits] == [
        ("emr-serverless", "s3://code/b.py", ("--y",))
    ]


def test_glue_and_databricks_submits():
    dag = one(
        """
        from airflow import DAG
        from airflow.providers.amazon.aws.operators.glue import GlueJobOperator
        from airflow.providers.databricks.operators.databricks import DatabricksSubmitRunOperator

        with DAG("g"):
            GlueJobOperator(task_id="glue", job_name="j", script_location="s3://code/g.py", script_args={"--a": "1", "--b": "2"})
            DatabricksSubmitRunOperator(task_id="dbx", spark_python_task={"python_file": "dbfs:/jobs/d.py", "parameters": ["p"]})
            DatabricksSubmitRunOperator(task_id="dbx2", json={"spark_python_task": {"python_file": "dbfs:/jobs/e.py"}})
        """
    )
    glue, dbx, dbx2 = dag.tasks
    assert [(s.runtime, s.script, s.args) for s in glue.submits] == [
        ("glue", "s3://code/g.py", ("--a", "1", "--b", "2"))
    ]
    assert [(s.runtime, s.script, s.args) for s in dbx.submits] == [
        ("databricks", "dbfs:/jobs/d.py", ("p",))
    ]
    assert dbx2.submits[0].script == "dbfs:/jobs/e.py"


def test_bash_submits_only_when_the_command_is_a_spark_submit_of_a_python_script():
    dag = one(
        """
        from airflow import DAG
        from airflow.operators.bash import BashOperator
        with DAG("b"):
            BashOperator(task_id="a", bash_command="spark-submit /j/a.py x")
            BashOperator(task_id="b", bash_command="/opt/spark/bin/spark-submit --class Main app.jar")
            BashOperator(task_id="c", bash_command="dbt run")
            BashOperator(task_id="d", bash_command=f"spark-submit {unknown}/d.py --x")
            BashOperator(task_id="e", bash_command="spark-submit 'unbalanced")
        """
    )
    by_id = {t.task_id: t.submits for t in dag.tasks}
    assert by_id["a"][0].script == "/j/a.py" and by_id["a"][0].args == ("x",)
    assert by_id["b"][0].script is None and "no Python script" in by_id["b"][0].why
    assert by_id["c"] == []
    # a field that did not fold is a field, not a path: the script is not a constant
    assert by_id["d"][0].script is None and by_id["d"][0].shown == "{unknown}/d.py"
    assert by_id["d"][0].args == ("--x",)
    assert by_id["e"] == []


def test_a_script_with_a_jinja_field_is_kept_and_marked():
    dag = one(
        """
        from airflow import DAG
        from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
        with DAG("t"):
            SparkSubmitOperator(task_id="a", application="{{ var.value.jobs }}/a.py")
        """
    )
    (submit,) = dag.tasks[0].submits
    assert submit.script == "{{ var.value.jobs }}/a.py" and submit.templated


def test_a_script_that_is_not_a_constant_is_unresolved_with_its_reason():
    dag = one(
        """
        import os
        from airflow import DAG
        from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
        with DAG("t"):
            SparkSubmitOperator(task_id="a", application=Variable.get("job"))
            SparkSubmitOperator(task_id="b")
        """
    )
    first, second = (t.submits[0] for t in dag.tasks)
    assert first.script is None and "call to" in first.why
    assert second.script is None and second.why == "no script is named"


# ── DAG facts ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs, kind, schedule",
    [
        ('schedule="@daily"', "cron", "@daily"),
        ('schedule_interval="0 * * * *"', "cron", "0 * * * *"),
        ("schedule=None", "none", None),
        ("schedule=timedelta(hours=1)", "expression", "timedelta(hours=1)"),
        ("schedule=[Dataset('s3://x')]", "expression", "[Dataset('s3://x')]"),
        ("timetable=MyTimetable()", "expression", "MyTimetable()"),
        ("", "default", None),
    ],
)
def test_the_schedule_in_every_spelling(kwargs, kind, schedule):
    dag = one(f"from airflow import DAG\nwith DAG('d', {kwargs}):\n    pass\n")
    assert (dag.schedule_kind, dag.schedule) == (kind, schedule)


def test_a_schedule_that_is_not_a_constant_is_noted():
    dag = one("from airflow import DAG\nwith DAG('d', schedule=SCHEDULES[env]):\n    pass\n")
    assert dag.schedule_kind == "unresolved"
    assert any("schedule is not a constant" in n for n in dag.notes)


def test_default_args_come_from_a_constant_or_a_literal_and_spreads_are_honest():
    base = "from airflow import DAG\n"
    assert one(base + "A = {'owner': 'x'}\nwith DAG('d', default_args=A):\n    pass\n").owner == "x"
    assert one(base + "with DAG('d', default_args={'owner': 'y'}):\n    pass\n").owner == "y"
    assert one(base + "with DAG('d', default_args={'retries': 1}):\n    pass\n").owner is None
    spread = one(base + "with DAG('d', default_args={**shared}):\n    pass\n")
    assert spread.owner is None and any("owner is not a constant" in n for n in spread.notes)
    merged = one(
        base + "S = {'owner': 'z'}\nwith DAG('d', default_args={**S, 'retries': 3}):\n    pass\n"
    )
    assert merged.owner == "z"


def test_the_dag_id_may_be_positional_or_a_constant():
    base = "from airflow import DAG\n"
    assert one(base + "ID = 'from_const'\nwith DAG(ID):\n    pass\n").dag_id == "from_const"
    assert one(base + "dag = DAG('assigned')\n").dag_id == "assigned"
    assert one(base + "x = DAG(dag_id='kw')\n").dag_id == "kw"


# ── what cannot be read ───────────────────────────────────────────────


def test_a_dag_id_that_is_not_a_constant_is_noted_and_not_read_further():
    dag = one("from airflow import DAG\nwith DAG(f'etl_{env}'):\n    pass\n")
    assert dag.dag_id is None and dag.shown == "etl_{env}"
    assert any("dag_id is not a constant" in n for n in dag.notes)


def test_dags_and_operators_built_in_loops_and_factories():
    found = dags(
        """
        from airflow import DAG
        from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator

        def make(name):
            with DAG(name) as d:
                SparkSubmitOperator(task_id="t", application="/j/a.py")
            return d

        for name in ["a", "b"]:
            globals()[name] = make(name)

        with DAG("looped") as looped:
            for t in ["x", "y"]:
                SparkSubmitOperator(task_id=f"load_{t}", application="/j/load.py", application_args=[t])

        if flag:
            with DAG("conditional"):
                pass

        for q in ["p"]:
            with DAG("in_loop"):
                pass
        """
    )
    by_id = {d.shown: d for d in found.dags}
    assert any("built by the function make()" in n for n in by_id["name"].notes)
    assert by_id["name"].dag_id is None
    looped = by_id["looped"]
    (task,) = looped.tasks
    assert task.task_id is None and task.shown == "load_{t}"
    assert any("built in a loop" in n for n in task.notes)
    assert any("under a condition" in n for n in by_id["conditional"].notes)
    assert any("built in a loop" in n for n in by_id["in_loop"].notes)


def test_dynamic_mapping_and_chain_over_a_list_are_noted():
    found = dags(
        """
        from airflow import DAG
        from airflow.models.baseoperator import chain
        from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
        with DAG("m"):
            a = SparkSubmitOperator(task_id="a", application="/j/a.py")
            SparkSubmitOperator.partial(task_id="p", application="/j/p.py").expand(application_args=[["1"], ["2"]])
            chain(*tasks)
        """
    )
    (dag,) = found.dags
    mapped = next(t for t in dag.tasks if t.task_id == "p")
    assert mapped.submits[0].script == "/j/p.py"
    assert any(".partial() and .expand()" in n for n in mapped.notes)
    assert any("chain(*...)" in n for n in found.notes)


def test_dag_factory_files_are_noted_not_read():
    found = dags(
        """
        import dagfactory
        factory = dagfactory.DagFactory("config.yml")
        dagfactory.load_yaml_dags(globals_dict=globals(), config_filepath="config.yml")
        """
    )
    assert not found.dags and any("dag-factory" in n for n in found.notes)


def test_an_operator_outside_any_dag_is_noted():
    found = dags(
        """
        from airflow import DAG
        from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
        with DAG("a"):
            pass
        with DAG("b"):
            pass
        SparkSubmitOperator(task_id="stray", application="/j/s.py")
        """
    )
    assert all(not d.tasks for d in found.dags)
    assert any("outside any DAG" in n for n in found.notes)


def test_a_file_without_a_dag_has_none():
    assert dags("import airflow\nprint(1)\n").dags == []
    assert dags("from other import DAG\nDAG('x')\n").dags == []


# ── dependencies ──────────────────────────────────────────────────────


def test_every_dependency_spelling():
    dag = one(
        """
        from airflow import DAG
        from airflow.models.baseoperator import chain, cross_downstream
        from airflow.operators.bash import BashOperator

        with DAG("deps"):
            a = BashOperator(task_id="a", bash_command="x")
            b = BashOperator(task_id="b", bash_command="x")
            c = BashOperator(task_id="c", bash_command="x")
            d = BashOperator(task_id="d", bash_command="x")
            e = BashOperator(task_id="e", bash_command="x")
            f = BashOperator(task_id="f", bash_command="x")
            g = BashOperator(task_id="g", bash_command="x")
            h = BashOperator(task_id="h", bash_command="x")
            a >> [b, c]
            [b, c] >> d
            e << d
            d.set_downstream(f)
            g.set_upstream(f)
            chain(g, h)
            BashOperator(task_id="i", bash_command="x") >> BashOperator(task_id="j", bash_command="x")
        """
    )
    assert edges(dag) == sorted(
        [
            ("a", "b"),
            ("a", "c"),
            ("b", "d"),
            ("c", "d"),
            ("d", "e"),
            ("d", "f"),
            ("f", "g"),
            ("g", "h"),
            ("i", "j"),
        ]
    )


def test_cross_downstream_and_a_parenthesised_chain():
    dag = one(
        """
        from airflow import DAG
        from airflow.models.baseoperator import cross_downstream
        from airflow.operators.bash import BashOperator
        with DAG("deps2"):
            a = BashOperator(task_id="a", bash_command="x")
            b = BashOperator(task_id="b", bash_command="x")
            c = BashOperator(task_id="c", bash_command="x")
            d = BashOperator(task_id="d", bash_command="x")
            cross_downstream([a, b], [c, d])
        """
    )
    assert edges(dag) == [("a", "c"), ("a", "d"), ("b", "c"), ("b", "d")]
    nested = one(
        """
        from airflow import DAG
        from airflow.operators.bash import BashOperator
        with DAG("deps3"):
            a = BashOperator(task_id="a", bash_command="x")
            b = BashOperator(task_id="b", bash_command="x")
            c = BashOperator(task_id="c", bash_command="x")
            a >> (b >> c)
        """
    )
    assert edges(nested) == [("a", "c"), ("b", "c")]


def test_two_dags_in_one_file_keep_their_tasks_and_edges_apart():
    found = dags(
        """
        from airflow import DAG
        from airflow.operators.bash import BashOperator
        with DAG("one") as one_dag:
            a = BashOperator(task_id="a", bash_command="x")
            b = BashOperator(task_id="b", bash_command="x")
            a >> b
        two = DAG("two")
        c = BashOperator(task_id="c", bash_command="x", dag=two)
        """
    )
    first, second = found.dags
    assert [t.task_id for t in first.tasks] == ["a", "b"] and edges(first) == [("a", "b")]
    assert [t.task_id for t in second.tasks] == ["c"] and second.edges == []


# ── it only reads ─────────────────────────────────────────────────────


def test_airflow_is_never_imported_and_the_file_never_runs(tmp_path):
    before, inside, after = (tmp_path / name for name in ("before", "inside", "after"))
    sys.modules.pop("airflow", None)
    found = dags(
        f"""
        from airflow import DAG
        from airflow.operators.bash import BashOperator
        open({str(before)!r}, "w").write("x")
        with DAG("d"):
            open({str(inside)!r}, "w").write("x")
            BashOperator(task_id="t", bash_command="rm -rf /")
        open({str(after)!r}, "w").write("x")
        raise SystemExit(2)
        """
    )
    assert [d.dag_id for d in found.dags] == ["d"]
    assert not (before.exists() or inside.exists() or after.exists())
    assert "airflow" not in sys.modules


# ── spark-submit is read as the command line it is ────────────────────


def bash_submits(command: str):
    dag = one(
        f"""
        from airflow import DAG
        from airflow.operators.bash import BashOperator
        with DAG("b"):
            BashOperator(task_id="t", bash_command={command!r})
        """
    )
    return dag.tasks[0].submits


@pytest.mark.parametrize(
    "command, script, args",
    [
        ("spark-submit --py-files deps.py main.py a b", "main.py", ("a", "b")),
        (
            "spark-submit --py-files=deps.py --conf a=b -c c=d /jobs/main.py x",
            "/jobs/main.py",
            ("x",),
        ),
        (
            "spark-submit --master yarn --deploy-mode cluster s3://code/job.py --day 1",
            "s3://code/job.py",
            ("--day", "1"),
        ),
        ("spark-submit --supervise --verbose job.py", "job.py", ()),
        ("spark-submit --some-new-flag job.py", "job.py", ()),
        ("spark-submit --some-new-option value job.py", "job.py", ()),
        ("spark-submit --files a.py,b.py --jars x.jar job.py", "job.py", ()),
        ("/opt/spark/bin/spark-submit local:///opt/job.py", "local:///opt/job.py", ()),
        ("$SPARK_HOME/bin/spark-submit --conf k=v job.py", "job.py", ()),
        ("spark-submit $EXTRA_OPTS job.py", "job.py", ()),
        ("spark-submit {{ var.value.opts }} job.py", "job.py", ()),
        (
            "spark-submit job.py --day {{ ds }} --to '{{ next_ds }}'",
            "job.py",
            ("--day", "{{ ds }}", "--to", "{{ next_ds }}"),
        ),
        ("spark-submit {{ params.script }} --day 1", "{{ params.script }}", ()),
    ],
)
def test_the_application_is_the_first_word_that_is_not_an_option_or_its_value(
    command, script, args
):
    (submit,) = bash_submits(command)
    assert (submit.script, submit.args) == (script, args)


@pytest.mark.parametrize(
    "command",
    [
        "echo spark-submit job.py",
        "# spark-submit job.py",
        "ps aux | grep spark-submit",
        "grep -q spark-submit job.py || true",
        "cat spark-submit.sh",
        "which spark-submit && echo ok",
        "spark-submit --version",
        "spark-submit --help",
        "test -x /opt/spark/bin/spark-submit",
    ],
)
def test_a_command_that_only_names_spark_submit_does_not_submit_a_job(command):
    assert bash_submits(command) == []


def test_several_spark_submits_in_one_command_are_all_read():
    submits = bash_submits("cd /opt && spark-submit a.py; spark-submit --conf k=v b.py x | tee log")
    assert [(s.script, s.args) for s in submits] == [("a.py", ()), ("b.py", ("x",))]
    assert all(s.command and "tee log" in s.command for s in submits)


def test_a_spark_submit_run_through_a_wrapper_or_a_shell_is_read():
    for command, script in [
        ("sudo -u hadoop spark-submit job.py", "job.py"),
        ("timeout 3600 spark-submit job.py", "job.py"),
        ("env A=1 B=2 spark-submit job.py", "job.py"),
        ("A=1 spark-submit job.py", "job.py"),
        ("nohup spark-submit job.py > out.log 2>&1 &", "job.py"),
        ("if [ -f x ]; then spark-submit a.py; fi", "a.py"),
        ("bash -c 'spark-submit a.py && echo ok'", "a.py"),
        ('/bin/bash -lc "cd /x; spark-submit a.py"', "a.py"),
    ]:
        (submit,) = bash_submits(command)
        assert submit.script == script, command


def test_redirections_and_line_continuations_are_not_arguments():
    (submit,) = bash_submits("spark-submit \\\n  --master yarn \\\n  job.py a 2>&1 | tee log")
    assert (submit.script, submit.args) == ("job.py", ("a",))
    (submit,) = bash_submits("spark-submit job.py a $(date +%F)")
    assert submit.args == ("a",)


def test_a_comment_line_before_the_command_is_not_part_of_it():
    (submit,) = bash_submits("# run the job\n# spark-submit old.py\nspark-submit new.py")
    assert submit.script == "new.py"


def test_a_jar_application_is_not_a_script():
    (submit,) = bash_submits("spark-submit --class Main app.jar")
    assert submit.script is None and "no Python script" in submit.why and "app.jar" in submit.why


def test_a_shell_variable_as_the_script_is_not_a_constant():
    (submit,) = bash_submits("spark-submit --master yarn $SCRIPT")
    assert submit.script is None and "shell variable" in submit.why


def test_a_field_that_did_not_fold_is_a_hole_the_rest_of_the_command_is_still_read():
    dag = one(
        """
        from airflow import DAG
        from airflow.operators.bash import BashOperator
        with DAG("b"):
            BashOperator(task_id="a", bash_command=f"spark-submit --master {master} /jobs/main.py --day {day}")
            BashOperator(task_id="b", bash_command=f"spark-submit {script} --x")
        """
    )
    first, second = (t.submits[0] for t in dag.tasks)
    assert (first.script, first.args) == ("/jobs/main.py", ("--day", "<not constant>"))
    assert first.command == "spark-submit --master {master} /jobs/main.py --day {day}"
    assert second.script is None and second.shown == "{script}"


def test_a_command_the_file_does_not_define_but_that_mentions_spark_submit_is_reported():
    dag = one(
        """
        from airflow import DAG
        from airflow.operators.bash import BashOperator
        with DAG("b"):
            BashOperator(task_id="a", bash_command=build("spark-submit"))
            BashOperator(task_id="b", bash_command=build("dbt"))
        """
    )
    first, second = (t.submits for t in dag.tasks)
    assert len(first) == 1 and first[0].script is None
    assert second == []


def test_an_emr_step_skips_option_values_too():
    dag = one(
        """
        from airflow import DAG
        from airflow.providers.amazon.aws.operators.emr import EmrAddStepsOperator
        STEPS = [{"Name": "s", "HadoopJarStep": {"Jar": "command-runner.jar",
                  "Args": ["spark-submit", "--py-files", "s3://code/deps.py", "--deploy-mode", "cluster", "s3://code/main.py", "x"]}}]
        with DAG("e"):
            EmrAddStepsOperator(task_id="add", job_flow_id="j", steps=STEPS)
        """
    )
    (submit,) = dag.tasks[0].submits
    assert (submit.script, submit.args) == ("s3://code/main.py", ("x",))


# ── dependencies: bounded, and what is not read is said ───────────────


def test_a_wide_cross_dependency_is_read_in_bounded_time_and_capped():
    import time

    body = "\n".join(
        [f'    a{i} = EmptyOperator(task_id="a{i}")' for i in range(300)]
        + [f'    b{i} = EmptyOperator(task_id="b{i}")' for i in range(300)]
        + [
            "    ["
            + ", ".join(f"a{i}" for i in range(300))
            + "] >> ["
            + ", ".join(f"b{i}" for i in range(300))
            + "]"
        ]
    )
    source = (
        "from airflow import DAG\n"
        "from airflow.operators.empty import EmptyOperator\n"
        'with DAG("wide"):\n' + body + "\n"
    )
    started = time.monotonic()
    dag = one(source)
    assert time.monotonic() - started < 5
    assert len(dag.edges) == 10_000
    assert any("more than 10000 dependencies" in note for note in dag.notes)
    assert len(set(dag.edges)) == len(dag.edges)


def test_a_dependency_written_twice_is_recorded_once():
    dag = one(
        """
        from airflow import DAG
        from airflow.operators.empty import EmptyOperator
        with DAG("d"):
            a = EmptyOperator(task_id="a")
            b = EmptyOperator(task_id="b")
            a >> b
            b << a
            a.set_downstream(b)
        """
    )
    assert edges(dag) == [("a", "b")]


def test_a_task_group_in_a_dependency_is_said_to_be_unread_and_the_rest_still_linked():
    found = dags(
        """
        from airflow import DAG
        from airflow.utils.task_group import TaskGroup
        from airflow.operators.empty import EmptyOperator
        with DAG("g") as dag:
            start = EmptyOperator(task_id="start")
            end = EmptyOperator(task_id="end")
            with TaskGroup("work") as work:
                inner = EmptyOperator(task_id="inner")
            start >> work >> end
            start >> end
        """
    )
    assert edges(found.dags[0]) == [("start", "end")]
    notes = [n for n in found.notes if "a dependency on `work`" in n]
    assert len(notes) == 1 and notes[0].startswith("dag.py:")


def test_unread_dependencies_are_a_few_notes_and_a_count_not_one_per_operand():
    lines = "\n".join(f"    start >> group{i}" for i in range(40))
    source = (
        "from airflow import DAG\n"
        "from airflow.operators.empty import EmptyOperator\n"
        'with DAG("g"):\n'
        '    start = EmptyOperator(task_id="start")\n' + lines + "\n"
    )
    found = dags(source)
    notes = [n for n in found.notes if "a dependency on" in n or "more dependencies" in n]
    assert len(notes) == 11 and "more dependencies" in notes[-1]


def test_an_edge_label_is_not_a_task_and_does_not_cut_the_dependency():
    found = dags(
        """
        from airflow import DAG
        from airflow.utils.edgemodifier import Label
        from airflow.operators.empty import EmptyOperator
        with DAG("l"):
            a = EmptyOperator(task_id="a")
            b = EmptyOperator(task_id="b")
            c = EmptyOperator(task_id="c")
            a >> Label("ok") >> b >> Label("then") >> c
        """
    )
    assert edges(found.dags[0]) == [("a", "b"), ("b", "c")]
    assert not [n for n in found.notes if "Label" in n]


def test_many_task_functions_are_read_without_walking_the_file_for_each():
    import time

    functions = "\n".join(f"    @task\n    def t{i}():\n        pass\n" for i in range(400))
    calls = "\n".join(f"    x{i} = t{i}()" for i in range(400))
    source = (
        "from airflow.decorators import dag, task\n"
        "@dag(dag_id='many')\n"
        "def many():\n" + functions + calls + "\n"
        "many()\n"
    )
    started = time.monotonic()
    dag = one(source)
    assert time.monotonic() - started < 5
    assert len(dag.tasks) == 400


def test_a_file_that_asks_for_too_much_folding_is_said_so_once():
    lines = ['x0 = ""']
    for level in range(1, 11):
        lines.append(f'x{level} = f"' + ("{x%d}" % (level - 1)) * 300 + '"')
    source = "\n".join(
        [
            "from airflow import DAG",
            *lines,
            'with DAG(x10 + "a", schedule=x10 + "b", tags=[x10 + "c"]):',
            "    pass",
        ]
    )
    found = dags(source)
    assert [n for n in found.notes if "folding stopped early" in n] == [
        "dag.py: folding stopped early (the file asks for too much work): some values are "
        "reported as not constant"
    ]


def test_a_command_too_long_to_fold_is_said_not_dropped():
    long_command = "echo " + "x" * 5000
    submits = bash_submits(long_command)
    assert len(submits) == 1
    assert submits[0].script is None and "longer than" in submits[0].why


def test_a_jinja_field_that_never_closes_costs_a_bounded_look_ahead():
    import time

    from fluid_build.cli.import_workflow import _airflow_dags as A

    for text in ("{{" * 2048, "{% " * 1365, "{# " * 1365):
        started = time.monotonic()
        A._shell_commands(text, {})
        assert time.monotonic() - started < 0.5


@pytest.mark.parametrize(
    "command",
    [
        "command -v spark-submit",
        "if ! command -v spark-submit >/dev/null; then echo missing; fi",
        "hash spark-submit",
        "dirname $(which spark-submit)",
        "readlink -f /opt/spark/bin/spark-submit",
    ],
)
def test_asking_where_spark_submit_is_does_not_run_it(command):
    assert bash_submits(command) == []


def test_command_in_front_of_spark_submit_still_runs_it():
    (submit,) = bash_submits("command spark-submit job.py")
    assert submit.script == "job.py"
