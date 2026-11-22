# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""A generated DAG's alerts, sent by a real Airflow 2 and Airflow 3 to a local server.

Airflow is not a test dependency, so this runs only against an Airflow you name: set
``FLUID_AIRFLOW2_PYTHON`` and/or ``FLUID_AIRFLOW3_PYTHON`` to the interpreter of a virtualenv
with apache-airflow and the providers the routes use, for example::

    uv venv --python 3.12 af3
    uv pip install --python af3/bin/python "apache-airflow==3.4.0" \\
        --constraint https://raw.githubusercontent.com/apache/airflow/constraints-3.4.0/constraints-3.12.txt
    uv pip install --python af3/bin/python "apache-airflow==3.4.0" \\
        apache-airflow-providers-http apache-airflow-providers-slack
    FLUID_AIRFLOW3_PYTHON=af3/bin/python pytest tests/schedulers/test_dag_alerts_airflow_live.py

(On Airflow 2.10, ``apache-airflow-providers-http>=5.5,<5.6``: HttpNotifier arrived in 5.5.0.)
Measured on Airflow 2.10.5 (http 5.5.0, slack 9.5.0) and 3.4.0 (http 6.4.0, slack 9.11.1).

The DAG runs with ``dag.test()``: a task that fails once (and retries) and a run that succeeds,
each through the webhook (Zenduty and Opsgenie formats) and Slack routes, whose connections
point at a server in the test process. The SLA paths are called as Airflow calls them, since
``dag.test()`` evaluates neither: Airflow 2's ``sla_miss_callback`` with its five arguments,
and on Airflow 3 the deadline alert's ``AsyncCallback``, awaited with the context the triggerer
gives it. Two limits of Airflow 3.4's ``dag.test()``, each met by Airflow's own documented examples too:
it cannot run a DAG with a deadline to success (``'dict' object has no attribute 'hex'``), so
the success run uses the product's DAG without an SLA; and it hands DAG-level callbacks its
``SerializedDAG``, which no notifier can render templates with (``no attribute
'get_template_env'``), so on Airflow 3 the success callbacks are then called with the context
the DAG processor gives them (``dag_processing/processor.py::_execute_dag_callbacks``).
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fluid_build.schedulers.airflow import fluid_apply
from tests.cli._schedule_dag_fixtures import DEMO_CONTRACT, DEMO_CONTRACT_PATH

DAG_ID = "bronze.customer_subscriptions__ingest_subscriptions"

ROUTES: List[Dict[str, Any]] = [
    {
        "name": "oncall",
        "kind": "webhook",
        "format": "zenduty",
        "connection": "zenduty_oncall",
        "events": ["failure", "retry", "success", "sla_miss"],
    },
    {
        "name": "ops",
        "kind": "webhook",
        "format": "opsgenie",
        "connection": "opsgenie_alerts",
        "events": ["failure", "success"],
    },
    {"name": "chat", "kind": "slack", "connection": "slack_alerts", "events": ["failure"]},
]

