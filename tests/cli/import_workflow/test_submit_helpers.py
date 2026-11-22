# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""Spark work a DAG submits through a Python task's function, read statically.

A team that runs its Spark jobs from Airflow often submits them from a ``PythonOperator``: a
function of its own EMR library (named in the conventions under ``submit_helpers``), or a function
of the DAG file that calls EMR itself. Nothing here is imported or run.
"""

from __future__ import annotations

import ast
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

            def imported():
                boto3.client("emr").add_job_flow_steps(JobFlowId="j", Steps=[{"HadoopJarStep": {"Args": ["spark-submit", "s3://c.py"]}}])
            from acme_lib.emr_tasks import imported
            imported = PythonOperator(task_id="c", python_callable=imported, dag=dag)

            PythonOperator(task_id="a", python_callable=submit, dag=dag)
            PythonOperator(task_id="b", python_callable=twice, dag=dag)
            """
        )
    )
    # `imported` is the library's function by the time the task is made, not the def above it
    assert found["a"].submits == [] and found["b"].submits == [] and found["c"].submits == []


def test_a_function_whose_name_a_task_takes_later_is_the_function_until_then():
    found = dags(
        _dag(
            """
            import subprocess

            early = PythonOperator(task_id="early", python_callable=extract, dag=dag)

            def extract(**kwargs):
                subprocess.run(["spark-submit", "s3://jobs/extract.py"])

            def load(**kwargs):
                subprocess.run(["spark-submit", "s3://jobs/load.py"])

            first = PythonOperator(task_id="first", python_callable=extract, dag=dag)
            extract = PythonOperator(task_id="extract", python_callable=extract, dag=dag)
            later = PythonOperator(task_id="later", python_callable=extract, dag=dag)
            late = PythonOperator(task_id="late", python_callable=lambda: extract(), dag=dag)
            load = PythonOperator(task_id="load", python_callable=lambda: load(), dag=dag)
            first >> extract >> later
            """
        )
    )
    (dag,) = found.dags
    by_id = {t.task_id: t for t in dag.tasks}
    for task_id in ("first", "extract"):  # before the assignment, and in its own value
        assert [s.script for s in by_id[task_id].submits] == ["s3://jobs/extract.py"]
        assert by_id[task_id].callable == "extract"
    # past it the name is the task, and a lambda runs when it already is, even one written in
    # that value; before the def, the name is nothing yet
    for task_id in ("later", "late", "load", "early"):
        assert by_id[task_id].submits == [], task_id
    assert found.unread_callables == {"extract": 2}
    keys = {t.task_id: t.key for t in dag.tasks}
    assert set(dag.edges) == {(keys["first"], keys["extract"]), (keys["extract"], keys["later"])}


def test_a_function_a_task_in_a_loop_takes_the_name_of_is_not_taken_for_it():
    found = tasks(
        _dag(
            """
            import subprocess

            def extract(**kwargs):
                subprocess.run(["spark-submit", "s3://jobs/extract.py"])

            for name in ("a", "b"):
                print(name)
            for day in ("mon", "tue"):
                extract = PythonOperator(task_id=f"extract_{day}", python_callable=extract, dag=dag)
            """
        )
    )
    # the second time round, `extract` is the first task: neither is said to run the function
    assert all(t.submits == [] for t in found.values())


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


def test_a_dotted_helper_name_matches_whole_parts_only():
    found = tasks(
        _dag(
            """
            import acme_lib.myemr as other
            import acme_lib.emr as emr
            PythonOperator(task_id="near", python_callable=other.run_step, op_kwargs={"script_path": "s3://n.py"}, dag=dag)
            PythonOperator(task_id="exact", python_callable=emr.run_step, op_kwargs={"script_path": "s3://e.py"}, dag=dag)
            """
        ),
        {"emr.run_step": {"script": "script_path"}},
    )
    assert found["near"].submits == []  # `myemr` is not `emr`
    assert [s.script for s in found["exact"].submits] == ["s3://e.py"]


def test_nested_partials_put_the_inner_arguments_first():
    found = tasks(
        _dag(
            """
            from functools import partial
            PythonOperator(task_id="t", python_callable=partial(partial(tasks.run_positional, "s3://a.py"), ["--x"]), dag=dag)
            """
        )
    )
    assert submits(found["t"]) == [("spark-submit", "s3://a.py", ("--x",))]


