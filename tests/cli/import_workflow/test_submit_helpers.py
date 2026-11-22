# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Spark work a DAG submits through a Python task's function, read statically.

A team that runs its Spark jobs from Airflow often submits them from a ``PythonOperator``: a
function of its own EMR library (named in the conventions under ``submit_helpers``), or a function
of the DAG file that calls EMR itself. Nothing here is imported or run.
"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest
import yaml

from fluid_build.cli.import_workflow import _callables
from fluid_build.cli.import_workflow._airflow_dags import ModuleDags, Task, read_dags
from fluid_build.cli.import_workflow._callables import HELPER_KINDS, Follower, SubmitHelper
from fluid_build.cli.import_workflow._conventions import (
    Conventions,
    ConventionsError,
    parse_conventions,
)
from fluid_build.cli.import_workflow._fold import Folder
from fluid_build.cli.import_workflow._pysource import parse_module
from fluid_build.cli.import_workflow._scan import scan_dags
from fluid_build.cli.import_workflow.airflow import AirflowImporter
from fluid_build.schema_manager import FluidSchemaManager

HELPERS = {
    "run_stream_step": {
        "script": "script_path",
        "config": "s3://{config_bucket}/{config_key}",
        "kind": "emr_add_steps",
    },
    "run_step": {
        "script": "additional_params.script_path",
        "config": "additional_params.config_path",
        "args": "additional_params.job_args",
        "kind": "emr_add_steps",
    },
    "run_positional": {"script": 0, "args": 1},
}


def helpers(entries: Optional[Mapping[str, Any]] = None) -> Mapping[str, SubmitHelper]:
    return parse_conventions({"submit_helpers": dict(entries or HELPERS)}).submit_helpers


def dags(source: str, entries: Optional[Mapping[str, Any]] = None, **folder: Any) -> ModuleDags:
    module = parse_module("dag.py", textwrap.dedent(source).encode("utf-8"))
    return read_dags(module, Folder(module, **folder), submit_helpers=helpers(entries))


def tasks(
    source: str, entries: Optional[Mapping[str, Any]] = None, **folder: Any
) -> Dict[str, Task]:
    found = dags(source, entries, **folder)
    return {t.task_id: t for d in found.dags for t in d.tasks}  # type: ignore[misc]


def submits(task: Task) -> List[tuple]:
    return [(s.runtime, s.script, s.args) for s in task.submits]


HEAD = """
import boto3
from airflow import DAG
from airflow.operators.python import PythonOperator
from acme_lib import emr_tasks as tasks
CODE = "s3://acme-code/jobs"
dag = DAG("nightly", schedule="0 3 * * *", default_args={"owner": "retail-data"})
"""


def _dag(body: str) -> str:
    return textwrap.dedent(HEAD) + textwrap.dedent(body)


# ── the conventions ───────────────────────────────────────────────────


def test_a_submit_helper_says_where_its_script_args_and_config_are():
    found = helpers()
    stream = found["run_stream_step"]
    assert stream.script == (("script_path", ()),)
    assert stream.config == ("s3://", ("config_bucket", ()), "/", ("config_key", ()))
    assert stream.runtime == "emr-step" and stream.args is None
    step = found["run_step"]
    assert step.script == (("additional_params", ("script_path",)),)
    assert step.args == ("additional_params", ("job_args",))
    assert found["run_positional"].script == ((0, ()),) and found["run_positional"].args == (1, ())
    assert found["run_positional"].runtime == "spark-submit"  # the kind defaults to spark_submit


def test_every_operator_kind_but_bash_is_a_kind_a_helper_may_submit_to():
    assert set(HELPER_KINDS) == {
        "spark_submit",
        "emr_add_steps",
        "emr_create_job_flow",
        "emr_containers",
        "emr_serverless",
        "glue_job",
        "databricks_submit",
    }
    for kind, runtime in HELPER_KINDS.items():
        assert helpers({"f": {"script": "s", "kind": kind}})["f"].runtime == runtime


@pytest.mark.parametrize(
    "entry, message",
    [
        ({"args": "a"}, "the script is required"),
        ({"script": "s", "where": "x"}, "unknown key"),
        ({"script": "s", "kind": "bash"}, "is not one of"),
        ({"script": "s", "kind": "spark"}, "is not one of"),
        ({"script": "s3://code/a.py"}, "expected an argument"),
        ({"script": "a b"}, "expected an argument"),
        ({"script": "-1"}, "expected an argument"),
        ({"script": -1}, "a position is 0 or more"),
        ({"script": True}, "expected a non-empty string"),
        ({"script": "s", "args": "s3://{x}"}, "expected an argument"),
        ({"script": "s3://{bucket}/{"}, "a brace that is not around an argument"),
        ({"script": "s3://{bucket}}"}, "a brace that is not around an argument"),
        ({"script": "s3://{}/a.py"}, "expected a non-empty string"),
        ({"script": "s3://{a b}/a.py"}, "expected an argument"),
        ("script_path", "expected a mapping"),
    ],
)
def test_a_submit_helper_the_conventions_cannot_read_is_refused(entry, message):
    with pytest.raises(ConventionsError) as caught:
        parse_conventions({"submit_helpers": {"run": entry}})
    assert message in str(caught.value) and "submit_helpers.run" in str(caught.value)


def test_a_submit_helper_name_is_a_function_name_or_a_dotted_path():
    assert set(helpers({"emr.run": {"script": "s"}, "run": {"script": "s"}})) == {"emr.run", "run"}
    with pytest.raises(ConventionsError, match="not an acceptable name"):
        parse_conventions({"submit_helpers": {"run step": {"script": "s"}}})


def test_the_conventions_file_takes_the_key_and_says_what_it_is_for(tmp_path):
    from fluid_build.cli.import_workflow import _conventions

    assert "submit_helpers" in _conventions._TOP_KEYS
    assert "submit_helpers:" in (_conventions.__doc__ or "")


# ── a function the conventions name ───────────────────────────────────


