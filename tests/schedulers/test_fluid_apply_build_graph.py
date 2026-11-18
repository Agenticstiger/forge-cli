# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``builds[].dependencies`` render as one DAG of ``fluid apply --build-id`` tasks.

The shape: builds the dependencies link share a DAG; it runs on the one trigger its builds
declare, on whichever build declares it, and is named after that build. Each dependency's
condition is the task's trigger rule. Every way a contract can make that shape impossible is
refused at generation with what to change.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.schedulers.airflow import fluid_apply
from tests.cli._schedule_dag_fixtures import load_dag
from tests.schedulers.fluid_apply_fixtures import SPARK_CONTRACT

CRON = {"type": "schedule", "schedule": "0 2 * * *"}


def _build(build_id: str, *deps: Any, trigger: Optional[Dict[str, Any]] = None, **extra: Any):
    build: Dict[str, Any] = {"id": build_id, "engine": "sql", "pattern": "embedded-logic"}
    if deps:
        build["dependencies"] = [d if isinstance(d, dict) else {"buildId": d} for d in deps]
    if trigger is not None:
        build["execution"] = {"trigger": trigger}
    build.update(extra)
    return build


def _contract(*builds: Dict[str, Any]) -> Dict[str, Any]:
    return {"fluidVersion": "0.7.7", "id": "sales.orders", "name": "Orders", "builds": list(builds)}


def _dags(*builds: Dict[str, Any]) -> List[fluid_apply.ScheduledDag]:
    return fluid_apply.scheduled_dags(_contract(*builds))


def _refused(*builds: Dict[str, Any]) -> str:
    with pytest.raises(fluid_apply.ScheduleRenderError) as exc:
        fluid_apply.render_fluid_apply_dags(
            _contract(*builds), env=None, contract_path="contract.fluid.yaml"
        )
    return str(exc.value)


def _shape(dag: fluid_apply.ScheduledDag) -> List[tuple]:
    return [(t.build_id, t.upstream, t.trigger_rule) for t in dag.tasks]


# --- the DAG shape -------------------------------------------------------------------------


def test_dependents_without_a_schedule_run_in_their_upstreams_dag():
    (dag,) = _dags(
        _build("extract", trigger=CRON), _build("transform", "extract"), _build("load", "transform")
    )
    assert dag.build_id == "extract"
    assert dag.schedule == fluid_apply.DagSchedule(cron="0 2 * * *", timezone="UTC")
    assert _shape(dag) == [
        ("extract", (), None),
        ("transform", ("extract",), "all_success"),
        ("load", ("transform",), "all_success"),
    ]


def test_the_build_that_carries_the_trigger_drives_and_names_the_dag():
    # The schedule sits on the last build: the builds before it run first, on its schedule.
    (dag,) = _dags(_build("staging"), _build("mart", "staging", trigger=CRON))
    assert dag.build_id == "mart"
    assert [t.build_id for t in dag.tasks] == ["staging", "mart"]
    files = fluid_apply.render_fluid_apply_dags(
        _contract(_build("staging"), _build("mart", "staging", trigger=CRON)),
        env="prod",
        contract_path="contract.fluid.yaml",
    )
    assert list(files) == ["mart_dag.py"]
    assert "dag_id='sales.orders__prod__mart'" in files["mart_dag.py"]
    assert "BUILD_ID = 'mart'" in files["mart_dag.py"]


def test_fan_in_and_fan_out_keep_builds_order_among_equals():
    (dag,) = _dags(
        _build("a", trigger=CRON),
        _build("d", "b", "c"),
        _build("c", "a"),
        _build("b", "a"),
    )
    assert _shape(dag) == [
        ("a", (), None),
        ("c", ("a",), "all_success"),
        ("b", ("a",), "all_success"),
        ("d", ("c", "b"), "all_success"),
    ]


def test_two_roots_with_one_trigger_share_the_dag_of_the_first():
    (dag,) = _dags(
        _build("orders", trigger=CRON),
        _build("customers", trigger={"cron": "0 2 * * *", "timezone": "UTC"}),
        _build("join", "orders", "customers"),
    )
    assert dag.build_id == "orders"
    assert [t.build_id for t in dag.tasks] == ["orders", "customers", "join"]