_RUN = r"""
import asyncio, http.server, importlib, json, os, sys, threading
from datetime import timedelta

dags_dir, mode = sys.argv[1:3]
captured = []

class Capture(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        captured.append({"path": self.path, "body": json.loads(body or b"{}"),
                         "auth": self.headers.get("Authorization")})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *args):
        pass

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Capture)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_address[1]}"
os.environ["AIRFLOW_CONN_ZENDUTY_ONCALL"] = json.dumps(
    {"conn_type": "http", "host": base + "/integration/T1/generic/K1/"})
os.environ["AIRFLOW_CONN_OPSGENIE_ALERTS"] = json.dumps(
    {"conn_type": "http", "host": base + "/v2/alerts",
     "extra": {"Authorization": "GenieKey 00000000-0000-0000-0000-000000000000"}})
os.environ["AIRFLOW_CONN_SLACK_ALERTS"] = json.dumps(
    {"conn_type": "slackwebhook", "host": base + "/services", "password": "T0/B0/X0"})  # pragma: allowlist secret
if mode == "succeed":
    os.environ["FLUID_BIN"] = "/usr/bin/true"
    os.environ["FLUID_PROJECT_DIR"] = os.environ["LIVE_PROJECT_DIR"]

try:
    from airflow.dag_processing.dagbag import DagBag
except ImportError:
    from airflow.models.dagbag import DagBag
try:
    bag = DagBag(dags_dir, include_examples=False)
except TypeError:  # Airflow 3.4 reads AIRFLOW__CORE__LOAD_EXAMPLES only
    bag = DagBag(dags_dir)
out = {"errors": {k: str(v)[-1500:] for k, v in bag.import_errors.items()}}
dag = bag.dags[sys.argv[3]]
out["deadline"] = len(getattr(dag, "deadline", None) or [])
out["sla_miss_callback"] = len(getattr(dag, "sla_miss_callback", None) or [])
if mode == "sla":
    for callback in getattr(dag, "sla_miss_callback", None) or []:
        callback(dag, "fluid_apply on 2026-11-22", "", [], [])
    for alert in getattr(dag, "deadline", None) or []:
        module, _, name = alert.callback.path.rpartition(".")
        notifier = getattr(importlib.import_module(module), name)
        context = {"dag_run": {"dag_id": dag.dag_id, "dag_run_id": "scheduled__2026-11-22"},
                   "deadline": {"id": "d1", "deadline_time": "2026-11-22T06:00:00+00:00"}}

        async def send():
            await notifier(**alert.callback.kwargs, context=context)

        asyncio.run(send())
else:
    for task in dag.tasks:
        task.retries = 1
        task.retry_delay = timedelta(seconds=0)
    dag.test()
    import airflow

    if mode == "succeed" and airflow.__version__.startswith("3."):
        # Airflow 3's dag.test() hands DAG callbacks its SerializedDAG, which a notifier cannot
        # render with; the DAG processor passes the parsed DAG, so call them as it does.
        for callback in dag.on_success_callback or []:
            callback({"dag": dag, "run_id": "manual__live", "reason": "success"})
out["captured"] = captured
print(json.dumps(out))
"""