def test_a_task_that_runs_a_named_helper_is_read_from_its_op_kwargs():
    found = tasks(
        _dag(
            """
            stream = PythonOperator(
                task_id="stream",
                python_callable=tasks.run_stream_step,
                op_kwargs={
                    "script_path": f"{CODE}/clicks_stream.py",
                    "config_bucket": "acme-conf",
                    "config_key": "streams/clicks.yaml",
                    "env": "prod",
                },
                dag=dag,
            )
            """
        )
    )
    task = found["stream"]
    assert task.kind == "python" and task.callable == "acme_lib.emr_tasks.run_stream_step"
    (submit,) = task.submits
    assert (submit.runtime, submit.script, submit.config) == (
        "emr-step",
        "s3://acme-code/jobs/clicks_stream.py",
        "s3://acme-conf/streams/clicks.yaml",
    )
    assert submit.config_why == "" and submit.line == task.line


def test_a_helper_given_a_whole_dict_of_settings_is_read_by_its_key_path():
    found = tasks(
        _dag(
            """
            MAINTAIN = {
                "step_name": "maintain",
                "additional_params": {
                    "script_path": f"{CODE}/maintain.py",
                    "config_path": "s3://acme-conf/maintain.yaml",
                    "job_args": ["--mode", "compact"],
                },
            }
            PythonOperator(task_id="maintain", python_callable=tasks.run_step, op_kwargs=MAINTAIN, dag=dag)
            """
        )
    )
    (submit,) = found["maintain"].submits
    assert (submit.script, submit.args, submit.config) == (
        "s3://acme-code/jobs/maintain.py",
        ("--mode", "compact"),
        "s3://acme-conf/maintain.yaml",
    )


def test_a_helper_is_matched_by_the_last_parts_of_its_name_the_longest_entry_first():
    source = _dag(
        """
        from acme_lib.emr_tasks import run_stream_step
        from other_lib import run_stream_step as other_run
        PythonOperator(task_id="a", python_callable=tasks.run_stream_step, op_kwargs={"script_path": "s3://a.py"}, dag=dag)
        PythonOperator(task_id="b", python_callable=run_stream_step, op_kwargs={"script_path": "s3://b.py"}, dag=dag)
        PythonOperator(task_id="c", python_callable=other_run, op_kwargs={"script_path": "s3://c.py", "other": "s3://o.py"}, dag=dag)
        """
    )
    found = tasks(
        source,
        {
            "run_stream_step": {"script": "script_path"},
            "other_lib.run_stream_step": {"script": "other"},
        },
    )
    assert [s.script for s in found["a"].submits] == ["s3://a.py"]
    assert [s.script for s in found["b"].submits] == ["s3://b.py"]
    assert [s.script for s in found["c"].submits] == ["s3://o.py"]  # the longer name wins
    # a name that only ends in the same letters is not the function
    other = tasks(
        _dag(
            """
            PythonOperator(task_id="d", python_callable=tasks.prerun_stream_step, op_kwargs={"script_path": "s3://d.py"}, dag=dag)
            """
        )
    )
    assert other["d"].submits == []


def test_a_helper_read_by_position_from_op_args_and_through_partial():
    found = tasks(
        _dag(
            """
            from functools import partial
            import functools
            PythonOperator(task_id="args", python_callable=tasks.run_positional, op_args=[f"{CODE}/a.py", ["--day", "{{ ds }}"]], dag=dag)
            PythonOperator(task_id="partial", python_callable=partial(tasks.run_positional, f"{CODE}/b.py"), op_args=[["--x"]], dag=dag)
            PythonOperator(task_id="named", python_callable=functools.partial(tasks.run_stream_step, script_path=f"{CODE}/c.py"), dag=dag)
            """
        )
    )
    assert submits(found["args"]) == [
        ("spark-submit", "s3://acme-code/jobs/a.py", ("--day", "{{ ds }}"))
    ]
    assert submits(found["partial"]) == [("spark-submit", "s3://acme-code/jobs/b.py", ("--x",))]
    assert [s.script for s in found["named"].submits] == ["s3://acme-code/jobs/c.py"]
    assert found["named"].callable == "acme_lib.emr_tasks.run_stream_step"


def test_what_a_helper_is_not_given_is_said_not_guessed():
    found = tasks(
        _dag(
            """
            PythonOperator(task_id="none", python_callable=tasks.run_stream_step, dag=dag)
            PythonOperator(task_id="by_position", python_callable=tasks.run_stream_step, op_args=["s3://x.py"], dag=dag)
            PythonOperator(task_id="no_key", python_callable=tasks.run_step, op_kwargs={"additional_params": {"other": 1}}, dag=dag)
            PythonOperator(task_id="not_a_dict", python_callable=tasks.run_step, op_kwargs={"additional_params": "x"}, dag=dag)
            PythonOperator(task_id="call", python_callable=tasks.run_step, op_kwargs=build_kwargs(), dag=dag)
            PythonOperator(task_id="spread", python_callable=tasks.run_stream_step, op_kwargs={**unknown}, dag=dag)
            """
        )
    )

    def why(task_id: str) -> str:
        (submit,) = found[task_id].submits
        assert submit.script is None
        return submit.why

    assert why("none") == "run_stream_step is not given its `script_path` argument"
    assert why("by_position") == "run_stream_step is not given `script_path` by keyword"
    assert why("no_key") == "`script_path` is not in the `additional_params` run_step is given"
    assert why("not_a_dict") == "not a dict"
    assert "the result of a call to build_kwargs()" in why("call")
    assert why("spread") == "op_kwargs is not all known"
    (none,) = found["none"].submits
    assert none.shown == "script_path of run_stream_step"
    assert none.config_why and none.config == "s3://{config_bucket}/{config_key}"


def test_a_script_that_waits_for_a_value_keeps_its_template_and_reason():
    found = tasks(
        _dag(
            """
            from acme_cfg.paths import JOBS_ROOT
            PythonOperator(task_id="t", python_callable=tasks.run_stream_step, op_kwargs={"script_path": f"{JOBS_ROOT}/a.py", "config_bucket": "b", "config_key": Variable.get("conf_key")}, dag=dag)
            from airflow.models import Variable
            """
        )
    )
    (submit,) = found["t"].submits
    assert submit.script is None and submit.shown == "{JOBS_ROOT}/a.py"
    assert "JOBS_ROOT" in submit.why
    assert submit.config == "s3://b/{Variable.get('conf_key')}"
    assert submit.config_why == "the Airflow Variable conf_key"