def test_unlinked_builds_keep_a_dag_each():
    dags = _dags(_build("a", trigger=CRON), _build("b", trigger={"cron": "@hourly"}))
    assert [(d.build_id, len(d.tasks)) for d in dags] == [("a", 1), ("b", 1)]


def test_a_linked_group_with_no_trigger_gets_no_dag():
    assert _dags(_build("a"), _build("b", "a")) == []


@pytest.mark.parametrize("kind", ["manual", "timetable", "event"])
def test_a_trigger_fluid_renders_no_dag_for_keeps_its_group_unscheduled(kind: str):
    assert _dags(_build("a", trigger={"type": kind}), _build("b", "a")) == []


@pytest.mark.parametrize(
    "condition, rule",
    [
        (None, "all_success"),
        ("success", "all_success"),
        ("completion", "all_done"),
        ("always", "always"),
    ],
)
def test_the_condition_is_the_trigger_rule(condition, rule):
    dep: Dict[str, Any] = {"buildId": "a"}
    if condition:
        dep["condition"] = condition
    (dag,) = _dags(_build("a", trigger=CRON), _build("b", dep))
    assert dag.tasks[1].trigger_rule == rule
    assert fluid_apply.TRIGGER_RULES == {
        "success": "all_success",
        "completion": "all_done",
        "always": "always",
    }


def test_a_repeated_dependency_is_one_edge():
    (dag,) = _dags(_build("a", trigger=CRON), _build("b", "a", "a"))
    assert dag.tasks[1].upstream == ("a",)


def test_trigger_type_dependency_runs_in_the_upstream_dag():
    (dag,) = _dags(_build("a", trigger=CRON), _build("b", "a", trigger={"type": "dependency"}))
    assert [t.build_id for t in dag.tasks] == ["a", "b"]


def test_each_task_keeps_its_own_retries_and_spark_timeout():
    contract = copy.deepcopy(SPARK_CONTRACT)
    daily = contract["builds"][1]
    daily["execution"]["retries"] = {"maxAttempts": 2}
    contract["builds"].append(
        {"id": "publish", "engine": "sql", "dependencies": [{"buildId": "daily-curated"}]}
    )
    (dag,) = fluid_apply.scheduled_dags(contract)
    assert [(t.build_id, t.retries, t.task_timeout_s) for t in dag.tasks] == [
        ("daily-curated", 1, fluid_apply.APPLY_TASK_TIMEOUT_S + fluid_apply.DEFAULT_SPARK_WAIT_S),
        ("publish", fluid_apply.DEFAULT_RETRIES, None),
    ]


# --- builds no DAG runs ---------------------------------------------------------------------


def test_a_dependency_on_a_stream_is_not_waited_for_and_says_so():
    contract = copy.deepcopy(SPARK_CONTRACT)
    contract["builds"][1]["dependencies"] = [{"buildId": "stream-raw"}]
    (dag,) = fluid_apply.scheduled_dags(contract)
    assert [t.build_id for t in dag.tasks] == ["daily-curated"]
    assert dag.unwaited == (("stream-raw", "a Spark stream that fluid apply keeps running"),)
    source = fluid_apply.render_fluid_apply_dags(contract, env=None, contract_path="c.yaml")
    text = " ".join(source["daily_curated_dag.py"].split())
    assert (
        "A build here depends on these, but no DAG runs them, so no task waits for them: "
        "stream-raw (a Spark stream that fluid apply keeps running)." in text
    )


@pytest.mark.parametrize(
    "upstream, why",
    [
        (
            {
                "id": "connector",
                "engine": "kafka-connect",
                "execution": {"trigger": {"type": "continuous"}},
            },
            "a job its platform keeps running (trigger.type: continuous)",
        ),
        ({"id": "flink-job", "engine": "flink"}, "a job that runs on its own platform"),
    ],
)
def test_a_dependency_on_a_job_that_runs_on_its_own_is_not_waited_for(upstream, why):
    (dag,) = _dags(upstream, _build("batch", upstream["id"], trigger=CRON))
    assert [t.build_id for t in dag.tasks] == ["batch"]
    assert dag.unwaited[0][0] == upstream["id"] and dag.unwaited[0][1].startswith(why)