def test_a_function_that_calls_itself_back_is_read_once():
    found = tasks(
        _dag(
            """
            import subprocess

            def f(script):
                boto3.client("emr").add_job_flow_steps(JobFlowId="j", Steps=[{"HadoopJarStep": {"Args": ["spark-submit", script]}}])
                g(script)

            def g(script):
                f(script)

            PythonOperator(task_id="t", python_callable=f, op_kwargs={"script": "s3://a.py"}, dag=dag)
            """
        )
    )
    assert submits(found["t"]) == [("emr-step", "s3://a.py", ())]


def test_op_args_with_a_spread_that_does_not_fold_give_no_positions():
    found = tasks(
        _dag(
            """
            import acme_cfg
            import subprocess

            def go(script):
                subprocess.run(["spark-submit", script])

            PythonOperator(task_id="helper", python_callable=tasks.run_positional, op_args=[*acme_cfg.ARGS, "y.py"], dag=dag)
            PythonOperator(task_id="function", python_callable=go, op_args=[*acme_cfg.ARGS, "y.py"], dag=dag)
            """
        )
    )
    # at run time the script is the spread's first item, not "y.py"
    (helper,) = found["helper"].submits
    assert helper.script is None and helper.why == "op_args is not all known"
    (function,) = found["function"].submits
    assert function.script is None and "is a parameter" in function.why


def test_a_key_of_op_kwargs_a_later_spread_may_replace_is_not_known():
    found = tasks(
        _dag(
            """
            import acme_cfg
            PythonOperator(task_id="before", python_callable=tasks.run_stream_step, op_kwargs={"script_path": "s3://a.py", **acme_cfg.KW}, dag=dag)
            PythonOperator(task_id="after", python_callable=tasks.run_stream_step, op_kwargs={**acme_cfg.KW, "script_path": "s3://b.py"}, dag=dag)
            """
        )
    )
    assert [s.script for s in found["before"].submits] == [None]
    assert [s.script for s in found["after"].submits] == ["s3://b.py"]


def test_a_function_named_like_an_emr_method_but_called_otherwise_is_followed(tmp_path):
    _write(
        tmp_path,
        {
            "dags/emr_lib.py": """
                import subprocess

                def submit_job(script):
                    subprocess.run(["spark-submit", script])

                def add_job_flow_steps(script, cluster=None):
                    subprocess.run(["spark-submit", "--deploy-mode", "cluster", script])
            """,
            "dags/dag.py": """
                from airflow import DAG
                from airflow.operators.python import PythonOperator
                import emr_lib

                def go():
                    emr_lib.submit_job(script="s3://acme-code/jobs/a.py")
                    emr_lib.add_job_flow_steps("s3://acme-code/jobs/b.py")

                with DAG("d", schedule=None) as dag:
                    PythonOperator(task_id="t", python_callable=go)
            """,
        },
    )
    scan = scan_dags(tmp_path / "dags", Conventions())
    (task,) = [t for d in scan.dags for t in d.tasks]
    assert submits(task) == [
        ("spark-submit", "s3://acme-code/jobs/a.py", ()),
        ("spark-submit", "s3://acme-code/jobs/b.py", ()),
    ]


def test_a_script_that_is_a_number_is_not_a_script():
    found = tasks(
        _dag(
            """
            PythonOperator(task_id="t", python_callable=tasks.run_positional, op_args=[7], dag=dag)
            PythonOperator(task_id="u", python_callable=tasks.run_stream_step, op_kwargs={"script_path": "s3://a.py", "config_bucket": "b", "config_key": 7}, dag=dag)
            """
        )
    )
    (t,) = found["t"].submits
    assert t.script is None and t.why == "it is not text"
    (u,) = found["u"].submits
    assert u.config == "s3://b/7"  # in a text, a number is written out as an f-string would


def test_a_list_that_is_a_piece_of_a_command_is_not_a_submit_of_its_own():
    found = tasks(
        _dag(
            """
            import subprocess

            def go(script):
                cmd = ["spark-submit", "--master", "yarn"]
                cmd += ["--deploy-mode", "cluster", script]
                subprocess.run(cmd)

            PythonOperator(task_id="t", python_callable=go, op_kwargs={"script": "s3://a.py"}, dag=dag)
            """
        )
    )
    assert submits(found["t"]) == [("spark-submit", "s3://a.py", ())]


