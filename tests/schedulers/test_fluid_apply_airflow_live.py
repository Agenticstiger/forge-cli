# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The DAGs the snapshot contracts render load in a real Airflow 2 and Airflow 3.

Airflow is not a test dependency, so this runs only against an Airflow you name: set
``FLUID_AIRFLOW2_PYTHON`` and/or ``FLUID_AIRFLOW3_PYTHON`` to the interpreter of a virtualenv
with ``apache-airflow`` installed (with its constraints file), for example::

    uv venv --python 3.12 af3
    uv pip install --python af3/bin/python "apache-airflow==3.4.0" \\
        --constraint https://raw.githubusercontent.com/apache/airflow/constraints-3.4.0/constraints-3.12.txt
    FLUID_AIRFLOW3_PYTHON=af3/bin/python pytest tests/schedulers/test_fluid_apply_airflow_live.py

Each Airflow parses the files with its own ``DagBag``, which runs them as the scheduler does,
and the test compares what Airflow built with what the contract says: the tasks, their order,
their trigger rules, their outlets and the timetable.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest

from fluid_build.schedulers.airflow import fluid_apply
from tests.schedulers.fluid_apply_fixtures import CASES, GRAPH_CASES

_PARSE = r"""
import json, sys
try:
    from airflow.dag_processing.dagbag import DagBag
except ImportError:
    from airflow.models.dagbag import DagBag

try:
    bag = DagBag(sys.argv[1], include_examples=False)
except TypeError:  # Airflow 3.4 reads AIRFLOW__CORE__LOAD_EXAMPLES only
    bag = DagBag(sys.argv[1])
out ={"errors": {k: str(v)[-800:] for k, v in bag.import_errors.items()}, "dags": {}}
for dag_id, dag in bag.dags.items():
    out["dags"][dag_id] = {
        "tasks": [t.task_id for t in dag.topological_sort()],
        "edges": sorted([t.task_id, d] for t in dag.tasks for d in t.downstream_task_ids),
        "rules": {t.task_id: getattr(t.trigger_rule, "value", t.trigger_rule) for t in dag.tasks},
        "outlets": {t.task_id: [o.uri for o in t.outlets] for t in dag.tasks},
        "retries": {t.task_id: t.retries for t in dag.tasks},
        "timetable": type(dag.timetable).__name__,
    }
print(json.dumps(out))
"""

#: The timetable each Airflow major builds for a schedule fluid renders.
_TIMETABLES = {
    "2": {"cron": "CronDataIntervalTimetable", "dataset": "DatasetTriggeredTimetable"},
    "3": {"cron": "CronTriggerTimetable", "dataset": "AssetTriggeredTimetable"},
}
_OR_TIME = {"2": "DatasetOrTimeSchedule", "3": "AssetOrTimeSchedule"}


def _expected(dag: fluid_apply.ScheduledDag, major: str) -> Dict[str, Any]:
    only = len(dag.tasks) == 1
    single = "fluid_keepalive" if dag.keepalive else "fluid_apply"
    task_id = {t.build_id: (single if only else t.build_id) for t in dag.tasks}
    if dag.schedule.datasets and dag.schedule.cron is not None:
        timetable = _OR_TIME[major]
    else:
        timetable = _TIMETABLES[major]["dataset" if dag.schedule.datasets else "cron"]
    return {
        "tasks": [task_id[t.build_id] for t in dag.tasks],
        "edges": sorted([task_id[u], task_id[t.build_id]] for t in dag.tasks for u in t.upstream),
        "rules": {task_id[t.build_id]: t.trigger_rule or "all_success" for t in dag.tasks},
        "outlets": {task_id[t.build_id]: list(t.outlets) for t in dag.tasks},
        "retries": {task_id[t.build_id]: t.retries for t in dag.tasks},
        "timetable": timetable,
    }


@pytest.mark.parametrize("major", ["2", "3"])
def test_every_snapshot_contract_loads_in_airflow(major: str, tmp_path: Path) -> None:
    python = os.environ.get(f"FLUID_AIRFLOW{major}_PYTHON")
    if not python:
        pytest.skip(f"set FLUID_AIRFLOW{major}_PYTHON to an Airflow {major} interpreter")
    dags_dir = tmp_path / "dags"
    expected: Dict[str, Dict[str, Any]] = {}
    for case, (contract, env, path) in {**CASES, **GRAPH_CASES}.items():
        folder = dags_dir / case
        folder.mkdir(parents=True)
        files = fluid_apply.render_fluid_apply_dags(contract, env=env, contract_path=path)
        for name, source in files.items():
            (folder / name).write_text(source, encoding="utf-8")
        product = fluid_apply.validate_id(contract["id"], kind="contract.id")
        for dag in fluid_apply.scheduled_dags(contract):
            dag_id = fluid_apply.dag_id_for(product, dag.build_id, env)
            expected[f"{case}:{dag_id}"] = _expected(dag, major)
    env_vars = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "AIRFLOW_HOME": str(tmp_path / "airflow"),
        "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
        "AIRFLOW__CORE__DAGS_FOLDER": str(dags_dir),
    }
    got: Dict[str, Dict[str, Any]] = {}
    for case in sorted(p.name for p in dags_dir.iterdir()):
        proc = subprocess.run(  # noqa: S603 - the interpreter the operator named
            [python, "-I", "-c", _PARSE, str(dags_dir / case)],
            env=env_vars,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        assert result["errors"] == {}, result["errors"]
        got.update({f"{case}:{dag_id}": shape for dag_id, shape in result["dags"].items()})
    assert got == expected