def test_a_dependency_on_a_build_another_engine_runs_is_refused():
    other = _build("elsewhere", trigger=CRON)
    other["execution"]["orchestration"] = {"engine": "dagster"}
    message = _refused(other, _build("b", "elsewhere", trigger=CRON))
    assert "'elsewhere' is not run by an Airflow DAG" in message
    assert "Run both on one engine, or give 'b' a dataset trigger" in message


# --- what is refused, and what to change ---------------------------------------------------


def test_a_cycle_is_refused_with_its_path():
    message = _refused(_build("a", "c", trigger=CRON), _build("b", "a"), _build("c", "b"))
    assert "builds[].dependencies form a cycle:" in message
    assert "'a'" in message and "'b'" in message and "'c'" in message
    assert "Remove one of these dependencies" in message


def test_a_build_that_depends_on_itself_is_refused():
    assert "build 'a' depends on itself; remove 'a' from its dependencies" in _refused(
        _build("a", "a", trigger=CRON)
    )


def test_an_unknown_build_is_refused_with_the_ids_there_are():
    message = _refused(_build("a", trigger=CRON), _build("b", "nope"))
    assert (
        "build 'b': dependency 'nope' names no build in builds[] (build ids: 'a', 'b')" in message
    )


def test_a_dependency_without_a_build_id_is_refused():
    message = _refused(_build("a", trigger=CRON), _build("b", {"condition": "success"}))
    assert "build 'b': dependencies[0] names no buildId" in message


def test_an_unknown_condition_is_refused():
    message = _refused(_build("a", trigger=CRON), _build("b", {"buildId": "a", "condition": "x"}))
    assert "condition 'x' is not one of success, completion, always" in message


def test_dependencies_that_are_not_a_list_are_refused():
    message = _refused(_build("a", trigger=CRON), _build("b", dependencies={"buildId": "a"}))
    assert "dependencies must be a list" in message


def test_a_dependency_on_a_shared_id_is_refused():
    message = _refused(_build("a", trigger=CRON), _build("a"), _build("b", "a"))
    assert "names 2 builds that share that id; give each build its own id" in message


def test_two_different_triggers_in_one_group_are_refused_with_the_fix():
    message = _refused(
        _build("daily", trigger=CRON), _build("hourly", "daily", trigger={"cron": "@hourly"})
    )
    assert "builds 'daily' and 'hourly' are linked by builds[].dependencies" in message
    assert "schedule '0 2 * * *' (UTC) vs schedule '@hourly' (UTC)" in message
    assert "give 'hourly' a dataset trigger on what 'daily' writes" in message


def test_the_same_schedule_in_another_timezone_is_another_trigger():
    message = _refused(
        _build("a", trigger=CRON),
        _build("b", "a", trigger={**CRON, "timezone": "Europe/Paris"}),
    )
    assert "(UTC) vs schedule '0 2 * * *' (Europe/Paris)" in message


def test_a_manual_build_cannot_depend_on_a_scheduled_one():
    message = _refused(_build("a", trigger=CRON), _build("b", "a", trigger={"type": "manual"}))
    assert "trigger.type manual" in message


def test_mixed_conditions_on_one_build_are_refused():
    message = _refused(
        _build("a", trigger=CRON),
        _build("b", "a"),
        _build("c", "a", {"buildId": "b", "condition": "completion"}),
    )
    assert "build 'c' depends on builds with different conditions (completion, success)" in message


def test_trigger_type_dependency_needs_a_build_a_dag_runs():
    assert "trigger.type dependency runs it after the builds in its dependencies" in _refused(
        _build("a", trigger={"type": "dependency"})
    )
    stream = copy.deepcopy(SPARK_CONTRACT["builds"][0])
    assert "no DAG runs any of them; give it a trigger of its own" in _refused(
        stream, _build("b", "stream-raw", trigger={"type": "dependency"})
    )


def test_a_dependent_task_id_is_its_build_id():
    with pytest.raises(fluid_apply.ScheduleRenderError, match="valid id for --build-id"):
        _dags(_build("a", trigger=CRON), _build("b c", "a"))


# --- the rendered DAG ------------------------------------------------------------------------