def test_a_lambda_given_the_tasks_arguments():
    found = tasks(
        _dag(
            """
            import subprocess

            def go(script):
                subprocess.run(["spark-submit", script])

            PythonOperator(task_id="t", python_callable=lambda script, **ctx: go(script), op_args=["s3://a.py"], dag=dag)
            """
        )
    )
    assert submits(found["t"]) == [("spark-submit", "s3://a.py", ())]


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


def test_many_tasks_with_their_own_arguments_cost_in_proportion(monkeypatch):
    # counted, not timed: the folds and the visits a file's reading makes grow with its tasks
    # (a busy CI runner's clock does not say that)
    work = {"folds": 0, "visits": 0}
    fold, charge = Folder.fold, Follower._charge

    def counted_fold(self: Folder, *args: Any, **kwargs: Any) -> Any:
        work["folds"] += 1
        return fold(self, *args, **kwargs)

    def counted_charge(self: Follower, visits: int) -> None:
        work["visits"] += visits
        charge(self, visits)

    monkeypatch.setattr(Folder, "fold", counted_fold)
    monkeypatch.setattr(Follower, "_charge", counted_charge)

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

    counts = []
    for count in (250, 1_000):
        work.update(folds=0, visits=0)
        found = tasks(source(count))
        counts.append(dict(work))
        assert len(found) == count
        assert found[f"t{count - 1}"].submits[0].script == f"s3://jobs/{count - 1}.py"
    for kind in ("folds", "visits"):  # four times the tasks
        assert 0 < counts[1][kind] <= 4.5 * counts[0][kind], counts


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


def test_the_note_on_unread_functions_does_not_offer_submit_helpers_for_the_standard_library():
    from fluid_build.cli.import_workflow._pipeline import _report_unread_callables
    from fluid_build.cli.import_workflow._scan import DagScan
    from fluid_build.cli.import_workflow.registry import ImportReport

    def note(unread: Dict[str, int]) -> str:
        report = ImportReport()
        _report_unread_callables(DagScan(root=Path("."), unread_callables=unread), report)
        (only,) = report.notes
        return only

    mixed = note({"subprocess.run": 2, "acme_lib.emr_tasks.start_cluster": 1, "os.system": 1})
    assert mixed == (
        "4 Python task(s) run a function that is not read here: subprocess.run (2), "
        + "acme_lib.emr_tasks.start_cluster (1), os.system (1). Name one that submits Spark "
        + "work under `submit_helpers` in the conventions, with where its script is among its "
        + "arguments; subprocess.run, os.system are the standard library's, Airflow's or "
        + "PySpark's, which `submit_helpers` does not take: call it from a function of the DAG "
        + "files, and what that function runs is read"
    )
    library = note({"subprocess.run": 1})
    assert "Name one that submits" not in library
    assert library.endswith(
        "subprocess.run (1). subprocess.run is the standard library's, Airflow's or PySpark's, "
        + "which `submit_helpers` does not take: call it from a function of the DAG files, and "
        + "what that function runs is read"
    )
    # a function of the project named like a module of the standard library is the project's
    assert "Name one that submits" in note({"queue": 1})


def test_the_dag_files_of_a_scan_share_one_follow_budget_and_it_is_said_once(tmp_path, monkeypatch):
    # each file's tasks fit the file's own budget; thirty files' do not fit the scan's
    monkeypatch.setattr(_callables, "MAX_VISITS_PER_SCAN", 2_000)
    monkeypatch.setattr(_callables, "VISITS_PER_TASK", 0)
    spent = [0]
    charge = Follower._charge

    def counted(self: Follower, visits: int) -> None:
        spent[0] += visits
        charge(self, visits)

    monkeypatch.setattr(Follower, "_charge", counted)
    _shared_helper_project(tmp_path, files=30, per_file=10)
    scan = scan_dags(tmp_path, Conventions())
    read = [t for d in scan.dags for t in d.tasks if t.submits]
    assert 0 < len(read) < 300
    # past the budget nothing more is spent but the one charge that crossed it
    assert 2_000 <= spent[0] < 2_000 + 10
    cut = {d.file for d in scan.dags if any(not t.submits for t in d.tasks)}
    (note,) = [n for n in scan.notes if "scan fewer DAG files at a time" in n]
    assert f"what the tasks of {len(cut)} DAG file(s) submit is not all read" in note
    assert not any("stopped following the functions its Python tasks run" in n for n in scan.notes)