def test_op_kwargs_are_templated_fields_their_variables_are_filled():
    found = tasks(
        _dag(
            """
            PythonOperator(task_id="t", python_callable=tasks.run_stream_step, op_kwargs={"script_path": "{{ var.value.code_root }}/a.py", "config_bucket": "{{ var.value.conf }}", "config_key": "{{ ds }}.yaml"}, dag=dag)
            """
        ),
        airflow_variables={"code_root": "s3://acme-code", "conf": "acme-conf"},
    )
    (submit,) = found["t"].submits
    assert submit.script == "s3://acme-code/a.py"
    assert submit.config == "s3://acme-conf/{{ ds }}.yaml"


# ── a function of the DAG file ────────────────────────────────────────


def test_a_function_of_the_dag_file_that_adds_emr_steps_itself():
    found = tasks(
        _dag(
            """
            from airflow.models import Variable

            def submit_backfill(**kwargs):
                ti = kwargs["ti"]
                cluster_id = ti.xcom_pull(task_ids="create_cluster")
                day = Variable.get("backfill_day")
                client = boto3.client("emr", region_name="eu-central-1")
                response = client.add_job_flow_steps(
                    JobFlowId=cluster_id,
                    Steps=[
                        {
                            "Name": "backfill",
                            "ActionOnFailure": "CONTINUE",
                            "HadoopJarStep": {
                                "Jar": "command-runner.jar",
                                "Args": [
                                    "spark-submit",
                                    "--deploy-mode", "client",
                                    "--jars", f"{CODE}/libs/runtime.jar",
                                    "--conf", "spark.yarn.submit.waitAppCompletion=true",
                                    f"{CODE}/backfill.py",
                                    day,
                                ],
                            },
                        }
                    ],
                )
                return response["StepIds"][0]

            backfill = PythonOperator(task_id="backfill", python_callable=submit_backfill, dag=dag)
            """
        )
    )
    task = found["backfill"]
    assert task.callable == "submit_backfill"
    assert submits(task) == [("emr-step", "s3://acme-code/jobs/backfill.py", ("<not constant>",))]


def test_the_steps_may_be_built_before_the_call_or_be_a_constant_of_the_file():
    found = tasks(
        _dag(
            """
            STEPS = [{"Name": "a", "HadoopJarStep": {"Jar": "command-runner.jar", "Args": ["spark-submit", f"{CODE}/a.py"]}}]

            def with_constant():
                boto3.client("emr").add_job_flow_steps(JobFlowId="j", Steps=STEPS)

            def with_local():
                args = ["spark-submit", "--master", "yarn", f"{CODE}/b.py", "--full"]
                step = {"Name": "b", "HadoopJarStep": {"Jar": "command-runner.jar", "Args": args}}
                boto3.client("emr").add_job_flow_steps(JobFlowId="j", Steps=[step])

            PythonOperator(task_id="constant", python_callable=with_constant, dag=dag)
            PythonOperator(task_id="local", python_callable=with_local, dag=dag)
            """
        )
    )
    assert submits(found["constant"]) == [("emr-step", "s3://acme-code/jobs/a.py", ())]
    # the argument list is the step's, read once
    assert submits(found["local"]) == [("emr-step", "s3://acme-code/jobs/b.py", ("--full",))]


def test_what_a_task_passes_is_what_the_functions_parameters_hold():
    found = tasks(
        _dag(
            """
            def submit(script, *args, mode="cluster", **kwargs):
                env = kwargs["env"]
                day = kwargs.get("ds")
                config = kwargs.get("config", f"{CODE}/conf/default.yaml")
                boto3.client("emr").add_job_flow_steps(
                    JobFlowId="j",
                    Steps=[{"HadoopJarStep": {"Args": ["spark-submit", "--deploy-mode", mode, script, env, *args, day]}}],
                )

            PythonOperator(task_id="orders", python_callable=submit, op_kwargs={"script": f"{CODE}/orders.py", "env": "prod"}, dag=dag)
            PythonOperator(task_id="customers", python_callable=submit, op_args=[f"{CODE}/customers.py", "--full"], op_kwargs={"env": "dev"}, dag=dag)
            """
        )
    )
    # the default of `mode` is not read for a task's own function (Airflow may pass the context
    # by that name), so it is the value of an option: skipped as one
    assert submits(found["orders"]) == [
        ("emr-step", "s3://acme-code/jobs/orders.py", ("prod", "<not constant>"))
    ]
    assert submits(found["customers"]) == [
        ("emr-step", "s3://acme-code/jobs/customers.py", ("dev", "--full", "<not constant>"))
    ]


def test_a_parameter_the_body_binds_again_does_not_hold_what_the_task_passed():
    found = tasks(
        _dag(
            """
            def submit(script):
                script = f"{script}.py"
                boto3.client("emr").add_job_flow_steps(JobFlowId="j", Steps=[{"HadoopJarStep": {"Args": ["spark-submit", script]}}])

            PythonOperator(task_id="t", python_callable=submit, op_kwargs={"script": "s3://a"}, dag=dag)
            """
        )
    )
    (submit,) = found["t"].submits
    assert submit.script is None and "is a parameter" in submit.why