def _python(major: str) -> str:
    python = os.environ.get(f"FLUID_AIRFLOW{major}_PYTHON")
    if not python:
        pytest.skip(f"set FLUID_AIRFLOW{major}_PYTHON to an Airflow {major} interpreter")
    probe = subprocess.run(  # noqa: S603 - the interpreter the operator named
        [
            python,
            "-I",
            "-c",
            "import airflow.providers.http.notifications.http, "
            + "airflow.providers.slack.notifications.slack_webhook",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if probe.returncode:
        pytest.skip(f"the Airflow {major} virtualenv lacks the http (>=5.5) or slack provider")
    return python


def _dags(tmp_path: Path, name: str, *, sla: bool) -> Path:
    import yaml

    contract = yaml.safe_load(DEMO_CONTRACT)
    contract["alerts"] = {"routes": copy.deepcopy(ROUTES)}
    if sla:
        contract["alerts"]["sla"] = "PT4H"
    else:
        contract["alerts"]["routes"][0]["events"].remove("sla_miss")
    folder = tmp_path / name
    folder.mkdir()
    files = fluid_apply.render_fluid_apply_dags(
        contract, env=None, contract_path=DEMO_CONTRACT_PATH
    )
    for filename, source in files.items():
        (folder / filename).write_text(source, encoding="utf-8")
    return folder


def _run(python: str, tmp_path: Path, folder: Path, mode: str) -> Dict[str, Any]:
    project = tmp_path / "project"
    (project / DEMO_CONTRACT_PATH).parent.mkdir(parents=True, exist_ok=True)
    (project / DEMO_CONTRACT_PATH).write_text(DEMO_CONTRACT, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "AIRFLOW_HOME": str(tmp_path / "airflow"),
        "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
        "AIRFLOW__CORE__DAGS_FOLDER": str(folder),
        "LIVE_PROJECT_DIR": str(project),
    }
    if not (tmp_path / "airflow" / "airflow.db").exists():
        migrate = subprocess.run(  # noqa: S603 - the interpreter the operator named
            [python, "-m", "airflow", "db", "migrate"],
            env=env,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        assert migrate.returncode == 0, migrate.stderr[-2000:]
    proc = subprocess.run(  # noqa: S603 - the interpreter the operator named
        [python, "-I", "-c", _RUN, str(folder), mode, DAG_ID],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    result: Dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["errors"] == {}, result["errors"]
    return result


def _by_path(captured: List[Dict[str, Any]], path: str) -> List[Dict[str, Any]]:
    return [c for c in captured if c["path"] == path]


@pytest.mark.parametrize("major", ["2", "3"])
def test_a_failing_task_alerts_each_route_with_airflows_context(major: str, tmp_path: Path) -> None:
    python = _python(major)
    result = _run(python, tmp_path, _dags(tmp_path, "dags", sla=False), "fail")
    captured = result["captured"]
    zenduty = [c["body"] for c in _by_path(captured, "/integration/T1/generic/K1/")]
    assert [z["alert_type"] for z in zenduty] == ["warning", "critical"]
    assert zenduty[0]["message"].endswith("task fluid_apply failed and will retry")
    assert zenduty[1]["message"].endswith("task fluid_apply failed")
    assert "try 2" in zenduty[1]["summary"] and "manual__" in zenduty[1]["summary"]
    assert zenduty[1]["entity_id"] == f"fluid/{DAG_ID}"
    (opsgenie,) = _by_path(captured, "/v2/alerts")
    assert opsgenie["auth"] == "GenieKey 00000000-0000-0000-0000-000000000000"
    assert opsgenie["body"]["alias"] == f"fluid-{DAG_ID}" and opsgenie["body"]["priority"] == "P1"
    (slack,) = _by_path(captured, "/services/T0/B0/X0")
    assert "task fluid_apply failed" in slack["body"]["text"]


@pytest.mark.parametrize("major", ["2", "3"])
def test_a_successful_run_resolves_the_incident(major: str, tmp_path: Path) -> None:
    python = _python(major)
    result = _run(python, tmp_path, _dags(tmp_path, "dags", sla=False), "succeed")
    captured = result["captured"]
    (zenduty,) = _by_path(captured, "/integration/T1/generic/K1/")
    assert zenduty["body"]["alert_type"] == "resolved"
    assert zenduty["body"]["entity_id"] == f"fluid/{DAG_ID}"
    (close,) = _by_path(captured, f"/v2/alerts/fluid-{DAG_ID}/close?identifierType=alias")
    assert close["auth"].startswith("GenieKey ")


@pytest.mark.parametrize("major", ["2", "3"])
def test_a_missed_sla_alerts_as_that_airflow_reports_it(major: str, tmp_path: Path) -> None:
    python = _python(major)
    result = _run(python, tmp_path, _dags(tmp_path, "dags", sla=True), "sla")
    if major == "2":
        assert result["sla_miss_callback"] == 1 and result["deadline"] == 0
    else:
        assert result["sla_miss_callback"] == 0 and result["deadline"] == 1
    (zenduty,) = _by_path(result["captured"], "/integration/T1/generic/K1/")
    body = zenduty["body"]
    assert body["message"].endswith("missed its SLA of 4h") and body["alert_type"] == "warning"
    if major == "2":
        assert body["summary"] == "Not done in time: fluid_apply on 2026-11-22"
    else:
        assert body["summary"] == (
            "Run scheduled__2026-11-22 was not done by 2026-11-22T06:00:00+00:00"
        )