def test_each_python_task_followed_brings_its_share_of_the_scans_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(_callables, "MAX_VISITS_PER_SCAN", 2_000)
    monkeypatch.setattr(_callables, "VISITS_PER_TASK", 100)
    spent = [0]
    charge = Follower._charge

    def counted(self: Follower, visits: int) -> None:
        spent[0] += visits
        charge(self, visits)

    monkeypatch.setattr(Follower, "_charge", counted)
    # many light tasks: each takes less than its share, and all are read
    _shared_helper_project(tmp_path / "light", files=30, per_file=10)
    scan = scan_dags(tmp_path / "light", Conventions())
    assert all(t.submits for d in scan.dags for t in d.tasks)
    assert not [n for n in scan.notes if "scan fewer DAG files at a time" in n]
    # one-task files that each run a heavy function: they spend no more than their shares
    heavy = "".join(f"    print('step {i}')\n" for i in range(2_000))
    function = "import subprocess\n\ndef submit(script, **kwargs):\n" + heavy
    _same_task_in_each(tmp_path / "heavy", 30, function)
    spent[0] = 0
    scan = scan_dags(tmp_path / "heavy", Conventions())
    assert spent[0] < 2_000 + 30 * 100 + 10
    (note,) = [n for n in scan.notes if "scan fewer DAG files at a time" in n]
    assert "had spent 5,000 visits of their bodies" in note


_EMR_SUBMIT = """
import boto3

def submit(script, **kwargs):
    args = ["spark-submit", "--deploy-mode", "cluster", script]
    boto3.client("emr").add_job_flow_steps(JobFlowId="j", Steps=[{"HadoopJarStep": {"Args": args}}])
"""


def _same_task_in_each(root: Path, files: int, function: str) -> None:
    sources = {"common/__init__.py": "", "common/emr.py": function}
    for f in range(files):
        sources[f"d{f}/dag.py"] = (
            "from airflow import DAG\n"
            "from airflow.operators.python import PythonOperator\n"
            "from common.emr import submit\n"
            f"with DAG('d{f}', schedule=None) as dag:\n"
            "    PythonOperator(task_id='t', python_callable=submit, "
            "op_kwargs={'script': 's3://jobs/same.py'})\n"
        )
    _write(root, sources)


def test_a_function_the_tasks_of_many_files_run_alike_is_read_once(tmp_path, monkeypatch):
    reads: List[str] = []
    read_body = Follower._read_body

    def counted(self: Follower, function: Any, *args: Any) -> Any:
        reads.append(function.name)
        return read_body(self, function, *args)

    monkeypatch.setattr(Follower, "_read_body", counted)
    _same_task_in_each(tmp_path, 3, _EMR_SUBMIT)
    scan = scan_dags(tmp_path, Conventions())
    scripts = [s.script for d in scan.dags for t in d.tasks for s in t.submits]
    assert scripts == ["s3://jobs/same.py"] * 3
    assert reads == ["submit"]


def test_a_dag_file_and_a_module_shown_by_the_same_path_are_kept_apart(tmp_path, monkeypatch):
    # run from above the scan, the DAG file dags/x.py is shown as `dags/x.py`, which is also the
    # path the scan knows dags/dags/x.py by: what one's function submitted is not the other's
    head = "import subprocess\nfrom airflow import DAG\nfrom airflow.operators.python import PythonOperator\n"
    _write(
        tmp_path,
        {
            "dags/x.py": head
            + "def f(script):\n    subprocess.run(['spark-submit', 's3://a/' + script])\n"
            + "with DAG('a', schedule=None) as dag:\n"
            + "    PythonOperator(task_id='t', python_callable=f, op_args=['one.py'])\n",
            "dags/dags/x.py": head
            + "def f(script):\n    subprocess.run(['spark-submit', 's3://b/' + script])\n",
            "dags/y.py": head
            + "from dags.x import f\n"
            + "with DAG('b', schedule=None) as dag:\n"
            + "    PythonOperator(task_id='t', python_callable=f, op_args=['one.py'])\n",
        },
    )
    monkeypatch.chdir(tmp_path)
    scan = scan_dags(Path("dags"), Conventions())
    scripts = {d.dag_id: [s.script for t in d.tasks for s in t.submits] for d in scan.dags}
    assert scripts == {"a": ["s3://a/one.py"], "b": ["s3://b/one.py"]}


