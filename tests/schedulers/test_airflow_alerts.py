# Copyright 2025 Agentics Transformation Limited
# SPDX-License-Identifier: Apache-2.0

"""The alerts a generated DAG wires, imported against stub Airflow 2, 3.0, 3.1 and 3.4.

The stubs keep each name where that Airflow keeps it: ``sla`` and ``sla_miss_callback`` are
Airflow 2's; Airflow 3.0 has neither and no deadline alerts; 3.1 has ``DeadlineAlert`` and
``AsyncCallback`` in ``airflow.sdk.definitions.deadline``, 3.2+ in ``airflow.sdk``. A real
Airflow 2.10 and 3.4 run the same files in ``test_dag_alerts_airflow_live.py``.

The orchestration renderer's snapshot (``golden/alerts/``) is regenerated with::

    UPDATE_AIRFLOW_GOLDEN=1 pytest tests/schedulers/test_airflow_alerts.py
"""

from __future__ import annotations

import ast
import copy
import logging
import os
import re
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from fluid_build.alerts import reset_alert_plugin_cache
from fluid_build.schedulers.airflow import fluid_apply, render_dag
from tests.schedulers.airflow_fixtures import CONTRACTS, PROVIDER_CONFIG
from tests.schedulers.fluid_apply_fixtures import ALERT_CASES, ALL_KINDS_ALERTS

pytestmark = pytest.mark.unit

GOLDEN = Path(__file__).parent / "golden" / "alerts"
_NOTIFIERS = {
    "airflow.providers.http.notifications.http": "HttpNotifier",
    "airflow.providers.smtp.notifications.smtp": "SmtpNotifier",
    "airflow.providers.slack.notifications.slack_webhook": "SlackWebhookNotifier",
    "airflow.providers.pagerduty.notifications.pagerduty": "PagerdutyNotifier",
}


@pytest.fixture(autouse=True)
def _no_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    from fluid_build import plugin_manager

    monkeypatch.setattr(plugin_manager, "_entry_points", lambda group: [])
    reset_alert_plugin_cache()


class Loaded:
    def __init__(self) -> None:
        self.dag: Dict[str, Any] = {}
        self.tasks: List[Dict[str, Any]] = []
        self.notifiers: List[Any] = []
        self.deadlines: List[Dict[str, Any]] = []


def _module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _load(source: str, monkeypatch: pytest.MonkeyPatch, airflow: str) -> Loaded:
    """Import ``source`` against stub modules of Airflow ``airflow`` ("2.10.5", "3.0.6",
    "3.1.0", "3.4.0" or "3.9.0-changed", whose DeadlineAlert refuses what it is given)."""
    loaded = Loaded()

    class _DAG:
        def __init__(self, **kwargs: Any) -> None:
            loaded.dag = kwargs

        def __enter__(self) -> "_DAG":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    class _Op:
        def __init__(self, **kwargs: Any) -> None:
            self.task_id = kwargs.get("task_id")
            loaded.tasks.append(kwargs)

        def __rshift__(self, other: Any) -> Any:
            return other

        def __rrshift__(self, others: Any) -> Any:
            return self

    def notifier_class(name: str) -> type:
        class _Notifier:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs
                self.calls: List[Any] = []
                loaded.notifiers.append(self)

            def __call__(self, *args: Any) -> None:
                self.calls.append(args)
                # what BaseNotifier does: render its fields in place
                for value in self.kwargs.values():
                    if isinstance(value, dict):
                        value["rendered"] = True

        _Notifier.__name__ = name
        return _Notifier

    major = airflow.split(".")[0]
    stubs: Dict[str, Optional[types.ModuleType]] = {
        "pendulum": _module("pendulum", datetime=lambda *a, **kw: ("pendulum", a, kw)),
        "airflow": _module("airflow", DAG=_DAG, __version__=airflow),
        "airflow.operators": _module("airflow.operators"),
        "airflow.operators.bash": _module("airflow.operators.bash", BashOperator=_Op),
        "airflow.operators.python": _module("airflow.operators.python", PythonOperator=_Op),
        "airflow.datasets": _module(
            "airflow.datasets",
            Dataset=lambda uri: ("asset", uri),
            DatasetAny=lambda *assets: ("any", assets),
        ),
        "airflow.timetables.datasets": _module(
            "airflow.timetables.datasets", DatasetOrTimeSchedule=lambda **kw: ("or", kw)
        ),
        "airflow.providers": _module("airflow.providers"),
        "airflow.timetables": _module("airflow.timetables"),
        "airflow.timetables.trigger": _module(
            "airflow.timetables.trigger", CronTriggerTimetable=lambda cron, **kw: ("cron", cron)
        ),
        "airflow.timetables.assets": _module(
            "airflow.timetables.assets", AssetOrTimeSchedule=lambda **kw: ("or", kw)
        ),
    }
    for module, name in _NOTIFIERS.items():
        stubs[module] = _module(module, **{name: notifier_class(name)})
    if major == "2":
        stubs["airflow.sdk"] = None
        stubs["airflow.providers.standard"] = None
        stubs["airflow.timetables.assets"] = None
    else:
        sdk: Dict[str, Any] = {
            "DAG": _DAG,
            "Asset": lambda uri: ("asset", uri),
            "AssetAny": lambda *assets: ("any", assets),
        }

        class _Reference:
            DAGRUN_QUEUED_AT = "DAGRUN_QUEUED_AT"

        def _deadline(**kwargs: Any) -> Dict[str, Any]:
            if airflow.endswith("-changed"):
                raise TypeError("DeadlineAlert() got an unexpected keyword argument")
            loaded.deadlines.append(kwargs)
            return kwargs

        def _callback(cls: Any, kwargs: Optional[Dict[str, Any]] = None) -> Any:
            return ("AsyncCallback", cls.__name__, kwargs)

        deadline = {
            "DeadlineAlert": _deadline,
            "DeadlineReference": _Reference,
            "AsyncCallback": _callback,
        }
        if airflow.startswith("3.1."):
            stubs["airflow.sdk.definitions"] = _module("airflow.sdk.definitions")
            stubs["airflow.sdk.definitions.deadline"] = _module(
                "airflow.sdk.definitions.deadline", **deadline
            )
        elif not airflow.startswith("3.0."):
            sdk.update(deadline)
        else:
            stubs["airflow.sdk.definitions.deadline"] = None
        stubs["airflow.sdk"] = _module("airflow.sdk", **sdk)
        for name in ("airflow.providers.standard", "airflow.providers.standard.operators"):
            stubs[name] = _module(name)
        stubs["airflow.providers.standard.operators.bash"] = _module(
            "airflow.providers.standard.operators.bash", BashOperator=_Op
        )
        stubs["airflow.providers.standard.operators.python"] = _module(
            "airflow.providers.standard.operators.python", PythonOperator=_Op
        )
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)
    namespace: Dict[str, Any] = {"__name__": "generated_dag"}
    exec(compile(source, "generated_dag.py", "exec"), namespace)  # noqa: S102 - test harness
    return loaded