def test_a_function_the_body_calls_is_followed_with_what_that_call_passes():
    found = tasks(
        _dag(
            """
            def _step(script, extra=None, *, deploy="cluster"):
                return {"Name": "s", "HadoopJarStep": {"Jar": "command-runner.jar", "Args": ["spark-submit", "--deploy-mode", deploy, script, extra]}}

            def _add(cluster, script):
                boto3.client("emr").add_job_flow_steps(JobFlowId=cluster, Steps=[_step(script)])

            def _submit(cluster, script):
                tasks.run_stream_step(script_path=script, config_bucket="b", config_key="k.yaml")
                args = ["spark-submit", f"{CODE}/{script}"]
                return args

            def run(**kwargs):
                _add("j-1", f"{CODE}/added.py")
                _submit("j-1", "helped.py")

            PythonOperator(task_id="t", python_callable=run, dag=dag)
            """
        )
    )
    got = submits(found["t"])
    # `_step(script)` builds the steps: its list is read in `_step` itself, as an argument list,
    # with the defaults of what the call does not pass (`extra` is None, which is not a word)
    assert ("spark-submit", "s3://acme-code/jobs/added.py", ()) in got
    assert ("emr-step", "helped.py", ()) in got  # the helper the conventions name
    assert ("spark-submit", "s3://acme-code/jobs/helped.py", ()) in got


def test_following_stops_at_a_depth_and_never_loops():
    chain = (
        "\n".join(f"def f{i}(script):\n    f{i + 1}(script)\n" for i in range(10))
        + "def f10(script):\n    boto3.client('emr').add_job_flow_steps(JobFlowId='j', Steps=[{'HadoopJarStep': {'Args': ['spark-submit', script]}}])\n"
    )
    found = tasks(
        _dag(
            chain
            + """
def near(script):
    f9(script)

def loop(script):
    loop(script)
    again(script)

def again(script):
    loop(script)

PythonOperator(task_id="deep", python_callable=f0, op_kwargs={"script": "s3://deep.py"}, dag=dag)
PythonOperator(task_id="near", python_callable=near, op_kwargs={"script": "s3://near.py"}, dag=dag)
PythonOperator(task_id="loop", python_callable=loop, op_kwargs={"script": "s3://loop.py"}, dag=dag)
"""
        )
    )
    assert found["deep"].submits == []  # f10 is more than MAX_DEPTH calls down
    assert [s.script for s in found["near"].submits] == ["s3://near.py"]
    assert found["loop"].submits == []


@pytest.mark.parametrize(
    "line, expected",
    [
        (
            'subprocess.run(["spark-submit", "--master", "yarn", script, "--day", day])',
            [("s3://a.py", ("--day", "<not constant>"))],
        ),
        ('subprocess.check_call(["/opt/spark/bin/spark-submit", script])', [("s3://a.py", ())]),
        ('subprocess.run(["bash", "-c", f"spark-submit {script} --x"])', [("s3://a.py", ("--x",))]),
        (
            'os.system(f"spark-submit --deploy-mode cluster {script} --y")',
            [("s3://a.py", ("--y",))],
        ),
        ('subprocess.getoutput("spark-submit " + script)', [("s3://a.py", ())]),
        ('cmd = ["spark-submit", script]\n    subprocess.Popen(cmd)', [("s3://a.py", ())]),
        ('print("spark-submit s3://not-run.py")', []),
        ('log.info(f"running spark-submit {script}")', []),
        ('names = ["spark-sql", "spark-submit", "pyspark"]', []),
        ('subprocess.run(["echo", "spark-submit", script])', []),
        ('subprocess.run(["spark-submit", "--version"])', []),
    ],
)
def test_a_command_the_function_runs_or_builds(line, expected):
    source = _dag(
        f"""
import os
import subprocess

def run(script, day=None, **kwargs):
    {line}

PythonOperator(task_id="t", python_callable=run, op_kwargs={{"script": "s3://a.py"}}, dag=dag)
"""
    )
    assert [(s.script, s.args) for s in tasks(source)["t"].submits] == expected


def test_emr_on_eks_serverless_run_job_flow_and_the_emr_hook():
    found = tasks(
        _dag(
            """
            from airflow.providers.amazon.aws.hooks.emr import EmrContainerHook, EmrHook

            def eks(**kwargs):
                boto3.client("emr-containers").start_job_run(
                    virtualClusterId="vc", name="n", executionRoleArn="r", releaseLabel="emr-7",
                    jobDriver={"sparkSubmitJobDriver": {"entryPoint": f"{CODE}/eks.py", "entryPointArguments": ["--a"]}},
                )

            def serverless(**kwargs):
                boto3.client("emr-serverless").start_job_run(
                    applicationId="app", executionRoleArn="r",
                    jobDriver={"sparkSubmit": {"entryPoint": f"{CODE}/srv.py"}},
                )

            def glue(**kwargs):
                boto3.client("glue").start_job_run(JobName="not-spark-submit", Arguments={"--x": "y"})

            def new_cluster(**kwargs):
                boto3.client("emr").run_job_flow(Name="c", Steps=[{"HadoopJarStep": {"Args": ["spark-submit", f"{CODE}/flow.py"]}}])

            def eks_hook(**kwargs):
                EmrContainerHook(virtual_cluster_id="vc").submit_job(
                    "n", "role", "emr-7", {"sparkSubmitJobDriver": {"entryPoint": f"{CODE}/eks_hook.py"}}
                )

            def hook(**kwargs):
                emr = EmrHook(aws_conn_id="aws")
                emr.add_job_flow_steps("j-1", [{"HadoopJarStep": {"Args": ["spark-submit", f"{CODE}/hook.py"]}}])
                emr.create_job_flow({"Name": "c", "Steps": [{"HadoopJarStep": {"Args": ["spark-submit", f"{CODE}/created.py"]}}]})

            PythonOperator(task_id="eks", python_callable=eks, dag=dag)
            PythonOperator(task_id="serverless", python_callable=serverless, dag=dag)
            PythonOperator(task_id="glue", python_callable=glue, dag=dag)
            PythonOperator(task_id="new_cluster", python_callable=new_cluster, dag=dag)
            PythonOperator(task_id="hook", python_callable=hook, dag=dag)
            PythonOperator(task_id="eks_hook", python_callable=eks_hook, dag=dag)
            """
        )
    )
    assert submits(found["eks"]) == [("emr-eks", "s3://acme-code/jobs/eks.py", ("--a",))]
    assert submits(found["serverless"]) == [("emr-serverless", "s3://acme-code/jobs/srv.py", ())]
    assert found["glue"].submits == []
    assert submits(found["eks_hook"]) == [("emr-eks", "s3://acme-code/jobs/eks_hook.py", ())]
    assert submits(found["new_cluster"]) == [("emr-step", "s3://acme-code/jobs/flow.py", ())]
    assert submits(found["hook"]) == [
        ("emr-step", "s3://acme-code/jobs/hook.py", ()),
        ("emr-step", "s3://acme-code/jobs/created.py", ()),
    ]