def test_what_a_file_that_ran_out_read_of_a_function_is_not_handed_to_the_next(
    tmp_path, monkeypatch
):
    # the first file runs out partway through the function; the next one, with room to spare,
    # reads it whole instead of taking the first one's part for the whole
    prints = "".join(f"    print('step {i}')\n" for i in range(30))
    function = (
        "import subprocess\n\ndef submit(script, **kwargs):\n"
        + prints
        + "    subprocess.run(['spark-submit', script])\n"
    )
    _same_task_in_each(tmp_path, 2, function)
    made: List[Follower] = []
    init = Follower.__init__

    def small_first(self: Follower, *args: Any, **kwargs: Any) -> None:
        init(self, *args, **kwargs)
        if not made:
            self.visits_left = 60
        made.append(self)

    monkeypatch.setattr(Follower, "__init__", small_first)
    scan = scan_dags(tmp_path, Conventions())
    by_file = {
        Path(d.file).parent.name: [s.script for t in d.tasks for s in t.submits] for d in scan.dags
    }
    assert by_file == {"d0": [], "d1": ["s3://jobs/same.py"]}
    (note,) = [n for n in scan.notes if "stopped following the functions its" in n]
    assert note.startswith("d0/dag.py: ")


def test_a_command_line_is_charged_by_its_length(monkeypatch):
    monkeypatch.setattr(_callables, "MAX_VISITS", 300)
    heavy = "spark-submit " + " ".join(f"--conf k{i}=v" for i in range(100)) + " s3://jobs/b.py"
    found = dags(
        _dag(
            f"""
import os

def light(**kwargs):
    os.system("spark-submit s3://jobs/a.py")

def heavy(**kwargs):
    os.system("{heavy}")

PythonOperator(task_id="light", python_callable=light, dag=dag)
PythonOperator(task_id="heavy", python_callable=heavy, dag=dag)
"""
        )
    )
    by_id = {t.task_id: t for d in found.dags for t in d.tasks}
    assert [s.script for s in by_id["light"].submits] == ["s3://jobs/a.py"]
    # about 600 visits' worth of command line, with 300 to spend in the file
    assert by_id["heavy"].submits == []
    assert any("stopped following the functions its Python tasks run" in n for n in found.notes)


def test_an_emr_step_a_function_builds_is_that_functions_step():
    found = tasks(
        _dag(
            """
            import os

            def _steps(script, args):
                return [{"Name": "run", "HadoopJarStep": {"Jar": "command-runner.jar", "Args": ["spark-submit", "--deploy-mode", "cluster", script, *args]}}]

            def submit(script, args=(), **kwargs):
                client = boto3.client("emr")
                client.add_job_flow_steps(JobFlowId="j", Steps=_steps(script, args))

            def unread(script, **kwargs):
                boto3.client("emr").add_job_flow_steps(JobFlowId="j", Steps=tasks.build_steps(script))
                os.system("spark-submit s3://jobs/c.py")

            PythonOperator(task_id="built", python_callable=submit, op_kwargs={"script": "s3://jobs/a.py", "args": ["--day", "1"]}, dag=dag)
            PythonOperator(task_id="unread", python_callable=unread, op_kwargs={"script": "s3://jobs/b.py"}, dag=dag)
            """
        )
    )
    # the step the function builds is the step, submitted to EMR: no step of unknown script
    assert submits(found["built"]) == [("emr-step", "s3://jobs/a.py", ("--day", "1"))]
    # a step a function that is not read builds stays one whose script is not known, and a
    # command run after the call is not taken for it
    step, command = found["unread"].submits
    assert step.runtime == "emr-step" and step.script is None
    assert (command.runtime, command.script) == ("spark-submit", "s3://jobs/c.py")


def test_a_helper_never_takes_a_name_of_the_standard_library():
    found = tasks(
        _dag(
            """
            import os
            import subprocess

            def go(**kwargs):
                subprocess.run(["spark-submit", "s3://jobs/run.py"])
                os.system("spark-submit s3://jobs/system.py")

            PythonOperator(task_id="t", python_callable=go, dag=dag)
            PythonOperator(task_id="s", python_callable=os.system, op_args=["s3://jobs/s.py"], dag=dag)
            PythonOperator(task_id="h", python_callable=tasks.run, op_kwargs={"script": "s3://jobs/h.py"}, dag=dag)
            """
        ),
        {"run": {"script": "script"}, "system": {"script": 0}},
    )
    # the commands are read as the commands they are, not as the helpers `run` and `system`
    assert sorted(s.script for s in found["t"].submits) == [
        "s3://jobs/run.py",
        "s3://jobs/system.py",
    ]
    assert found["s"].submits == []
    assert [s.script for s in found["h"].submits] == ["s3://jobs/h.py"]