def _all_kinds_dag() -> str:
    contract, env, path = ALERT_CASES["alerts_all_kinds"]
    (source,) = fluid_apply.render_fluid_apply_dags(contract, env=env, contract_path=path).values()
    return source


def _names(callbacks: List[Any], loaded: Loaded, *args: Any) -> List[str]:
    """Call each callback; the class name of the notifier it built."""
    before = len(loaded.notifiers)
    for cb in callbacks:
        cb(*args)
    return [type(n).__name__ for n in loaded.notifiers[before:]]


class TestTheOneBuildDag:
    def test_airflow_2_wires_task_callbacks_sla_and_sla_miss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loaded = _load(_all_kinds_dag(), monkeypatch, "2.10.5")
        args = loaded.dag["default_args"]
        ctx = {"ti": "x"}
        assert _names(args["on_failure_callback"], loaded, ctx) == [
            "HttpNotifier",
            "SmtpNotifier",
            "PagerdutyNotifier",
            "SlackWebhookNotifier",
            "HttpNotifier",
        ]
        assert _names(args["on_retry_callback"], loaded, ctx) == ["SmtpNotifier", "HttpNotifier"]
        assert _names(loaded.dag["on_success_callback"], loaded, ctx) == [
            "HttpNotifier",
            "PagerdutyNotifier",
            "HttpNotifier",
        ]
        assert args["sla"].total_seconds() == 4 * 3600
        five = ("dag", "task_list", "blocking", [], [])
        assert _names(loaded.dag["sla_miss_callback"], loaded, *five) == ["HttpNotifier"]
        assert loaded.notifiers[-1].calls == [five]  # Airflow 2's five arguments, passed on
        assert "deadline" not in loaded.dag
        # the task itself keeps its own settings beside the merged alert args
        assert args["owner"] == "fluid" and args["retries"] == 3

    @pytest.mark.parametrize("airflow", ["3.1.0", "3.4.0"])
    def test_airflow_3_1_and_later_get_a_deadline_alert_instead(
        self, monkeypatch: pytest.MonkeyPatch, airflow: str
    ) -> None:
        loaded = _load(_all_kinds_dag(), monkeypatch, airflow)
        assert "sla" not in loaded.dag["default_args"]
        assert "sla_miss_callback" not in loaded.dag
        (alert,) = loaded.dag["deadline"]
        assert alert["reference"] == "DAGRUN_QUEUED_AT"
        assert alert["interval"].total_seconds() == 4 * 3600
        kind, name, kwargs = alert["callback"]
        assert (kind, name) == ("AsyncCallback", "HttpNotifier")
        assert kwargs["http_conn_id"] == "zenduty_oncall"
        assert kwargs["json"]["alert_type"] == "warning"

    def test_airflow_3_0_has_neither_and_still_loads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loaded = _load(_all_kinds_dag(), monkeypatch, "3.0.6")
        assert "deadline" not in loaded.dag and "sla_miss_callback" not in loaded.dag
        assert "sla" not in loaded.dag["default_args"]
        assert len(loaded.dag["default_args"]["on_failure_callback"]) == 5

    def test_a_changed_deadline_api_costs_the_deadline_not_the_dag(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="generated_dag"):
            loaded = _load(_all_kinds_dag(), monkeypatch, "3.9.0-changed")
        assert loaded.dag["dag_id"] and "deadline" not in loaded.dag
        assert any("refused the deadline alerts (TypeError)" in r.message for r in caplog.records)

    def test_each_alert_gets_a_new_notifier_from_fresh_arguments(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loaded = _load(_all_kinds_dag(), monkeypatch, "2.10.5")
        first = loaded.dag["default_args"]["on_failure_callback"][0]
        first({"ti": "a"})
        first({"ti": "b"})
        one, two = loaded.notifiers[-2:]
        assert one is not two
        assert one.calls == [({"ti": "a"},)] and two.calls == [({"ti": "b"},)]
        # the first notifier rendered its json in place; the second got pristine arguments
        assert one.kwargs["json"].get("rendered") is True
        assert "rendered" not in two.kwargs["json"] or two.kwargs["json"] is not one.kwargs["json"]
        assert two.kwargs["json"] is not one.kwargs["json"]

    def test_no_secret_and_no_connection_url_in_the_file(self) -> None:
        source = _all_kinds_dag()
        assert "://" not in source.split('"""', 2)[2].replace("http://", "")
        for conn in ("zenduty_oncall", "slack_data_alerts", "opsgenie_alerts", "alerts_audit"):
            assert repr(conn) in source

    def test_a_dag_without_alerts_is_the_same_file(self) -> None:
        contract, env, path = ALERT_CASES["alerts_all_kinds"]
        plain = copy.deepcopy(contract)
        del plain["alerts"]
        (with_none,) = fluid_apply.render_fluid_apply_dags(
            plain, env=env, contract_path=path
        ).values()
        assert "ALERT_" not in with_none and "import copy" not in with_none


class TestGraphAndKeepalive:
    def test_every_dag_of_the_product_alerts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        contract, env, path = ALERT_CASES["alerts_graph_and_keepalive"]
        files = fluid_apply.render_fluid_apply_dags(contract, env=env, contract_path=path)
        assert set(files) == {"daily_curated_dag.py", "stream_raw_dag.py"}
        for name, source in files.items():
            loaded = _load(source, monkeypatch, "3.4.0")
            (cb,) = loaded.dag["default_args"]["on_failure_callback"]
            cb({"ti": "t"})
            body = loaded.notifiers[-1].kwargs["json"]
            assert body["dag_id"] == loaded.dag["dag_id"], name

    def test_a_multi_build_dag_merges_the_alert_args_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        contract, env, path = ALERT_CASES["alerts_pipeline"]
        files = fluid_apply.render_fluid_apply_dags(contract, env=env, contract_path=path)
        for source in files.values():
            assert (
                source.count("**ALERT_TASK_ARGS,") == 1 and source.count("**ALERT_DAG_ARGS,") == 1
            )
            loaded = _load(source, monkeypatch, "2.10.5")
            assert loaded.dag["default_args"]["sla"].total_seconds() == 90 * 60


class TestTheOrchestrationDag:
    def _render(self, provider: str) -> str:
        contract = copy.deepcopy(CONTRACTS[provider])
        contract["alerts"] = copy.deepcopy(ALL_KINDS_ALERTS)
        source = render_dag(
            contract,
            provider=provider,
            provider_config=PROVIDER_CONFIG[provider],
            allow_unmapped=True,
        )
        return re.sub(r"Generated: .*", "Generated: <ts>", source).rstrip() + "\n"

    def test_matches_its_snapshot(self) -> None:
        snapshot = GOLDEN / "aws.py.txt"
        rendered = self._render("aws")
        if os.environ.get("UPDATE_AIRFLOW_GOLDEN"):
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text(rendered, encoding="utf-8")
        assert rendered == snapshot.read_text(encoding="utf-8")

    @pytest.mark.parametrize("provider", sorted(CONTRACTS))
    def test_is_python_with_the_alerts_merged(self, provider: str) -> None:
        source = self._render(provider)
        ast.parse(source)
        assert source.index("ALERT_TASK_ARGS = ") < source.index("default_args = {")
        assert "    **ALERT_TASK_ARGS,\n}" in source and "    **ALERT_DAG_ARGS,\n)" in source
        # the notifier imports sit with the others, before the module logger
        assert source.index("import HttpNotifier") < source.index("logger = logging.getLogger")

    def test_a_generic_dag_runs_its_alerts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        contract = {
            "id": "ops.generic",
            "name": "Generic",
            "orchestration": {
                "tasks": [{"taskId": "one", "type": "provider_action", "action": "noop"}]
            },
            "alerts": {"routes": [{"name": "chat", "kind": "slack", "events": ["failure"]}]},
        }
        loaded = _load(render_dag(contract, allow_unmapped=True), monkeypatch, "3.4.0")
        (cb,) = loaded.dag["default_args"]["on_failure_callback"]
        cb({"ti": "t"})
        assert loaded.notifiers[-1].kwargs["slack_webhook_conn_id"] == "slack_default"
        assert loaded.dag["dag_id"] == "ops_generic"