def test_a_lambda_a_taskflow_function_and_a_callable_inside_the_dag_block():
    found = tasks(
        _dag(
            """
            from airflow.decorators import task

            with dag:
                def inside(**kwargs):
                    boto3.client("emr").add_job_flow_steps(JobFlowId="j", Steps=[{"HadoopJarStep": {"Args": ["spark-submit", f"{CODE}/inside.py"]}}])

                PythonOperator(task_id="inside", python_callable=inside)
                PythonOperator(task_id="lam", python_callable=lambda **ctx: tasks.run_stream_step(script_path=f"{CODE}/lam.py"))

                @task
                def flow():
                    boto3.client("emr").add_job_flow_steps(JobFlowId="j", Steps=[{"HadoopJarStep": {"Args": ["spark-submit", f"{CODE}/flow.py"]}}])

                flow()
            """
        )
    )
    assert [s.script for s in found["inside"].submits] == ["s3://acme-code/jobs/inside.py"]
    assert [s.script for s in found["lam"].submits] == ["s3://acme-code/jobs/lam.py"]
    assert found["lam"].callable == "lambda"
    assert [s.script for s in found["flow"].submits] == ["s3://acme-code/jobs/flow.py"]
    assert found["flow"].callable == "flow"


def test_a_name_bound_more_than_one_way_is_not_taken_for_a_function():
    found = tasks(
        _dag(
            """
            def submit():
                boto3.client("emr").add_job_flow_steps(JobFlowId="j", Steps=[{"HadoopJarStep": {"Args": ["spark-submit", "s3://a.py"]}}])
            submit = wrap(submit)

            def twice():
                pass
            def twice():
                boto3.client("emr").add_job_flow_steps(JobFlowId="j", Steps=[{"HadoopJarStep": {"Args": ["spark-submit", "s3://b.py"]}}])

            PythonOperator(task_id="a", python_callable=submit, dag=dag)
            PythonOperator(task_id="b", python_callable=twice, dag=dag)
            """
        )
    )
    assert found["a"].submits == [] and found["b"].submits == []


def test_the_functions_tasks_run_that_are_not_read_are_counted():
    found = dags(
        _dag(
            """
            def local_noop():
                pass
            cb = local_noop
            PythonOperator(task_id="create", python_callable=tasks.create_cluster, dag=dag)
            PythonOperator(task_id="wait", python_callable=tasks.wait_for_cluster, dag=dag)
            PythonOperator(task_id="create2", python_callable=tasks.create_cluster, dag=dag)
            PythonOperator(task_id="alias", python_callable=cb, dag=dag)
            PythonOperator(task_id="read", python_callable=local_noop, dag=dag)
            PythonOperator(task_id="expr", python_callable=pick()[0], dag=dag)
            PythonOperator(task_id="helper", python_callable=tasks.run_stream_step, op_kwargs={"script_path": "s3://x.py"}, dag=dag)
            """
        )
    )
    assert found.unread_callables == {
        "acme_lib.emr_tasks.create_cluster": 2,
        "acme_lib.emr_tasks.wait_for_cluster": 1,
        "cb": 1,
    }


# ── other files of the scan ───────────────────────────────────────────