def test_the_note_on_a_module_that_ran_out_names_the_file_a_person_opens(tmp_path, monkeypatch):
    from fluid_build.cli.import_workflow import _fold

    monkeypatch.setattr(_fold, "MAX_STEPS_PER_MODULE", 12)
    _shared_helper_project(tmp_path / "proj" / "dags", files=1, per_file=5)
    monkeypatch.chdir(tmp_path / "proj")
    scan = scan_dags(Path("dags"), Conventions())
    tired = [n for n in scan.notes if "while the functions DAG tasks run were read" in n]
    assert tired == [tired[0]] and tired[0].startswith("dags/common/emr.py: ")


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


def test_the_parameters_of_every_call_are_charged_and_the_defaults_folded_once():
    params = ", ".join(f"a{i}=1" for i in range(3_000))
    calls = "".join("    g()\n" for _ in range(3_000))
    start = time.process_time()
    found = dags(
        _dag(
            f"def g({params}):\n    pass\n"
            "def f():\n" + calls + 'PythonOperator(task_id="t", python_callable=f, dag=dag)\n'
        )
    )
    assert time.process_time() - start < 10
    assert any("stopped following the functions its Python tasks run" in n for n in found.notes)


def test_a_heavy_function_does_not_take_the_folding_the_files_operators_need():
    chain = "    a0 = p\n" + "".join(
        f"    a{i} = {{'x': a{i - 1}, 'y': a{i - 1}}}\n" for i in range(1, 26)
    )
    heavy = "import subprocess\ndef f(p):\n" + chain + "    subprocess.run(a25)\n" * 100
    tasks_ = "".join(
        f"    PythonOperator(task_id='t{i}', python_callable=f, op_kwargs={{'p': 'v{i}'}})\n"
        for i in range(60)
    )
    found = dags(
        _dag(
            "from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator\n"
            "BASE = 's3://acme-code/jobs'\n"
            + heavy
            + "with dag:\n"
            + tasks_
            + "    SparkSubmitOperator(task_id='last', application=BASE + '/job.py')\n"
        )
    )
    last = [t for d in found.dags for t in d.tasks if t.task_id == "last"]
    assert [s.script for s in last[0].submits] == ["s3://acme-code/jobs/job.py"]


def test_another_files_function_syntax_is_not_kept_by_the_reader(tmp_path):
    from fluid_build.cli.import_workflow._airflow_dags import _Reader
    from fluid_build.cli.import_workflow._fold import ModuleIndex
    from fluid_build.cli.import_workflow._pysource import find_python_files

    _shared_helper_project(tmp_path / "dags", files=1, per_file=3)
    walk = find_python_files(tmp_path / "dags")
    index = ModuleIndex(walk.files)
    source = next(f for f in walk.files if f.rel == "d0/dag.py")
    module = parse_module("d0/dag.py", source.path.read_bytes())
    module.index_rel = source.rel
    reader = _Reader(module, index.folder(module), {}, {})
    result = reader.run()
    assert [s.script for d in result.dags for t in d.tasks for s in t.submits] == [
        f"s3://jobs/0_{i}.py" for i in range(3)
    ]
    own = {id(node) for node in ast.walk(module.tree)}
    follower = reader._follower
    assert follower is not None and all(id(f) in own for f in follower._bodies)


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
        (
            "6 task(s) submit a script or name a config file that depends on `acme_cfg.ROOT`, "
            + "which is not in the scanned files (DAG d task t0, DAG d task t1, DAG d task t2, "
            + "and 3 more): name the file of its module under `config_modules` in the conventions"
        ),
        (
            "3 task(s) submit a script or name a config file that depends on the environment "
            + "variable CONF_0 (DAG d task t0, DAG d task t2, DAG d task t4): set what it holds "
            + "under `environments` in the conventions"
        ),
        (
            "3 task(s) submit a script or name a config file that depends on the environment "
            + "variable CONF_1 (DAG d task t1, DAG d task t3, DAG d task t5): set what it holds "
            + "under `environments` in the conventions"
        ),
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
