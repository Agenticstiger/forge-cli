# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""``fluid validate`` warns about an Airflow task's ``buildStepRef``: the schema accepts it, it
names a legacy ``transformations[]`` step, and no command runs a single step, so no DAG fluid
generates ever runs it. The order of builds is ``builds[].dependencies``."""

from __future__ import annotations

from fluid_build.cli._authoring_lint import lint_contract

EVENT = "contract_unrendered_build_step_ref"


def _task(task_id: str, **extra):
    return {"taskId": task_id, "type": "fluid_apply", **extra}


def _findings(contract):
    return [f for f in lint_contract(contract) if f.event == EVENT]


def test_each_build_step_ref_is_a_warning_that_says_what_to_write_instead():
    contract = {
        "id": "p",
        "orchestration": {
            "engine": "airflow",
            "airflow": {
                "dagId": "p",
                "tasks": [_task("a", buildStepRef="extract"), _task("b")],
                "taskGroups": [{"groupId": "g", "tasks": [_task("c", buildStepRef="load")]}],
            },
        },
        "builds": [
            {
                "id": "x",
                "execution": {
                    "orchestration": {
                        "engine": "airflow",
                        "airflow": {"dagId": "x", "tasks": [_task("d", buildStepRef="t")]},
                    }
                },
            }
        ],
    }
    findings = _findings(contract)
    assert [f.pointer for f in findings] == [
        "/orchestration/airflow/tasks/0/buildStepRef",
        "/orchestration/airflow/taskGroups/0/tasks/0/buildStepRef",
        "/builds/0/execution/orchestration/airflow/tasks/0/buildStepRef",
    ]
    first = findings[0]
    assert first.message.startswith("orchestration.airflow.tasks[0].buildStepRef: no generated DAG")
    assert first.fix == (
        "Remove buildStepRef. To run a build after another, list that one in its "
        "builds[].dependencies."
    )


def test_a_key_of_that_name_outside_orchestration_is_not_flagged():
    contract = {"id": "p", "metadata": {"buildStepRef": "x"}, "orchestration": {"airflow": {}}}
    assert _findings(contract) == []


def test_an_empty_build_step_ref_is_not_flagged():
    contract = {"id": "p", "orchestration": {"airflow": {"tasks": [_task("a", buildStepRef="")]}}}
    assert _findings(contract) == []