def _write(root: Path, files: Mapping[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")


DAG_IN_PACKAGE = """
from airflow import DAG
from airflow.operators.python import PythonOperator
from acme_lib import emr_tasks as tasks
from teamx.dags.nightly.settings import MAINTAIN_KWARGS
from teamx.dags.common.emr import submit
from teamx.dags.common import reexport

with DAG("nightly", schedule=None) as dag:
    PythonOperator(task_id="maintain", python_callable=tasks.run_step, op_kwargs=MAINTAIN_KWARGS)
    PythonOperator(task_id="shared", python_callable=submit, op_kwargs={"script": "s3://acme-code/jobs/shared.py"})
    PythonOperator(task_id="again", python_callable=reexport.submit, op_kwargs={"script": "s3://acme-code/jobs/again.py"})
"""


def test_settings_and_functions_of_other_files_of_the_scan(tmp_path):
    root = tmp_path / "teamx"
    _write(
        root,
        {
            "dags/nightly/dag.py": DAG_IN_PACKAGE,
            "dags/nightly/settings.py": """
                from airflow.models import Variable
                MAINTAIN_KWARGS = {
                    "step_name": "maintain",
                    "cluster": Variable.get("cluster_id", None),
                    "additional_params": {
                        "script_path": "s3://acme-code/jobs/maintain.py",
                        "config_path": "s3://acme-conf/maintain.yaml",
                    },
                }
            """,
            "dags/common/__init__.py": "",
            "dags/common/emr.py": """
                import boto3

                def submit(script, **kwargs):
                    boto3.client("emr").add_job_flow_steps(JobFlowId="j", Steps=[{"HadoopJarStep": {"Args": ["spark-submit", script]}}])
            """,
            "dags/common/reexport.py": "from teamx.dags.common.emr import submit\n",
        },
    )
    conventions = parse_conventions({"submit_helpers": HELPERS})
    scan = scan_dags(root / "dags", conventions)
    found = {t.task_id: t for d in scan.dags for t in d.tasks}
    assert [(s.script, s.config) for s in found["maintain"].submits] == [
        ("s3://acme-code/jobs/maintain.py", "s3://acme-conf/maintain.yaml")
    ]
    assert submits(found["shared"]) == [("emr-step", "s3://acme-code/jobs/shared.py", ())]
    assert submits(found["again"]) == [("emr-step", "s3://acme-code/jobs/again.py", ())]


def test_nothing_the_scan_reads_is_imported_or_run(tmp_path, monkeypatch):
    marker = tmp_path / "ran"
    _write(
        tmp_path,
        {
            "dags/dag.py": f"""
                from pathlib import Path
                Path({str(marker)!r}).write_text("module")
                from airflow import DAG
                from airflow.operators.python import PythonOperator
                from helpers import submit

                def run(**kwargs):
                    Path({str(marker)!r}).write_text("callable")
                    submit("s3://acme-code/jobs/a.py")

                with DAG("d", schedule=None) as dag:
                    PythonOperator(task_id="t", python_callable=run)
            """,
            "dags/helpers.py": f"""
                from pathlib import Path
                Path({str(marker)!r}).write_text("helper")
                import subprocess

                def submit(script):
                    subprocess.run(["spark-submit", script])
            """,
        },
    )
    monkeypatch.syspath_prepend(str(tmp_path / "dags"))
    scan = scan_dags(tmp_path / "dags", Conventions())
    (task,) = [t for d in scan.dags for t in d.tasks]
    assert submits(task) == [("spark-submit", "s3://acme-code/jobs/a.py", ())]
    assert not marker.exists()


# ── bounded ───────────────────────────────────────────────────────────


def test_a_function_many_tasks_run_with_the_same_arguments_is_read_once(monkeypatch):
    calls: List[int] = []
    original = Follower._scan

    def counting(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Follower, "_scan", counting)
    many = "\n".join(
        f'PythonOperator(task_id="t{i}", python_callable=submit, op_kwargs={{"script": "s3://a.py"}}, dag=dag)'
        for i in range(300)
    )
    found = tasks(
        _dag(
            """
def submit(script):
    boto3.client("emr").add_job_flow_steps(JobFlowId="j", Steps=[{"HadoopJarStep": {"Args": ["spark-submit", script]}}])
"""
            + many
        )
    )
    assert len(calls) == 1
    assert all(submits(t) == [("emr-step", "s3://a.py", ())] for t in found.values())
    assert {t.submits[0].line for t in found.values()} == {t.line for t in found.values()}


def test_many_tasks_with_their_own_arguments_cost_in_proportion():
    def source(count: int) -> str:
        many = "\n".join(
            f'PythonOperator(task_id="t{i}", python_callable=submit, op_kwargs={{"script": "s3://jobs/{i}.py"}}, dag=dag)'
            for i in range(count)
        )
        return _dag(
            """
def submit(script, **kwargs):
    client = boto3.client("emr")
    args = ["spark-submit", "--deploy-mode", "cluster", script, kwargs.get("day")]
    client.add_job_flow_steps(JobFlowId="j", Steps=[{"Name": "s", "HadoopJarStep": {"Jar": "command-runner.jar", "Args": args}}])
"""
            + many
        )

    timings = []
    for count in (250, 1_000):
        start = time.process_time()
        found = tasks(source(count))
        timings.append(time.process_time() - start)
        assert len(found) == count
        assert found[f"t{count - 1}"].submits[0].script == f"s3://jobs/{count - 1}.py"
    assert timings[1] < 15 * max(timings[0], 0.01), timings


def test_a_body_that_asks_for_too_much_work_is_cut_and_said_so(monkeypatch):
    monkeypatch.setattr(_callables, "MAX_VISITS", 500)
    body = "\n".join(f'    log("step {i}")' for i in range(2_000))
    found = dags(
        _dag(
            "def huge(**kwargs):\n"
            + body
            + '\n    subprocess.run(["spark-submit", "s3://last.py"])\n'
            + 'PythonOperator(task_id="t", python_callable=huge, dag=dag)\n'
        )
    )
    (task,) = [t for d in found.dags for t in d.tasks]
    assert task.submits == []
    assert any("stopped following the functions its Python tasks run" in n for n in found.notes)


def test_a_wide_and_deep_call_graph_is_read_in_bounded_time():
    # each function calls the next ten: a tree of 10**4 calls if nothing stopped it
    functions = "\n".join(
        f"def f{i}(script):\n" + "".join(f"    f{i + 1}(script)\n" for _ in range(10))
        for i in range(30)
    )
    many = "\n".join(
        f'PythonOperator(task_id="t{i}", python_callable=f0, op_kwargs={{"script": "s3://{i}.py"}}, dag=dag)'
        for i in range(200)
    )
    start = time.process_time()
    found = tasks(_dag(functions + "\ndef f30(script):\n    pass\n" + many))
    assert time.process_time() - start < 10
    assert len(found) == 200


def _shared_helper_project(root: Path, files: int, per_file: int) -> None:
    sources = {
        "common/__init__.py": "",
        "common/emr.py": """
            import boto3

            def submit(script, conf="a=b", **kwargs):
                args = ["spark-submit", "--conf", conf, "--deploy-mode", "cluster", script]
                steps = [{"Name": "s", "HadoopJarStep": {"Jar": "command-runner.jar", "Args": args}}]
                boto3.client("emr").add_job_flow_steps(JobFlowId="j", Steps=steps)
        """,
    }
    for f in range(files):
        tasks_ = "\n".join(
            f'    PythonOperator(task_id="t{i}", python_callable=submit, op_kwargs={{"script": "s3://jobs/{f}_{i}.py"}})'
            for i in range(per_file)
        )
        sources[f"d{f}/dag.py"] = (
            "from airflow import DAG\n"
            "from airflow.operators.python import PythonOperator\n"
            "from common.emr import submit\n"
            f"with DAG('d{f}', schedule=None) as dag:\n{tasks_}\n"
        )
    _write(root, sources)


def test_a_module_the_tasks_of_many_files_reach_does_not_run_out_for_the_last(
    tmp_path, monkeypatch
):
    from fluid_build.cli.import_workflow import _fold

    # one file's tasks fit the module's budget; thirty files' do not
    monkeypatch.setattr(_fold, "MAX_STEPS_PER_MODULE", 3_000)
    _shared_helper_project(tmp_path, files=30, per_file=10)
    scan = scan_dags(tmp_path, Conventions())
    scripts = [s.script for d in scan.dags for t in d.tasks for s in t.submits]
    assert len(scripts) == 300 and None not in scripts
    assert not any("folding stopped early" in n for n in scan.notes)


def test_what_one_file_may_spend_in_other_modules_is_bounded_and_said(tmp_path, monkeypatch):
    monkeypatch.setattr(_callables, "MAX_STEPS_PER_MODULE", 200)
    _shared_helper_project(tmp_path, files=1, per_file=40)
    scan = scan_dags(tmp_path, Conventions())
    found = [t.submits for d in scan.dags for t in d.tasks]
    read = [s for s in found if s]
    assert 0 < len(read) < 40
    assert any("stopped following the functions its Python tasks run" in n for n in scan.notes)


def test_a_command_line_many_tasks_run_is_read_once(monkeypatch):
    calls: List[int] = []
    original = _callables.spark_submits_in_text

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(_callables, "spark_submits_in_text", counting)
    many = "\n".join(
        f'PythonOperator(task_id="t{i}", python_callable=run, op_kwargs={{"day": "d{i}"}}, dag=dag)'
        for i in range(100)
    )
    found = tasks(
        _dag(
            """
import os
CMD = "spark-submit --deploy-mode cluster s3://acme-code/jobs/a.py --full"

def run(day, **kwargs):
    os.system(CMD)
"""
            + many
        )
    )
    assert all(
        submits(t) == [("spark-submit", "s3://acme-code/jobs/a.py", ("--full",))]
        for t in found.values()
    )
    assert len(calls) == 1


def test_a_large_set_of_arguments_is_read_on_its_own_not_kept(monkeypatch):
    from fluid_build.cli.import_workflow._callables import _Arguments, _key

    assert _key(_Arguments(keywords={f"k{i}": "v" for i in range(10)})) is not None
    assert _key(_Arguments(keywords={f"k{i}": "v" for i in range(300)})) is None
    assert _key(_Arguments(keywords={"k": ["v"] * 300})) is None
    assert _key(_Arguments(keywords={"k": "v" * 30_000})) is None


# ── the import, end to end ────────────────────────────────────────────


PROJECT = {
    "dags/nightly/dag.py": """
        import boto3
        from airflow import DAG
        from airflow.models import Variable
        from airflow.operators.python import PythonOperator
        from acme_lib import emr_tasks as tasks
        from acme_cfg.paths import JOBS_ROOT
        from teamx.dags.nightly.settings import ORDERS_KWARGS

        CODE = "s3://acme-code/jobs"

        def submit_customers(**kwargs):
            boto3.client("emr").add_job_flow_steps(
                JobFlowId=kwargs["ti"].xcom_pull(task_ids="create_cluster"),
                Steps=[{"Name": "c", "HadoopJarStep": {"Jar": "command-runner.jar", "Args": ["spark-submit", f"{CODE}/customers.py", Variable.get("run_day")]}}],
            )

        with DAG("nightly", schedule="0 3 * * *", default_args={"owner": "retail-data"}) as dag:
            create = PythonOperator(task_id="create_cluster", python_callable=tasks.create_cluster)
            orders = PythonOperator(task_id="orders", python_callable=tasks.run_step, op_kwargs=ORDERS_KWARGS)
            customers = PythonOperator(task_id="customers", python_callable=submit_customers)
            stream = PythonOperator(
                task_id="clicks",
                python_callable=tasks.run_stream_step,
                op_kwargs={"script_path": f"{JOBS_ROOT}/clicks.py", "config_bucket": "acme-conf", "config_key": "clicks.yaml"},
            )
            create >> [orders, customers, stream]
    """,
    "dags/nightly/settings.py": """
        ORDERS_KWARGS = {
            "step_name": "orders",
            "additional_params": {
                "script_path": "s3://acme-code/jobs/orders.py",
                "config_path": "s3://acme-conf/orders.yaml",
                "job_args": ["--mode", "full"],
            },
        }
    """,
    "jobs/orders.py": """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        spark.table("retail.orders_raw").write.mode("overwrite").format("iceberg").saveAsTable("retail.orders")
    """,
    "jobs/customers.py": """
        from pyspark.sql import SparkSession
        spark = SparkSession.builder.getOrCreate()
        spark.table("retail.customers_raw").write.mode("overwrite").format("iceberg").saveAsTable("retail.customers")
    """,
    "conventions.yaml": """
        script_roots:
          "s3://acme-code/jobs/": jobs
        submit_helpers:
          run_stream_step:
            script: script_path
            config: "s3://{config_bucket}/{config_key}"
            kind: emr_add_steps
          run_step:
            script: additional_params.script_path
            config: additional_params.config_path
            args: additional_params.job_args
            kind: emr_add_steps
        defaults: {domain: retail}
        owners:
          retail-data: {team: retail-data}
    """,
}


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    _write(tmp_path, PROJECT)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _valid(contract: Dict[str, Any]) -> None:
    result = FluidSchemaManager().validate_contract(
        contract, schema_version=contract["fluidVersion"], offline_only=True
    )
    assert result.is_valid, result.errors


def test_fluid_import_airflow_proposes_the_jobs_python_tasks_submit(project):
    contracts, report = AirflowImporter().import_to_contracts(
        "dags", options={"conventions": "conventions.yaml"}
    )
    products = {c["id"]: c for c in contracts}
    assert set(products) == {"retail.orders", "retail.customers"}
    for contract in contracts:
        _valid(contract)
    build = products["retail.orders"]["builds"][0]
    assert build["execution"]["runtime"] == {"platform": "aws"}
    airflow = build["execution"]["orchestration"]["airflow"]
    (task,) = airflow["tasks"]
    assert task["taskId"] == "orders" and task["operator"] == "PythonOperator"
    assert task["params"] == {
        "runtime": "emr-step",
        "script": "s3://acme-code/jobs/orders.py",
        "args": ["--mode", "full"],
        "python_callable": "acme_lib.emr_tasks.run_step",
        "config": "s3://acme-conf/orders.yaml",
    }
    (customers,) = products["retail.customers"]["builds"][0]["execution"]["orchestration"][
        "airflow"
    ]["tasks"]
    assert customers["params"]["python_callable"] == "submit_customers"
    assert "config" not in customers["params"]
    notes = "\n".join(report.notes)
    assert "4 task(s), 3 submit a script" in notes
    assert "2 submitted script(s) joined to a job file (2 by script_root); 1 not joined" in notes
    assert (
        "1 Python task(s) run a function that is not read here: acme_lib.emr_tasks.create_cluster (1)"
        in notes
    )
    assert "`submit_helpers`" in notes
    defaults = "\n".join(report.required_defaults)
    assert (
        "1 task(s) submit a script or name a config file that depends on "
        "`acme_cfg.paths.JOBS_ROOT`, which is not in the scanned files (DAG nightly task clicks): "
        "name the file of its module under `config_modules` in the conventions"
    ) in defaults
    unsupported = "\n".join(report.unsupported)
    assert "task clicks" in unsupported and "not joined" in unsupported


def test_a_config_module_and_a_variable_the_conventions_give_complete_the_scripts(project):
    (project / "libs").mkdir()
    (project / "libs" / "paths.py").write_text('JOBS_ROOT = "s3://acme-code/jobs"\n', "utf-8")
    (project / "jobs" / "clicks.py").write_text(
        textwrap.dedent(
            """
            from pyspark.sql import SparkSession
            spark = SparkSession.builder.getOrCreate()
            (spark.readStream.format("kafka").option("subscribe", "clicks_raw").load()
                .writeStream.format("kafka").option("topic", "clicks").option("checkpointLocation", "s3://acme-state/clicks").start())
            """
        ),
        "utf-8",
    )
    conventions = yaml.safe_load((project / "conventions.yaml").read_text("utf-8"))
    conventions["config_modules"] = [{"module": "acme_cfg.paths", "path": "libs/paths.py"}]
    (project / "conventions.yaml").write_text(yaml.safe_dump(conventions), "utf-8")
    contracts, report = AirflowImporter().import_to_contracts(
        "dags", options={"conventions": "conventions.yaml"}
    )
    assert {c["id"] for c in contracts} == {"retail.orders", "retail.customers", "clicks"}
    for contract in contracts:
        _valid(contract)
    assert not [x for x in report.required_defaults if "depends on" in x]
    assert (
        "3 submitted script(s) joined to a job file (3 by script_root); 0 not joined"
        in "\n".join(report.notes)
    )


def test_the_values_the_scripts_wait_for_are_listed_once_with_the_tasks_and_the_way_out(tmp_path):
    many = "\n".join(
        f'    PythonOperator(task_id="t{i}", python_callable=run, op_kwargs={{"script": f"{{ROOT}}/{i}.py", "conf": os.environ["CONF_{i % 2}"]}})'
        for i in range(6)
    ) + (
        '\n    PythonOperator(task_id="s", python_callable=run_secret, op_kwargs={"script": "s3://a.py", "secret": Variable.get("db_password")})'
    )
    _write(
        tmp_path,
        {
            "dags/dag.py": "import os\n"
            "from airflow import DAG\n"
            "from airflow.models import Variable\n"
            "from airflow.operators.python import PythonOperator\n"
            "from acme_cfg import ROOT\n"
            "with DAG('d', schedule=None) as dag:\n" + many + "\n",
        },
    )
    conventions = parse_conventions(
        {
            "submit_helpers": {
                "run": {"script": "script", "config": "{conf}/x.yaml"},
                "run_secret": {"script": "script", "config": "{secret}"},
            }
        }
    )
    from fluid_build.cli.import_workflow._pipeline import _report_waiting
    from fluid_build.cli.import_workflow.registry import ImportReport

    report = ImportReport()
    _report_waiting(scan_dags(tmp_path / "dags", conventions), report)
    assert report.required_defaults == [
        "6 task(s) submit a script or name a config file that depends on `acme_cfg.ROOT`, which is "
        "not in the scanned files (DAG d task t0, DAG d task t1, DAG d task t2, and 3 more): name "
        "the file of its module under `config_modules` in the conventions",
        "3 task(s) submit a script or name a config file that depends on the environment "
        "variable CONF_0 (DAG d task t0, DAG d task t2, DAG d task t4): set what it holds under "
        "`environments` in the conventions",
        "3 task(s) submit a script or name a config file that depends on the environment "
        "variable CONF_1 (DAG d task t1, DAG d task t3, DAG d task t5): set what it holds under "
        "`environments` in the conventions",
    ]  # the Variable whose name says it is a credential is not one to set in the conventions


def test_the_waiting_values_listed_are_capped(tmp_path):
    from fluid_build.cli.import_workflow import _pipeline
    from fluid_build.cli.import_workflow.registry import ImportReport

    many = "\n".join(
        f'    PythonOperator(task_id="t{i}", python_callable=run, op_kwargs={{"script": os.environ["S_{i}"]}})'
        for i in range(_pipeline.MAX_WAITING_LINES + 5)
    )
    _write(
        tmp_path,
        {
            "dags/dag.py": "import os\nfrom airflow import DAG\n"
            "from airflow.operators.python import PythonOperator\n"
            "with DAG('d', schedule=None) as dag:\n" + many + "\n"
        },
    )
    conventions = parse_conventions({"submit_helpers": {"run": {"script": "script"}}})
    report = ImportReport()
    _pipeline._report_waiting(scan_dags(tmp_path / "dags", conventions), report)
    assert len(report.required_defaults) == _pipeline.MAX_WAITING_LINES + 1
    assert report.required_defaults[-1] == (
        "5 more value(s) that submitted scripts or config files depend on are not listed"
    )


def test_the_skill_example_conventions_describe_a_submit_helper():
    from fluid_build.cli.import_workflow import _conventions

    example = (
        Path(_conventions.__file__).parents[2]
        / "agent_skills"
        / "skills"
        / "adopting-existing-pipelines"
        / "references"
        / "conventions.example.yaml"
    )
    document = yaml.safe_load(example.read_text("utf-8"))
    assert "submit_helpers" in document