@pytest.mark.parametrize("airflow3", [True, False])
def test_the_dag_runs_each_build_after_its_upstream(monkeypatch, airflow3: bool):
    contract = _contract(
        _build("a", trigger=CRON),
        _build("b", {"buildId": "a", "condition": "completion"}),
        _build("c", {"buildId": "b", "condition": "always"}),
    )
    (source,) = fluid_apply.render_fluid_apply_dags(
        contract, env="aws", contract_path="contract.fluid.yaml"
    ).values()
    loaded = load_dag(source, monkeypatch, airflow3=airflow3)
    assert [t["task_id"] for t in loaded.tasks] == ["a", "b", "c"]
    assert [t["env"]["FLUID_DAG_BUILD_ID"] for t in loaded.tasks] == ["a", "b", "c"]
    assert [t.get("trigger_rule") for t in loaded.tasks] == [None, "all_done", "always"]
    assert loaded.edges == [("a", "b"), ("b", "c")]
    assert loaded.dag["schedule"] == "0 2 * * *"
    assert loaded.dag["dag_id"] == "sales.orders__aws__a"
    assert all(t["retries"] == fluid_apply.DEFAULT_RETRIES for t in loaded.tasks)
    assert loaded.namespace["BUILD_ID"] == "a"


def test_a_fan_in_is_one_edge_from_a_list(monkeypatch):
    contract = _contract(
        _build("a", trigger=CRON), _build("b", trigger=CRON), _build("c", "a", "b")
    )
    (source,) = fluid_apply.render_fluid_apply_dags(
        contract, env=None, contract_path="contract.fluid.yaml"
    ).values()
    assert "    [tasks['a'], tasks['b']] >> tasks['c']\n" in source
    assert load_dag(source, monkeypatch).edges == [("a", "c"), ("b", "c")]


def test_every_task_runs_the_same_static_script():
    contract = _contract(_build("a", trigger=CRON), _build("b", "a"))
    (source,) = fluid_apply.render_fluid_apply_dags(
        contract, env=None, contract_path="contract.fluid.yaml"
    ).values()
    assert source.count("bash_command=BASH_COMMAND,") == 2
    assert source.count("BASH_COMMAND = ") == 1


def test_the_build_task_is_rendered_by_one_function(monkeypatch):
    """A stand-in for render_build_task changes the nodes and leaves schedule and edges alone."""
    original = fluid_apply.render_build_task

    def as_group(task: fluid_apply.BuildTask, *, only: bool) -> str:
        if task.build_id != "b":
            return original(task, only=only)
        return f"    tasks['b'] = BashOperator(task_id='b_group', trigger_rule={task.trigger_rule!r})\n"

    monkeypatch.setattr(fluid_apply, "render_build_task", as_group)
    contract = _contract(_build("a", trigger=CRON), _build("b", "a"), _build("c", "b"))
    (source,) = fluid_apply.render_fluid_apply_dags(
        contract, env=None, contract_path="contract.fluid.yaml"
    ).values()
    loaded = load_dag(source, monkeypatch)
    assert loaded.edges == [("a", "b_group"), ("b_group", "c")]
    assert loaded.dag["schedule"] == "0 2 * * *"


def test_scheduled_builds_lists_every_task_with_its_dags_schedule():
    builds = fluid_apply.scheduled_builds(_contract(_build("a", trigger=CRON), _build("b", "a")))
    assert [(b.build_id, b.schedule, b.timezone) for b in builds] == [
        ("a", "0 2 * * *", "UTC"),
        ("b", "0 2 * * *", "UTC"),
    ]


def test_an_id_no_task_checks_never_reaches_the_dag_documentation():
    """doc_md is Markdown the Airflow UI renders as HTML: only a plain identifier is echoed."""
    stream = copy.deepcopy(SPARK_CONTRACT["builds"][0])
    stream["id"] = '<img src=x onerror=alert(1)>"""'
    contract = copy.deepcopy(SPARK_CONTRACT)
    contract["builds"] = [stream, contract["builds"][1]]
    contract["builds"][1]["dependencies"] = [{"buildId": stream["id"]}]
    (source,) = fluid_apply.render_fluid_apply_dags(
        contract, env=None, contract_path="contract.fluid.yaml"
    ).values()
    assert "<img" not in source and "onerror" not in source
    assert "a build whose id is not a plain identifier (a Spark stream" in " ".join(source.split())
